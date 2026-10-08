from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class WeftException(HTTPException):
    """Base exception for Weft application errors."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.error_message = message
        self.details = details or {}
        super().__init__(
            status_code=status_code,
            detail={
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                }
            },
        )


class BadRequestError(WeftException):
    def __init__(
        self,
        code: str = "BAD_REQUEST",
        message: str = "Bad request",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, code, message, details)


class UnauthorizedError(WeftException):
    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED", message)


class ForbiddenError(WeftException):
    def __init__(self, message: str = "Insufficient permissions") -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, "FORBIDDEN", message)


class NotFoundError(WeftException):
    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, "NOT_FOUND", f"{resource} not found")


class ConflictError(WeftException):
    def __init__(
        self,
        code: str = "CONFLICT",
        message: str = "Resource conflict",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status.HTTP_409_CONFLICT, code, message, details)


class RateLimitError(WeftException):
    def __init__(self, limit: int, window: str, retry_after_seconds: int) -> None:
        super().__init__(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMIT_EXCEEDED",
            f"You have exceeded the maximum of {limit} requests per {window}.",
            {"limit": limit, "window": window, "retry_after_seconds": retry_after_seconds},
        )


async def weft_exception_handler(request: Request, exc: WeftException) -> JSONResponse:
    """Global handler for WeftException — returns structured error JSON."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
    )
