# Operations runbook

## Rolling out safely

Follow the [setup guide](setup.md). For a low-risk rollout, don't enforce every queue
at once:

1. Give users a very large quota at first, so the first week only *measures* usage.
2. Turn on enforcement for one low-risk queue and print a test page. Confirm it in
   **Reports** and with `journalctl -u cups -n 50`.
3. Check for a few days that page counts are exact (setup guide, step 9).
4. Lower quotas to real values, then turn on the remaining queues.
5. Secure the deployment (setup guide, step 10) before relying on the numbers.

To stop enforcing a queue, click **Turn off** in **Printers & queues**, or run
`sudo ./scripts/register_backend.sh <queue> --undo`.

## Troubleshooting

### Jobs disappear and the user sees no reason

Check the backend decision:

```bash
journalctl -u cups -n 100 | grep -i printquota
quotactl usage --days 1 --status denied
```

`denial_reason` on the job row is the authoritative answer.

### The queue stopped

A wrapper should never stop a queue on a denial — it returns CANCEL. A
stopped queue means the device URI is wrong (`CUPS_BACKEND_STOP`) or the
real backend itself failed. Check:

```bash
lpstat -v <queue>                      # should read quota:<real-uri>
ls -l /usr/lib/cups/backend/quota      # must be 0700 root:root
cupsenable <queue>
```

### Jobs are held instead of printing

`CUPS_BACKEND_HOLD` means the wrapper could not reach the database. Check
`quota-accounting`'s journal and the permissions on
`/var/lib/printquota/printquota.db` (the backend runs as root; the services
run as `printquota`).

### Balances look wrong after printing

Charges are made **before** printing, from the page count of the submitted
document, and corrected afterwards from CUPS's `page_log` when there is one.
Many drivers never report pages, so there may be no `page_log` at all. That
is normal (see [known issues](known-issues.md#there-is-no-page_log-file)).

To see how recent jobs were counted:

```bash
sudo journalctl -u cups -n 50 --no-pager | grep -o "method='[^']*'\|pages=[0-9]*"
```

`spool:postscript` and `spool:pdf` are exact. A method ending in `fallback`
means the document couldn't be counted: check the client's driver
([known issues](known-issues.md#which-windows-driver-to-use)).

If `page_log` exists, the daemon expects the default `PageLogFormat`. Leave
it at the default in `cupsd.conf`.

### A user was charged for a job that didn't print

There is no per-job refund. Give the pages back through the user's quota,
in **Users** → the user:

- raise their quota by the number of pages, or
- click **Reset usage and restart period**.

Both are recorded in the audit log.

## Backup and restore

The nightly timer writes to `/var/backups/printquota` using SQLite's online
backup API (safe while the services run):

```bash
systemctl list-timers quota-backup.timer
sudo /opt/printquota/share/scripts/backup.sh /var/backups/printquota
```

Restore:

```bash
sudo systemctl stop quota-api quota-accounting
sudo gunzip -c /var/backups/printquota/printquota-<stamp>.db.gz \
     > /var/lib/printquota/printquota.db
sudo chown printquota:printquota /var/lib/printquota/printquota.db
sudo systemctl start quota-accounting quota-api
```

Losing this database loses every balance and all usage history. Copy the
backups off the VM as well.

## Upgrades

```bash
cd /path/to/print-quota-system
git pull
sudo ./scripts/install.sh          # idempotent: re-installs and migrates
```

`install.sh` keeps your existing `settings.yaml` and `env`, reinstalls the
package, runs `alembic upgrade head` and restarts the units. Schema changes
always ship as an Alembic migration — never edit the schema by hand.

## Monitoring

- `GET /healthz` — liveness plus a database round-trip (503 when degraded).
- `GET /admin/api/summary?days=30` — JSON totals for a dashboard (requires
  an admin session).
- Alert on `quota-accounting.service` not being active: if it stops, jobs
  still print and are charged the estimate, but the corrections stop.
