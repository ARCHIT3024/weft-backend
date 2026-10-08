from __future__ import annotations

import math
from enum import StrEnum
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ── Pagination ──────────────────────────────────────────────────────────


class SortDirection(StrEnum):
    """Sort direction for list queries."""

    ASC = "asc"
    DESC = "desc"


class PaginationParams(BaseModel):
    """Query parameters for paginated list endpoints."""

    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(default=20, ge=1, le=100, description="Items per page")
    sort_by: str = Field(default="created_at", description="Field to sort by")
    sort_dir: SortDirection = Field(default=SortDirection.DESC, description="Sort direction")

    @property
    def offset(self) -> int:
        """Calculate SQL OFFSET from page number."""
        return (self.page - 1) * self.page_size


DataT = TypeVar("DataT")


class PaginatedResponse(BaseModel, Generic[DataT]):  # noqa: UP046 — Pydantic requires Generic subclass
    """Standard paginated response wrapper."""

    model_config = ConfigDict(from_attributes=True)

    items: list[DataT]
    total: int = Field(description="Total number of items matching the query")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Items per page")
    total_pages: int = Field(description="Total number of pages")

    @classmethod
    def create(
        cls,
        items: list[DataT],
        total: int,
        page: int,
        page_size: int,
    ) -> PaginatedResponse[DataT]:
        """Factory method to create a paginated response."""
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)),
        )


# ── Geospatial ──────────────────────────────────────────────────────────


class GeoPoint(BaseModel):
    """A geographic point (latitude, longitude)."""

    latitude: float = Field(..., ge=-90, le=90, description="Latitude in degrees")
    longitude: float = Field(..., ge=-180, le=180, description="Longitude in degrees")


# ── Error Response ──────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    """Error detail within the standard error response."""

    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: dict[str, Any] = Field(default_factory=dict, description="Additional error context")


class ErrorResponse(BaseModel):
    """Standard error response format per TRD Section 4."""

    error: ErrorDetail


# ── Common Mixins ───────────────────────────────────────────────────────


class TimestampMixin(BaseModel):
    """Mixin for created_at / updated_at fields."""

    model_config = ConfigDict(from_attributes=True)

    created_at: str = Field(description="ISO 8601 creation timestamp")
    updated_at: str | None = Field(default=None, description="ISO 8601 last update timestamp")


class IDMixin(BaseModel):
    """Mixin providing a UUID id field."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier")


# ── Health Check ────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str
    env: str
