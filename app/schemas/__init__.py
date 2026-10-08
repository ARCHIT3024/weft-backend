"""Pydantic request/response schemas.

Public schema surface for the Weft API, derived from the frozen OpenAPI
contract (`openapi.yaml`).
"""

from __future__ import annotations

from app.schemas.auth import (
    AppleOAuthRequest,
    AuthResponse,
    GoogleOAuthRequest,
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    TokenType,
)
from app.schemas.common import (
    ErrorDetail,
    ErrorResponse,
    GeoPoint,
    HealthResponse,
    IDMixin,
    PaginatedResponse,
    PaginationParams,
    SortDirection,
    TimestampMixin,
)
from app.schemas.notification import (
    MarkAllNotificationsReadResponse,
    NotificationChannel,
    NotificationListResponse,
    NotificationOut,
    NotificationType,
)
from app.schemas.user import (
    PreferredLanguage,
    UpdateProfileRequest,
    UserProfile,
    UserRole,
)

__all__ = [
    # auth
    "AppleOAuthRequest",
    "AuthResponse",
    # common
    "ErrorDetail",
    "ErrorResponse",
    "GeoPoint",
    "GoogleOAuthRequest",
    "HealthResponse",
    "IDMixin",
    "LoginRequest",
    "LogoutRequest",
    # notification
    "MarkAllNotificationsReadResponse",
    "NotificationChannel",
    "NotificationListResponse",
    "NotificationOut",
    "NotificationType",
    "PaginatedResponse",
    "PaginationParams",
    "PreferredLanguage",
    "RefreshTokenRequest",
    "RegisterRequest",
    "RegisterResponse",
    "SortDirection",
    "TimestampMixin",
    "TokenResponse",
    "TokenType",
    # user
    "UpdateProfileRequest",
    "UserProfile",
    "UserRole",
]
