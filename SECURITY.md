# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security problems in Vitality BSS.

Report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/fernandogarzaaa/vitality-bss/security/advisories/new)
or by email to **fernandogarzaaa@gmail.com**. Include a description, steps to reproduce and the
affected version or commit. You should get an acknowledgement within a few days; please allow time
for a fix before public disclosure.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | yes       |
| < 1.0   | no        |

## What the application does to protect data

- **Authentication**: bcrypt password hashing (configurable cost), password policy, signed
  short-lived JWT sessions, account lockout after repeated failures, server-side session revocation
  on logout / password change / deactivation (token versioning).
- **Authorization**: three roles (`admin`, `member`, `viewer`); every API and page checks them.
- **CSRF**: cookie sessions are `HttpOnly`, `SameSite=Strict`, `Secure` in production, and every
  state-changing form or cookie-authenticated API call requires a session-bound CSRF token.
- **Browser hardening**: strict Content-Security-Policy (no third-party scripts or styles, no inline
  code), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `Cache-Control: no-store` on PHI pages,
  HSTS when served over HTTPS.
- **Encryption at rest (optional)**: uploaded documents are Fernet-encrypted (AES-128-CBC + HMAC)
  when `ENCRYPTION_KEYS` is set; key rotation via `vitality-bss rotate-keys`. Files are stored under
  random names, never by patient data.
- **PHI-safe logging**: structured logs never include document text; a redaction filter masks names,
  dates of birth, SSNs, emails, phone numbers and medical/claim identifiers in every log record.
  Validation errors never echo submitted values.
- **Audit log**: logins (success and failure), document upload/view/download/update/delete, binder
  generation, user management and logouts are recorded with actor, time, IP and request ID.
- **Third-party egress**: OCR runs locally. A remote LLM is refused unless
  `LLM_ALLOW_REMOTE_PHI=true` is set explicitly.
- **Input validation**: PDF signature and structure checks, page/size/file-count limits, filename
  sanitisation, strict schemas for every API body.

## HIPAA and other regulations

Vitality BSS provides technical safeguards that help, but **running it does not make you HIPAA (or
GDPR, or Philippine Data Privacy Act) compliant**. Compliance depends on the deployer, including:

- a Business Associate Agreement with every party that hosts or processes PHI (cloud provider,
  backup provider, any remote LLM provider);
- TLS termination in front of the app, network isolation, and OS/disk encryption on the host;
- encryption and access control for database backups (database fields are **not** encrypted by the
  application, only stored document files are);
- retention and disposal policies, audit-log review, workforce training, incident response, and a
  risk assessment.

Default settings are for development. Run `vitality-bss check-config` before going live.
