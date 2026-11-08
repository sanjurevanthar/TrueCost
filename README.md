# TrueCost – know before you owe: breast cancer cost navigator for Western New York

Builds `truecost.db` (SQLite: `prices`, `hospitals`, `codes`) from hospital price-transparency files.

```
data/                raw downloads (git-ignored): 3 Catholic Health CSVs, Roswell Park JSON, ECMC CSV, ICD-10 codes
scripts/build_db.py  builds truecost.db  ->  uv run python scripts/build_db.py
notebooks/           build_db.ipynb (build + checks), preview_data.ipynb (first 5 rows of each file)
truecost.db  output database
```

## Quick start (uses [uv](https://docs.astral.sh/uv/))
```
make setup      # uv sync: creates .venv from pyproject.toml / uv.lock
make run        # API at http://127.0.0.1:8000/docs
make test       # all tests (make test-unit | make test-int)
make smoke URL=https://your-tunnel   # black-box check of a running API
make db         # rebuild truecost.db from data/
```
No make? Use `uv sync`, `uv run pytest`, `uv run uvicorn app.main:create_app --factory --reload --app-dir backend`.
