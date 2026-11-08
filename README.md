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
curl https://<your-domain>/health      # alive
curl https://<your-domain>/ready       # alive AND the price database answers
```
Open ports 80 and 443 only (firewall: `ufw allow 80,443/tcp && ufw allow OpenSSH && ufw enable`).

| `.env` variable | Purpose |
|---|---|
| `DOMAIN` | e.g. `api.example.com` (required by Caddy) |
| `GEMINI_API_KEY` | AI note reading (paid tier recommended) |
| `SESSION_SECRET` | random string (32+ chars) that signs session tokens. `openssl rand -hex 32` |
| `API_ACCESS_TOKEN` | **admin** key: needed for `/usage` and for scripts. Do NOT put it in the Lovable app |
| `DAILY_TOKEN_BUDGET` | max LLM tokens per UTC day (compose default 200000, about 200 note reads; 0 = unlimited). When spent, `/extract` falls back to the basic matcher and image reading pauses |
| `AI_RATE_LIMIT_PER_MIN` | AI calls allowed per client per minute (default **10**); extra calls get `429` + `Retry-After`. A hard cap of 4x this applies across all clients |
| `SESSION_RATE_LIMIT_PER_MIN` | new sessions per client per minute (default 20) |
| `REQUIRE_SESSION` | default **false** in compose. Set `true` once the frontend sends the session token |
| `SESSION_TTL_MIN` | session lifetime in minutes (default 60) |
| `DOCS_ENABLED` | default true; set `false` for the final deployment to hide `/docs` and `/openapi.json` |
| `CORS_ORIGINS` | your Lovable site, e.g. `https://yourapp.lovable.app` (not `*` in production; the server logs a warning if it is) |
| `CORS_ORIGIN_REGEX` | optional, for Lovable preview URLs that change: `https://.*\.lovable\.app` |
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

### Authentication model (no accounts, nothing stored)
1. The frontend calls `POST /api/v1/session` once when it loads and keeps the token in memory.
2. It sends `Authorization: Bearer <token>` on `/extract` and `/extract/image`.
3. On a `401` ("Session expired") it requests a new session and retries once.
4. Limits: new sessions are throttled per IP; AI calls are limited per IP **and** per session; a daily token budget caps spend.
`/estimate` and the reference endpoints need no session (they cost nothing). `/usage` needs the admin `X-API-Key`.
Responses carry `X-Content-Type-Options`, `X-Frame-Options`, a locked-down CSP, `Cache-Control: no-store` and HSTS (behind https).

## CI/CD (GitHub Actions -> GitHub Container Registry -> Vultr)
`.github/workflows/ci-cd.yml` runs on every push to any branch. Steps 1-2 need no configuration at all; step 3 happens on `main`; step 4 stays switched off until you create the repository variable `DEPLOY_ENABLED` = `true`:

| Step | When | What |
|---|---|---|
| **Tests and security scan** | every push | `pytest`, `bandit`, `pip-audit` (warns only), workflow + script lint |
| **Build and smoke-test the image** | every push | builds the Docker image, starts it exactly as production does (read-only, non-root, JSON logs), checks `/ready`, an estimate, and that no `.env`/database is baked in |
| **Publish** | main only | pushes `ghcr.io/<owner>/truecost-api:sha-<commit>` and `:latest` |
| **Deploy to Vultr** | main only | copies `docker-compose.yml` + `Caddyfile` + `deploy.sh` to the server, runs `deploy.sh`: pull, restart, wait for "healthy", **roll back automatically** if it never gets healthy |

Your secrets and the price database never go through GitHub: `.env` and `truecost.db` live only on the server.

### One-time setup
**1. The Vultr server** (Cloud Compute, Ubuntu 24.04, x86, 1 GB+ RAM; the image is built for amd64)
```
ssh root@<server-ip>
curl -fsSL https://get.docker.com | sh                       # install Docker
adduser --disabled-password --gecos "" deploy && usermod -aG docker deploy
mkdir -p /home/deploy/.ssh && chmod 700 /home/deploy/.ssh && chown deploy:deploy /home/deploy/.ssh
ufw allow OpenSSH && ufw allow 80,443/tcp && ufw --force enable
```
Also add a firewall group in the Vultr dashboard (Products -> Firewall) allowing only 22, 80 and 443.

**2. An SSH key just for GitHub** (on your laptop)
```
ssh-keygen -t ed25519 -f ~/.ssh/truecost_deploy -N "" -C "github-actions"
ssh-copy-id -i ~/.ssh/truecost_deploy.pub deploy@<server-ip>      # or paste the .pub into /home/deploy/.ssh/authorized_keys
ssh-keyscan -t ed25519 <server-ip>                                  # copy this output: it is VULTR_KNOWN_HOSTS
```
**3. The server's private files** (once)
```
ssh deploy@<server-ip> 'mkdir -p ~/truecost'
scp truecost.db deploy@<server-ip>:truecost/truecost.db
ssh deploy@<server-ip> 'nano ~/truecost/.env && chmod 600 ~/truecost/.env'      # see the .env table above
```
Point your domain's **A record** at the server IP (Caddy needs it to get the HTTPS certificate).

**4. GitHub secrets** (repo -> Settings -> Secrets and variables -> Actions -> New repository secret)

| Secret | Value |
|---|---|
| `VULTR_HOST` | the server IP |
| `VULTR_USER` | `deploy` |
| `VULTR_SSH_KEY` | the contents of `~/.ssh/truecost_deploy` (the **private** key, whole file) |
| `VULTR_KNOWN_HOSTS` | the `ssh-keyscan` output from step 2 |
| `VULTR_DOMAIN` | optional, e.g. `api.example.com`: the last step then checks the live site |

Then switch deployment on: Settings -> Secrets and variables -> Actions -> **Variables** tab -> New repository variable: `DEPLOY_ENABLED` = `true`.

**5. Recommended GitHub settings**
- Settings -> Branches -> add a rule for `main`: require a pull request and the two checks *Tests and security scan* and *Build and smoke-test the Docker image*. Nothing broken can then reach `main`, so nothing broken deploys.
- Settings -> Environments -> `production` -> *Required reviewers*: adds a click-to-approve before each deploy (good on demo day).

### Day to day
Open a pull request: the tests and the image build run. Merge it into `main`: the image is published and deployed (about 3-4 minutes). Watch it in the repo's **Actions** tab; the server shows the running version in `~/truecost/.deployed_tag`.

**Roll back by hand:** find the old tag under the repo's *Packages* (or `git log`, tags are `sha-<7 chars>`), then
`ssh deploy@<ip> 'cd ~/truecost && ./deploy.sh sha-1a2b3c4'`.
**Update the price database:** `scp truecost.db deploy@<ip>:truecost/ && ssh deploy@<ip> 'cd ~/truecost && docker compose restart api'`.
**Change a setting** (rate limit, CORS...): edit `~/truecost/.env`, then `docker compose up -d` in that folder.
