from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """An expected failure with a stable, client-safe `code`."""

    def __init__(self, status_code: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


def not_found(what: str) -> AppError:
    return AppError(404, "not_found", f"{what} not found")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, **exc.extra}},
        )
