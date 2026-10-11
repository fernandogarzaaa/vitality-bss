"""ASGI entrypoint: ``uvicorn vitality_bss.main:app``."""

from vitality_bss.web.app import create_app

app = create_app()
