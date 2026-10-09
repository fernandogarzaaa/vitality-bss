# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `requirements.lock` pinning the full (transitive) dependency set, generated with `uv pip compile --universal`.
- Dependabot configuration for pip, the Docker base image and GitHub Actions (weekly).
- `.gitignore` for Python caches, the local SQLite database and local `.env` files.
- SECURITY.md describing private vulnerability reporting.
- This CHANGELOG.
