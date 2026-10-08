"""SQLAlchemy ORM models.

Every model MUST be imported here.  Two things depend on it:

1. `app/migrations/env.py` does `from app.models import *` — anything missing
   from this module is invisible to Alembic autogenerate.
2. Relationship targets are resolved by class name against the declarative
   registry, so a model that is never imported makes `configure_mappers()`
   fail on the first ORM query that touches it.

Import order is safe in any arrangement: `user.py` is the only module with
runtime cross-model imports (`authority_user`, `refresh_token`, appended at
the bottom of the module to satisfy its own relationship annotations).  Every
other module takes TYPE_CHECKING-only imports, so no import cycle can form.

`tests/unit/test_models_import.py` guards all of the above.
"""

from __future__ import annotations

from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.department_category import DepartmentCategory
from app.models.fcm_token import FcmToken
from app.models.issue import Issue
from app.models.issue_image import IssueImage
from app.models.issue_status_history import IssueStatusHistory
from app.models.notification import Notification
from app.models.refresh_token import RefreshToken
from app.models.upvote import Upvote
from app.models.user import User
from app.models.zone import Zone

__all__ = [
    "AuthorityUser",
    "AuthorityZone",
    "Department",
    "DepartmentCategory",
    "FcmToken",
    "Issue",
    "IssueImage",
    "IssueStatusHistory",
    "Notification",
    "RefreshToken",
    "Upvote",
    "User",
    "Zone",
]
