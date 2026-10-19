# Deployment: Google Cloud (cross-team test environment)

The service runs on one Compute Engine VM in project **AIREG** (`aireg-512020`): the same
container image as development, started with [deploy/gcp/compose.yaml](../deploy/gcp/compose.yaml).

```
internet ──443/80──► caddy (HTTPS, Let's Encrypt) ─┬─► api (uvicorn) ──► db (PostgreSQL 16)
                                                    │   worker ─────────┘   (not published)
                                                    └─► tester (/tester: hosted contract suite)
```

## Hosted tester (`/tester`)

The registry's "Tester base URL" is `https://<host>/tester`. It runs the black-box contract
suite (`tests/contract`) against an **allowlisted** service and returns the report:

```bash
curl -X POST https://<host>/tester/runs -H "Content-Type: application/json" -d '{
  "target_url": "https://<target>", "system_key": "...", "viewer_key": "...", "admin_key": "...",
  "system_id": "<optional, target fixture id>"}'          # → 202 {"run_id": ..., "status_url": ...}
curl https://<host>/tester/runs/<run_id>                  # status, summary, coverage, per-test results
curl https://<host>/tester/runs/<run_id>/report           # Markdown report
```

- **Allowed targets:** this deployment, plus `TESTER_EXTRA_TARGETS` in `deploy/gcp/.env`
  (default: `https://eu-ai-act-monitor-api.fly.dev`, Team 8). Anything else is `400`.
- **Keys:** the caller sends the target's keys for that run only. They're passed to the test
  process and never stored, logged or returned.
- **Limits:** one run at a time (`409` otherwise), at most 10 minutes; a full run takes about a
  minute. A run resets the target's test data (`POST /v1/test/reset`).
- **Webhooks:** during a run the suite's receiver is public at `https://<host>/tester/hook/<run>`,
  so webhook delivery (AC-04) is checked against the target's deployment.
- The tester container has no database access and receives nothing from `.env` except the site
  address.

| Item | Value |
|---|---|
| VM | `compliance-monitor`, `e2-small`, `us-central1-a`, Debian 12, 20 GB standard disk |
| Address | static IP `compliance-monitor-ip`; hostname `<ip-with-dashes>.sslip.io` (free wildcard DNS) |
| Firewall | `compliance-monitor-web` allows tcp 80 and 443 to the VM; SSH via `gcloud compute ssh` |
| Environment | `APP_ENV=test`, `ENABLE_TEST_ROUTES=true` (for cross-team testing), random DB password in `/opt/compliance-monitor/deploy/gcp/.env` on the VM |
| Cost (estimate) | about $12/month for e2-small, plus about $1/month for the disk and about $4/month for the in-use IPv4 address; stop or delete the VM when not testing |

The decision to use GCP replaces the AWS Lightsail plan in Phase 2 Q7.3. The container layout is
unchanged.

## Deploy or update

Needs the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) with `gcloud auth login`
done once, and Compute Engine billing enabled on the project. From the repository root:

```bash
deploy/gcp/deploy.sh                    # Git Bash on Windows: GCLOUD=gcloud.cmd deploy/gcp/deploy.sh
```

It creates the IP, firewall rule and VM if they don't exist, uploads the **committed HEAD**,
rebuilds the image and restarts the stack. Migrations run when the API starts. Data in the
PostgreSQL volume survives redeploys. The script prints the URL, for example
`https://34-123-45-67.sslip.io`.

## Issue keys for a team

```bash
gcloud compute ssh compliance-monitor --zone us-central1-a --command "bash ~/issue-keys.sh team-b"
```

This prints admin, viewer, reviewer and two system keys (loan-scorer, resume-ranker) once. Send
them privately. To revoke one, run on the VM:
`cd /opt/compliance-monitor/deploy/gcp && sudo docker compose exec api python -m app.cli revoke-key --key-id <id>`.

## Operate

On the VM (`gcloud compute ssh compliance-monitor --zone us-central1-a`), in
`/opt/compliance-monitor/deploy/gcp`:

```bash
sudo docker compose ps                     # status
sudo docker compose logs -f api worker     # JSON logs (add tester for hosted test runs)
sudo docker compose exec api python -m app.cli verify-ledger
sudo docker compose restart worker
```

`https://<host>/healthz` reports the database and the worker heartbeat.

## Stop or remove

```bash
gcloud compute instances stop compliance-monitor --zone us-central1-a      # pauses VM billing
gcloud compute instances delete compliance-monitor --zone us-central1-a    # deletes VM and data
gcloud compute addresses delete compliance-monitor-ip --region us-central1
gcloud compute firewall-rules delete compliance-monitor-web
```

## Security notes

- Only Caddy is reachable from outside. PostgreSQL and the API port aren't published.
- This is a **test** environment: `/v1/test/reset` and the simulated clock are enabled (admin
  key required). For a production deployment set `APP_ENV=production`, which disables them and
  requires https webhooks.
- `/docs` (OpenAPI UI) is public, as in development. The API itself requires keys.
- Not yet done: backups (the VM disk only), rate limiting, image vulnerability fixes flagged by
  Trivy (see PR #4).
