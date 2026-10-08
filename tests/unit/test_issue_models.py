"""Structural guards for the `issues` / `issue_images` ORM models.

These are pure metadata assertions — no database, no fixtures — so they run
in the unit tier everywhere, including on a machine with no PostGIS.  What
they protect:

1. Both models reach `Base.metadata`, so Alembic autogenerate and
   `migrations/env.py`'s `from app.models import *` can see them.
2. Every `relationship()` target resolves (`configure_mappers()`), which is
   the failure a missing import in `app/models/__init__.py` produces.
3. The geospatial contract: `issues.location` is GEOMETRY(POINT, 4326) — the
   same type family and SRID as `zones.boundary`, because zone assignment
   runs `ST_Contains` across the two — is GIST-indexed, and is generated
   from latitude/longitude rather than written by the application.
4. The MVP data ceiling: the columns cut with their features stay cut, so
   nobody quietly reintroduces an AI/moderation/dedupe field without a
   migration.
"""

from __future__ import annotations

import pytest
from geoalchemy2 import Geometry
from sqlalchemy.orm import configure_mappers

from app.database import Base
from app.models import Issue, IssueImage

ISSUE_MODELS = [Issue, IssueImage]

# Present in 05_weft_backend_schema.md §3.7/§3.8 but deliberately absent from
# the MVP; each one belongs to a cut feature.
OMITTED_ISSUE_COLUMNS = {
    "ai_suggested_category",
    "ai_confidence",
    "phash",
    "is_flagged",
    "captured_at",
}
OMITTED_IMAGE_COLUMNS = {
    "s3_key",
    "cdn_url",
    "uploaded_by",
    "file_size_bytes",
    "width_px",
    "height_px",
}


@pytest.mark.parametrize("model", ISSUE_MODELS, ids=lambda m: m.__name__)
def test_model_registered_in_metadata(model: type) -> None:
    assert model.__tablename__ in Base.metadata.tables
    assert Base.metadata.tables[model.__tablename__] is model.__table__


def test_models_exported_from_package() -> None:
    """`from app.models import *` (migrations/env.py) must see both models."""
    import app.models as models_pkg

    assert "Issue" in models_pkg.__all__
    assert "IssueImage" in models_pkg.__all__
    assert models_pkg.Issue is Issue
    assert models_pkg.IssueImage is IssueImage


def test_mappers_configure() -> None:
    """Every relationship target resolves — catches an unimported model."""
    configure_mappers()


def test_issue_foreign_keys_resolve() -> None:
    """FK targets point at the tables migration 009 wires up."""
    fk_targets = {
        col.name: {fk.target_fullname for fk in col.foreign_keys} for col in Issue.__table__.columns if col.foreign_keys
    }
    assert fk_targets == {
        "reporter_id": {"users.id"},
        "zone_id": {"zones.id"},
        "department_id": {"departments.id"},
        "assigned_to_id": {"authority_users.id"},
    }
    for name in ("reporter_id", "zone_id", "department_id", "assigned_to_id"):
        col = Issue.__table__.columns[name]
        assert col.nullable is True, f"{name} must be nullable (anonymous / unrouted issues)"
        for fk in col.foreign_keys:
            assert fk.ondelete == "SET NULL"


def test_issue_image_foreign_key_cascades() -> None:
    """Deleting an issue takes its images with it."""
    col = IssueImage.__table__.columns["issue_id"]
    assert col.nullable is False
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].target_fullname == "issues.id"
    assert fks[0].ondelete == "CASCADE"


def test_issue_image_relationship_back_references() -> None:
    configure_mappers()
    assert Issue.images.property.back_populates == "issue"
    assert IssueImage.issue.property.back_populates == "images"


def test_location_is_point_4326() -> None:
    """Geospatial contract: WGS84 point geometry, consistent with zones.boundary."""
    from app.models import Zone

    col = Issue.__table__.columns["location"]
    assert isinstance(col.type, Geometry)
    assert col.type.geometry_type == "POINT"
    assert col.type.srid == 4326
    # Same type family and SRID as zones.boundary — ST_Contains has no
    # geography overload, so the two must not diverge.
    assert col.type.srid == Zone.__table__.columns["boundary"].type.srid
    assert isinstance(Zone.__table__.columns["boundary"].type, Geometry)


def test_location_is_generated_from_lat_lng() -> None:
    """`location` is a STORED generated column; the app never writes it."""
    col = Issue.__table__.columns["location"]
    assert col.computed is not None
    assert col.computed.persisted is True
    sql = str(col.computed.sqltext)
    assert "ST_MakePoint(longitude, latitude)" in sql
    assert "4326" in sql

    for name in ("latitude", "longitude"):
        latlng = Issue.__table__.columns[name]
        assert latlng.nullable is False
        assert latlng.type.precision == 10
        assert latlng.type.scale == 7


def test_location_has_gist_index() -> None:
    """The map query is the one thing that must be fast."""
    indexes = {ix.name: ix for ix in Issue.__table__.indexes}
    assert "idx_issues_location_gist" in indexes
    gist = indexes["idx_issues_location_gist"]
    assert gist.dialect_options["postgresql"]["using"] == "gist"
    assert [c.name for c in gist.columns] == ["location"]
    assert "idx_issue_images_issue" in {ix.name for ix in IssueImage.__table__.indexes}


def test_status_and_category_use_existing_enum_types() -> None:
    """Enum types are owned by migration 002 — these columns only reference them."""
    status = Issue.__table__.columns["status"].type
    category = Issue.__table__.columns["category"].type
    image_type = IssueImage.__table__.columns["image_type"].type

    assert status.name == "issue_status"
    assert set(status.enums) == {"REPORTED", "IN_PROGRESS", "RESOLVED", "REJECTED"}
    assert Issue.__table__.columns["status"].server_default.arg == "REPORTED"

    assert category.name == "issue_category"
    assert set(category.enums) == {
        "POTHOLE",
        "GARBAGE_ACCUMULATION",
        "WATER_LOGGING",
        "BROKEN_STREET_LIGHT",
        "DAMAGED_FOOTPATH",
        "SEWAGE_OVERFLOW",
        "OTHER",
    }

    assert image_type.name == "image_type"
    assert set(image_type.enums) == {"REPORT", "RESOLUTION_PROOF"}


def test_upvote_count_defaults_to_zero() -> None:
    """Kept for the map UI; nothing writes it (no upvote endpoint in the MVP)."""
    col = Issue.__table__.columns["upvote_count"]
    assert col.nullable is False
    assert col.server_default.arg == "0"


def test_mvp_columns_stay_cut() -> None:
    """Guards the MVP data ceiling against a silent reintroduction."""
    issue_cols = set(Issue.__table__.columns.keys())
    image_cols = set(IssueImage.__table__.columns.keys())
    assert issue_cols & OMITTED_ISSUE_COLUMNS == set()
    assert image_cols & OMITTED_IMAGE_COLUMNS == set()
    # Local disk, not S3.
    assert "file_path" in image_cols
