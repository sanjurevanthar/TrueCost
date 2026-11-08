# TrueCost API image. Build:  docker build -t truecost-api .
FROM python:3.12-slim

# uv for fast, locked installs (versions come from uv.lock)
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    LOG_FORMAT=json LOG_LEVEL=INFO DB_PATH=/app/truecost.db TRUECOST_NO_DOTENV=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# runtime dependencies only: no dev/data/notebook groups (no pandas, no jupyter)
RUN uv sync --frozen --no-default-groups --no-install-project

COPY backend/app ./backend/app

# run as an unprivileged user
RUN useradd --system --uid 10001 --no-create-home truecost && chown -R truecost /app
USER truecost

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# uvicorn's own access log is off: the app logs every request itself (with request ids)
CMD ["uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
