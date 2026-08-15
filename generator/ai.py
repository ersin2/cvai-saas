"""
How the app talks to the model.

Holds the one transport function every AI endpoint calls, the model chosen per
task, and the language rule shared by all six prompts.

There is exactly one path out to a provider. A FastAPI worker used to sit
between Django and Anthropic; see `_call_ai_service` for why it was removed.
"""

import logging

from django.conf import settings

from .ai_client import call_anthropic, AIClientError

logger = logging.getLogger(__name__)


def _language_rule(language=None):
    """
    The output-language instruction shared by every AI prompt in this module.

    Was previously spelled out inline in five places — three byte-identical
    copies plus two near-variants — so tuning the wording meant editing five
    prompts and hoping they stayed in sync. Pass `language` when the caller
    has an explicit user selection (cover letter, section rewrite); omit it for
    the Tools endpoints, whose forms have no language field and which rely on
    detection alone.
    """
    rule = (
        "⚠️ ABSOLUTE LANGUAGE RULE ⚠️\n"
        "You MUST automatically detect the language of the candidate's raw input "
        "text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE — "
        "every section header, bullet point, analysis line, question and tip. "
        "DO NOT write even one word in another language. If you fail to match the "
        "input language, you fail the task.\n"
    )
    if language:
        rule += (
            f"The user has also selected: {language} — use this as a fallback ONLY "
            "if you cannot detect the input language.\n"
        )
    return rule + "\n"

# ---------------------------------------------------------------------------
# MODEL SELECTION
# ---------------------------------------------------------------------------
# Task difficulty used to be inverted against model capability: schema-
# constrained JSON extraction (mechanical, temperature 0.1, effort "low") ran on
# the frontier model, while every task that actually needs writing judgement —
# the cover letter, the Studio bullet rewriter, ATS analysis — ran on
# llama-3.1-8b-instant, a throughput-optimised 8B model. That is the main reason
# generated prose read as generic.
#
# Extraction keeps the reasoning model: it has to hold a whole resume in view
# and assign each fact to the right field. Prose moves to Sonnet, which writes
# at close to Opus quality for a fraction of the cost on short generations.
AI_MODEL_EXTRACTION = None            # inherit ANTHROPIC_MODEL (claude-opus-5)
AI_MODEL_PROSE = "claude-sonnet-5"

# ---------------------------------------------------------------------------
# AI TRANSPORT
# Django calls Anthropic directly. There is no worker service and no HTTP hop;
# see the docstring below for why the hop was removed.
# ---------------------------------------------------------------------------
async def _call_ai_service(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    json_mode: bool = False,
    max_tokens: int = 4096,
    json_schema: dict | None = None,
    effort: str | None = None,
    model: str | None = None,
):
    """
    Call Anthropic and return (result_text | None, error_message | None).

    max_tokens  — structured resumes need more headroom than prose responses.
    json_schema — constrains the response to the schema.
    effort      — reasoning depth; thinking shares the max_tokens budget with
                  the response, so extraction sends 'low'.

    `temperature` and `json_mode` are accepted and ignored. They are left in the
    signature because call sites still pass them, and because the current Claude
    models reject sampling parameters with a 400 — so the value must not reach
    the API even if a caller supplies one.

    HISTORY — why there is only one path here now
    --------------------------------------------
    This used to POST to a FastAPI worker, which owned the provider keys and
    could route to Groq or Anthropic. That worker has been deleted.

    The hop could not work on the current hosting. The plan has no private
    networking between services, so the worker's internal address did not
    resolve; pointing at its public URL instead sent every call out of the
    datacenter and back in through the platform edge, which rate-limited it
    with a 429 the worker never saw. That took generation down in production.

    The interim fix added a direct Anthropic call here and kept the HTTP hop as
    a fallback. That left the same call logic — thinking blocks, refusals, the
    `effort` retry — written twice, in two languages of failure, where fixing
    one would silently miss the other. Since every endpoint routes to Anthropic
    and the fallback could not work in production anyway, the fallback was not
    a safety net; it was a second copy pretending to be one.
    """
    anthropic_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        # A configuration problem, not a transient one. Saying "try again"
        # here sent users into a retry loop against a server that could never
        # answer, which is how the last outage stayed invisible for so long.
        logger.error("ANTHROPIC_API_KEY is not set — no AI generation is possible.")
        return None, ("The AI service is not configured on this server. This is "
                      "not something retrying will fix — please contact support.")

    try:
        text = await call_anthropic(
            system_prompt, user_prompt,
            api_key=anthropic_key,
            model=model or getattr(settings, "ANTHROPIC_MODEL", "claude-sonnet-5"),
            max_tokens=max_tokens,
            json_schema=json_schema,
            effort=effort,
        )
        return text, None
    except AIClientError as exc:
        # Already phrased for a user by ai_client.
        return None, str(exc)
    except Exception as exc:
        logger.exception("Anthropic call failed: %s", exc)
        return None, "Something went wrong with AI generation."
