# syntax=docker/dockerfile:1
FROM python:3.14-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.3.0
COPY pyproject.toml README.md LICENSE ./
COPY vitality_bss ./vitality_bss
RUN python -m build --wheel --outdir /dist

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/app.db \
    PORT=8001

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app && useradd --system --gid app --home /srv/app app \
 && mkdir -p /data /srv/app && chown app:app /data /srv/app

WORKDIR /srv/app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm /tmp/*.whl

USER app
VOLUME ["/data"]
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/readyz" || exit 1
CMD ["sh", "-c", "exec vitality-bss serve --host 0.0.0.0 --port ${PORT} --forwarded-allow-ips ${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
