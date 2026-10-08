"""Analytics router — mock stubs for Phase 0."""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/summary")
async def analytics_summary_mock() -> dict:
    """Mock: KPI summary (authority only)."""
    return {
        "total_reported": 342,
        "total_in_progress": 45,
        "total_resolved": 280,
        "total_rejected": 17,
        "sla_breach_count": 8,
        "avg_resolution_hours": 48.5,
    }


@router.get("/heatmap")
async def analytics_heatmap_mock() -> dict:
    """Mock: Geo-aggregated issue density for Leaflet.heat."""
    return {
        "points": [
            {"lat": 13.0827, "lng": 80.2707, "issue_count": 12, "total_upvotes": 45},
            {"lat": 13.0650, "lng": 80.2550, "issue_count": 8, "total_upvotes": 22},
            {"lat": 13.0900, "lng": 80.2800, "issue_count": 5, "total_upvotes": 15},
        ],
    }


@router.get("/resolution-times")
async def analytics_resolution_times_mock() -> dict:
    """Mock: Avg resolution time per category/department."""
    return {
        "data": [
            {"category": "POTHOLE", "avg_hours": 36.2, "count": 85},
            {"category": "GARBAGE_ACCUMULATION", "avg_hours": 24.1, "count": 62},
            {"category": "WATER_LOGGING", "avg_hours": 52.8, "count": 40},
            {"category": "BROKEN_STREET_LIGHT", "avg_hours": 28.5, "count": 55},
        ],
    }


@router.get("/sla-breaches")
async def analytics_sla_breaches_mock() -> dict:
    """Mock: Issues exceeding SLA threshold."""
    return {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "total_pages": 1,
    }


@router.get("/export")
async def analytics_export_mock() -> dict:
    """Mock: CSV or PDF export endpoint."""
    return {
        "message": "Export endpoint — will generate CSV/PDF in Phase 3/4",
        "format": "csv",
    }
