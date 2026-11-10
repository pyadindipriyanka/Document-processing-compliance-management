# Document Processing & Compliance Management Platform

Secure REST API (FastAPI · PostgreSQL · SQLAlchemy 2 · Alembic · Pydantic v2) for uploading, versioning, reviewing,
approving, expiring and archiving business documents, with multi-stage approval workflows, retention policies,
background jobs, notifications and an append-only audit trail.

> **Honest status note:** the code was written without access to a package index in the authoring environment, so
> the test-suite has **not been executed yet**. Run `pytest` first (instructions below) and fix any small
> issues it surfaces; the PostgreSQL-only concurrency tests need `TEST_DATABASE_URL`.

## Quick start (Docker)
```bash
cp .env.example .env            # edit SECRET_KEY, SUPER_ADMIN_EMAIL
docker compose up --build       # runs `alembic upgrade head`, then uvicorn on :8000
# Swagger UI: http://localhost:8000/docs   ReDoc: /redoc   OpenAPI: /openapi.json
```

## Quick start (local)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
createdb docplatform                       # or: docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=docplatform postgres:16
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

### Database setup
`alembic upgrade head` creates all 20 tables, constraints and indexes, a GIN full-text index, and PostgreSQL triggers
that make `audit_logs`, `document_versions` and `approval_history` append-only even for direct SQL.
Revision `0001` builds the schema from the model metadata; every later change should be created with
`alembic revision --autogenerate -m "..."`.

### File storage setup
Bytes are stored on disk under `STORAGE_DIR` (`<org_id>/<xx>/<uuid>.<ext>`); only metadata is in PostgreSQL.
Mount a persistent volume there (docker-compose does). `app/storage/local.py` exposes four methods
(`save_upload`, `path_for`, `delete`); an S3/MinIO backend can replace it without touching services.

### Running the tests
```bash
pytest                                              # SQLite, fast, everything except PostgreSQL-only concurrency tests
TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/docplatform_test pytest   # incl. concurrency tests
```

### First steps with the API
1. `POST /api/auth/register` (register with `SUPER_ADMIN_EMAIL` to become Super Admin), then `POST /api/auth/login`.
2. `POST /api/organizations` – the creator becomes `org_admin`. Add people with `POST /api/organizations/{id}/members`.
3. Admin creates a category (with metadata definitions), a workflow (`docs/workflow-examples.md`) and activates it.
4. Employee: `POST /api/documents` → `POST /api/documents/{id}/versions` (multipart) → `POST /api/documents/{id}/submit`.
5. Reviewers: `GET /api/reviews/pending` → approve / reject / request-changes.
A Postman collection is in `postman/`. Users in several organizations send `X-Organization-ID`.

## Architecture
```
Routes (app/api/routes)              HTTP, validation, response models
   ↓
Dependencies & authorization (app/api/deps.py, services/access.py)   JWT → user → org/role context
   ↓
Services (app/services)              workflow, versions, files, retention, jobs, audit rules
   ↓
Repositories (app/repositories)      all SQL, incl. the single document-visibility predicate
   ↓
SQLAlchemy models (app/models)  →  PostgreSQL   (+ local/object storage for file bytes)
```
```mermaid
flowchart TD
  C[Client] --> R[FastAPI routes]
  R --> D[Auth + org/role context]
  D --> S[Services]
  S --> P[Repositories]
  P --> DB[(PostgreSQL)]
  S --> ST[(File storage)]
  S --> J[(background_jobs)]
  W[BackgroundTasks drain / scheduler loop] --> J
  W --> S
```

## ER diagram
```mermaid
erDiagram
  users ||--o{ organization_members : "has"
  organizations ||--o{ organization_members : "has"
  organizations ||--o{ document_categories : "owns"
  document_categories ||--o{ metadata_definitions : "configures"
  document_categories ||--o{ documents : "classifies"
  documents ||--|| document_metadata : "values"
  documents ||--o{ document_files : "stores"
  documents ||--o{ document_versions : "history"
  document_files ||--o{ document_versions : "content of"
  documents ||--o{ document_reviews : "assigned"
  document_versions ||--o{ approval_history : "decided on (document_id, version_number)"
  document_reviews ||--o{ approval_history : "recorded as"
  workflows ||--o{ workflow_stages : "has"
  workflow_stages ||--o{ workflow_assignments : "assigns"
  documents }o--o| workflows : "frozen snapshot of"
  document_categories ||--o| retention_policies : "governed by"
  users ||--o{ notifications : "receives"
  users ||--o{ refresh_tokens : "sessions"
  users ||--o{ audit_logs : "acts"
  documents ||--o{ document_access : "grants"
```
**Versions ↔ approvals.** `document_versions` is immutable and unique on `(document_id, version_number)`.
`approval_history` has a *composite foreign key* to that pair, so every decision (SUBMITTED, APPROVED, REJECTED,
CHANGES_REQUESTED, REASSIGNED, STAGE_COMPLETED, FINAL_APPROVED, INVALIDATED, RENEWED) is permanently bound to the
exact version that was reviewed, together with a JSON snapshot of the metadata the reviewers saw. A *material* new
version never rewrites old rows: it appends `INVALIDATED`, cancels pending reviews and returns the document to
`DRAFT`, so re-approval is required while the earlier approval trail stays intact. Non-material versions
(`is_material=false`) keep the approval. Restoring an old version creates a **new** version (same file, same checksum,
`restored_from_version_id` set) and is treated as material.

> The `roles` table from the suggested list is intentionally an enum: roles are fixed by the assignment and are
> stored on `organization_members.role` (with a CHECK constraint). `Super Admin` is a platform flag on `users`.

