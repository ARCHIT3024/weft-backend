# Weft — Branching Strategy

## Branch Model

| Branch | Purpose | Merges From | Merges To | CI |
|--------|---------|-------------|-----------|-----|
| `main` | Production releases only | `develop` (via approved PR) | — | Full pipeline + deploy prod |
| `develop` | Integration branch | `feature/*`, `hotfix/*` | `main` | Lint → Test → Build |
| `feature/{task-id}-{name}` | Individual task work | — | `develop` | Lint → Test |
| `hotfix/{description}` | Critical production fixes | — | `main` + `develop` | Full pipeline |

## Rules

1. **Never commit directly to `main` or `develop`.** All changes via Pull Request.
2. One feature branch per task (e.g., `feature/1.3-jwt-login`).
3. PRs require passing CI + at least 1 approval.
4. Squash-merge to `develop`; merge commit to `main`.
5. Delete feature branches after merge.

## Repositories

| Repo | Description |
|------|-------------|
| `weft-backend` | Python FastAPI backend |
| `weft-mobile` | Flutter mobile app (Android + iOS) |
| `weft-web` | React.js authority web dashboard |

## GitHub Projects Board

Use GitHub Projects v2 with the following columns:
- **Backlog** → **Sprint Ready** → **In Progress** → **In Review** → **Done**

Sprint milestones match phases:
- Sprint 1: Phase 0 (Week 1)
- Sprint 2–3: Phase 1 (Weeks 2–3)
- Sprint 4–6: Phase 2 (Weeks 4–6)
- Sprint 7–8: Phase 3 (Weeks 7–8)
- Sprint 9–10: Phase 4 (Weeks 9–10)
- Sprint 11–12: Phase 5 (Weeks 11–12)
