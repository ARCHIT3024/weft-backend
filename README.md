# weft-backend

API for Weft — a crowdsourced civic-issue reporting platform. Citizens report
civic problems (potholes, garbage, water logging, broken street lights) with a
photo and a GPS location; each report is auto-routed to a municipal department
and triaged by authority staff.

FastAPI · async SQLAlchemy 2 · PostgreSQL 15 + PostGIS 3.4 · Redis 7 · Alembic

## Running it locally

You need Docker running. Everything below assumes this directory.

```bash
docker compose up -d db redis     # PostgreSQL+PostGIS and Redis
pip install -r requirements-dev.txt
alembic upgrade head              # schema + department/category routing seed
python -m scripts.seed_dev_data   # dev-only: zones + demo accounts
uvicorn app.main:app --reload
```

Swagger UI at <http://localhost:8000/docs>. Health check at `/health`.

### Demo logins

Created by `scripts/seed_dev_data.py`. **Development only** — the script refuses
to run unless `ENV` is a development value *and* `DATABASE_URL` points at
localhost.

| Email | Password | Role |
|---|---|---|
| `admin@weft.local` | `DevPassword123!` | ADMIN |
| `authority@weft.local` | `DevPassword123!` | AUTHORITY |
| `citizen@weft.local` | `DevPassword123!` | CITIZEN |

`python -m scripts.seed_dev_data --drop` removes exactly what it created.

The authority account is posted to the Public Works department and covers both
demo zones, which are rectangles over central Bengaluru. A report submitted
near `12.9716, 77.5946` lands in Central Zone; one far away gets no zone, so
both the assigned and unassigned paths are easy to exercise.

## Tests

```bash
pytest                                            # whole suite
pytest --cov=app --cov-report=term-missing        # with coverage
ruff check . && black --check .                   # lint + format gates
```

Integration tests need the database up. They **skip** rather than fail when it
is unreachable — so a green run with skips is not the same as a green run.
Check the skip count: if `tests/integration/` is skipping, the auth and issue
endpoints are not actually being tested.

## Things that will bite you

**Any route returning 204 must pass `response_model=None`.** Every module uses
`from __future__ import annotations`, so FastAPI resolves a `-> None` return
annotation to `NoneType` — a truthy class — and asserts at *import time* that a
204 response cannot have a body. Without it the entire app fails to import, not
just that route. This has bitten the project twice; see D-6 in `../DECISIONS.md`.

**Install the pinned dependency versions.** `requirements.txt`,
`requirements-dev.txt` and `.pre-commit-config.yaml` are pinned to a verified
working set, and `ruff`/`black` must stay identical across all three. When they
drift, local and CI reformat each other's work indefinitely and every PR carries
spurious noise (D-5).

**`issues.location` is a generated column.** Write `latitude`/`longitude`; the
PostGIS point is derived by the database and can never drift from them.

**`issues.upvote_count` is maintained by a trigger** (`trg_upvote_count`,
migration 012), never by application code. Insert or delete `upvotes` rows and
let the trigger own the counter — it is correct under concurrent votes in a way
that a read-modify-write in Python is not.

**Department routing must be ordered explicitly.** `department_categories` has
a composite primary key `(department_id, category)`, so the schema permits one
category mapped to several departments. The seed maps each exactly once, but
nothing enforces it — an unordered lookup could route identical submissions to
different departments once an operator adds a second mapping.

**Uploaded images are not moderated.** `moderate_image()` in
`app/services/image_service.py` is a deliberate no-op marking where AWS
Rekognition would go. EXIF *is* stripped, which is a privacy requirement rather
than an optimisation: phone photos carry GPS coordinates, and a citizen who
pins a report coarsely would otherwise leak their exact position.

## Layout

```
app/
  core/         security, permissions, rate limiting, captcha, storage
  models/       SQLAlchemy ORM — read the docstrings before writing queries
  routers/      HTTP layer, thin; logic belongs in services/
  schemas/      Pydantic request/response models
  services/     business logic
  migrations/   Alembic revisions
scripts/        dev-only utilities (never run against production)
tests/          unit/ needs no database; integration/ does
```

`openapi.yaml` is the frozen contract. `tests/unit/test_contract_drift.py` pins
the Pydantic models against it, so a schema change that is not reflected in the
spec fails the build on purpose.

## Project docs

`../PROGRESS.md` is the current state of the build; `../DECISIONS.md` records
why things are the way they are, and is worth reading before changing anything
that looks odd — several of the odd-looking parts are deliberate.
