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

It also owns what the app does when the provider is slow, down, or too
expensive: a bounded timeout, a circuit breaker, and a daily token count.
"""

import asyncio
import logging
import weakref
from typing import Optional

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Provider declined, was unreachable, or returned nothing usable."""


# ---------------------------------------------------------------------------
# CLIENT
# ---------------------------------------------------------------------------
# One client per event loop rather than one per call. A new AsyncAnthropic per
# call opened a new connection pool — a fresh TLS handshake on every generation
# — and ran on the SDK defaults: a 600s timeout with two retries, so a stuck
# call could hold a request for half an hour.
#
# Keyed by loop because the pool's connections belong to the loop that opened
# them. Production runs one loop per uvicorn worker, so that is one client per
# worker; tests start a loop per call and get a fresh client each time.
#
# The read timeout has to cover a whole non-streamed response: nothing arrives
# until generation finishes, and a full 8192-token resume parse takes well
# over a minute.
_TIMEOUT = httpx.Timeout(180.0, connect=5.0)
_clients = weakref.WeakKeyDictionary()


def _client_for(anthropic, api_key):
    per_loop = _clients.setdefault(asyncio.get_running_loop(), {})
    key = (id(anthropic), api_key)
    if key not in per_loop:
        per_loop[key] = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=_TIMEOUT, max_retries=1,
        )
    return per_loop[key]


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER
# ---------------------------------------------------------------------------
# When the provider is down, every generation used to wait out its own
# timeout and fail — each one holding a request and, before refunds, a
# generation. After BREAKER_THRESHOLD provider-side failures inside
# BREAKER_WINDOW seconds, calls fail fast for BREAKER_COOLDOWN seconds instead.
# The state lives in the shared cache, so all workers trip together.
BREAKER_THRESHOLD = 5
BREAKER_WINDOW = 60
BREAKER_COOLDOWN = 30
_BREAKER_FAILS_KEY = 'ai:breaker:fails'
_BREAKER_OPEN_KEY = 'ai:breaker:open'


async def _record_provider_failure():
    from .guards import hit_rate_limit
    tripped = await sync_to_async(hit_rate_limit)(
        _BREAKER_FAILS_KEY, BREAKER_THRESHOLD - 1, BREAKER_WINDOW,
    )
    if tripped:
        await cache.aset(_BREAKER_OPEN_KEY, 1, BREAKER_COOLDOWN)
        logger.error(
            "[anthropic] %d provider failures within %ss — failing fast for %ss",
            BREAKER_THRESHOLD, BREAKER_WINDOW, BREAKER_COOLDOWN,
        )


# ---------------------------------------------------------------------------
# TOKEN ACCOUNTING
# ---------------------------------------------------------------------------
# Nothing recorded what generation cost, so a spend spike was invisible until
# the invoice. Every call's usage is logged and added to a per-day counter.
# AI_DAILY_TOKEN_BUDGET (0 = off) turns that counter into a hard stop.
def _tokens_key():
    return f"ai:tokens:{timezone.now().date().isoformat()}"


def _add_tokens(n):
    key = _tokens_key()
    if not cache.add(key, n, 2 * 86400):
        try:
            cache.incr(key, n)
        except ValueError:
            cache.set(key, n, 2 * 86400)


async def _check_availability():
    """Raise AIClientError if the breaker is open or today's budget is spent."""
    if await cache.aget(_BREAKER_OPEN_KEY):
        raise AIClientError(
            "The AI service is having trouble right now. Please try again in a minute."
        )
    budget = getattr(settings, 'AI_DAILY_TOKEN_BUDGET', 0)
    if budget and (await cache.aget(_tokens_key()) or 0) >= budget:
        logger.error("[anthropic] daily token budget of %d reached — refusing calls", budget)
        raise AIClientError(
            "AI generation is paused for today. Please try again tomorrow."
        )


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

    await _check_availability()
    client = _client_for(anthropic, api_key)

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

    timeout_error = getattr(anthropic, "APITimeoutError", None)

    async def _create(**kw):
        try:
            return await client.messages.create(**kw)
        except anthropic.RateLimitError:
            logger.warning("[anthropic] rate limited")
            await _record_provider_failure()
            raise AIClientError(
                "The AI is busy right now. Please try again in a moment."
            )
        except anthropic.APIConnectionError as exc:
            await _record_provider_failure()
            if timeout_error is not None and isinstance(exc, timeout_error):
                logger.error("[anthropic] timed out after %ss", _TIMEOUT.read)
                raise AIClientError("The AI took too long to respond. Please try again.")
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
        if exc.status_code >= 500:
            await _record_provider_failure()
        if exc.status_code == 529:
            raise AIClientError(
                "The AI provider is overloaded right now. Please try again in a minute."
            )
        raise AIClientError(f"AI provider error ({exc.status_code}). Please try again.")

    usage = getattr(message, "usage", None)
    if usage is not None:
        used = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
        logger.info(
            "[anthropic] %s: %s input / %s output tokens",
            model, getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
        )
        if used:
            await sync_to_async(_add_tokens)(used)

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
