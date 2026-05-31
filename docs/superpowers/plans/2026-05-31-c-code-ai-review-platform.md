# C Language AI Code Review Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Docker Compose deployable C language AI code review platform with enterprise accounts, asynchronous single-model VLLM reviews, structured reports, administration, and a development-only mock model.

**Architecture:** Use a Vue 3 single-page application behind Nginx and a modular FastAPI backend backed by MySQL, Redis, and Celery. Keep VLLM inference nodes independent and call their OpenAI-compatible endpoints through a model router. Use a strict production failure policy: failed model calls fail the task and never create a report.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic Settings, PyJWT, pwdlib, Celery, Redis, MySQL 8, httpx, ReportLab, pytest, Vue 3, Vite, TypeScript, Element Plus, Pinia, Vue Router, Axios, ECharts, Docker Compose, Nginx

---

## File Structure

```text
.
|-- .env.example                         # Runtime configuration template
|-- .gitignore                           # Local, secret, and generated files
|-- docker-compose.yml                   # Application stack
|-- start.sh                             # Linux operations wrapper
|-- backend/
|   |-- Dockerfile
|   |-- pyproject.toml
|   |-- alembic.ini
|   |-- alembic/
|   |   |-- env.py
|   |   `-- versions/0001_initial.py
|   |-- app/
|   |   |-- main.py                      # FastAPI application
|   |   |-- worker.py                    # Celery application
|   |   |-- api/                         # HTTP routers and dependencies
|   |   |-- core/                        # Settings, security, lifecycle
|   |   |-- db/                          # ORM models and session
|   |   |-- schemas/                     # API and model-response schemas
|   |   |-- services/                    # Submission, routing, report logic
|   |   |-- tasks/reviews.py             # Celery review task
|   |   `-- prompts/default_c_review.md  # Built-in prompt seed
|   `-- tests/                           # Backend unit and integration tests
|-- frontend/
|   |-- Dockerfile
|   |-- nginx.conf
|   |-- package.json
|   |-- vite.config.ts
|   `-- src/
|       |-- api/                          # Axios client and typed endpoints
|       |-- assets/theme.css              # iOS-inspired visual system
|       |-- components/                   # Shared status and report widgets
|       |-- layouts/AppLayout.vue
|       |-- router/index.ts
|       |-- stores/auth.ts
|       |-- types/index.ts
|       |-- views/                        # Login, workspace, report, history,
|       |                                 # profile, and admin pages
|       |-- App.vue
|       `-- main.ts
|-- deploy/
|   |-- nginx/default.conf                # Unified reverse proxy
|   `-- vllm/
|       |-- .env.example                  # One-node VLLM settings
|       `-- start-vllm.sh                 # Independent GPU-node example
`-- docs/
    `-- deployment-linux.md               # Linux deployment and operations
```

## Task 1: Bootstrap Backend Configuration And Database

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `backend/pyproject.toml`
- Create: `backend/app/core/config.py`
- Create: `backend/app/db/session.py`
- Create: `backend/app/db/base.py`
- Create: `backend/app/db/models.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_config.py`

- [ ] **Step 1: Write the failing configuration test**

```python
def test_settings_enable_mock_model_only_when_explicit(monkeypatch):
    monkeypatch.setenv("MOCK_MODEL_ENABLED", "true")
    from app.core.config import Settings
    assert Settings().mock_model_enabled is True
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `cd backend && pytest tests/test_config.py -v`

Expected: FAIL because `app.core.config` does not exist.

- [ ] **Step 3: Create the backend package, settings, and ORM models**

Implement `Settings` with database URL, Redis URL, JWT secret and expiry,
administrator seed credentials, upload limits, CORS origins, storage path, and
`mock_model_enabled: bool = False`. Create SQLAlchemy models for `User`,
`ModelNode`, `PromptVersion`, `ReviewTask`, `ReviewFile`, and `Report` using the
states and fields defined in the approved design.

- [ ] **Step 4: Add SQLite test fixtures while keeping MySQL production defaults**

Use a temporary SQLite database in `backend/tests/conftest.py`, create tables
for each test session, and override the SQLAlchemy dependency through a test
session factory. Production `.env.example` must use MySQL.

- [ ] **Step 5: Run the configuration test**

Run: `cd backend && pytest tests/test_config.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .gitignore .env.example backend
git commit -m "feat: bootstrap backend configuration and database models"
```

## Task 2: Add Authentication And Administrator Seed

