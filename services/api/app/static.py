"""Serve the built web app (apps/web/dist) from the API, so UI and API share one origin.

One origin keeps the session cookie SameSite=Strict, and SSE then needs no CORS rules.
"""

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SpaStaticFiles(StaticFiles):
    """Static files with client-side routing: unknown paths fall back to index.html."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            response = None
        if response is None or response.status_code == 404:
            response = await super().get_response("index.html", scope)
        if path.startswith("assets/"):
            # Vite fingerprints asset filenames, so they can be cached forever.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def mount_web_app(app: FastAPI, static_root: str | None) -> None:
    if not static_root:
        return
    root = Path(static_root)
    if not (root / "index.html").is_file():
        return
    app.mount("/", SpaStaticFiles(directory=root, html=True), name="web")
