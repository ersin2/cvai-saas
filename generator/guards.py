"""
Request guards for the generator app.

Everything here runs *before* a view does its work and can stop the request:
the JSON auth guard, the per-plan rate limiter, the SSRF check applied to
user-supplied URLs, and the upload validators for resume PDFs and photos.

Split out of views.py, which had grown to 1,793 lines by mixing HTTP handling
with these cross-cutting checks. They are imported back into views under the
same names, so `from generator.views import _check_rate_limit` still resolves.
"""

import ipaddress
import logging
import socket
from functools import wraps
from urllib.parse import urlparse

from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AUTH GUARD FOR THE ASYNC JSON ENDPOINTS
# ---------------------------------------------------------------------------
# Django's @login_required is async-aware (it detects a coroutine view and
# wraps it correctly), so it works here — but it answers an unauthenticated
# request with a 302 to the login page. Every view below returns JsonResponse
# exclusively and is only ever reached from fetch(), where following that
# redirect yields 200 + HTML and surfaces as a confusing JSON parse error
# instead of "your session expired". A 401 lets the caller detect it directly.
#
# This deliberately does NOT sniff the path or X-Requested-With: these routes
# have no HTML representation, so the JSON answer is always the right one.
# For method checks keep Django's @require_POST — its 405 is already correct.
# ---------------------------------------------------------------------------
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


def json_login_required(view_func):
    """Async login guard that answers 401 JSON instead of redirecting."""
    @wraps(view_func)
    async def _wrapped(request, *args, **kwargs):
        user = await request.auser()
        if not user.is_authenticated:
            return JsonResponse(
                {'error': 'Authentication required. Please sign in again.'},
                status=401,
            )
        return await view_func(request, *args, **kwargs)
    return _wrapped

# ---------------------------------------------------------------------------
# RATE LIMITER  (uses Django's default cache — LocMemCache or Redis)
# Free: 3 req/min  |  Pro: 20 req/min  |  Elite: 50 req/min
# ---------------------------------------------------------------------------
RATE_LIMITS = {'free': 3, 'pro': 20, 'elite': 50}
RATE_WINDOW = 60  # seconds

# Live preview (Studio) re-renders the PDF on every debounced edit, so it needs a
# generous per-user bucket separate from the AI-generation limit — otherwise a free
# user's preview 429s after 3 edits/min. This is a render, not an AI generation.
PREVIEW_RATE_LIMIT = 60  # renders/min per user


def _check_rate_limit(user, plan: str, *, limit=None, key_prefix='rl'):
    """
    Returns None if the user is within limits, or a JsonResponse(429) if throttled.
    Uses a simple per-user counter stored in Django's cache.

    Deployment note: in production the cache is Redis (shared across the 4 ASGI
    workers), so the limit is global. With the LocMem fallback (local dev) the
    counter is per-process, so multi-worker dev servers see a looser effective
    limit — acceptable for local use.

    Fails OPEN: if the cache backend is degraded (django-redis is configured with
    IGNORE_EXCEPTIONS=True and returns None on connection errors), we allow the
    request rather than 500 every generation.
    """
    if limit is None:
        limit = RATE_LIMITS.get(plan, 3)
    cache_key = f"{key_prefix}:{user.id}"

    # First request in this window — add() returns True when the key was created.
    if cache.add(cache_key, 1, RATE_WINDOW):
        return None

    try:
        count = cache.incr(cache_key)
    except ValueError:
        # Key expired between add() and incr() — treat as a fresh window.
        cache.set(cache_key, 1, RATE_WINDOW)
        return None

    # Degraded cache backend returned None instead of an int — fail open.
    if count is None:
        return None

    if count > limit:
        return JsonResponse(
            {'error': 'Too many requests. Please wait a minute and try again.'},
            status=429,
        )
    return None