## Authorization model
| Role | Summary |
|---|---|
| Super Admin | platform-wide; acts inside any organization via `X-Organization-ID`; has org-admin powers there |
| Organization Admin | configures categories, workflows, retention, members; sees everything in the org; audit log |
| Document Manager | creates/edits/archives any document, renews, reassigns reviews; no private docs unless granted |
| Reviewer | read-only on org documents; decides **only** reviews assigned to them |
| Auditor | read-only (documents, history, audit log); can never modify or decide |
| Employee | creates documents and manages their own; reads ORGANIZATION-level non-draft documents |

* Organization isolation: every query is scoped by `organization_id`; foreign ids from other orgs return 404.
* Access levels: `ORGANIZATION` (all members), `RESTRICTED` (owner, admins, managers, auditors, explicit grants,
  assigned reviewers), `PRIVATE` (owner, admins, explicit grants, assigned reviewers). Others' drafts are hidden from
  employees/reviewers. One SQL predicate (`visible_clause`) powers detail, list, search, and downloads.
* Downloads: always re-check document visibility, are audited, and are sent as attachments with `nosniff`.
* Only the assigned reviewer can approve/reject/request changes (even admins cannot); admins/managers can reassign.
* Document owners are never reviewers of their own document.

## Key behaviours
* **Status machine** `DRAFT → SUBMITTED → UNDER_REVIEW → APPROVED | REJECTED | DRAFT(changes requested)`;
  `APPROVED → EXPIRED | ARCHIVED`; `ARCHIVED → previous status`; invalid moves → `409 INVALID_WORKFLOW_TRANSITION`.
* **Expired / archived documents**: archived documents must be restored before submission; expired ones need
  `POST /submit {"allow_expired": true}` by a manager/admin (or `POST /renew`). Expired documents stay readable;
  if the category policy has `auto_archive_expired`, the retention job moves them to ARCHIVED.
* **Retention**: per-category `retention_days` + `prohibit_deletion`. While the period (counted from creation) runs,
  deleting a document or its attachments → `409 DOCUMENT_RETENTION_ACTIVE`. Deletes are soft; versions, files,
  reviews and audit rows are never destroyed. Policy changes are audited with before/after values.
* **Files**: size limit (`MAX_UPLOAD_SIZE_MB`), content sniffed from bytes (PDF, PNG, JPEG, TXT, DOCX, XLSX; zip-bomb
  guard), client filename/MIME ignored, random storage names, resolved-path traversal guard, atomic temp→final move,
  stored file removed if the DB write fails.
* **Concurrency**: document rows are locked (`SELECT … FOR UPDATE`) for version numbering and review decisions;
  unique constraint `(document_id, version_number)` is the backstop (`409 VERSION_CONFLICT`); decisions are re-read
  under the lock → exactly one wins, others get `REVIEW_ALREADY_COMPLETED`.
* **Auth**: bcrypt, 15-min access JWT, 7-day rotating refresh tokens stored server-side; reuse of a rotated token
  revokes the entire session family; logout/change-password revoke sessions; inactive users cannot log in or use tokens.
* **Audit**: login/failed login, document/file/version/metadata/workflow/review/retention/admin actions with actor,
  entity, correlation id (`X-Correlation-ID`) and redacted metadata. ORM listeners + PostgreSQL triggers reject
  UPDATE/DELETE; there is no write endpoint. Readable by org admins and auditors via `GET /api/audit-logs`.
* **Background jobs** (`background_jobs` table): checksum verification, metadata extraction (PDF suggestions via
  `pypdf`, never auto-applied), search indexing, expiration scan, approval reminders, reviewer escalation, retention
  check, notification e-mail delivery. Status tracking, exponential-backoff retries, permanent-failure handling,
  duplicate prevention (partial unique index on `dedupe_key` for PENDING/RUNNING), `SKIP LOCKED` claiming.
  Jobs are drained after requests via `BackgroundTasks`, by the optional scheduler loop
  (`SCHEDULER_ENABLED=true`), or on demand with `POST /api/jobs/run-scheduled`. Workflow transitions are committed
  synchronously and never depend on a worker. (Celery can call `jobs.run_job(db, id)` instead.)
* **Search**: `ILIKE` over a denormalised `search_text` (title, description, number, category, metadata values) plus
  PostgreSQL full-text (`to_tsvector`) with a GIN index; filters, sorting, pagination, date and expiration ranges.
  Results always respect the visibility predicate.
* **Errors**: `{"success": false, "error": {"code", "message", "details?"}}` for 400/401/403/404/409/422/500.
  Successful responses return the resource (or `{items,total,page,page_size,pages}` for lists).

## Project layout
```
app/
  api/{deps.py, routes/*}     routers + auth/context dependencies
  core/                       config, enums, errors, security (JWT/bcrypt), types
  db/                         engine, session, declarative base
  models/models.py            all tables
  repositories/repos.py       SQL
  schemas/schemas.py          Pydantic models
  services/                   business logic (documents, files, versions, reviews, workflows, retention, jobs, ...)
  storage/local.py            file storage + content sniffing
alembic/                      migrations
tests/                        pytest + HTTPX TestClient
postman/                      collection
docs/workflow-examples.md
```

## Known limitations / next steps
* Test-suite not yet executed in the authoring environment (see note at top).
* Refresh-token and auth rate limiting, account lockout and e-mail verification are not implemented.
* `workflow_assignments` roles are resolved at submission; role changes later don't alter in-flight reviews
  (use reassign).
* Redis caching, Celery, S3/MinIO, Prometheus and CI are optional extras left out in favour of correctness.
