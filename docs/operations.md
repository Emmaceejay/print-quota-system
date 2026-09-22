# Operations runbook

## Rolling out safely

Do not wrap every queue at once. The sequence that avoids surprises:

1. **Install, seed nobody.** `sudo ./scripts/install.sh`, then
   `quotactl db init --admin <you>`. Nothing is enforced yet: the wrapper is
   installed but no queue points at it.
2. **Register printers with their real device URIs.**
   `quotactl printer add <queue> --device-uri <uri> --mono 2 --color 10`.
   Get the URI from `lpstat -v`.
3. **Create users with a generous quota** (or a very large one) so the first
   week only *measures*. `quotactl user add <name> --quota 100000`.
4. **Wrap one low-risk queue.** `sudo ./scripts/register_backend.sh <queue>`
   Print a test page. Confirm with `quotactl usage --days 1` and
   `journalctl -u cups -n 50`.
5. **Compare estimate to actual for a few days.** In the reports, the
   estimated and actual page columns should track closely. Large gaps point
   at a driver that recomposes jobs — see below.
6. **Lower quotas to real values**, then wrap the remaining queues.
7. **Turn on `Require valid-user`** in `cupsd.conf` (see
   [architecture.md](architecture.md#trusting-the-user-name)) before you
   depend on the numbers.

Undo for any queue: `sudo ./scripts/register_backend.sh <queue> --undo`.

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

### Balances do not move after printing

The accounting daemon is not seeing `page_log`:

```bash
systemctl status quota-accounting
sudo -u printquota head /var/log/cups/page_log     # must be readable
grep PageLogFormat /etc/cups/cupsd.conf            # leave it at the default
```

The daemon expects the default `PageLogFormat`. If you have customised it,
restore the default or adjust `_LINE_RE` in
`src/printquota/accounting/daemon.py`.

### Estimates are consistently wrong

`pdfinfo` gives an exact count for PDF. Windows drivers that send PCL or
raw data give a weaker estimate (form feeds or a fallback of one page). The
daemon still corrects the charge from `page_log`, so billing stays right —
only the pre-flight *block* is approximate. If pre-flight accuracy matters,
point clients at the IPP Everywhere / driverless queue so jobs arrive as
PDF.

### A user was charged for a job that jammed

Refund it from the CLI's reconciliation path or by resetting the user:

```bash
quotactl user show <name>        # find the job
quotactl user set-quota <name> <higher>   # or
quotactl user reset <name>
```

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