**Files:**
- Create: `backend/app/core/security.py`
- Create: `backend/app/core/bootstrap.py`
- Create: `backend/app/api/deps.py`
- Create: `backend/app/api/auth.py`
- Create: `backend/app/schemas/auth.py`
- Create: `backend/app/main.py`
- Create: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing authentication tests**

```python
def test_login_returns_token(client, admin_user):
    response = client.post("/api/auth/login", json={
        "username": "admin", "password": "Admin123!"
    })
    assert response.status_code == 200
    assert response.json()["access_token"]

def test_disabled_user_cannot_login(client, disabled_user):
    response = client.post("/api/auth/login", json={
        "username": "disabled", "password": "Password123!"
    })
    assert response.status_code == 403
```

- [ ] **Step 2: Run the authentication tests and verify they fail**

Run: `cd backend && pytest tests/test_auth.py -v`

Expected: FAIL because authentication routes do not exist.

- [ ] **Step 3: Implement JWT security and authentication routes**

Add password hashing, password verification, JWT creation, JWT parsing,
`get_current_user`, and `require_admin`. Add `/api/auth/login`,
`/api/auth/me`, and `/api/auth/password`. Return `401` for invalid credentials,
`403` for disabled users, and never expose password hashes.

- [ ] **Step 4: Implement startup administrator seeding**

Create the configured administrator only when no administrator exists. Invoke
the seed function during FastAPI startup. Keep credentials environment-driven.

- [ ] **Step 5: Run authentication tests**

Run: `cd backend && pytest tests/test_auth.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat: add authentication and administrator bootstrap"
```

## Task 3: Validate And Persist Review Submissions

**Files:**
- Create: `backend/app/schemas/reviews.py`
- Create: `backend/app/services/submissions.py`
- Create: `backend/app/api/reviews.py`
- Create: `backend/tests/test_submissions.py`

- [ ] **Step 1: Write failing source and ZIP validation tests**

```python
def test_zip_traversal_is_rejected():
    archive = make_zip({"../escape.c": "int main(void) { return 0; }"})
    with pytest.raises(SubmissionError, match="unsafe"):
        extract_zip_sources(archive)

def test_zip_without_c_sources_is_rejected():
    archive = make_zip({"README.md": "hello"})
    with pytest.raises(SubmissionError, match="no C source"):
        extract_zip_sources(archive)
```

- [ ] **Step 2: Run submission tests and verify they fail**

Run: `cd backend && pytest tests/test_submissions.py -v`

Expected: FAIL because submission services do not exist.

- [ ] **Step 3: Implement safe submission extraction**

Support text, `.c`, `.h`, and `.zip` input. Reject empty input, absolute ZIP
paths, `..` traversal, symbolic links, unsupported single-file extensions,
oversized individual files, excessive extracted bytes, and excessive valid
source-file counts. Normalize archive paths to forward-slash relative paths.

- [ ] **Step 4: Implement task creation routes**

Add `POST /api/reviews/text`, `POST /api/reviews/file`,
`POST /api/reviews/archive`, `GET /api/reviews`, `GET /api/reviews/{id}`, and
`DELETE /api/reviews/{id}`. Store accepted sources and create a `queued` task
owned by the authenticated user.

- [ ] **Step 5: Run submission tests**

Run: `cd backend && pytest tests/test_submissions.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat: add secure C source submission workflow"
```

## Task 4: Add Prompt Versions And Model Router

**Files:**
- Create: `backend/app/prompts/default_c_review.md`
- Create: `backend/app/schemas/model_response.py`
- Create: `backend/app/services/prompts.py`
- Create: `backend/app/services/model_router.py`
- Create: `backend/app/api/models.py`
- Create: `backend/tests/test_model_router.py`

- [ ] **Step 1: Write failing strict-failure router tests**

```python
@pytest.mark.asyncio
async def test_router_does_not_fallback_when_real_node_fails(node, respx_mock):
    respx_mock.post(f"{node.base_url}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("offline")
    )
    with pytest.raises(ModelInvocationError, match="unavailable"):
        await invoke_model(node=node, files=[source_file()], prompt="review")

def test_mock_router_requires_explicit_setting(monkeypatch):
    monkeypatch.setenv("MOCK_MODEL_ENABLED", "false")
    assert Settings().mock_model_enabled is False
```

- [ ] **Step 2: Run router tests and verify they fail**

Run: `cd backend && pytest tests/test_model_router.py -v`

