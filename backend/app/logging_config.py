"""Logging setup: one place decides format, level and what must never be logged.

LOG_LEVEL   DEBUG | INFO (default) | WARNING ...
LOG_FORMAT  text (default, readable in a terminal) | json (one object per line, for Docker / Vultr / log tools)

Rules: log to stdout only (Docker collects it); every line carries the request id; secrets are scrubbed from every
line as a safety net; note text, image contents and insurance numbers are never logged by the app.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

SECRET_ENV_VARS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "API_ACCESS_TOKEN")
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "request_id", "taskName"}
_HANDLER_TAG = "_truecost_handler"


def redact(text: str) -> str:
    """Replace the value of any configured secret with ***, wherever it appears."""
    for name in SECRET_ENV_VARS:
        val = os.environ.get(name)
        if val and len(val) >= 8:
            text = text.replace(val, "***")
    return text


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        line = super().format(record)
        if extras:
            line += " | " + " ".join(f"{k}={v}" for k, v in extras.items())
        return redact(line)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
             "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
             "request_id": getattr(record, "request_id", "-")}
        d.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")})
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(d, default=str))


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Idempotent. Replaces only the handler this function installed earlier (so pytest's caplog keeps working)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_TAG, True)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(JsonFormatter() if fmt.lower() == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn: use our handler/format; our request middleware replaces its access log.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate = [], True
    acc = logging.getLogger("uvicorn.access")
    acc.handlers, acc.propagate, acc.disabled = [], False, True
    # HTTP client libraries can echo URLs/headers at DEBUG/INFO: keep them quiet.
    for name in ("httpx", "httpx2", "httpcore", "google", "google_genai", "anthropic", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
