# Vitality BSS

AI-powered healthcare document processing: classify, extract, dedupe, and auto-generate billing binders. (Beta)

## Quickstart

```bash
cp .env.example .env
docker compose up --build
# open http://localhost:8001
# login: admin@clusterx.local / ChangeMe123!
```

Local dev (SQLite):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.seed
uvicorn app.main:app --port 8001
```

## Tests

```bash
python -m pytest -q
ruff check app
```

## License

MIT