Expected: FAIL because the router does not exist.

- [ ] **Step 3: Add the prompt seed and structured response schema**

The default prompt must request summary, score, and findings for memory safety,
logic, security, concurrency, performance, style, and portability. Define
Pydantic response models with `high`, `medium`, `low`, and `suggestion`
severity values and required remediation fields.

- [ ] **Step 4: Implement VLLM invocation and health checks**

Call `{base_url}/v1/chat/completions` with the configured API key and timeout.
Parse JSON content from the assistant response and validate it. Add
`GET /api/models` for enabled nodes and `POST /api/models/{id}/health` for
administrators. Use mock output only when explicitly enabled and a mock node is
selected.

- [ ] **Step 5: Run router tests**

Run: `cd backend && pytest tests/test_model_router.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat: add C review prompt and strict VLLM model router"
```

## Task 5: Execute Reviews With Celery And Persist Reports

**Files:**
- Create: `backend/app/worker.py`
- Create: `backend/app/tasks/reviews.py`
- Create: `backend/app/services/reports.py`
- Modify: `backend/app/api/reviews.py`
- Create: `backend/tests/test_review_worker.py`

- [ ] **Step 1: Write failing worker state-transition tests**

```python
def test_worker_persists_report_on_success(db_session, queued_task, mocker):
    mocker.patch("app.tasks.reviews.invoke_selected_model", return_value=result())
    run_review_task(str(queued_task.id))
    db_session.refresh(queued_task)
    assert queued_task.state == "completed"
    assert queued_task.report.finding_count == 1

def test_worker_marks_failure_without_report(db_session, queued_task, mocker):
    mocker.patch("app.tasks.reviews.invoke_selected_model",
                 side_effect=ModelInvocationError("offline"))
    run_review_task(str(queued_task.id))
    db_session.refresh(queued_task)
    assert queued_task.state == "failed"
    assert queued_task.report is None
```

- [ ] **Step 2: Run worker tests and verify they fail**

Run: `cd backend && pytest tests/test_review_worker.py -v`

Expected: FAIL because the worker task does not exist.

- [ ] **Step 3: Implement Celery review execution**

Change task state to `running`, invoke the chosen model, validate output,
calculate severity and category counters, persist one report, and mark the task
`completed`. On any invocation or validation error, rollback partial report
data, mark the task `failed`, and save a concise diagnostic message.

- [ ] **Step 4: Enqueue submissions and expose status**

Dispatch the Celery task after transaction commit. Return task progress,
duration, finding count, and failure reason through the task detail endpoint.

- [ ] **Step 5: Run worker tests**

Run: `cd backend && pytest tests/test_review_worker.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat: process asynchronous reviews and persist reports"
```

## Task 6: Add Report Detail, Export, And History Filtering

**Files:**
- Create: `backend/app/api/reports.py`
- Modify: `backend/app/services/reports.py`
- Create: `backend/tests/test_reports.py`

- [ ] **Step 1: Write failing report authorization and export tests**

```python
def test_user_cannot_read_other_users_report(client, user_token, other_report):
    response = client.get(
        f"/api/reports/{other_report.id}",
        headers=auth(user_token),
    )
    assert response.status_code == 403

def test_markdown_export_contains_finding(client, user_token, own_report):
    response = client.get(
        f"/api/reports/{own_report.id}/markdown",
        headers=auth(user_token),
    )
    assert response.status_code == 200
    assert "Buffer overflow" in response.text
```

- [ ] **Step 2: Run report tests and verify they fail**

Run: `cd backend && pytest tests/test_reports.py -v`

Expected: FAIL because report routes do not exist.

- [ ] **Step 3: Implement report endpoints**

Add `GET /api/reports/{id}`, `GET /api/reports/{id}/markdown`, and
`GET /api/reports/{id}/pdf`. Render Markdown with deterministic headings and
findings. Render PDF with ReportLab using a bundled or system font fallback and
return a clear error if Chinese glyph support is unavailable.

- [ ] **Step 4: Complete history filtering**

Support keyword, state, model node, severity, start time, end time, offset, and
limit query parameters. Ordinary users see only their own tasks; administrators
receive a separate platform-wide endpoint.

- [ ] **Step 5: Run report tests**

Run: `cd backend && pytest tests/test_reports.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat: add report exports and review history filters"
```

## Task 7: Add Administrative APIs And Migrations

