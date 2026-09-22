"""
Request guards for the generator app.

Everything here runs *before* a view does its work and can stop the request:
the JSON auth guard, the generation quota reservation, the per-plan rate
limiter, the SSRF check applied to user-supplied URLs, and the upload
validators for resume PDFs and photos.

Split out of views.py, which had grown to 1,793 lines by mixing HTTP handling
with these cross-cutting checks. They are imported back into views under the
same names, so `from generator.views import _check_rate_limit` still resolves.
"""

import inspect
import ipaddress
import logging
import socket
from contextlib import asynccontextmanager
from functools import wraps
from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.http import JsonResponse

from users.models import Profile

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

# Fetching a job page and reading an uploaded PDF call no model. They shared
# the AI bucket, so a free user who pasted a job link, uploaded a resume and
# clicked Generate had spent 3 of 3 requests and was told to wait a minute.
UTILITY_RATE_LIMIT = 20  # requests/min per user


def hit_rate_limit(key, limit, window=RATE_WINDOW):
    """
    Count one hit on `key` and return True once hits exceed `limit` within
    `window` seconds.

    add() then incr() are both atomic in Redis, so concurrent hits are all
    counted. The get-then-set this replaces in the sign-up limiter was not:
    two simultaneous requests read the same count and each wrote count + 1.

    Fails OPEN: if the cache backend is degraded (django-redis is configured
    with IGNORE_EXCEPTIONS=True and returns None on connection errors), the
    hit is allowed rather than turning a Redis outage into a site outage.
    """
    # First hit in this window — add() returns True when the key was created.
    if cache.add(key, 1, window):
        return 1 > limit
    try:
        count = cache.incr(key)
    except ValueError:
        # Key expired between add() and incr() — treat as a fresh window.
        cache.set(key, 1, window)
        return False
    # Degraded cache backend returned None instead of an int — fail open.
    if count is None:
        return False
    return count > limit


def rate_limit_reached(key, limit):
    """True if `key` has already used up `limit`, without counting a hit."""
    return (cache.get(key) or 0) >= limit


def client_ip(request):
    """
    The address of the client that sent the request.

    Production sits behind Cloudflare, which sets CF-Connecting-IP on every
    request it forwards, overwriting anything the client sent. The leftmost
    X-Forwarded-For entry, which the sign-up limiter used to trust, is written
    by the client and can be anything; the rightmost is a Cloudflare edge that
    thousands of users share. Neither identifies a client.
    """
    cf_ip = request.META.get('HTTP_CF_CONNECTING_IP', '').strip()
    if cf_ip:
        return cf_ip
    return request.META.get('REMOTE_ADDR', '')


def _check_rate_limit(user, plan: str, *, limit=None, key_prefix='rl'):
    """
    Returns None if the user is within limits, or a JsonResponse(429) if throttled.
    Uses a simple per-user counter stored in Django's cache.

    Deployment note: in production the cache is Redis (shared across the 4 ASGI
    workers), so the limit is global. With the LocMem fallback (local dev) the
    counter is per-process, so multi-worker dev servers see a looser effective
    limit — acceptable for local use.
    """
    if limit is None:
        limit = RATE_LIMITS.get(plan, 3)
    if hit_rate_limit(f"{key_prefix}:{user.id}", limit):
        return JsonResponse(
            {'error': 'Too many requests. Please wait a minute and try again.'},
            status=429,
        )
    return None

# ---------------------------------------------------------------------------
# GENERATION QUOTA — reserve up front, refund unless the view commits
# ---------------------------------------------------------------------------
# The quota is taken BEFORE the model is called: checking it and charging it
# afterwards let parallel requests all pass the check and each get a paid call.
# That made every AI view a hand-written pair of use_generation() and
# refund_generation(), with a refund before each of four to seven exits. Two
# things went wrong with that shape:
#
#   * an exit without a refund — generate_resume once returned early on every
#     request, with the generation spent and nothing generated;
#   * an exception between the two — a DB error while saving the result left
#     the generation spent, after the model call had already been paid for.
#
# The reservation below refunds on every way out of the block except an
# explicit commit(), so a view only has to say when it delivered something.
# ---------------------------------------------------------------------------
class QuotaExhausted(Exception):
    """The user has no generation left in the current period."""


class _Reservation:
    __slots__ = ('committed',)

    def __init__(self):
        self.committed = False

    def commit(self):
        """The user got a result; keep the generation spent."""
        self.committed = True


@asynccontextmanager
async def reserved_generation(profile):
    """
    Spend one generation for the duration of the block.

    Raises QuotaExhausted if there is none to spend. Refunds on any exit —
    return, exception, or falling off the end — unless commit() was called.
    Charge and refund go through the same Profile instance, so both hit the
    same counter even if the plan changes mid-request.
    """
    if profile.needs_email_verification():
        raise QuotaExhausted(profile.VERIFY_EMAIL_MESSAGE)
    if not await sync_to_async(profile.use_generation)():
        raise QuotaExhausted(profile.quota_message())
    reservation = _Reservation()
    try:
        yield reservation
    finally:
        if not reservation.committed:
            await sync_to_async(profile.refund_generation)()


def spends_generation(on_exhausted):
    """
    Decorator for the AI views: reserve a generation, then apply the throttle.

    Quota before throttle, deliberately. Both gates can reject the same click
    and they give opposite advice: being out of generations is permanent until
    you upgrade, being rate-limited clears in a minute. Checked the other way
    round, a user who had spent their last generation and clicked again was
    told to wait a minute — advice that never comes true.

    The view is called as view(request, *args, profile=..., reservation=...)
    and must call reservation.commit() once it has something to show.
    `on_exhausted(request, profile, message)` builds the out-of-quota response;
    it may be sync or async, because the Tools views answer with a page.
    """
    def decorator(view_func):
        @wraps(view_func)
        async def _wrapped(request, *args, **kwargs):
            profile, _ = await Profile.objects.aget_or_create(user=request.user)
            try:
                async with reserved_generation(profile) as reservation:
                    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
                    if throttled:
                        return throttled
                    return await view_func(
                        request, *args, profile=profile, reservation=reservation, **kwargs
                    )
            except QuotaExhausted as exc:
                response = on_exhausted(request, profile, str(exc))
                if inspect.isawaitable(response):
                    response = await response
                return response
        return _wrapped
    return decorator

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
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:  # e.g. 'http://host:99999/' — urlparse raises on access
        return False, 'Invalid URL.'
    # Job postings are served on the standard ports. Allowing 8000/8080 only
    # widened the set of internal services a rebinding attack could reach.
    if port not in (80, 443):
        return False, 'Requests to non-standard ports are not allowed.'

    try:
        addrinfo = socket.getaddrinfo(
            hostname, port
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
    valid_format = False
    for magic, _label in _PHOTO_MAGIC:
        if head.startswith(magic):
            valid_format = True
            break
    if not valid_format and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        valid_format = True

    if not valid_format:
        return False, 'Unsupported photo format. Please use a JPEG, PNG, GIF or WebP image.'

    # Guard against decompression bombs by checking image dimensions
    try:
        from PIL import Image
        photo_file.seek(0)
        with Image.open(photo_file) as im:
            w, h = im.size
            if w * h > 16_000_000 or w > 4096 or h > 4096:
                photo_file.seek(0)
                return False, 'Image dimensions are too large. Maximum allowed is 4000x4000 pixels.'
    except Exception:
        pass
    finally:
        photo_file.seek(0)

    return True, None
