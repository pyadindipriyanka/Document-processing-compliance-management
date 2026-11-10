import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        self.status_code, self.code, self.message, self.details = status_code, code, message, details
        super().__init__(message)


def bad_request(code: str, message: str, details=None) -> AppError:
    return AppError(400, code, message, details)


def unauthorized(code: str = "NOT_AUTHENTICATED", message: str = "Authentication required.") -> AppError:
    return AppError(401, code, message)


def forbidden(code: str, message: str) -> AppError:
    return AppError(403, code, message)


def not_found(code: str, message: str) -> AppError:
    return AppError(404, code, message)


def conflict(code: str, message: str, details=None) -> AppError:
    return AppError(409, code, message, details)


def unprocessable(code: str, message: str, details=None) -> AppError:
    return AppError(422, code, message, details)


def _body(status: int, code: str, message: str, details=None) -> JSONResponse:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status, content={"success": False, "error": error})


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _body(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return _body(422, "VALIDATION_ERROR", "Request validation failed.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 401: "NOT_AUTHENTICATED", 403: "FORBIDDEN"}
        return _body(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error", exc_info=exc)
        return _body(500, "INTERNAL_ERROR", "An unexpected error occurred.")