**Files:**
- Create: `backend/app/api/admin.py`
- Create: `backend/app/schemas/admin.py`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_initial.py`
- Create: `backend/tests/test_admin.py`

- [ ] **Step 1: Write failing admin permission tests**

```python
def test_regular_user_cannot_create_account(client, user_token):
    response = client.post("/api/admin/users", headers=auth(user_token), json={
        "username": "new-user", "password": "Password123!", "role": "user"
    })
    assert response.status_code == 403

def test_admin_can_create_model_node(client, admin_token):
    response = client.post("/api/admin/models", headers=auth(admin_token), json={
        "name": "Qwen Coder", "model_id": "qwen-coder",
        "base_url": "http://model-a:8000", "enabled": True
    })
    assert response.status_code == 201
```

- [ ] **Step 2: Run admin tests and verify they fail**

Run: `cd backend && pytest tests/test_admin.py -v`

Expected: FAIL because admin routes do not exist.

- [ ] **Step 3: Implement administrative endpoints**

Add dashboard metrics, user list/create/disable/password-reset, model
list/create/update/enable-disable, prompt list/create/activate, and
platform-task list endpoints. Require `admin` for every route.

- [ ] **Step 4: Add initial Alembic migration**

Create all six tables, foreign keys, unique constraints, task-state indexes,
owner indexes, and timestamp indexes. Make API container startup run
`alembic upgrade head` before Uvicorn.

- [ ] **Step 5: Run admin tests and migration smoke check**

Run: `cd backend && pytest tests/test_admin.py -v && alembic upgrade head`

Expected: PASS and migration completes successfully against the configured
test database.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat: add administration APIs and database migration"
```

## Task 8: Build Frontend Shell, Authentication, And Workspace

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/assets/theme.css`
- Create: `frontend/src/types/index.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/index.ts`
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/layouts/AppLayout.vue`
- Create: `frontend/src/views/LoginView.vue`
- Create: `frontend/src/views/WorkspaceView.vue`

- [ ] **Step 1: Scaffold frontend dependencies and type checking**

Use Vue 3, Vite, TypeScript, Element Plus, Pinia, Vue Router, Axios, and
ECharts. Add scripts for `dev`, `build`, and `typecheck`.

- [ ] **Step 2: Implement the visual system**

Create CSS tokens for slate text, muted blue primary actions, translucent
cards, rounded surfaces, readable code blocks, severity colors, responsive
breakpoints, and reduced-motion behavior.

- [ ] **Step 3: Implement login and guarded application layout**

Store the token and user identity in Pinia, inject bearer tokens through the
Axios client, clear expired sessions on `401`, and guard authenticated and
administrator routes.

- [ ] **Step 4: Implement the workspace**

Add three input tabs, model selection, validation messages, upload controls,
submission actions, recent task polling, progress state, and visible failure
details. Keep mock-node labeling explicit.

- [ ] **Step 5: Run frontend checks**

Run: `cd frontend && npm install && npm run typecheck && npm run build`

Expected: type checking and production build complete successfully.

- [ ] **Step 6: Commit**

```bash
git add frontend
git commit -m "feat: add authenticated frontend review workspace"
```

## Task 9: Build Report, History, Profile, And Admin Views

**Files:**
- Create: `frontend/src/components/SeverityBadge.vue`
- Create: `frontend/src/components/StatusBadge.vue`
- Create: `frontend/src/components/ReportChart.vue`
- Create: `frontend/src/views/ReportView.vue`
- Create: `frontend/src/views/HistoryView.vue`
- Create: `frontend/src/views/ProfileView.vue`
- Create: `frontend/src/views/AdminView.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/api/index.ts`

- [ ] **Step 1: Implement shared report components**

Create consistent severity and task-state badges. Build an ECharts category
distribution widget that handles empty data and resizes with its container.

- [ ] **Step 2: Implement report detail and downloads**

Display summary, score, counters, metadata, category chart, and severity-sorted
findings. Add Markdown and PDF download buttons using authenticated API calls.

- [ ] **Step 3: Implement history and profile**

Add paginated history filters, detail navigation, delete confirmation, and
password update feedback.

- [ ] **Step 4: Implement administrator workspace**

Add overview cards and tabs for users, model nodes, prompts, and platform
tasks. Provide create/edit dialogs and explicit model health checks.

- [ ] **Step 5: Run frontend checks**

Run: `cd frontend && npm run typecheck && npm run build`

Expected: type checking and production build complete successfully.

- [ ] **Step 6: Commit**

```bash
git add frontend
git commit -m "feat: add reports history profile and administration views"
```

