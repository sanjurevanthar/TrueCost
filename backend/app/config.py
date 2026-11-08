from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    db_path: Path
    cors_origins: tuple
    extractor: str = "auto"  # auto | gemini | claude | keyword
    gemini_model: str = "gemini-3.5-flash"
    claude_model: str = "claude-opus-5-5"

    @classmethod
    def from_env(cls) -> "Settings":
        if not os.getenv("TRUECOST_NO_DOTENV"):  # tests set this so a developer's real keys never leak in
            load_dotenv(PROJECT_ROOT / ".env")  # real environment variables win; no-op if absent
        return cls(
            db_path=Path(os.getenv("DB_PATH", PROJECT_ROOT / "truecost.db")),
            cors_origins=tuple(os.getenv("CORS_ORIGINS", "*").split(",")),
            extractor=os.getenv("EXTRACTOR", "auto").lower(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
        )
