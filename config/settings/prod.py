"""Production settings.

Tuned for a single Render web service: HTTPS behind their proxy, whitenoise
serving static files, and host/origin config derived from the hostname Render
injects rather than hard-coded.
"""

from .base import *  # noqa: F401,F403
from .base import env, env_bool

DEBUG = False

# Render terminates TLS at its edge and forwards plain HTTP, so without this
# Django thinks every request is insecure: it would redirect forever and mark
# secure cookies as unsendable.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# --- Hosts and CSRF origins ----------------------------------------------
# RENDER_EXTERNAL_HOSTNAME is set by Render at runtime (e.g.
# "query-engine.onrender.com"). Appending rather than replacing so a custom
# domain in DJANGO_ALLOWED_HOSTS keeps working.
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

RENDER_HOST = env("RENDER_EXTERNAL_HOSTNAME")
if RENDER_HOST:
    ALLOWED_HOSTS.append(RENDER_HOST)

if not ALLOWED_HOSTS:
    # Fail visibly in logs rather than 400-ing every request with no clue why.
    ALLOWED_HOSTS = [".onrender.com"]

# Django 4+ checks the Origin header on every unsafe request against this list,
# and it must include the scheme. Without it every form POST -- running a query,
# rerunning one -- fails with "CSRF verification failed: Origin checking failed"
# even though the token is perfectly valid. This is THE Django-on-Render gotcha.
CSRF_TRUSTED_ORIGINS = [f"https://{h.lstrip('.')}" for h in ALLOWED_HOSTS if h != "*"]
if RENDER_HOST:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_HOST}")
CSRF_TRUSTED_ORIGINS = sorted(set(CSRF_TRUSTED_ORIGINS))

# --- Hardening ------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Render already redirects http->https at the edge; doing it again here only
# adds a hop, so it stays opt-in.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", False)

# --- Rate limiting --------------------------------------------------------
# The API is public and every call executes a real query across a process pool.
# On a 512 MB instance a loop of concurrent scans is enough to OOM the service,
# so anonymous callers get a ceiling. Generous enough that nobody exploring the
# demo will notice; low enough that a script cannot take the site down.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": env("API_THROTTLE_ANON", "60/min")},
}

# Hashed, compressed static filenames so a CSS change can never be served from
# a stale browser cache -- the exact failure that made the UI look broken in dev.
STORAGES["staticfiles"]["BACKEND"] = (  # noqa: F405
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
