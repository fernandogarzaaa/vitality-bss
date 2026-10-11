# Vitality BSS

[![ci](https://github.com/fernandogarzaaa/vitality-bss/actions/workflows/ci.yml/badge.svg)](https://github.com/fernandogarzaaa/vitality-bss/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/fernandogarzaaa/vitality-bss)](https://github.com/fernandogarzaaa/vitality-bss/releases)

AI-powered healthcare document processing: classify, extract, dedupe, and auto-generate
patient billing binders.

Upload PDFs (digital or scanned) and Vitality BSS will:

1. **Read** them: text layer first, local OCR (tesseract) for scanned pages.
2. **Classify** each one: EOB, Clinical Note, Lab Report, Invoice, Insurance Form (or *Unclassified*).
3. **Extract** patient name, patient ID / MRN, claim number, service date, billed amount and insurer,
   with optional LLM help that is verified against the document text.
4. **Deduplicate**: identical files by SHA-256, re-scans and re-exports by text similarity.
5. **Route for review**: anything incomplete, low-confidence or possibly duplicated is flagged with reasons.
6. **Build billing binders** per patient: one merged PDF with a cover page, totals, table of contents and bookmarks.

Everything is behind authentication with roles, audited, and optionally encrypted at rest.

> **Healthcare data.** The repository and test suite use **synthetic documents only**. Read
> [SECURITY.md](SECURITY.md) before processing real PHI: HIPAA compliance also requires controls
> only you, the deployer, can put in place (BAAs, TLS, backups, policies).

## Quickstart

### Docker (recommended)

```bash
cp .env.example .env
# set at least: SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD, POSTGRES_PASSWORD (and ENCRYPTION_KEYS)
docker compose up -d --build
open http://localhost:8001
```

Generate secrets with `pip install . && vitality-bss generate-secrets`, or
`python -c "import secrets;print(secrets.token_urlsafe(48))"` for `SECRET_KEY`.

### Local (SQLite)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install -e .
# optional, for scanned PDFs: apt install tesseract-ocr poppler-utils  (macOS: brew install tesseract poppler)
export ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD='Choose-A-Str0ng-Passw0rd'
export SEED_DEMO_DATA=true          # optional synthetic demo documents
vitality-bss serve --port 8001
```

**GitHub Codespaces demo:** `Code → Codespaces → Create codespace` installs everything, seeds the
synthetic demo data and starts the app on port 8001 (sign in as `admin@example.com` /
`Demo-Passw0rd-2026`; development mode only, never reuse that password).

Open <http://localhost:8001> and sign in with the admin you configured. API docs are at
<http://localhost:8001/api/docs>.

### CLI

| Command | What it does |
|---------|--------------|
| `vitality-bss serve [--host --port --workers]` | run the web server (migrations run on startup) |
| `vitality-bss migrate` | apply database migrations |
| `vitality-bss create-user --email E --role admin\|member\|viewer [--password-stdin]` | add a user |
| `vitality-bss seed-demo` | load the synthetic demo documents |
| `vitality-bss process FILE.pdf ...` | analyse PDFs and print JSON, without storing anything |
| `vitality-bss generate-secrets` | print a new `SECRET_KEY` and `ENCRYPTION_KEYS` value |
| `vitality-bss rotate-keys` | re-encrypt stored files with the first key in `ENCRYPTION_KEYS` |
| `vitality-bss check-config` | validate configuration (fails on unsafe production settings) |

## Configuration

All settings are environment variables (a `.env` file is read too). The full list with comments is
in [`.env.example`](.env.example). The important ones:

| Variable | Default | Notes |
|----------|---------|-------|
| `APP_ENV` | `development` | `production` refuses insecure defaults and enables secure cookies/HSTS |
| `SECRET_KEY` | placeholder | ≥ 32 random characters; required in production |
| `DATABASE_URL` | `sqlite:///./data/app.db` | PostgreSQL: `postgresql+psycopg2://user:pass@host/db` (install `.[postgres]`) |
| `DATA_DIR` | `./data` | where uploaded documents are stored |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | empty | first admin, created only when there are no users |
| `ENCRYPTION_KEYS` | empty | comma-separated Fernet keys; first encrypts, all decrypt |
| `REQUIRE_ENCRYPTION` | `false` | refuse to start without encryption keys |
| `OCR_ENABLED` / `OCR_LANG` / `OCR_DPI` | `true` / `eng` / `300` | local tesseract OCR |
| `AI_MODE` | `off` | `off`, `mock` (offline deterministic) or `llm` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | empty / empty / `llama-3.1-8b-instant` | any OpenAI-compatible endpoint |
| `LLM_ALLOW_REMOTE_PHI` | `false` | must be `true` to send text to a non-local LLM |
| `NEAR_DUPLICATE_THRESHOLD` | `0.97` | cosine similarity for re-scan detection; `0` disables |
| `MAX_UPLOAD_MB` / `MAX_FILES_PER_REQUEST` / `MAX_PAGES_PER_DOCUMENT` | `25` / `20` / `500` | upload limits |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES` | `5` / `15` | lockout policy |
| `LOG_FORMAT` / `LOG_LEVEL` | `json` / `INFO` | logs go to stdout, PHI patterns redacted |

### LLM providers (all optional, free options included)

| Provider | `LLM_BASE_URL` | Notes |
|----------|----------------|-------|
| Ollama (local, free) | `http://localhost:11434/v1` | `LLM_MODEL=llama3.1`; no API key; PHI stays on the host |
| Groq (free tier) | `https://api.groq.com/openai/v1` | needs `LLM_API_KEY`, `LLM_ALLOW_REMOTE_PHI=true` |
| OpenRouter (free models) | `https://openrouter.ai/api/v1` | needs `LLM_API_KEY`, `LLM_ALLOW_REMOTE_PHI=true` |
| Any OpenAI-compatible server | `.../v1` | vLLM, llama.cpp, LM Studio, OpenAI |

The rules engine always runs first. The LLM only fills fields the rules left blank, and a value is
accepted only if it literally appears in the document (amounts: if the number appears). The LLM's
document type is used only when the rules are unsure. If the LLM is down, processing continues with
rules and the document is flagged `ai_unavailable`.

## Architecture

```
vitality_bss/
  config.py        settings from env, production safety checks
  pdftext.py       PDF validation, text layer, OCR fallback (pdftoppm + tesseract)
  classify.py      keyword/filename classifier with confidence
  extract.py       regex field extraction + normalisation
  ai.py            OpenAI-compatible client, mock client, verified merge
  dedupe.py        local text fingerprints for near-duplicate detection
  pipeline.py      upload pipeline (hash → dedupe → text → classify → extract → enrich → store)
  binders.py       per-patient grouping and binder PDF rendering
  storage.py       blob store with optional Fernet encryption and key rotation
  security.py      bcrypt, JWT, CSRF tokens, password policy
  services.py      auth/lockout, users, document corrections, stats
  audit.py         audit-event recording
  redact.py        PHI redaction used by logging and audit
  logging_setup.py JSON logging with request IDs
  db.py, models.py, migrations/   SQLAlchemy 2 models + Alembic
  web/             FastAPI app: api.py (JSON), pages.py (HTML), deps.py (auth/roles/CSRF), app.py
  cli.py           `vitality-bss` command
```

Request flow: the browser UI uses an `HttpOnly` session cookie plus CSRF tokens; API clients send
`Authorization: Bearer <token>` from `POST /api/auth/login`. Documents are processed synchronously on
upload; OCR of scanned pages is the slow step, so very large scanned batches need generous proxy timeouts.

### Main API endpoints

| Method | Path | Role |
|--------|------|------|
| `POST` | `/api/auth/login`, `/api/auth/logout`, `/api/auth/change-password`; `GET /api/auth/me` | any |
| `POST` | `/api/documents` (multipart `files`) | admin, member |
| `GET` | `/api/documents?patient_id=&doc_type=&status=&q=&limit=&offset=` | any |
| `GET` / `PATCH` / `DELETE` | `/api/documents/{id}` | any / admin, member / admin |
| `GET` | `/api/documents/{id}/file`; `POST /api/documents/{id}/reprocess` | any / admin, member |
| `GET` | `/api/binders`, `/api/binders/{patient_id}/pdf`; `POST /api/binders/generate` | any |
| `GET` | `/api/duplicates`, `/api/stats` | any |
| `GET` / `POST` / `PATCH` | `/api/users`, `/api/users/{id}` | admin |
| `GET` | `/api/audit?action=&limit=&offset=` | admin |
| `GET` | `/healthz` (liveness), `/readyz` (DB + schema) | public |

## Deploy

1. Provision a host with Docker, put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front,
   and keep port 8001 private (the compose file binds it to `127.0.0.1`).
2. `cp .env.example .env`, set `APP_ENV=production`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
   `POSTGRES_PASSWORD`, `ENCRYPTION_KEYS` (and `REQUIRE_ENCRYPTION=true`).
3. `docker compose up -d --build`, then check `curl -fsS http://127.0.0.1:8001/readyz`.
4. Back up the `pgdata` and `appdata` volumes **and** the encryption keys (files cannot be decrypted without them).
5. Behind a proxy, pass its address with `FORWARDED_ALLOW_IPS` so client IPs in the audit log are correct.

To rotate the encryption key: prepend a new key to `ENCRYPTION_KEYS` (keep the old one after it),
restart, run `vitality-bss rotate-keys`, then remove the old key.

Scaling: run several app containers against one PostgreSQL and a shared `DATA_DIR` volume. Uploads are
processed in the request.

## Development

```bash
pip install -r requirements.lock && pip install -e ".[dev,postgres]"
ruff check . && ruff format --check .
mypy vitality_bss
pytest --cov=vitality_bss
testteeth run   # mutation testing of the core modules listed in pyproject.toml
```

Tests use synthetic PDFs generated on the fly; the OCR test runs when tesseract and poppler are
installed, the PostgreSQL test when `VBSS_TEST_DATABASE_URL` is set (both run in CI).

Releases: bump `vitality_bss/__init__.py` and `pyproject.toml`, add a CHANGELOG entry, tag `vX.Y.Z`;
the release workflow tests, builds the wheel and sdist, and publishes a GitHub release.

## License

[MIT](LICENSE)
