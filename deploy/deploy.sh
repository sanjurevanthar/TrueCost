#!/usr/bin/env bash
# Deploy one image tag on the Vultr server, wait until it is healthy, roll back to the previous tag if it is not.
#   ./deploy.sh sha-1a2b3c4          deploy that version
#   ./deploy.sh $(cat .deployed_tag) re-deploy the current one
# Runs in the folder that holds docker-compose.yml, Caddyfile, .env and truecost.db (the GitHub workflow copies the
# first two there on every deploy; .env and truecost.db are created by hand once and never leave the server).
set -euo pipefail
cd "$(dirname "$0")"

TAG="${1:?usage: deploy.sh <image-tag>   e.g. sha-1a2b3c4}"
STATE=.deployed_tag
PREV="$(cat "$STATE" 2>/dev/null || true)"
WAIT_SECONDS="${WAIT_SECONDS:-120}"

log() { printf '%s deploy: %s\n' "$(date -u +%FT%TZ)" "$*"; }
[ -f .env ]          || { log "ERROR .env is missing (see README: Deploy on Vultr)"; exit 1; }
[ -f truecost.db ]   || { log "ERROR truecost.db is missing (scp it to this folder once)"; exit 1; }
[ -f docker-compose.yml ] || { log "ERROR docker-compose.yml is missing"; exit 1; }

wait_healthy() {            # returns 0 when the api container reports healthy within WAIT_SECONDS
  local cid status
  for _ in $(seq 1 "$WAIT_SECONDS"); do
    cid="$(docker compose ps -q api 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)"
      [ "$status" = "healthy" ] && return 0
      [ "$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null)" = "exited" ] && return 1
    fi
    sleep 1
  done
  return 1
}

log "deploying $TAG (previous: ${PREV:-none})"
export IMAGE_TAG="$TAG"
docker compose pull api
docker compose up -d --no-build --remove-orphans

if wait_healthy; then
  echo "$TAG" > "$STATE"
  log "OK $TAG is healthy"
  docker image prune -f >/dev/null 2>&1 || true
  docker compose ps
  exit 0
fi

log "FAILED $TAG did not become healthy. Last logs:"
docker compose logs --tail 60 api || true
if [ -n "$PREV" ] && [ "$PREV" != "$TAG" ]; then
  log "rolling back to $PREV"
  export IMAGE_TAG="$PREV"
  docker compose up -d --no-build --remove-orphans
  if wait_healthy; then
    log "rollback OK, still serving $PREV"
  else
    log "ROLLBACK ALSO UNHEALTHY: check 'docker compose logs api'"
  fi
else
  log "no previous version to roll back to"
fi
exit 1
