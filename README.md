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

## Deploy on Vultr (Docker)
Needs a Vultr VM (Ubuntu, 1 GB RAM is enough) with Docker, and a domain whose **A record points at the VM** (the Lovable
frontend is https, so the API must be https too; Caddy gets the certificate automatically).

```
# on the VM
git clone https://github.com/sanjurevanthar/TrueCost.git && cd TrueCost
# from your laptop: the price database is not in git
scp truecost.db root@<vm-ip>:~/TrueCost/truecost.db
# create .env (see table), then:
docker compose up -d --build
docker compose ps                  # api should show "healthy"
curl https://<your-domain>/health
```
Open ports 80 and 443 only (firewall: `ufw allow 80,443/tcp && ufw allow OpenSSH && ufw enable`).

| `.env` variable | Purpose |
|---|---|
| `DOMAIN` | e.g. `api.example.com` (required by Caddy) |
| `GEMINI_API_KEY` | AI note reading (paid tier recommended) |
| `API_ACCESS_TOKEN` | if set, AI endpoints need header `X-API-Key` (put the same value in Lovable) |
| `CORS_ORIGINS` | your Lovable site, e.g. `https://yourapp.lovable.app` (not `*` in production) |
| `AI_RATE_LIMIT_PER_MIN` | per-client cap on AI endpoints (default 10) |
| `LOG_LEVEL` | `INFO` normally, `DEBUG` while debugging |

### Debugging with logs
Logs are JSON lines on stdout, rotated by Docker (10 MB x 5). Every line has a `request_id`; the same id is returned to the
caller in the `X-Request-ID` header (and in the body of any 500), so a user can quote it.
```
docker compose logs -f api                                  # live
docker compose logs api --since 1h | grep '"level": "ERROR"'
docker compose logs api | grep <request_id>                 # everything one request did
docker compose logs api | grep -E 'extraction failed|rate limit|rejected'
```
What is logged: startup config (key *presence* only), one `request` line per call (method, path, status, ms, client IP),
`estimate` summaries, `extract` results (codes, token counts, `fallback_used`), LLM latency/failures, DB query timings
(DEBUG), and warnings for rejected requests, rate limits and bad tokens. **Never logged:** note text, image contents,
insurance numbers, query strings, or secrets (values are scrubbed from every line).
Update: `git pull && docker compose up -d --build`.
