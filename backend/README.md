# TrueCost – backend (FastAPI)

```
make run     # http://127.0.0.1:8000/docs   (from project root; or: uv run uvicorn app.main:create_app --factory --reload --app-dir backend)
make test    # all tests;  make test-unit = no DB needed
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

## Note -> codes (`/extract`)
`EXTRACTOR=auto` (default) uses Claude when `ANTHROPIC_API_KEY` is set, else the keyword matcher; force with
`EXTRACTOR=claude|keyword`; model via `CLAUDE_MODEL` (default `claude-opus-5-5`). Claude only suggests codes from the
catalog; output is schema-constrained, unknown codes and evidence not quoted from the note are dropped, and any failure
falls back to the keyword matcher. Measure accuracy: `make eval` (baseline) / `ANTHROPIC_API_KEY=... make eval CLAUDE=1`.

## Protecting the AI endpoints
`/extract` and `/extract/image` spend LLM tokens, so they are rate limited per client (`AI_RATE_LIMIT_PER_MIN`, default 10,
`0` = off; honours `X-Forwarded-For` so it works behind ngrok) and can require a header `X-API-Key` when
`API_ACCESS_TOKEN` is set. Over the limit -> `429` with `Retry-After`. Put these in `.env` (git-ignored); `.env.example` is the template.

## Price warnings
A line carries `warning` when its price looks unreliable (negotiated rate > 2x the hospital's listed charge, or self-pay cash
price < 25% of it). Show it next to the number. The numbers themselves are never altered.
