"""Regression guard for the ORM model registry.

`app/main.py` never imports models and `app/core/permissions.py` imports
`User` lazily, so a broken model import used to be invisible: the app booted,
every test passed, and the first authenticated request 500'd.  Alembic was
equally blind — `migrations/env.py` does `from app.models import *`, so a
model missing from `app/models/__init__.py` silently vanishes from
autogenerate.

These tests are deliberately dependency-free: pure imports plus metadata
assertions, no database connection and no fixtures.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from sqlalchemy.orm import configure_mappers

import app.models as models_pkg
from app.database import Base

# Named imports for assertions about specific models. ALL_MODELS below is
# discovered rather than hardcoded, so adding a model does not break the suite;
# these names exist only because the tests that use them are *about* these
# models, and naming them there is clearer than indexing into the registry.
from app.models import (
    AuthorityUser,
    AuthorityZone,
    Department,
    DepartmentCategory,
    FcmToken,
    Notification,
    RefreshToken,
    User,
    Zone,
)


def _discover_model_classes() -> list[type]:
    """Import every module under app/models and return the mapped classes found.

    Discovered from disk rather than hardcoded. An earlier version of this file
    listed the models by name, which meant adding any new model broke the suite
    by construction — the failure said nothing about correctness, only that the
    list was stale. Walking the package instead means a newly added model is
    picked up automatically, and the assertions below still catch the bug that
    actually matters: a model file that exists but was never exported from
    `app/models/__init__.py`, and so is invisible to `from app.models import *`
    in migrations/env.py.
    """
    for module in pkgutil.walk_packages(models_pkg.__path__, models_pkg.__name__ + "."):
        importlib.import_module(module.name)

    seen: dict[str, type] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        seen[cls.__name__] = cls
    return [seen[name] for name in sorted(seen)]


ALL_MODELS = _discover_model_classes()

EXPECTED_TABLES = {model.__tablename__ for model in ALL_MODELS}


def test_at_least_the_core_models_exist() -> None:
    """Sanity floor so discovery returning nothing cannot make the suite vacuous.

    Every assertion here is derived from discovery, so a bug that found zero
    models would leave the other tests trivially passing.
    """
    names = {model.__name__ for model in ALL_MODELS}
    assert {"User", "RefreshToken", "Department", "Zone"} <= names
    assert len(ALL_MODELS) >= 7


def test_user_module_imports_standalone() -> None:
    """`import app.models.user` must not raise.

    `app/core/permissions.py` does exactly this inside `get_current_user`.
    """
    user_module = importlib.import_module("app.models.user")

    assert user_module.User is models_pkg.User
    # Bottom-of-module imports that dodge circularity must resolve.
    assert user_module.AuthorityUser is models_pkg.AuthorityUser
    assert user_module.RefreshToken is models_pkg.RefreshToken


def test_star_import_exposes_every_model() -> None:
    """`from app.models import *` (used by migrations/env.py) sees all models.

    Discovery walks the package from disk, so this compares what actually exists
    against what `__init__.py` exports — catching a model file that was written
    but never added to `__all__`, which Alembic autogenerate would silently miss.
    """
    assert set(models_pkg.__all__) == {m.__name__ for m in ALL_MODELS}
    for name in models_pkg.__all__:
        assert hasattr(models_pkg, name), f"{name} in __all__ but not importable"


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_model_registered_in_metadata(model: type) -> None:
    """Each model's __tablename__ is present in Base.metadata.tables."""
    assert model.__tablename__ in Base.metadata.tables
    assert Base.metadata.tables[model.__tablename__] is model.__table__


def test_metadata_contains_expected_tables() -> None:
    """Alembic autogenerate sees exactly the tables Phase 1 defines."""
    assert EXPECTED_TABLES.issubset(set(Base.metadata.tables))


def test_mappers_configure() -> None:
    """All relationship() targets resolve — catches an unimported model."""
    configure_mappers()


def test_relationship_back_references() -> None:
    """user.py's declared back_populates targets exist on the other side."""
    configure_mappers()
    assert User.authority_profile.property.back_populates == "user"
    assert AuthorityUser.user.property.back_populates == "authority_profile"
    assert User.refresh_tokens.property.back_populates == "user"
    assert RefreshToken.user.property.back_populates == "refresh_tokens"


def test_primary_keys_present() -> None:
    """Every table has a primary key (composite for the association tables)."""
    for model in ALL_MODELS:
        pk_cols = [c.name for c in model.__table__.primary_key.columns]
        assert pk_cols, f"{model.__name__} has no primary key"

    assert [c.name for c in AuthorityZone.__table__.primary_key.columns] == [
        "authority_user_id",
        "zone_id",
    ]
    assert [c.name for c in DepartmentCategory.__table__.primary_key.columns] == [
        "department_id",
        "category",
    ]


def test_department_has_upvote_alert_threshold() -> None:
    """Pulled forward from task 4.15a — task 3.11 depends on it in Week 7."""
    col = Department.__table__.columns["upvote_alert_threshold"]
    assert col.nullable is False
    assert col.server_default.arg == "10"


def test_zone_boundary_is_polygon_4326() -> None:
    """Geospatial contract: WGS84 polygon, GIST-indexed."""
    col = Zone.__table__.columns["boundary"]
    assert col.type.geometry_type == "POLYGON"
    assert col.type.srid == 4326
    index_names = {ix.name for ix in Zone.__table__.indexes}
    assert "idx_zones_boundary" in index_names


def test_notification_models_are_exported() -> None:
    """Tasks 1.25/1.26 — both tables must be visible to Alembic autogenerate."""
    assert {"FcmToken", "Notification"} <= set(models_pkg.__all__)
    assert {"user_fcm_tokens", "notifications"} <= set(Base.metadata.tables)


def test_device_token_has_exactly_one_owner() -> None:
    """`UNIQUE (device_token)` is the upsert target that moves a token between users."""
    assert FcmToken.__table__.columns["device_token"].unique is True
    assert FcmToken.__table__.columns["user_id"].nullable is False


def test_notification_enums_reuse_the_migration_002_types() -> None:
    """`create_type=False`, or SQLAlchemy would emit a second CREATE TYPE."""
    for column, type_name in (("type", "notification_type"), ("channel", "notification_channel")):
        enum_type = Notification.__table__.columns[column].type
        assert enum_type.name == type_name
        assert enum_type.create_type is False


def test_notification_issue_link_survives_issue_deletion() -> None:
    """ON DELETE SET NULL: deleting an issue must not erase what its reporter was told."""
    (fk,) = Notification.__table__.columns["issue_id"].foreign_keys
    assert fk.ondelete == "SET NULL"
    assert Notification.__table__.columns["issue_id"].nullable is True
