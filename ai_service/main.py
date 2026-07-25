"""
FastAPI AI Microservice — Standalone async LLM worker.

Responsibilities:
  - Owns all LLM API keys (Groq, Anthropic)
  - Exposes a single POST /generate endpoint consumed by the Django app
  - Never exposed to the public internet — internal service only

Django acts as a thin client that forwards pre-built prompts here and
awaits the response without blocking its own worker threads.
"""

import os
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# ── Sentry Error Tracking (optional — reads from SENTRY_DSN env var) ──────
_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.1,
            send_default_pii=False,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        )
    except ImportError:
        pass  # sentry-sdk not installed — skip silently

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# ---------------------------------------------------------------------------
# App & Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai_service")

app = FastAPI(
    title="AI Generation Service",
    description="Internal async LLM worker for the CVAI SaaS platform.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Schemas (Pydantic strict validation — bad payloads never reach the LLM)
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    system_prompt: str = Field(..., min_length=1, description="LLM system instruction")
    user_prompt: str = Field(..., min_length=1, description="User data / content payload")
    provider: str = Field(
        default="groq",
        pattern="^(groq|anthropic)$",
        description="LLM provider: 'groq' (default) or 'anthropic'",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    json_mode: bool = Field(
        default=False,
        description="Force strict JSON output. Used by the resume parser; "
                    "prose callers (cover letter, ATS) leave this off.",
    )
    max_tokens: int = Field(
        default=4096, ge=256, le=32000,
        description="Upper bound on the completion. Structured resume JSON needs "
                    "more headroom than a cover letter, or the object is cut off.",
    )


class GenerateResponse(BaseModel):
    result: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM helpers (both async — no thread blocking)
# ---------------------------------------------------------------------------
async def _call_groq(
    system_prompt: str, user_prompt: str, temperature: float,
    json_mode: bool = False, max_tokens: int = 4096,
) -> str:
    """
    Async Groq/Llama call via raw httpx.
    httpx.AsyncClient is used so the uvicorn event loop is never blocked
    while waiting for the upstream LLM response.

    `max_tokens` is always sent explicitly — relying on the provider default
    silently truncated long structured resumes mid-object.
    """
    if not GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY not configured on AI service.")

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # Constrained decoding — the model cannot emit prose or code fences.
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    # A 'length' finish reason means the completion was cut off — surface it so
    # Django can retry rather than trying to parse a half-written object.
    if choice.get("finish_reason") == "length":
        logger.warning("Groq completion hit max_tokens (%s) — output truncated.", max_tokens)
    return choice["message"]["content"]


async def _call_anthropic(
    system_prompt: str, user_prompt: str, temperature: float,
    max_tokens: int = 4096,
) -> str:
    """
    Async Anthropic / Claude call using the official SDK's AsyncAnthropic client.
    Switch via provider='anthropic' in the request payload.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured on AI service.")

    try:
        import anthropic
    except ImportError:
        raise HTTPException(status_code=503, detail="'anthropic' package not installed in ai_service env.")

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=temperature,
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness probe — used by Docker health checks and Render."""
    return {"status": "ok"}


@app.post("/generate", response_model=GenerateResponse, tags=["generation"])
async def generate(body: GenerateRequest) -> GenerateResponse:
    """
    Central async generation endpoint.

    Django sends the fully-assembled system_prompt + user_prompt here.
    This service owns the LLM API keys and handles retry/timeout logic
    so that knowledge of external APIs never leaks into the Django layer.

    Flow:
      Django (async view) ──httpx.AsyncClient──▶ POST /generate
                                                        │
                                        ┌───────────────┴──────────────────┐
                                        ▼ provider=groq      ▼ provider=anthropic
                               _call_groq()            _call_anthropic()
                                        │                     │
                                        └────────── result ───┘
                                                        │
      Django (async view) ◀──────────── GenerateResponse ───┘
    """
    logger.info(
        "[/generate] provider=%s  system_len=%d  user_len=%d",
        body.provider,
        len(body.system_prompt),
        len(body.user_prompt),
    )

    try:
        if body.provider == "anthropic":
            text = await _call_anthropic(
                body.system_prompt, body.user_prompt, body.temperature,
                max_tokens=body.max_tokens,
            )
        else:
            text = await _call_groq(
                body.system_prompt, body.user_prompt, body.temperature,
                json_mode=body.json_mode, max_tokens=body.max_tokens,
            )

        logger.info("[/generate] success, result_len=%d", len(text))
        return GenerateResponse(result=text)

    except httpx.TimeoutException:
        logger.warning("[/generate] LLM call timed out")
        return GenerateResponse(error="AI took too long to respond. Please try again.")

    except httpx.HTTPStatusError as exc:
        logger.error(
            "[/generate] LLM HTTP error: %s — %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return GenerateResponse(error=f"AI API returned an error ({exc.response.status_code}). Try again.")

    except HTTPException:
        # Config errors — re-raise so FastAPI returns the proper 503
        raise

    except Exception as exc:
        logger.exception("[/generate] Unexpected error: %s", exc)
        return GenerateResponse(error="Something went wrong with AI generation. Please try again.")
