from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    db_path: Path
    cors_origins: tuple

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.getenv("DB_PATH", PROJECT_ROOT / "truecost.db")),
            cors_origins=tuple(os.getenv("CORS_ORIGINS", "*").split(",")),
        )
