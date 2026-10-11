"""Command line interface: ``vitality-bss <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

from vitality_bss import __version__


def _cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - starts a server
    import uvicorn

    from vitality_bss.config import get_settings

    s = get_settings()
    uvicorn.run(
        "vitality_bss.main:app",
        host=args.host or s.HOST,
        port=args.port or s.PORT,
        workers=args.workers,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
        access_log=False,
    )
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from vitality_bss.db import current_revision, run_migrations

    run_migrations(args.revision)
    print(f"database at revision {current_revision()}")
    return 0


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")  # pragma: no cover - interactive
    if first != getpass.getpass("Repeat password: "):  # pragma: no cover - interactive
        raise SystemExit("passwords do not match")
    return first  # pragma: no cover - interactive


def _cmd_create_user(args: argparse.Namespace) -> int:
    from vitality_bss import audit, services
    from vitality_bss.db import new_session, run_migrations

    run_migrations()
    password = _read_password(args)
    with new_session() as db:
        try:
            user = services.create_user(db, args.email, password, args.role, args.name)
        except services.UserError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        audit.record(
            db,
            "user.create",
            actor_email="cli",
            resource_type="user",
            resource_id=user.id,
            details={"role": user.role, "source": "cli"},
        )
        print(f"created {user.role} user id={user.id}")
    return 0


def _cmd_seed_demo(_: argparse.Namespace) -> int:
    from vitality_bss.db import new_session, run_migrations
    from vitality_bss.demo import seed_demo

    run_migrations()
    with new_session() as db:
        print(json.dumps(seed_demo(db)))
    return 0


def _cmd_process(args: argparse.Namespace) -> int:
    """Analyse PDFs locally without storing them (prints JSON; output contains PHI)."""
    from vitality_bss.pdftext import InvalidDocument
    from vitality_bss.pipeline import analyze

    rc = 0
    for path in args.files:
        p = Path(path)
        try:
            info = analyze(p.name, p.read_bytes())
        except (OSError, InvalidDocument) as exc:
            print(json.dumps({"file": str(p), "error": str(exc)}))
            rc = 1
            continue
        print(json.dumps({"file": str(p), **info.summary()}, default=str))
    return rc


def _cmd_generate_secrets(_: argparse.Namespace) -> int:
    from vitality_bss.storage import generate_key

    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"ENCRYPTION_KEYS={generate_key()}")
    return 0


def _cmd_rotate_keys(_: argparse.Namespace) -> int:
    from sqlalchemy import select

    from vitality_bss.db import new_session
    from vitality_bss.models import Document
    from vitality_bss.storage import StorageError, get_store

    try:
        store = get_store()
        count = 0
        with new_session() as db:
            for doc in db.scalars(select(Document)):
                doc.encrypted = store.rotate(doc.storage_key, doc.encrypted)
                count += 1
            db.commit()
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"re-encrypted {count} documents with the primary key")
    return 0


def _cmd_check_config(_: argparse.Namespace) -> int:
    from vitality_bss.config import ConfigError, get_settings

    try:
        warnings = get_settings().validate_for_runtime()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"warning: {w}")
    print("configuration ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vitality-bss", description="Vitality BSS document processing")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web server")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("--revision", default="head")
    p.set_defaults(func=_cmd_migrate)

    p = sub.add_parser("create-user", help="create a user")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--role", default="member", choices=["admin", "member", "viewer"])
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=_cmd_create_user)

    p = sub.add_parser("seed-demo", help="load synthetic demo documents")
    p.set_defaults(func=_cmd_seed_demo)

    p = sub.add_parser("process", help="analyse PDFs and print JSON (not stored)")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=_cmd_process)

    p = sub.add_parser("generate-secrets", help="print a fresh SECRET_KEY and ENCRYPTION_KEYS value")
    p.set_defaults(func=_cmd_generate_secrets)

    p = sub.add_parser("rotate-keys", help="re-encrypt stored documents with the primary key")
    p.set_defaults(func=_cmd_rotate_keys)

    p = sub.add_parser("check-config", help="validate configuration")
    p.set_defaults(func=_cmd_check_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