# ---------------------------------------------------------------------------
# SSRF PROTECTION
# Validate that a user-supplied URL points at a PUBLIC host before we fetch it.
# The check runs on every redirect hop (see scrape_job_url) so a public URL
# cannot 302-redirect us into the internal network — cloud metadata
# (169.254.169.254), Redis, or Postgres.
# Note: a determined attacker could still DNS-rebind between this resolve and
# httpx's connect (TOCTOU); blocking every private range on each hop keeps the
# residual risk low without a custom pinned-IP transport.
# ---------------------------------------------------------------------------
_SCRAPE_MAX_REDIRECTS = 5


def _url_points_to_public_host(url: str):
    """
    Return (True, None) if `url` is an http(s) URL whose hostname resolves
    ONLY to public IP addresses; otherwise (False, reason).
    Every resolved address (IPv4 + IPv6) must be public.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, 'Invalid URL.'

    if parsed.scheme not in ('http', 'https'):
        return False, 'Only http and https URLs are allowed.'

    hostname = parsed.hostname
    if not hostname:
        return False, 'Invalid URL.'

    try:
        addrinfo = socket.getaddrinfo(
            hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
        )
    except socket.gaierror:
        return False, 'Could not resolve hostname.'

    for *_head, sockaddr in addrinfo:
        try:
            ip_obj = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False, 'Invalid IP address resolved.'
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
            return False, 'Requests to internal or private network addresses are not allowed.'

    return True, None

# ---------------------------------------------------------------------------
# PDF UPLOAD VALIDATION  (shared by parse_resume_pdf and generate_resume)
# Rejects oversized files and anything whose leading bytes are not a real PDF
# signature, so pdfminer never parses a disguised or huge payload.
# ---------------------------------------------------------------------------
PDF_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_pdf_upload(pdf_file):
    """
    Return (True, None) if the upload is a real PDF within the size cap,
    else (False, error_message). Leaves the file pointer reset to 0 so the
    caller can hand the file straight to pdfminer.
    """
    if pdf_file.size > PDF_MAX_BYTES:
        return False, 'File too large. Maximum size is 5MB.'
    magic = pdf_file.read(4)
    pdf_file.seek(0)
    if magic != b'%PDF':
        return False, 'Invalid file format. Only real PDF files are accepted.'
    return True, None

# ---------------------------------------------------------------------------
# PHOTO UPLOAD VALIDATION  (avatar on the photo-bearing resume templates)
# ---------------------------------------------------------------------------
# Previously unvalidated: any file of any size reached PIL, and a large enough
# image exhausted the worker's memory during decode. The worker died mid-render,
# so the browser saw a failed request rather than an error page — which is what
# produced the generic "Preview failed / try a smaller photo" toast with no way
# to tell whether the photo was actually the problem.
PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB, matching PDF_MAX_BYTES

# Leading bytes for the formats Pillow handles well here. WebP is RIFF....WEBP,
# so the container tag is checked separately at offset 8.
_PHOTO_MAGIC = (
    (b'\xff\xd8\xff', 'JPEG'),
    (b'\x89PNG\r\n\x1a\n', 'PNG'),
    (b'GIF87a', 'GIF'),
    (b'GIF89a', 'GIF'),
)


def _validate_photo_upload(photo_file):
    """
    Return (True, None) if the upload is a real image within the size cap,
    else (False, error_message). Leaves the pointer at 0 for the caller.

    Deliberately specific in its errors: the point is that a user who picks a
    12MB camera original learns that, instead of being told the preview failed.
    """
    if not photo_file:
        return True, None  # no photo is valid — the field is optional

    if photo_file.size > PHOTO_MAX_BYTES:
        mb = photo_file.size / 1024 / 1024
        return False, (
            f'Photo is too large ({mb:.1f}MB). Maximum size is 5MB — '
            'try a smaller image or one exported at a lower resolution.'
        )

    head = photo_file.read(12)
    photo_file.seek(0)
    for magic, _label in _PHOTO_MAGIC:
        if head.startswith(magic):
            return True, None
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return True, None

    return False, 'Unsupported photo format. Please use a JPEG, PNG, GIF or WebP image.'
