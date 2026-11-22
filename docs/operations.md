# Operations: signals, alerts, backups and recovery

The operations note from the Phase 5 milestone (Phase 2 §6.3). It covers what to watch on a
running deployment, which alerts exist, how data is (and isn't yet) backed up, how to recover,
and how to roll out a new release. Deployment mechanics are in
[deployment-gcp.md](deployment-gcp.md).

## 1. Signals to watch

| Signal | Where | Healthy | Act when |
|---|---|---|---|
| Database reachable | `GET /healthz` → `database` | `ok` (HTTP 200) | `503` |
| Last ledger verification | `GET /healthz` → `last_verification`, `last_verification_age_seconds`; `GET /v1/ledger/verifications` | `valid: true`, age under a day | `valid: false`, or age over a day (worker not checking) |
| Release and test resets | `GET /healthz` → `version` (`APP_VERSION`), `reset_count` | expected release | unexpected resets during a test run |
| Worker alive | `GET /healthz` → `worker` | `ok` | `stale` or `missing`: `sudo docker compose restart worker` |
| Late reviewer notifications (REQ-04) | worker log `REQ-04: … passed their maximum delay` | absent | present: is a webhook registered and reachable? |
| Ledger integrity (REQ-06) | `ledger_alert` webhook, worker log at `CRITICAL`, `ledger_check` table | latest row `valid = true` | any failure: see §4 |
| On-demand ledger check | `python -m app.cli verify-ledger` (exit 1 if broken) or `GET /v1/ledger/verify` | `valid: true` | `valid: false` |
| Personal-data erasure (REQ-16) | `PERSONAL_DATA_ERASED` ledger entries; `record_key.destroyed_at` | appears after each event's retention | an event past `erasure_due_at` still has a key |
| Worker step failures | worker log `… failed` (gap sweep, reminders, erasure, ledger check, dispatch) | absent | repeated: read the traceback |

Logs are structured JSON: `sudo docker compose logs -f api worker` on the VM.

## 2. Alerts

All alerts go to webhooks registered with `POST /v1/webhooks` (admin keys only), signed with
HMAC-SHA256, retried with backoff (D-15):

| Notification type | When | Requirement |
|---|---|---|
| `flag_notification` | a breach or a monitoring gap is flagged | REQ-04, REQ-02 |
| `reminder` | 50 %, 80 %, 100 % of an Article 73 deadline window | REQ-11 |
| `ledger_alert` (`severity: critical`) | the first failing scheduled ledger check of an incident | REQ-06, Model 5 Ex-5 |

Register an operations webhook that subscribes to `ledger_alert`; without one the alert is only
logged. There is no monitoring dashboard: Phase 2 §6.4 allowed dropping it in favour of alerts, and
that reduction still needs the pod's agreement (see [phase5-progress.md](phase5-progress.md)).

## 3. Backups

**Current state.** The GCP test deployment keeps PostgreSQL in a Docker volume on the VM's
persistent disk. There is **no scheduled backup**: losing or deleting the VM loses the data.
The Phase 2 plan (Lightsail's 7-day point-in-time restore) no longer applies after the move to GCP.

**Recommended (needs approval; nothing below has been run):**

1. A daily snapshot schedule on the VM's disk, kept 7 days:
   ```bash
   gcloud compute resource-policies create snapshot-schedule compliance-daily \
       --region us-central1 --daily-schedule --start-time 03:00 --max-retention-days 7
   gcloud compute disks add-resource-policies compliance-monitor \
       --zone us-central1-a --resource-policies compliance-daily
   ```
2. Or a logical dump, copied off the VM:
   ```bash
   sudo docker compose exec -T db pg_dump -U compliance -Fc compliance > compliance-$(date +%F).dump
   ```

**Back up `RECORD_KEY_MASTER_KEY` separately** (from `/opt/compliance-monitor/deploy/gcp/.env`),
in a secret store, not next to the database backups. Without it every sealed rationale, evidence
text, report and note is unreadable, in the live database and in every backup.

**Backups and erasure (REQ-16).** A backup taken before an event's erasure still holds that
event's wrapped key, so restoring it would make the personal data readable again. Keep backup
retention short (7 days above) and never restore a backup older than the newest
`PERSONAL_DATA_ERASED` entry without re-running erasure. The worker does re-erase on its next
pass, because it erases every event whose erasure is due and whose key still exists.

## 4. Recovery

**Ledger check failed (`ledger_alert`).**
1. Don't change anything in the database; take a snapshot or dump first.
2. `python -m app.cli verify-ledger` and `GET /v1/ledger/verify` give `first_mismatch`.
3. Export the ledger (`GET /v1/ledger/export`) and re-verify it offline with
   `scripts/verify_ledger_export.py` to confirm the database copy is what changed.
4. Compare the head with the last passing `ledger_check` row. A missing tail means entries were
   removed from the end (D-14, D-27).
5. Restore from the last snapshot whose ledger verifies, then re-run erasure (§3).

**Restoring a backup.**
1. Stop the stack: `sudo docker compose stop api worker`.
2. Restore the disk snapshot (new disk from the snapshot, attached in place of the old one), or
   `pg_restore --clean` the dump into the `db` container.
3. Make sure `.env` has the **same** `RECORD_KEY_MASTER_KEY` as when the backup was taken.
4. Start the stack. The API runs `alembic upgrade head` on start.
5. Check `/healthz`, run `python -m app.cli verify-ledger`, and confirm `GET /v1/meta`.

## 5. Rolling out a release

`deploy/gcp/deploy.sh` uploads the committed `HEAD`, rebuilds the image and restarts the stack;
migrations run when the API starts. Before a release that contains migrations:

1. Take a disk snapshot (rollback point).
2. Deploy from a clean checkout of the commit to release.
3. Verify:
   - `GET /healthz` is `200` with `worker: ok`;
   - `GET /v1/meta` lists the expected phases;
   - `GET /openapi.json` lists the expected routes;
   - `python -m app.cli verify-ledger` passes;
   - the contract suite (or the hosted tester at `/tester`) passes against the deployment.
4. **Rollback:** redeploy the previous commit. If a migration can't be downgraded (for example
   `0012` refuses once a key has been destroyed), restore the snapshot from step 1 instead.

Releases that add `RECORD_KEY_MASTER_KEY` (migration `0012`): `remote-up.sh` creates it in `.env`
before the API starts. Copy it to the secret store right after the first such deploy, and reset
the test environment (`POST /v1/test/reset`) so no ledger entry from before `0012` holds personal
text in clear.
