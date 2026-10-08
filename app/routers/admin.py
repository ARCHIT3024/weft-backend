"""Admin router — mock stubs for Phase 0."""

from __future__ import annotations

import logging

from fastapi import APIRouter, status

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/authority-users", status_code=status.HTTP_201_CREATED)
async def create_authority_user_mock() -> dict:
    """Mock: Create a new authority account (admin only)."""
    return {
        "user_id": "550e8400-e29b-41d4-a716-446655440010",
        "email": "authority@municipality.gov.in",
        "name": "Authority User",
        "role": "AUTHORITY",
        "employee_id": "EMP-2025-001",
        "department": "Roads Department",
        "designation": "Field Supervisor",
        "created_at": "2026-07-22T12:00:00Z",
    }


@router.patch("/authority-users/{user_id}/deactivate")
async def deactivate_authority_user_mock(user_id: str) -> dict:
    """Mock: Deactivate authority account (admin only)."""
    return {
        "user_id": user_id,
        "is_active": False,
        "deactivated_at": "2026-07-22T12:30:00Z",
    }


@router.get("/departments")
async def list_departments_mock() -> dict:
    """Mock: List all departments."""
    return {
        "items": [
            {"id": "dept-uuid-1", "name": "Roads Department", "code": "ROADS", "sla_hours": 72},
            {"id": "dept-uuid-2", "name": "Sanitation Division", "code": "SANITATION", "sla_hours": 48},
            {"id": "dept-uuid-3", "name": "Water Supply Board", "code": "WATER", "sla_hours": 72},
            {"id": "dept-uuid-4", "name": "Electricity Department", "code": "ELECTRIC", "sla_hours": 24},
            {"id": "dept-uuid-5", "name": "Urban Infrastructure", "code": "INFRASTRUCTURE", "sla_hours": 96},
            {"id": "dept-uuid-6", "name": "General Administration", "code": "GENERAL", "sla_hours": 72},
        ],
    }


@router.post("/departments", status_code=status.HTTP_201_CREATED)
async def create_department_mock() -> dict:
    """Mock: Create a new department (admin only)."""
    return {
        "id": "dept-uuid-new",
        "name": "New Department",
        "code": "NEW",
        "sla_hours": 72,
        "created_at": "2026-07-22T12:00:00Z",
    }


@router.post("/zones", status_code=status.HTTP_201_CREATED)
async def create_zone_mock() -> dict:
    """Mock: Create a new geographic zone (admin only)."""
    return {
        "id": "zone-uuid-new",
        "name": "Zone 5 — Central Chennai",
        "department_id": "dept-uuid-1",
        "created_at": "2026-07-22T12:00:00Z",
    }


@router.get("/system/stats")
async def system_stats_mock() -> dict:
    """Mock: System-wide usage statistics (admin only)."""
    return {
        "total_users": 1250,
        "total_issues": 342,
        "total_resolved": 280,
        "active_authority_users": 12,
        "departments": 6,
        "zones": 15,
    }
