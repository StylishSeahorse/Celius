"""HTTP-level protections for every response, and a cross-site check for cookie requests.

The session cookie is SameSite=Strict already; the Origin check is a second layer for
state-changing requests that rely on the cookie (not on a bearer token).
"""

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.sessions import SESSION_COOKIE
from app.config import Settings

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

# The web app: scripts, styles and connections only from this origin. Inline styles are
# allowed (React style attributes); inline scripts are not.
WEB_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
    # ipc: is the desktop (Tauri) app's bridge to its native side.
    "connect-src 'self' ipc: http://ipc.localhost; worker-src 'self'; manifest-src 'self'; "
    "frame-src 'none'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class SecurityMiddleware:
    """Plain ASGI (not BaseHTTPMiddleware), so streamed responses (SSE) and client
    disconnects pass through untouched."""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings
        self.allowed_origins = {_origin(settings.app_public_url)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope.get("path", "")
        if (
            request.method in UNSAFE
            and path.startswith("/api/")
            and SESSION_COOKIE in request.cookies
            and not request.headers.get("authorization")
        ):
            origin = request.headers.get("origin")
            same_host = _origin(str(request.base_url))
            if origin and origin.lower() not in self.allowed_origins | {same_host}:
                response = JSONResponse(
                    {"error": {"code": "cross_origin", "message": "Cross-site request refused"}},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

        extra = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"same-origin"),
            (b"x-frame-options", b"DENY"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
        ]
        if self.settings.cookie_secure:
            extra.append((b"strict-transport-security", b"max-age=31536000"))
        if not path.startswith("/api/"):
            extra.append((b"content-security-policy", WEB_CSP.encode()))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(name, value) for name, value in extra if name not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
