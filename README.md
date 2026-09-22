# printquota

A free, self-hosted print quota, policy and accounting system for CUPS on
Ubuntu Server. It does the parts of PaperCut NG that matter for a single
organisation — per-user quotas, department budgets, cost tracking, print
policies, reporting and a self-service portal — with no licence, no user
cap, and a codebase one engineer can hold in their head.

## Why a custom backend is necessary

CUPS records usage in `page_log`, but it does not enforce anything. Blocking
a job before it reaches the printer requires a **wrapper backend**: a
program CUPS invokes in place of the real device backend, which decides
whether to hand the job on. printquota is built around that:

```
client --IPP--> CUPS queue (DeviceURI = quota:socket://10.0.0.5:9100)
                    |
                    v
       quota backend: identify user -> estimate pages -> evaluate policy
                      -> check quota -> ALLOW or CANCEL
                    |
            ALLOW: exec the real backend (socket/ipp/usb/...)
                    |
              CUPS writes page_log
                    |
       accounting daemon: read the authoritative page count,
       reconcile the balance, compute cost, raise alerts
```

The estimate made before printing is deliberately provisional. Drivers
compose, scale and duplex jobs after the backend has seen them, so the
daemon corrects the charge from `page_log` afterwards. Both halves are
needed; neither is sufficient alone.

## Features

- **Per-user quotas** in pages, over a rolling period (30 days by default).
- **Group / department shared budgets**, enforced *in addition to* the
  member's own quota — a job must fit both.
- **Cost tracking** that is mono/colour aware with a per-printer duplex
  discount, so reports show money as well as pages.
- **Print policies**: block colour, force duplex, cap pages or copies per
  job, block file types, deny a printer. Scoped to a user, group, printer
  or globally; the most specific scope wins.
- **Admin web console**: users, groups, printers, policies, reports, CSV
  export, audit log.
- **Self-service portal** so users check their own balance and history
  instead of asking you.
- **Alerts** on low balance and over quota, by email or webhook, with a
  cooldown so nobody gets spammed.
- **Admin audit log** covering both the console and the CLI.
- **Admin CLI** (`quotactl`) — everything the console does is scriptable.
- **AD/LDAP hook** for authentication and group mapping.

## Requirements

- Ubuntu Server 22.04 LTS or newer (tested on a Hyper-V guest)
- CUPS, `poppler-utils` (for `pdfinfo`), Python 3.11+
- SQLite by default; PostgreSQL by changing one setting

## Install

```bash
git clone <your-repo-url> print-quota-system
cd print-quota-system
sudo ./scripts/install.sh
```

Then:

```bash
# 1. create your administrator
sudo /opt/printquota/bin/quotactl db init --admin ceejay

# 2. register a printer and its cost model
sudo /opt/printquota/bin/quotactl printer add hp-mono \
     --device-uri socket://10.0.0.5:9100 --mono 2 --color 10 \
     --duplex --duplex-discount 0.5

# 3. put the queue behind the quota backend
sudo ./scripts/register_backend.sh hp-mono

# 4. give someone a quota
sudo /opt/printquota/bin/quotactl user add ada --quota 500 --group finance
```

The console is at `http://<server>:8080/`. Users sign in there too and land
on their own balance page.

> **Security prerequisite.** Enforcement trusts
> `job-originating-user-name`. Configure CUPS to require authenticated
> users (`Require valid-user`) before relying on it — see
> [docs/architecture.md](docs/architecture.md#trusting-the-user-name).

## Everyday commands

```bash
quotactl user list                        # balances at a glance
quotactl user show ada                    # one user and their recent jobs
quotactl user set-quota ada 800
quotactl user reset ada                   # zero usage, restart the period
quotactl group add finance --budget 5000
quotactl policy add --scope group --value finance --rule block_color --rule-value true
quotactl policy add --scope global --rule max_pages_per_job --rule-value 100
quotactl usage --days 30 --csv /tmp/usage.csv
quotactl audit
```

## Configuration

Everything lives in `/etc/printquota/settings.yaml`, and any value can be
overridden by a `PRINTQUOTA_*` environment variable (see `.env.example`).
Nothing operational is hardcoded in the source.

Key settings:

| Setting | Meaning |
|---|---|
| `quota.default_limit` | Pages a new user gets per period |
| `quota.period_days` | Length of the rolling period |
| `quota.enforcement` | `strict` (deny) or `soft` (allow and record the overrun) |
| `quota.enforce_group_budget` | Also check the group's shared pool |
| `printing.page_log` | Path to the CUPS page log |
| `printing.real_backend_dir` | Where the real CUPS backends live |
| `alerts.channel` | `email`, `webhook` or `none` |
| `api.auth_backend` | `local`, `pam` or `ldap` |

## Services

| Unit | Role |
|---|---|
| `quota-accounting.service` | Reconciles `page_log` against balances |
| `quota-api.service` | Web console and portal |
| `quota-reset.timer` | Rolls elapsed quota periods daily |
| `quota-backup.timer` | Nightly database backup |

```bash
journalctl -u quota-accounting -f     # every reconciliation
journalctl -u cups -f                 # every allow/deny from the backend
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
PRINTQUOTA_DB_URL=sqlite:///./dev.db .venv/bin/alembic upgrade head
.venv/bin/pytest                      # unit + integration suite
PRINTQUOTA_DB_URL=sqlite:///./dev.db .venv/bin/python scripts/seed_demo.py
PRINTQUOTA_DB_URL=sqlite:///./dev.db .venv/bin/uvicorn printquota.api.main:app --reload
```

The integration tests drive the CUPS backend exactly as CUPS does — with a
stand-in for the real device backend — so the allow/deny path is verified
without a printer.

## Documentation

- [docs/architecture.md](docs/architecture.md) — design decisions, data
  model, enforcement mechanics, security model
- [docs/operations.md](docs/operations.md) — day-two runbook: rollout,
  troubleshooting, backup and restore, upgrades

## Licence

MIT — see [LICENSE](LICENSE).
