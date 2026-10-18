#!/usr/bin/env bash
# Runs ON the VM: issue API keys for one testing team and print them ONCE (only hashes are stored).
#   bash ~/issue-keys.sh team-b
# Send the output to that team privately. Revoke later with:
#   sudo docker compose exec api python -m app.cli revoke-key --key-id <key_id>
set -euo pipefail
TEAM="${1:?usage: issue-keys.sh <team-name>}"
cd /opt/compliance-monitor/deploy/gcp

cli() { sudo docker compose exec -T api python -m app.cli "$@" 2>/dev/null; }
key() { sed -n 's/^api_key: //p'; }

# The baseline fixture systems (app/modules/testing/fixtures.py); /v1/test/reset keeps them.
LOAN=550e8400-e29b-41d4-a716-446655440000
RESUME=550e8400-e29b-41d4-a716-446655440001
cli create-system --id "$LOAN" --name loan-scorer >/dev/null || true
cli create-system --id "$RESUME" --name resume-ranker >/dev/null || true

echo "admin=$(cli create-user --name "$TEAM-admin" --role admin | key)"
echo "viewer=$(cli create-user --name "$TEAM-viewer" --role viewer | key)"
echo "reviewer=$(cli create-user --name "$TEAM-reviewer" --role reviewer | key)"
echo "system_loan_scorer=$(cli create-user --name "$TEAM-loan-scorer" --role system --system-id "$LOAN" | key)"
echo "system_resume_ranker=$(cli create-user --name "$TEAM-resume-ranker" --role system --system-id "$RESUME" | key)"
