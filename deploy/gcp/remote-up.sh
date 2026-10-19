#!/usr/bin/env bash
# Runs ON the VM (deploy.sh copies it there): unpack the release and (re)start the stack.
# Keeps deploy/gcp/.env (database password, site address) across releases.
set -euo pipefail
: "${SITE_ADDRESS:?}"

APP=/opt/compliance-monitor
ENV_FILE="$APP/deploy/gcp/.env"
saved="$(mktemp)"
if sudo test -f "$ENV_FILE"; then
    sudo cp "$ENV_FILE" "$saved"
fi
sudo rm -rf "$APP"
sudo mkdir -p "$APP"
sudo tar -xzf ~/release.tgz -C "$APP"

if [ -s "$saved" ]; then
    sudo mv "$saved" "$ENV_FILE"
else
    rm -f "$saved"
    # First deployment: test environment for cross-team testing (test routes and the shared
    # simulated clock on; APP_ENV=production would switch both off).
    sudo tee "$ENV_FILE" >/dev/null <<EOF
# Created once by deploy/gcp/remote-up.sh. Never commit.
APP_ENV=test
ENABLE_TEST_ROUTES=true
LOG_LEVEL=INFO
WEBHOOK_ALLOW_PRIVATE_NETWORKS=false
POSTGRES_PASSWORD=$(openssl rand -hex 24)
SITE_ADDRESS=${SITE_ADDRESS}
EOF
fi
sudo chmod 600 "$ENV_FILE"

cd "$APP/deploy/gcp"
sudo docker compose up -d --build --remove-orphans
# The Caddyfile is a bind-mounted file and this release replaced it, but compose only recreates
# containers whose definition changed; recreate Caddy so it reads the new routes.
sudo docker compose up -d --force-recreate --no-deps caddy

for attempt in $(seq 1 60); do
    if sudo docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" 2>/dev/null; then
        break
    fi
    [ "$attempt" -eq 60 ] && { echo "API did not become healthy" >&2; sudo docker compose logs --tail 50 api >&2; exit 1; }
    sleep 5
done
sudo docker compose ps --format "table {{.Service}}\t{{.Status}}"
