# TrueCost – backend (FastAPI)

```
cd backend
../.venv/bin/uvicorn app.main:create_app --factory --reload   # http://127.0.0.1:8000/docs
../.venv/bin/python -m pytest -q                              # unit + API tests (no DB needed)
```
Needs `truecost.db` in the project root (`scripts/build_db.py`), or set `DB_PATH`.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/codes?phase=` | supported billing codes |
| `GET /api/v1/hospitals` | hospitals + coordinates |
| `GET /api/v1/plans?payer=` | plan names for a dropdown |
| `POST /api/v1/extract` | doctor's note -> suggested codes (swap in the LLM extractor) |
| `POST /api/v1/estimate` | rank hospitals by out-of-pocket cost (insured or self_pay) |

Layers: `api/` (HTTP) -> `services/` (use cases) -> `domain/` (entities, calculators, rate selector) and
`repositories/` (data access). `container.py` wires concrete classes together.