## Task 10: Add Docker Compose, Nginx, And Linux Operations

**Files:**
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Create: `deploy/nginx/default.conf`
- Create: `docker-compose.yml`
- Create: `start.sh`
- Create: `deploy/vllm/.env.example`
- Create: `deploy/vllm/start-vllm.sh`
- Create: `docs/deployment-linux.md`

- [ ] **Step 1: Containerize backend and frontend**

Build a Python backend image with application dependencies and a frontend
image that compiles Vue assets. Use an Nginx runtime for static assets.

- [ ] **Step 2: Compose the application stack**

Define `mysql`, `redis`, `api`, `worker`, `frontend`, and `nginx` services with
health checks, persistent MySQL and upload volumes, environment files, restart
policies, and startup ordering.

- [ ] **Step 3: Add the operations wrapper**

Implement:

```bash
bash start.sh install
bash start.sh start
bash start.sh stop
bash start.sh status
bash start.sh logs
```

`install` must validate Docker Compose availability and create `.env` from
`.env.example` only when `.env` is absent.

- [ ] **Step 4: Add the independent VLLM example**

Document and script one GPU-node launch using `vllm serve`, model path,
served-model name, API key, tensor parallel size, GPU memory utilization,
quantization option, host, and port. Do not automate remote SSH or model
downloads.

- [ ] **Step 5: Write Linux deployment documentation**

Cover prerequisites, environment configuration, application startup, first
administrator login, model registration, mock development mode, strict failure
behavior, VLLM node startup, health checks, logs, backups, and upgrades.

- [ ] **Step 6: Validate Compose rendering**

Run: `docker compose config`

Expected: configuration renders without interpolation or schema errors.

- [ ] **Step 7: Commit**

```bash
git add backend frontend deploy docker-compose.yml start.sh docs
git commit -m "feat: add Docker Compose deployment and Linux operations"
```

## Task 11: Complete Automated Verification And Browser Acceptance

**Files:**
- Modify: `backend/tests/` as needed
- Modify: `frontend/src/` as needed
- Create: `docs/verification.md`

- [ ] **Step 1: Run the complete backend suite**

Run: `cd backend && pytest -v`

Expected: all authentication, permission, ZIP-safety, routing, worker,
history, export, and admin tests pass.

- [ ] **Step 2: Run frontend production checks**

Run: `cd frontend && npm run typecheck && npm run build`

Expected: both commands pass.

- [ ] **Step 3: Start the stack in explicit development mode**

Set `MOCK_MODEL_ENABLED=true`, start the Compose stack, log in as the seeded
administrator, create a normal user and a mock model node, then submit pasted
code, a `.c` file, and a ZIP archive.

Run: `docker compose up -d --build`

Expected: all application containers become healthy.

- [ ] **Step 4: Verify the browser workflow**

Open the local Nginx URL and confirm login, workspace input tabs, model
selection, task polling, completed report charts, finding details, Markdown
download, PDF download, history filters, profile password update, and admin
navigation.

- [ ] **Step 5: Verify strict production failure**

Disable mock mode, register an unreachable model node, submit a review, and
confirm that it reaches `failed`, displays a useful diagnostic, and creates no
report.

- [ ] **Step 6: Record verification evidence**

Write `docs/verification.md` with commands run, pass results, local limitations,
and any Linux/GPU validation that remains for the target server.

- [ ] **Step 7: Commit**

```bash
git add backend frontend docs
git commit -m "test: verify end-to-end C review platform workflow"
```

## Task 12: Final Review

**Files:**
- Review: all created files

- [ ] **Step 1: Run repository status and secret scan**

Run: `git status --short && rg -n "(password|secret|api[_-]?key)\\s*[:=]\\s*['\\\"][^$<{]" -g '!*.lock' -g '!.env.example'`

Expected: no committed runtime secrets and no accidental generated files.

- [ ] **Step 2: Run final verification commands**

Run:

```bash
cd backend && pytest -v
cd ../frontend && npm run typecheck && npm run build
cd .. && docker compose config
```

Expected: all commands pass.

- [ ] **Step 3: Review documentation against acceptance criteria**

Confirm the Linux guide covers application deployment, first login, model-node
registration, VLLM startup, strict failure semantics, mock development mode,
health checks, logs, backups, and upgrades.

- [ ] **Step 4: Commit final fixes if any**

```bash
git add .
git commit -m "chore: finalize C code review platform"
```

