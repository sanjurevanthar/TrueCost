from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    cors_origins: tuple
    cors_origin_regex: str | None = None  # e.g. https://.*\.lovable\.app  (preview URLs change)
    extractor: str = "auto"  # auto | gemini | claude | keyword
    gemini_model: str = "gemini-3.5-flash"
    claude_model: str = "claude-opus-5-5"
    log_level: str = "INFO"
    log_format: str = "text"   # text | json
    require_session: bool = False        # True in production: AI endpoints need a session token or the API key
    session_secret: str | None = None    # sign session tokens; unset = random per start (sessions reset on restart)
    session_ttl_min: int = 60
    session_rate_limit_per_min: int = 20  # new sessions per client per minute
    daily_token_budget: int = 0          # LLM tokens per UTC day; 0 = unlimited
    docs_enabled: bool = True            # False hides /docs, /redoc, /openapi.json
    ai_rate_limit_per_min: int = 10   # per client on AI endpoints; 0 disables
    api_access_token: str | None = None  # if set, AI endpoints need header X-API-Key

    @classmethod
    def from_env(cls) -> "Settings":
        if not os.getenv("TRUECOST_NO_DOTENV"):  # tests set this so a developer's real keys never leak in
            load_dotenv(PROJECT_ROOT / ".env")  # real environment variables win; no-op if absent
        return cls(
            db_path=Path(os.getenv("DB_PATH", PROJECT_ROOT / "truecost.db")),
            cors_origins=tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()),
            cors_origin_regex=os.getenv("CORS_ORIGIN_REGEX") or None,
            extractor=os.getenv("EXTRACTOR", "auto").lower(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_format=os.getenv("LOG_FORMAT", "text").lower(),
            require_session=_flag("REQUIRE_SESSION", False),
            session_secret=os.getenv("SESSION_SECRET") or None,
            session_ttl_min=int(os.getenv("SESSION_TTL_MIN", "60")),
            session_rate_limit_per_min=int(os.getenv("SESSION_RATE_LIMIT_PER_MIN", "20")),
            daily_token_budget=int(os.getenv("DAILY_TOKEN_BUDGET", "0")),
            docs_enabled=_flag("DOCS_ENABLED", True),
            ai_rate_limit_per_min=int(os.getenv("AI_RATE_LIMIT_PER_MIN", "10")),
            api_access_token=os.getenv("API_ACCESS_TOKEN") or None,
        )
