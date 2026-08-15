"""
Direct Anthropic client for the Django app.

WHY THIS EXISTS
---------------
Generation used to go Django -> FastAPI worker -> Anthropic. That worked
locally and broke in production, because the deployment is on Render's free
plan, which does not offer private networking between services:

  * pointing AI_SERVICE_URL at the worker's private address fails to resolve
    (httpx.ConnectError — "AI service is not running")
  * pointing it at the worker's public URL sends every call out of the
    datacenter and back through the platform edge, which rate-limits it with a
    plain-text 429 that never reaches the worker at all

Neither is fixable with an environment variable. Since every endpoint now uses
Anthropic, the worker had become a network detour between Django and an API
Django can call itself — so it calls it directly.

The worker has since been deleted. It was kept for a while as a "fallback",
which meant this call logic existed twice, in two services, with one test
covering only the copy production did not use — the `effort` retry that every
resume parse depends on was never the one under test. Two implementations of
one thing is not redundancy when only one of them runs.

This is the only client. It handles the parts that are easy to get wrong:
thinking blocks arriving before the text block, refusals returning HTTP 200
with no content, and `effort` being rejected outright by models that do not
support it. Covered by AnthropicEffortFallbackTest.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Provider declined, was unreachable, or returned nothing usable."""


async def call_anthropic(
    system_prompt: str,
    user_prompt: str,
    *,
    api_key: str,
    model: str,
    max_tokens: int = 4096,
    json_schema: Optional[dict] = None,
    effort: Optional[str] = None,
) -> str:
    """
    Call Anthropic and return the response text.

    Raises AIClientError with a user-safe message on any failure.

    Deliberately does not accept `temperature`: the current Claude models
    removed the sampling parameters and reject the request with a 400. Several
    call sites still pass one, so the transport in views.py swallows it and it
    must not be reintroduced here.
    """
    try:
        import anthropic
    except ImportError:
        raise AIClientError("The AI client library is not installed on the server.")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    output_config = {}
    if json_schema:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    if effort:
        # Thinking is on by default and shares the max_tokens budget with the
        # response, so a low effort keeps a long resume from truncating.
        output_config["effort"] = effort
    if output_config:
        kwargs["output_config"] = output_config

    async def _create(**kw):
        try:
            return await client.messages.create(**kw)
        except anthropic.RateLimitError:
            logger.warning("[anthropic] rate limited")
            raise AIClientError(
                "The AI is busy right now. Please try again in a moment."
            )
        except anthropic.APIConnectionError as exc:
            logger.error("[anthropic] connection error: %s", exc)
            raise AIClientError("Could not reach the AI provider. Please try again.")

    try:
        message = await _create(**kwargs)
    except anthropic.BadRequestError as exc:
        # `effort` is model-gated: the Opus/Sonnet reasoning models accept it,
        # Haiku 4.5 rejects the whole request. Since the model is an env var,
        # switching to a cheaper one would otherwise 400 every call. Retry
        # without it rather than maintaining a model list that drifts.
        if "effort" in str(exc).lower() and "output_config" in kwargs:
            logger.warning(
                "[anthropic] %s does not support `effort` — retrying without it", model
            )
            retry = dict(kwargs)
            oc = {k: v for k, v in kwargs["output_config"].items() if k != "effort"}
            if oc:
                retry["output_config"] = oc
            else:
                retry.pop("output_config", None)
            message = await _create(**retry)
        else:
            logger.error("[anthropic] bad request: %s", str(exc)[:300])
            raise AIClientError("The AI provider rejected the request.")
    except anthropic.APIStatusError as exc:
        logger.error("[anthropic] API error %s: %s", exc.status_code, str(exc)[:300])
        raise AIClientError(f"AI provider error ({exc.status_code}). Please try again.")

    # Safety classifiers decline with HTTP 200 and an empty or partial body, so
    # this has to be checked before touching content — indexing it would raise.
    if getattr(message, "stop_reason", None) == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        logger.warning("[anthropic] declined by safety classifiers (category=%s)", category)
        raise AIClientError("The AI declined this request. Please rephrase and try again.")

    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning("[anthropic] hit max_tokens (%s) — output truncated", max_tokens)

    # content is a list of blocks. Thinking is on by default, so content[0] is a
    # ThinkingBlock rather than the answer — find the text block.
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text

    logger.error(
        "[anthropic] no text block in response (blocks=%s)",
        [getattr(b, "type", "?") for b in message.content],
    )
    raise AIClientError("The AI returned an empty response. Please try again.")
