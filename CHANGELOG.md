# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Accessibility: the PDF upload input and the per-user role select, active checkbox and
  Save button on the Users page have accessible names.

## [1.0.0] - 2026-10-11

First production release. The beta's `app/` package was rewritten as the installable
`vitality_bss` package.

### Added

- **OCR for scanned PDFs**: pages without a usable text layer are OCR'd locally with
  tesseract + poppler (nothing leaves the host). Missing OCR tools are reported, not silently ignored.
- **Optional LLM enrichment** through any OpenAI-compatible API (Ollama, Groq, OpenRouter, OpenAI, vLLM...)
  with retries, timeouts and a fallback for servers without JSON mode. LLM output is only accepted when it
  can be verified against the document text. `AI_MODE=mock` gives a deterministic offline mode.
  Remote LLMs require an explicit `LLM_ALLOW_REMOTE_PHI=true`.
- **Near-duplicate detection** (re-scans / re-exports) using a local text fingerprint, scoped to the
  same patient and document type; flagged documents stay out of binders until reviewed.
- **Review workflow**: documents missing key fields, unclassified, low-confidence or possibly
  duplicated get `needs_review` with explicit reasons; reviewers correct fields or mark reviewed.
- **Billing binders** now have a cover page with patient details, invoiced total, a table of contents
  with page numbers, PDF bookmarks per document and a confidentiality notice.
- **Users and roles** (`admin`, `member`, `viewer`) with admin UI and API, password policy,
  account lockout, password change, session revocation.
- **Audit log** of auth events and every PHI read/write, with UI (`/admin/audit`) and API (`/api/audit`).
- **Encryption at rest** for stored documents (Fernet, multi-key rotation, `rotate-keys` command).
- **PHI-safe structured JSON logging** with redaction and request IDs.
- **Alembic migrations** (run automatically on startup or via `vitality-bss migrate`); PostgreSQL support.
- **CLI** `vitality-bss`: `serve`, `migrate`, `create-user`, `seed-demo`, `process`,
  `generate-secrets`, `rotate-keys`, `check-config`.
- JSON API: pagination and filters on `/api/documents`, `PATCH`/`DELETE` documents, file download,
  reprocess, `/api/duplicates`, `/api/stats`, `/api/users`, `/api/binders/{patient_id}/pdf`,
  OpenAPI docs at `/api/docs`.
- Health endpoints `/healthz` (liveness) and `/readyz` (database + schema).
- Security headers (strict CSP, frame denial, no-store), CSRF protection, request size limits.
- `pyproject.toml` packaging with pinned dependencies and a `requirements.lock`.
- Non-root multi-stage Docker image with OCR tools and a health check; production docker-compose
  with PostgreSQL; CI (lint, format, types, tests on 3.11/3.12, PostgreSQL job, Docker smoke test) and a
  tag-triggered release workflow.
- Test suite (unit + integration, including real OCR and PostgreSQL in CI) with mutation testing.

### Changed

- Production mode refuses to start with the default `SECRET_KEY`, the documented demo password or
  demo data enabled. The admin user is created only from `ADMIN_EMAIL`/`ADMIN_PASSWORD` when no users exist.
- Classification returns `Unclassified` (instead of guessing `EOB`) when nothing matches and reports a confidence.
- Extraction understands generic MRN / member / account IDs and claim numbers, ignores dates of birth
  when picking the service date, and validates amounts.
- Upload UI posts to an HTML route and shows per-file results (the beta returned raw JSON).
- The UI no longer loads Tailwind/htmx/Alpine/Chart.js from public CDNs; it ships its own stylesheet.
- Passwords use `bcrypt` directly and tokens use `PyJWT` (replacing unmaintained `passlib` / `python-jose`).

### Removed

- Committed sample uploads under `data/uploads/` (all were synthetic) and unused dependencies
  (`apscheduler`, `numpy`).
- Leftover "ClusterX" template branding and defaults.

### Fixed

- `/documents/{id}/preview` returned document data without authentication.
- Editing a non-numeric billed amount caused a 500 error.
- Uploaded files were written inside the source tree.

### Security

- See [SECURITY.md](SECURITY.md) for the safeguards and for what HIPAA compliance additionally requires
  from the deployer.

## Beta (unversioned)

- Initial beta: dashboard, PDF upload, rule-based classification and extraction, exact-duplicate
  detection, patient binders, Docker deploy.
