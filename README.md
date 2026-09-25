# printquota

**A self-hosted print quota, policy and accounting system for CUPS on Ubuntu Server.**

printquota covers the parts of PaperCut NG that a single organisation needs:
per-user page quotas, department budgets, cost tracking, print policies,
reporting, an admin console and a self-service portal. It has no licence
fee and no user cap, and the codebase is small enough for one engineer to
understand in full.

| | |
|---|---|
| **Version** | 0.2.4 (see [CHANGELOG.md](CHANGELOG.md)) |
| **Language** | Python 3.10+ |
| **Platform** | Ubuntu Server 22.04 LTS / 24.04 LTS with CUPS |
| **Datastore** | SQLite (default) or PostgreSQL |
| **Licence** | MIT |

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Features](#2-features)
3. [Repository layout](#3-repository-layout)
4. [Requirements](#4-requirements)
5. [Installation](#5-installation)
6. [First-time setup and safe rollout](#6-first-time-setup-and-safe-rollout)
7. [Configuration reference](#7-configuration-reference)
8. [Quotas, periods and group budgets](#8-quotas-periods-and-group-budgets)
9. [Print policies](#9-print-policies)
10. [Page estimation and the cost model](#10-page-estimation-and-the-cost-model)
11. [The `quotactl` command-line tool](#11-the-quotactl-command-line-tool)
12. [Web console and self-service portal](#12-web-console-and-self-service-portal)
13. [Authentication](#13-authentication)
14. [Alerts](#14-alerts)
15. [Services and timers](#15-services-and-timers)
16. [Data model](#16-data-model)
17. [Logging and monitoring](#17-logging-and-monitoring)
18. [Backup and restore](#18-backup-and-restore)
19. [Upgrades and database migrations](#19-upgrades-and-database-migrations)
20. [Security model](#20-security-model)
21. [Troubleshooting](#21-troubleshooting)
22. [Development and testing](#22-development-and-testing)
23. [Known limitations and gaps](#23-known-limitations-and-gaps)
24. [Roadmap](#24-roadmap)
25. [Further documentation](#25-further-documentation)
26. [Licence](#26-licence)

---

## 1. How it works

CUPS records usage in `page_log`, but it does not enforce anything. To
block a job before it reaches the printer you need a **wrapper backend**:
a program that CUPS runs in place of the real device backend, and which
decides whether to pass the job on. printquota is built around this.

```
 client ──IPP──▶ CUPS queue   DeviceURI = quota:socket://10.0.0.5:9100
                     │
                     ▼
   ┌──────────── /usr/lib/cups/backend/quota  (runs as root) ─────────────┐
   │ 1. identify user     job-originating-user-name (argv[2])             │
   │ 2. estimate pages    pdfinfo / %%Pages / form feeds / line count     │
   │                      × copies ÷ number-up                            │
   │ 3. evaluate policy   deny_printer, block_color, max_pages, ...       │
   │ 4. check quota       user balance, then the group's shared pool      │
   │ 5. debit estimate    so back-to-back jobs can't overspend a balance  │
   └──────────────┬───────────────────────────────────┬───────────────────┘
          ALLOW   │                                    │  DENY
                  ▼                                    ▼
   run real backend (socket/ipp/usb/…)        exit CUPS_BACKEND_CANCEL
   with DEVICE_URI rewritten                  (job cancelled, queue stays up)
                  │
                  ▼
      CUPS writes one page_log line per page
                  │
                  ▼
   ┌──────── quota-accounting daemon (every 15 s) ────────┐
   │ read new page_log lines → total pages per job        │
   │ charge the difference (actual − estimate) → cost     │
   │ → low-balance / over-quota alerts                    │
   └──────────────────────────────────────────────────────┘
```

**Why both halves are needed.** The estimate made before printing is only
provisional. The filter chain can add banner pages, drivers can reflow or
duplex the job, and a job can fail halfway. `page_log` is the only
reliable count, but it exists only after the paper is out, which is too
late to block anything. So printquota charges the estimate first, which
stops a burst of jobs from all passing against the same balance, and
corrects it from `page_log` afterwards.

**Backend exit codes.** These decide what CUPS does with the queue:

| Situation | Exit code | Effect |
|---|---|---|
| Job allowed | Real backend's own code | Job prints normally |
| Job denied (policy or quota) | `CUPS_BACKEND_CANCEL` (5) | That job is cancelled; **the queue stays enabled** |
| Database unreachable / backend crash | `CUPS_BACKEND_HOLD` (3) | Job is held in the queue for an administrator to release. Nothing is lost and nothing prints unaccounted |
| Bad `quota:` device URI or real backend missing | `CUPS_BACKEND_STOP` (4) | Queue is stopped, so jobs cannot print for free |
| Wrong argument count | `CUPS_BACKEND_FAILED` (1) | Standard CUPS failure |

A denial never returns `FAILED`, because that disables the queue. One user
running out of quota would then take the printer offline for everyone.

---

## 2. Features

- **Per-user quotas**, counted in pages over a rolling period (30 days by default).
- **Group / department shared budgets.** These are enforced *in addition to*
  each member's own quota, so a job must fit both.
- **Cost tracking.** Separate mono and colour rates, plus a per-printer duplex
  discount, so reports show money as well as pages.
- **Print policies**: block colour, force duplex, cap pages or copies per
  job, block file types, deny a printer. Each policy is scoped to a user,
  group, printer or globally, and the most specific scope wins.
- **Strict or soft enforcement.** Strict mode denies the job. Soft mode lets it
  through and lets the balance go negative.
- **Everything in the browser after installation.** A setup wizard creates the
  first administrator, and the admin console covers users (including CSV
  import/export and bulk actions), administrators, groups, CUPS queues and
  quota enforcement, cost models, policies, settings (quotas, costs, alerts
  and SMTP), reports with CSV export, and the audit log.
- **Self-service portal**, where users check their own balance and history
  and can download it as CSV. It also has a JSON endpoint for client agents.
- **Alerts** for low balance and over-quota, sent by email or webhook, with a
  per-user cooldown.
- **Admin audit log**, recording every privileged action from both the console and the CLI.
- **Optional admin CLI** (`quotactl`) for scripting, automation and recovery.
- **Pluggable authentication**: local bcrypt passwords, PAM, or AD/LDAP.
- **Hardened systemd units**, a reversible queue-wrapping script, and nightly
  backups.

---

## 3. Repository layout

```
print-quota-system/
├── config/settings.yaml          Default configuration (copied to /etc/printquota/)
├── .env.example                  Example PRINTQUOTA_* environment overrides
├── alembic.ini                   Alembic config (DB URL is read from settings, not here)
├── pyproject.toml                Package metadata, dependencies, console scripts
├── scripts/
│   ├── install.sh                Idempotent installer / upgrader (run as root)
│   ├── register_backend.sh       Install the CUPS backend; wrap / unwrap a queue
│   ├── backup.sh                 Online SQLite backup or pg_dump, keeps last N
│   └── seed_demo.py              Populate a demo database for evaluation
├── systemd/                      Service and timer units (see §15)
├── docs/
│   ├── architecture.md           Design decisions, enforcement mechanics, security
│   └── operations.md             Day-two runbook
├── src/printquota/
│   ├── backend/quota_backend.py  The CUPS wrapper backend (pre-flight enforcement)
│   ├── accounting/
│   │   ├── pages.py              Page estimation from the spool file
│   │   ├── cost.py               Pure cost arithmetic
│   │   └── daemon.py             page_log reader and reconciler
│   ├── policies/engine.py        Pure policy + quota evaluation (no I/O)
│   ├── services/
│   │   ├── quota.py              The ONLY code that changes balances
│   │   ├── accounts.py           Account deletion (jobs + policies)
│   │   ├── cups_queues.py        lpstat / lpadmin wrapper for the console
│   │   ├── user_import.py        CSV user import / export
│   │   ├── runtime_settings.py   Console-editable settings
│   │   └── audit.py              Audit-log writer
│   ├── notifications/alerts.py   Email / webhook alerts with cooldown
│   ├── api/                      FastAPI app: auth, admin console, portal, templates
│   ├── cli/quotactl.py           Admin CLI
│   ├── cli/reset.py              Entry point for the daily period-reset timer
│   ├── core/                     Config loading, logging, exceptions
│   └── db/                       SQLAlchemy models, session handling, Alembic migrations
└── tests/                        unit/ and integration/ pytest suites (206 tests)
```

**Console scripts** installed by the package:

| Command | Entry point | Purpose |
|---|---|---|
| `quotactl` | `printquota.cli.quotactl:cli` | Admin CLI |
| `printquota-accounting` | `printquota.accounting.daemon:main` | Accounting daemon |
| `printquota-reset` | `printquota.cli.reset:main` | Roll elapsed quota periods |

The backend itself runs as `python -m printquota.backend.quota_backend`,
through a small shell shim installed at `/usr/lib/cups/backend/quota`.

---

## 4. Requirements

| Requirement | Notes |
|---|---|
| Ubuntu Server 22.04 LTS or newer | Tested on a Hyper-V guest |
| Python 3.10+ | The stock interpreter on 22.04 and 24.04, so no PPA is needed. `install.sh` checks this first |
| CUPS | The print server being controlled |
| `poppler-utils` | Provides `pdfinfo` for exact PDF page counts |
| `build-essential`, `python3-dev`, `libsystemd-dev`, `pkg-config` | Needed to build `systemd-python` (optional journald logging) |
| System `python3-yaml` | Used by `scripts/backup.sh`, which runs under the system interpreter rather than the virtualenv. Present on stock Ubuntu Server |
| PostgreSQL + a driver (optional) | e.g. `psycopg`. Install it into `/opt/printquota` if you use PostgreSQL |

**Python dependencies** (from `pyproject.toml`): SQLAlchemy ≥ 2.0, Alembic ≥
1.13, Click ≥ 8.1, PyYAML ≥ 6.0, FastAPI ≥ 0.110, Uvicorn[standard] ≥ 0.29,
Jinja2 ≥ 3.1, python-multipart ≥ 0.0.9, itsdangerous ≥ 2.1, bcrypt ≥ 4.0,
Pydantic ≥ 2.6.
Extras: `dev` (pytest, httpx) and `ldap` (ldap3).

---

## 5. Installation

```bash
git clone <your-repo-url> print-quota-system
cd print-quota-system
chmod +x scripts/*.sh scripts/seed_demo.py   # needed if the checkout lost the exec bit (e.g. copied via Windows)
sudo ./scripts/install.sh
```

### What `install.sh` does

The installer is idempotent, so running it again performs an upgrade.

1. Checks it is running as root and that `python3` is 3.10 or newer.
2. Installs any missing OS packages: `python3 python3-venv python3-dev
   build-essential poppler-utils cups libsystemd-dev pkg-config`. If they are
   all present, this step is skipped. An `apt-get update` error (e.g. a mirror
   mid-sync) is only a warning.
3. Creates the system user `printquota` (no login shell) and adds it to
   the `lp` group (to read `page_log`) and the `lpadmin` group (so the web
   console can manage CUPS queues).
4. Creates `/var/lib/printquota` (0750, owned by `printquota`) and
   `/etc/printquota` (0750, `root:printquota`).
5. Builds a virtualenv at `/opt/printquota` and installs the package into
   it, plus `systemd-python` if it builds.
6. Copies `config/settings.yaml` to `/etc/printquota/settings.yaml` **only if
   that file does not already exist**, pointing `database.url` at
   `/var/lib/printquota/printquota.db`.
7. Generates `/etc/printquota/env` (0640) with `PRINTQUOTA_CONFIG` and a
   random `PRINTQUOTA_SECRET_KEY`, **only if it does not already exist**, and
   adds a one-time `PRINTQUOTA_SETUP_TOKEN` if there isn't one.
8. Copies `backup.sh` and the docs to `/opt/printquota/share`, then runs
   `alembic upgrade head` against the configured database.
9. Installs the CUPS backend shim (`register_backend.sh --install-only`).
10. Installs the systemd units, enables `quota-accounting.service`,
    `quota-api.service`, `quota-reset.timer` and `quota-backup.timer`, and
    restarts the services so that an upgrade takes effect.
11. Prints the web console address and the **setup link** (see §6).

It **does not** edit `cupsd.conf` and **does not** wrap any queue. Queues are
enforced from the web console (or with `register_backend.sh`).

Installer paths can be overridden with environment variables:
`PREFIX` (default `/opt/printquota`), `CONFIG_DIR` (`/etc/printquota`),
`STATE_DIR` (`/var/lib/printquota`), `LOG_USER` (`printquota`). The systemd
units hard-code the default paths, so if you change them, edit the units too.

### Installed file-system layout

| Path | Contents | Owner / mode |
|---|---|---|
| `/opt/printquota/` | Virtualenv with the package and console scripts | root |
| `/etc/printquota/settings.yaml` | Main configuration | `root:printquota` 0640 |
| `/etc/printquota/env` | `PRINTQUOTA_CONFIG`, `PRINTQUOTA_SECRET_KEY`, `PRINTQUOTA_SETUP_TOKEN` (and any other overrides) | `root:printquota` 0640 |
| `/var/lib/printquota/printquota.db` | SQLite database | `printquota` |
| `/var/lib/printquota/accounting.state` | Accounting daemon's `page_log` cursor (inode + offset) | `printquota` |
| `/opt/printquota/share/` | `alembic.ini`, `scripts/backup.sh`, `docs/` | root |
| `/usr/lib/cups/backend/quota` | Backend shim | `root:root` **0700** |
| `/etc/systemd/system/quota-*.{service,timer}` | Units | root 0644 |
| `/var/backups/printquota/` | Backups (see §18) | root |

---

## 6. First-time setup and safe rollout

After `install.sh` finishes, **everything is done in the web browser**. You
don't need the command line again for day-to-day administration.

### Step 1: create the first administrator (setup wizard)

The installer ends by printing a link like this one:

```
http://10.0.0.20:8080/setup?token=Qm3v…
```

Open it, then enter a username, full name, email (optional, used for
alerts) and a password of at least 8 characters, typed twice. Click
**Create administrator and sign in**. You're signed straight in to the
dashboard.

- The token is a one-time secret, so nobody else on the network can claim
  the server before you do. If you've lost the link, the token is the
  `PRINTQUOTA_SETUP_TOKEN` line in `/etc/printquota/env` on the server.
  Open `http://<server>:8080/setup` and paste it in.
- Until an administrator exists, the sign-in page sends you to `/setup`.
  Once one exists, `/setup` returns 404 permanently, even with the token.
- If you enter the username of an existing print account, that account is
  promoted to administrator and keeps its quota.
- With no token configured (e.g. a development checkout), setup is only
  allowed from the server itself (`http://localhost:8080/setup`).

### Step 2: follow the "Getting started" checklist

The dashboard shows a checklist until the essentials are done:

| Step | Where | What you do |
|---|---|---|
| Create your departments | **Groups** | Add groups, each with an optional shared page budget |
| Add your users | **Users → Import users…** or the **Add a user** form | Import many from a CSV, or add them one by one |
| Turn on quota enforcement | **Printers & queues** | Click **Turn on** for each CUPS queue that should be metered |
| Set up alerts | **Settings** | Enter your SMTP server (or a webhook), then **Send test alert** |

The checklist also reminds you to require logins for printing in CUPS
(§20). That is the one step that has to be done in `cupsd.conf` on the
server.

### Managing users in the browser

**Add one user or another administrator.** On **Users**, fill in *Add a user or
administrator*. Tick **Administrator** to give console access. For
administrators the password is required, because an admin without a
password could never sign in.

**Import many users.** Go to **Users → Import users…**:

1. Upload a CSV file (save your spreadsheet as CSV, up to 2 MB / 5,000 rows)
   or paste rows into the box. Click **Download a template** for the exact
   format.
2. Choose the options: default quota and alert threshold for rows that don't
   specify one, a default group, **Create groups that do not exist yet**, and
   **Update users who already exist**.
3. Click **Preview import**. Every row is listed as *create*, *update*,
   *skip* or *error*, with the reason. **Nothing is saved yet.**
4. Click **Apply**. Rows with errors are left out and everything else is
   saved in one step. The whole import is recorded in the audit log.

| Column | Meaning |
|---|---|
| `username` | Required. The logon name people print as, e.g. `j.doe`: letters, digits, `. _ @ -`. No `DOMAIN\` prefix (see §8) |
| `display_name` | Full name |
| `email` | Where alerts go |
| `group` | Group (department) name |
| `quota` | Pages per period (blank = the default you chose) |
| `threshold` | Low-balance alert level in pages |
| `password` | Portal password, at least 8 characters (blank = none, or keep the current one) |
| `admin` | `yes` to make the user an administrator |
| `active` | `no` to create the account disabled |

With a header row, the columns can be in any order and extra columns are
ignored; common alternative names such as `name`, `department` or
`quota_limit` are recognised. Without a header, the columns are read in the
order above, so a plain list of usernames (one per line) works. Comma,
semicolon and tab separators are all accepted. When updating existing users,
**blank cells leave a field unchanged**.

**Export users.** **Users → Export CSV** downloads every account in the same
format (never with passwords), ready to edit and re-import.

**Bulk actions.** On **Users**, filter by search text, group or status,
tick users (or the header box to tick all), choose an action and click
**Apply**:

| Action | Effect |
|---|---|
| Move to group | Sets the group of every selected user (or clears it) |
| Set quota to | Sets the page allowance |
| Reset usage and restart period | Sets usage to zero and starts a new period now |
| Enable / Disable | Disabled users can't print or sign in |
| Grant / Revoke admin rights | Gives or removes console access |
| Delete (with job history) | Needs the *I understand* tick; removes the accounts, their jobs and policies aimed at them |

You can never disable, demote or delete **your own** account this way. It is
skipped and the page tells you so.

**One user.** Click a username to edit their full name, email, group,
quota, alert threshold, active/admin flags and password (typed twice), to
reset their usage, or to delete them.

**Groups.** On **Groups** you can create a group, change its budget and
description, reset its pool usage, or delete it. Deleting a group keeps
its members' accounts (they become ungrouped) and removes policies aimed at
that group. The member count links to the filtered user list.

### Managing printers and quota enforcement in the browser

**Printers & queues** lists every queue CUPS knows about, merged with
printquota's cost model for each one:

- **Turn on** puts the queue behind the quota backend. printquota first
  registers the printer with its real device URI (at the default rates if
  it's new), then points the queue at `quota:<real-uri>`. If CUPS refuses,
  the registration is rolled back, so a half-configured queue is never left
  behind.
- **Turn off** (with the *confirm* tick) restores the real device URI. The
  queue keeps printing, but is no longer metered.
- **Edit costs** sets the mono and colour rates, the duplex discount,
  *supports duplex*, and *Accept jobs*. Unticking *Accept jobs* makes every
  job on that queue be denied.
- **Add a new printer to CUPS** creates a queue from a device URI
  (`ipp://…`, `ipps://…`, `socket://…:9100`, `lpd://…`, `usb://…`) with
  **IPP Everywhere** (driverless, recommended for network printers) or as a
  **raw** queue. It registers the costs and, if ticked, turns enforcement on
  straight away.

This works because `install.sh` puts the `printquota` service account in
CUPS's `lpadmin` group. If a button reports *Forbidden*, see §21. Queue
names and URIs are validated before CUPS is called, and nothing runs
through a shell.

### Changing settings in the browser

**Settings** edits quotas (default quota, period length, default alert
threshold, strict/soft enforcement, group budgets), costs (currency and
default rates) and alerts (on/off, email/webhook/none, cooldown, webhook URL,
SMTP server, port, STARTTLS, username, password, from address).

- Changes take effect immediately for new print jobs, and within 15 seconds
  in the accounting service. No restart is needed.
- Each field shows where its value comes from: *default*, *config file*,
  *set here* or *set by environment*. Fields set by an environment variable
  are locked, because the environment always wins (§7).
- **Use file value** removes a console value so that the file or default
  applies again.
- **Send test alert** sends a message to you through the saved settings and
  tells you whether it was delivered.
- The SMTP password is never shown back. Leave it blank to keep the current one.
- **Only what you change is saved, and a problem in one field never blocks the
  others.** Every valid change is saved. A field that can't be saved is
  outlined in red, keeps what you typed, and says what's wrong (e.g. a webhook
  address without `https://`). Fields you didn't touch are never re-checked.
- Numbers can be typed as `1,000`, `0,5` or `500.0`.
- Browser autofill is switched off on this page, so saved logins can't end up
  in the webhook or SMTP boxes.

The database location, session secret, sign-in backend, ports and CUPS paths
are shown read-only. They decide how the services start, so they stay in
`/etc/printquota/settings.yaml` or `/etc/printquota/env`.

### Setting or resetting an administrator password

These steps apply to the default `local` sign-in backend. With `pam` or
`ldap`, the password is the user's system or directory password, and it
can't be changed from printquota (see §13).

**In the browser (normal case):**

- **Your own or another user's password:** **Users** → click the user → type
  it in **New password** and **Confirm new password** → **Save**. Leave both
  blank to keep the current password.
- **Another administrator:** create one on **Users** with **Administrator**
  ticked, or tick **Admin** on an existing user's page (a password is required
  if they don't have one yet).

**Recovery when nobody can sign in** (the only case that needs the server
shell). Run this on the server. It sets a new password, restores admin
rights and re-enables the account, without touching any other data:

```bash
sudo /opt/printquota/bin/quotactl db init --admin ceejay
sudo /opt/printquota/bin/quotactl user enable ceejay    # only if the account was disabled
```

`quotactl user set-password <name>` also works. Both commands prompt for the
password. Avoid the `--password`/`--admin-password` flags for real
accounts, because they leave the password in shell history.

**Notes**

- Passwords set in the console must be at least 8 characters. Only the
  first 72 bytes are used.
- Every password change is recorded in the audit log (`user.update` with
  `password_changed`, `setup.admin`, `user.set_password` or `db.init`). The
  password itself is never logged.
- Changing a password **does not end sessions that are already signed in**.
  Sessions expire after `api.session_max_age` (default 8 hours). To sign
  everyone out now, change `PRINTQUOTA_SECRET_KEY` in `/etc/printquota/env`
  and run `sudo systemctl restart quota-api`.
- The console won't let you remove your own admin rights, or disable or delete
  your own account. Another administrator has to do that.

### Doing the same from the command line (optional)

Everything above can also be scripted with `quotactl` (§11):

```bash
sudo /opt/printquota/bin/quotactl db init --admin ceejay
sudo /opt/printquota/bin/quotactl group add finance --budget 5000
sudo /opt/printquota/bin/quotactl user add ada --quota 500 --group finance --email ada@example.com
sudo /opt/printquota/bin/quotactl printer add hp-mono --device-uri socket://10.0.0.5:9100 \
     --mono 2 --color 10 --duplex --duplex-discount 0.5
sudo ./scripts/register_backend.sh hp-mono
```

### What `register_backend.sh` does

The command-line equivalent of **Turn on** / **Turn off** on the Printers &
queues page, useful for scripting:

```bash
sudo ./scripts/register_backend.sh --install-only   # (re)install the backend shim only
sudo ./scripts/register_backend.sh <queue>          # wrap a queue
sudo ./scripts/register_backend.sh <queue> --undo   # restore the original device URI
```

When wrapping a queue, the script:

1. reads the current URI with `lpstat -v`;
2. installs the backend shim;
3. runs `quotactl printer add <queue> --device-uri <current>`, leaving any
   existing printer row and its cost model untouched;
4. runs `lpadmin -p <queue> -v quota:<current>`.

The original URI is embedded in the new one, so `--undo` just strips the
`quota:` prefix and needs no other state. The script refuses to wrap a
queue that is already wrapped.

> **Important:** every wrapped queue must have a row in `printers`, because
> `print_jobs.printer` is a foreign key. If the row is missing, recording the
> job fails and the backend returns **HOLD**. The console's **Turn on** button
> and `register_backend.sh` both create the row for you. If you wrap a queue
> by hand with `lpadmin`, register the printer first.

### Recommended rollout sequence

1. **Install, but enforce nothing yet.** The backend is installed, but no queue uses it.
2. **Set the costs** of each printer on **Printers & queues**.
3. **Give users a very large quota** (e.g. import them with a default quota
   of 100000) so that the first week only *measures* usage.
4. **Turn on enforcement for one low-risk queue.** Print a test page, then
   check **Reports** (or `journalctl -u cups -n 50` on the server).
5. **Compare estimated and actual pages for a few days** on the Reports
   page. A large gap points to a driver that recomposes jobs (see §10).
6. **Lower quotas to real values** (Users → select all → *Set quota to*), then
   turn on the remaining queues.
7. **Enable `Require valid-user` in `cupsd.conf`** before you rely on the
   numbers (see §20).

---

## 7. Configuration reference

### Precedence

Configuration is merged from four layers. Higher layers win:

1. `PRINTQUOTA_*` **environment variables** (usually set in `/etc/printquota/env`)
2. **Values saved on the console's Settings page** (the `app_settings`
   table). This applies only to the keys marked **Console** in the table
   below.
3. **YAML file**: the `--config` CLI option, else `$PRINTQUOTA_CONFIG`, else
   the first of `/etc/printquota/settings.yaml` or `./config/settings.yaml`
   that exists
4. **Built-in defaults** in `src/printquota/core/config.py`

If `PRINTQUOTA_CONFIG` or `--config` points at a missing file, loading
fails loudly instead of silently using the defaults. Environment values are
converted to the type of the default they replace. Booleans accept
`1/true/yes/on` and `0/false/no/off`.

**Console-editable settings** (quotas, costs, alerts, SMTP) take effect
without a restart. The CUPS backend starts fresh for every job, the web app
reloads them as soon as they are saved, and the accounting daemon re-reads
them within 15 seconds.

**File/environment-only settings** (database, secret key, `api.*`,
`logging.*`, the CUPS paths, the setup token) decide how a process starts
and reaches its database, so they can never come from the database.
Settings loaded from the file are cached per process, so **restart the
services after editing `settings.yaml` or `env`**:

```bash
sudo systemctl restart quota-accounting quota-api
```

### All settings

| Key | Default | Console | Description |
|---|---|---|---|
| `database.url` | `sqlite:///./printquota.db` (installer sets `sqlite:////var/lib/printquota/printquota.db`) | — | SQLAlchemy URL. Use `postgresql+psycopg://user:pass@host/db` for PostgreSQL |
| `database.echo` | `false` | — | Log every SQL statement (debugging only) |
| `quota.default_limit` | `500` | Console | Pages per period given to new users when none is specified |
| `quota.period_days` | `30` | Console | Length of the rolling quota period |
| `quota.default_low_balance_threshold` | `50` | Console | Remaining-pages level that triggers a low-balance alert, for new users |
| `quota.enforcement` | `strict` | Console | `strict` denies jobs that don't fit; `soft` allows them and lets balances go negative |
| `quota.enforce_group_budget` | `true` | Console | Also require a job to fit the group's shared pool |
| `printing.default_cost_per_page_mono` | `1.0` | Console | Fallback mono rate (see §10 on how fallbacks apply) |
| `printing.default_cost_per_page_color` | `5.0` | Console | Fallback colour rate |
| `printing.default_duplex_discount` | `0.0` | Console | Duplex discount used only when a job's printer has no row |
| `printing.currency` | `NGN` | Console | Label shown next to costs in the UI |
| `printing.real_backend_dir` | `/usr/lib/cups/backend` | — | Directory the wrapped real backend must live in (security check) |
| `printing.page_log` | `/var/log/cups/page_log` | — | CUPS page log the daemon reads (may legitimately not exist, see §10) |
| `printing.spool_dir` | `/var/spool/cups` | — | CUPS spool (`RequestRoot`) where submitted documents are counted before printing |
| `printing.estimator_timeout` | `15` | — | Seconds allowed for `pdfinfo` |
| `alerts.enabled` | `true` | Console | Master switch for alerts |
| `alerts.channel` | `email` | Console | `email`, `webhook` or `none` |
| `alerts.webhook_url` | `""` | Console | JSON POST target when the channel is `webhook` |
| `alerts.cooldown_hours` | `24` | Console | Minimum gap between two alerts of the same type to the same user (`0` disables it) |
| `alerts.smtp.host` | `""` | Console | SMTP server. Empty means no email is sent |
| `alerts.smtp.port` | `25` | Console | SMTP port |
| `alerts.smtp.user` / `password` | `""` | Console | SMTP credentials (login only happens if `user` is set) |
| `alerts.smtp.from_address` | `printquota@localhost` | Console | From address |
| `alerts.smtp.use_tls` | `false` | Console | Use STARTTLS |
| `api.host` / `api.port` | `0.0.0.0` / `8080` | — | Web app bind address. **Note:** `quota-api.service` passes `--host`/`--port` to uvicorn directly, so edit the unit to change them |
| `api.session_cookie` | `printquota_session` | — | Session cookie name |
| `api.session_max_age` | `28800` | — | Session lifetime in seconds (8 hours) |
| `api.auth_backend` | `local` | — | `local`, `pam` or `ldap` (see §13) |
| `api.cookie_secure` | `false` | — | *(not in the sample file)* Set `true` when the console is served over HTTPS so that the cookie is only sent over TLS |
| `logging.level` | `INFO` | — | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `logging.use_journald` | `true` | — | Log to journald if `systemd-python` is installed, otherwise stderr |
| `setup_token` | `""` | — | One-time token for the setup wizard (§6). Usually set as `PRINTQUOTA_SETUP_TOKEN` in `/etc/printquota/env` |
| `secret_key` | `""` | — | Signs session cookies. If it is empty, each process uses a random key, so every restart signs everyone out. The installer generates one |
| `ldap.*` | *(absent)* | — | AD/LDAP settings (see §13) |

### Environment variables

| Variable | Overrides |
|---|---|
| `PRINTQUOTA_CONFIG` | Path to the YAML file |
| `PRINTQUOTA_DB_URL` | `database.url` |
| `PRINTQUOTA_DB_ECHO` | `database.echo` |
| `PRINTQUOTA_SECRET_KEY` | `secret_key` |
| `PRINTQUOTA_LOG_LEVEL` | `logging.level` |
| `PRINTQUOTA_PAGE_LOG` | `printing.page_log` |
| `PRINTQUOTA_REAL_BACKEND_DIR` | `printing.real_backend_dir` |
| `PRINTQUOTA_SPOOL_DIR` | `printing.spool_dir` |
| `PRINTQUOTA_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` | `alerts.smtp.*` |
| `PRINTQUOTA_API_HOST` / `PRINTQUOTA_API_PORT` | `api.host` / `api.port` |
| `PRINTQUOTA_AUTH_BACKEND` | `api.auth_backend` |
| `PRINTQUOTA_SETUP_TOKEN` | `setup_token`: unlocks `/setup` while no administrator exists (generated by `install.sh`) |
| `PRINTQUOTA_STATE` | Accounting daemon cursor file (default `/var/lib/printquota/accounting.state`) |

Put secrets such as the SMTP password in `/etc/printquota/env` rather than in
`settings.yaml`. The backend shim sources that file too, but it only
exports `PRINTQUOTA_CONFIG` and `PRINTQUOTA_SECRET_KEY` to the backend.

---

## 8. Quotas, periods and group budgets

### Units

Quotas are counted in **pages**, meaning printed sides after copies and
N-up are applied. Cost is tracked in money alongside pages, for reporting
and chargeback, but **cost never decides whether a job may print**. Mixing
the two units in one decision would make denials hard for users to
understand.

### Rolling periods

- Each user (and each group) has its own `period_start` anchor.
- The period ends `quota.period_days` after the anchor.
- When the period has passed, the anchor moves forward **in whole windows**
  and `pages_used` resets to 0. A user who has not printed for three months
  keeps their original alignment, so periods don't drift apart across the
  organisation.
- Periods roll when either of these happens first:
  - the daily `quota-reset.timer` (00:30, or at the next boot if that run was missed), or
  - the next time the user's balance is loaded (a job, the portal, `/api/me`).
- `quotactl user reset <name>` (or the console button) sets usage to zero
  **and restarts the period from now**.

### Charging lifecycle of a job

| Stage | Where | What happens to the balance |
|---|---|---|
| Pre-flight allow | Backend → `authorize_job` | Estimated pages are **debited immediately** from the user and their group. `charged_pages = estimate` |
| Pre-flight deny | Backend → `authorize_job` | Nothing is charged, and the job's cost is 0 |
| Post-print | Daemon → `charge_job` | The balance changes by `actual − charged` (up or down). `actual_pages` and `cost` are set, status becomes `completed`, and `reconciled = true` |
| Replay of old log lines | Daemon | No-op: reconciled jobs are never charged again |

Balances never go below 0 when a credit is applied. In soft mode they can go
past the limit, so `remaining` can be negative.

### Group budgets

A group can have a shared pool (`shared_quota`), or none (`None` means
unlimited). When `quota.enforce_group_budget` is true, a job must fit
**both** the user's remaining quota **and** the group's remaining pool. The
individual allowance stops one person from using up the pool, and the pool
stops the department as a whole from overspending. Group usage is charged
and corrected together with user usage.

### Enforcement modes

| Mode | User over quota | Group over budget |
|---|---|---|
| `strict` | Denied: `quota exceeded: N page(s) requested, R remaining of L` | Denied: `group 'X' budget exceeded: …` |
| `soft` | Allowed, and the overrun is noted in the log | **Not checked** (soft mode skips all quota checks) |

Policies are enforced in both modes.

### Accounts that cannot print

- **Unknown user** (no matching account): denied, reason `no print account for this user`.
- **Disabled user** (`is_active = false`): denied, reason `print account is disabled`.
- **Ambiguous name** (two accounts differ only in capitals): denied, reason
  `'<name>' matches more than one print account (…)`.
- **Disabled printer** (`printers.is_active = false`): denied, reason `printer '<name>' is disabled`.

Users are **not** created automatically. Everyone who prints to a wrapped
queue needs an account.

### How the job's user name is matched to an account

Windows doesn't always send the bare logon name. The same person can arrive
as `j.doe`, `J.Doe`, `CORP\J.Doe` or, once the server is
domain-joined, `j.doe@corp.example.com`. As in Active Directory, these are all
one account (`services/identity.py`). For each form of the name (as sent,
then without the `DOMAIN\` prefix, then without the `@realm`), printquota
tries:

1. an **exact** match, so anything that matched before still matches;
2. a match **ignoring capital letters**, only if exactly one account
   qualifies. Two accounts that differ only in capitals are never guessed
   between: the job is refused as ambiguous.

The job is then charged to, recorded under, and checked against the policies
of the matched account. The name that was actually sent is logged
(`print user matched sent_as=… account=…`). Portal sign-in uses the same
matching.

**Create accounts with the bare logon name** (`j.doe`). The console,
CLI, setup wizard and CSV import refuse names containing `\` and names
that differ from an existing account only in capitals. In a CSV import, a
row for `J.Doe` updates the existing `j.doe`.

Matching ignores the domain, so on a network with non-domain PCs a *local*
Windows account with the same short name counts against the domain user.
Requiring a login in CUPS (§20) is what prevents printing under someone
else's name.

---

## 9. Print policies

A policy is one rule attached to a scope.

### Rule types

| Rule | `rule_value` | Effect |
|---|---|---|
| `deny_printer` | `true` / `false` | Deny every job from the matched scope. Usually scoped to a user or group, e.g. "the reception group may not use `color-mfp`" |
| `block_color` | `true` / `false` | Deny colour jobs |
| `block_filetype` | Comma-separated extensions, e.g. `exe,zip` | Deny jobs whose spool file name has one of these extensions (case- and dot-insensitive). See the limitation in §23 |
| `max_copies_per_job` | Positive integer | Deny jobs with more copies than this |
| `max_pages_per_job` | Positive integer | Deny jobs whose estimated total pages exceed this |
| `force_duplex` | `true` / `false` | **Modifies instead of denying:** adds `sides=two-sided-long-edge` to the options passed to the real backend, and charges the job as duplex |

The truthy values for boolean rules are `1`, `true`, `yes`, `on`, and the
empty string. Rules are validated when they are created (via the CLI or the
console). Unknown rule types stored in the database are ignored at
evaluation time, so a stray row can never break a running backend.

### Scopes and precedence

| Scope | `scope_value` | Matches when |
|---|---|---|
| `user` | username | The job's user is this user |
| `group` | group name | The job's user belongs to this group |
| `printer` | queue name | The job is for this queue |
| `global` | *(none)* | Always |

**The most specific scope wins for each rule type: user > group > printer
> global.** A more specific rule *replaces* a less specific one of the same
type instead of adding to it. For example, a global `max_pages_per_job=50`
plus a user-scoped `max_pages_per_job=200` gives that user a limit of 200.

### Evaluation order

1. `deny_printer`
2. `block_color`
3. `block_filetype`
4. `max_copies_per_job`
5. `max_pages_per_job`
6. `force_duplex` (never denies)
7. **Quota**: the user's balance, then the group pool

The first failing check decides the outcome. Policies are checked before
quota on purpose, so that a policy violation is reported as a policy
violation and not as a confusing balance message.

### Examples

```bash
quotactl policy add --scope global  --rule max_pages_per_job --rule-value 100
quotactl policy add --scope group   --value finance --rule block_color --rule-value true
quotactl policy add --scope printer --value hp-mono --rule force_duplex
quotactl policy add --scope user    --value ada --rule max_pages_per_job --rule-value 300
quotactl policy add --scope printer --value color-mfp --rule deny_printer  # nobody may use color-mfp…
quotactl policy add --scope group   --value design --rule deny_printer --rule-value false  # …except the design group
quotactl policy list
quotactl policy remove 4
```

> `deny_printer` applies to every printer in the chosen scope. Because each
> rule type has only one winner, you cannot currently say "group X may not use
> printer Y" with a single rule. Use a `printer` scope to deny everyone on
> that queue, and a `user`/`group` scope with `deny_printer=false` to exempt
> people.

---

## 10. Page estimation and the cost model

### Pre-flight estimation (`accounting/pages.py`, `accounting/cups_spool.py`)

Quotas are counted in **pages**, and a job that doesn't fit is refused
**before anything prints**. printquota never prints part of a job.

**What is counted.** On a queue with a driver (a PPD), CUPS converts each
job into the printer's own language (PCL XL, raster, …) *before* the backend
runs, and that data usually can't be counted. So the backend counts the
**documents the client submitted**, which CUPS keeps in its spool directory
(`printing.spool_dir`, default `/var/spool/cups`):

| Spool file | Used for |
|---|---|
| `d<job>-001`, `d<job>-002`, … | Each submitted document, counted as below. Gzip-compressed documents are decompressed first |
| `c<job>` | The job's IPP attributes: `copies`, `number-up` and `page-ranges` |

Only if those are missing or can't be counted does the backend fall back to
the data it received on stdin. If neither can be counted, it charges the
larger of the two guesses.

The format of each document is identified from its first bytes:

| Format | Detected by | Page count method |
|---|---|---|
| PDF | `%PDF` | `pdfinfo` (exact). Runs with an argument list and a timeout |
| PostScript (e.g. Windows **Microsoft PS Class Driver**) | `%!PS` / `%!PS-Adobe` | The last positive `%%Pages:` value (the trailer beats a header placeholder), else the number of `%%Page:` markers |
| Apple raster (AirPrint) | `UNIRAST` | Page count stored in the header (exact) |
| PCL | `ESC%-12345X` or `ESC E` | Number of form feeds (PCL XL has none, so it falls back) |
| Plain text | ≥ 90 % printable bytes | Lines ÷ 60, rounded up |
| PWG raster, ESC/P, binary, unparseable, empty | — | **Falls back to 1 page per copy** |

Then **total = ⌈pages in range ÷ number-up⌉ × copies**, per document. If
estimation fails completely, the backend charges one page per copy and logs
`method=error`. The method used is logged with every decision (e.g.
`method='spool:postscript'`).

**After printing.** If CUPS writes a `page_log`, the accounting daemon
corrects each charge to the real count. It reads either per-page lines or
CUPS's `total` summary line. Many drivers never report pages, so CUPS never
creates `page_log`. That's normal: the daemon logs it once, and charges then
stay at the pre-print count. That count is exact for PDF, PostScript and
Apple raster.

**Windows clients:** add the printer with the **Microsoft PS Class Driver**
(PostScript), so jobs arrive in a format that can be counted exactly. Avoid
the PCL6 and XPS class drivers. CUPS can't convert their output, and it
can't be counted.

Job attributes are read from the CUPS options string:

- **Colour:** `print-color-mode` (anything except monochrome, auto-monochrome,
  bi-level or process-monochrome counts as colour); else `ColorModel`
  (gray/grayscale/monochrome/black/mono/bw count as mono); else a
  `color`/`colour` flag. The default is **mono**.
- **Duplex:** `sides=two-sided-*`, or a truthy `Duplex` value (not
  `None`/`DuplexNone`/false/off/0). The daemon also marks a job as duplex if
  `page_log` shows two-sided pages.
- **N-up:** `number-up`.

### Cost model (`accounting/cost.py`)

```
rate  = cost_per_page_color if colour else cost_per_page_mono
rate ×= (1 − duplex_discount)   if duplex and discount > 0     # discount must be 0 ≤ d < 1
cost  = round(pages × rate, 4)
```

- The rates come from the job's `printers` row. **A rate of `0` on the
  printer falls back to the configured default rate**, so you cannot make a
  queue free by setting its rate to 0 (see §23).
- If the printer has no row at all, all three values come from the
  `printing.default_*` settings.
- Denied jobs always cost 0.
- The pre-flight cost uses the estimate, and the daemon recomputes it from
  the actual page count.

---

## 11. The `quotactl` command-line tool

The CLI is **optional**: everything below can also be done in the web console
(§6, §12). It's useful for scripting, automation and recovery when nobody
can sign in.

On an installed system use `/opt/printquota/bin/quotactl` (with `sudo` so
that it can read `/etc/printquota/env` and write the database), or add
`/opt/printquota/bin` to your `PATH`. Every command that changes something
writes an audit-log row. The actor is `$SUDO_USER`, or the current login
if that isn't set.

**Global options:** `--config PATH`, `--db-url URL`, `--version`, `-h/--help`.

### Database

| Command | Description |
|---|---|
| `db init [--admin NAME] [--admin-password PW]` | Create the schema if it is missing. With `--admin`, create or promote that user to administrator and set their password (prompts if `--admin-password` is omitted) |
| `db stats` | Row counts for users, groups, printers, policies, jobs and alerts |

### Users

| Command | Description |
|---|---|
| `user add NAME [--display-name] [--email] [--group] [--quota N] [--threshold N] [--admin] [--password PW]` | Create an account. The group must already exist. Quota and threshold default to the config values |
| `user list [--group G] [--inactive]` | Username, group, used/limit, remaining, active, admin |
| `user show NAME` | Full details, period dates, and the last 10 jobs |
| `user set-quota NAME PAGES` | Change the allowance (≥ 0) |
| `user set-password NAME [--password PW]` | Set the portal password (prompts if omitted) |
| `user set-group NAME GROUP` | Move to a group, or `-` to clear the group |
| `user enable NAME` / `user disable NAME` | A disabled account has all its jobs denied and cannot sign in |
| `user reset NAME` | Set usage to zero and restart the period now |
| `user delete NAME` | Asks for confirmation, then deletes the user **and their whole job history** |

### Groups

| Command | Description |
|---|---|
| `group add NAME [--budget N] [--description TEXT]` | Create a group. Leave out `--budget` for no shared pool |
| `group list` | Members, budget, used, remaining |
| `group set-budget NAME PAGES` | Set the pool size. `none`, `-` or `unlimited` removes the pool |
| `group reset NAME` | Set pool usage to zero and restart the group period |

### Printers

| Command | Description |
|---|---|
| `printer add NAME [--device-uri URI] [--mono X] [--color X] [--duplex/--no-duplex] [--duplex-discount D] [--description]` | Register a queue. `D` must satisfy 0 ≤ D < 1 |
| `printer list` | URI, rates, duplex, discount, active |
| `printer set-cost NAME [--mono X] [--color X] [--duplex-discount D]` | Update the cost model |
| `printer disable NAME` / `printer enable NAME` | While disabled, every job to the queue is denied |

### Policies

| Command | Description |
|---|---|
| `policy add --scope {user,group,printer,global} [--value V] --rule RULE [--rule-value X]` | `--value` is required unless the scope is global, and must name an existing user, group or printer. `--rule-value` defaults to `true` |
| `policy list` | ID, scope, value, rule, rule value, active |
| `policy remove ID` | Delete a rule |

### Reports and maintenance

| Command | Description |
|---|---|
| `usage [--days 30] [--user U] [--printer P] [--status S …] [--csv PATH\|-]` | Job report with totals. `--status` can be repeated (`allowed`, `denied`, `completed`, `error`). With `--csv` it writes 13 columns to a file or to stdout |
| `reset-periods [--dry-run]` | Roll all elapsed periods (the same thing the timer does). `--dry-run` lists the users who are due |
| `audit [--limit 25]` | Most recent admin actions |

---

## 12. Web console and self-service portal

The FastAPI app is served by `quota-api.service` on port 8080. Signing in
sets a signed `HttpOnly`, `SameSite=Lax` cookie. After sign-in,
administrators are sent to `/admin` and other users to `/me`.

### Pages

| Path | Who | Purpose |
|---|---|---|
| `/login`, `/logout` | Anyone | Sign in / out. After sign-in it only redirects to relative `next` targets |
| `/` | Signed in | Sends you to `/admin` or `/me` |
| `/me` | Any user | Own balance, period end, usage bar, group pool remaining, last 50 jobs |
| `/me/history.csv?days=90` | Any user | Download own history as CSV |
| `/admin` | Admin | Dashboard: 30-day jobs/pages/cost/denials, users at or below their threshold, top 10 users by pages, last 10 denials |
| `/setup` | Anyone with the setup token, only while no administrator exists | Create the first administrator (§6) |
| `/admin` (checklist) | Admin | Also shows a *Getting started* checklist until groups, users, an enforced queue and alerts are set up |
| `/admin/users?q=&group=&status=` | Admin | Search and filter users (group, `-` for no group, `active`/`disabled`/`admin`). Bulk actions. Create a user or administrator |
| `/admin/users/import` | Admin | Upload or paste CSV, preview, apply (§6) |
| `/admin/users/import/template.csv` | Admin | CSV template |
| `/admin/users.csv` | Admin | Export every user in the import format (no passwords) |
| `/admin/users/bulk` (POST) | Admin | Apply one bulk action to the selected users |
| `/admin/users/{name}` | Admin | Edit full name, quota, threshold, email, group, active, admin, password. Reset usage. Delete. See the user's policies, last 10 alerts and last 50 jobs. You cannot remove your own admin rights, or disable or delete yourself |
| `/admin/groups` | Admin | Create groups, edit budget and description, reset pool usage, delete a group |
| `/admin/printers` | Admin | **Printers & queues**: list CUPS queues, turn quota enforcement on/off, add a CUPS queue, edit each printer's cost model |
| `/admin/settings` | Admin | Console-editable settings, test alert, revert to the file value, read-only server facts |
| `/admin/policies` | Admin | List, create (validated) and delete rules |
| `/admin/reports?days=&username=&printer=&status=` | Admin | Filtered report (up to 500 rows shown) with totals |
| `/admin/reports.csv?…` | Admin | Full filtered CSV export (14 columns). The export itself is written to the audit log |
| `/admin/audit?limit=100` | Admin | Audit log (maximum 500 rows) |
| `/static/*` | Anyone | Stylesheet |

### JSON endpoints

| Endpoint | Auth | Response |
|---|---|---|
| `GET /healthz` | None | `{"status":"ok"}`, or HTTP 503 with `{"status":"degraded","error":…}` if the database round-trip fails |
| `GET /api/me` | Session | `username, quota_limit, pages_used, remaining, group, group_remaining, period_start`. Intended for client agents |
| `GET /admin/api/summary?days=30` | Admin session | `window_days, jobs, denied, pages, cost, users, printers` |
| `GET /api/docs`, `/api/openapi.json` | None | Swagger UI / OpenAPI schema |

Unauthenticated requests to `/api/*` get a JSON 401. Unauthenticated page
requests are redirected to `/login?next=…`. Non-admins get a 403 page.

---

## 13. Authentication

Selected with `api.auth_backend`. For every backend, a user can only sign in
if they **also** have an active row in `users`. The `is_admin` flag on that
row decides console access.

| Backend | Validates against | Setup |
|---|---|---|
| `local` (default) | `users.password_hash` (bcrypt, 12 rounds; passwords are truncated to 72 bytes) | Set passwords with `quotactl user set-password`, `user add --password`, or the console. See [Setting or resetting an administrator password](#setting-or-resetting-an-administrator-password) |
| `pam` | The host's PAM stack, service name `printquota` | `pip install python-pam` into `/opt/printquota` and create `/etc/pam.d/printquota`. Note that the service runs as the unprivileged `printquota` user, which limits which PAM modules work (e.g. `pam_unix` cannot read `/etc/shadow`) |
| `ldap` | AD/LDAP: a service bind, a search for the user's DN, then a bind as the user | `/opt/printquota/bin/pip install 'printquota[ldap]'` from the repo, then add an `ldap:` section (below) |

```yaml
ldap:
  server: "ldaps://dc01.example.local"
  bind_dn: "CN=svc-printquota,OU=Service,DC=example,DC=local"
  bind_password: "..."
  user_base: "OU=Staff,DC=example,DC=local"
  user_filter: "(sAMAccountName={username})"
  group_attribute: "memberOf"
  group_map:
    "CN=Finance,OU=Groups,DC=example,DC=local": finance
```

LDAP usernames must match `^[A-Za-z0-9._-]{1,128}$`, and filter values are
escaped according to RFC 4515. `group_map` is read by a helper function
(`lookup_groups`) that is **not yet wired to any sync job**. Automatic group
synchronisation is planned for Phase 9.

---

## 14. Alerts

After each reconciled job, the accounting daemon checks the user's balance:

| Condition | Alert type | Subject |
|---|---|---|
| `remaining ≤ 0` | `over_quota` | "Print quota exhausted" |
| `remaining ≤ low_balance_threshold` | `low_balance` | "Print quota running low" |

Only one alert is sent per check, and `over_quota` takes priority. Alerts
are skipped for disabled users and when `alerts.enabled` is false or
`alerts.channel` is `none`.

- **Email:** sent to `users.email` through the configured SMTP server. If the
  user has no email address or no SMTP host is set, the attempt is logged as
  undelivered.
- **Webhook:** JSON POST to `alerts.webhook_url` containing `type, username,
  display_name, subject, message, pages_used, quota_limit, remaining`. Any
  2xx response counts as delivered.
- **Cooldown:** the same alert type is not sent to the same user again within
  `alerts.cooldown_hours`. Every attempt, delivered or not, is recorded in
  `alerts_log`, and that record is what the cooldown checks.
- Delivery failures are logged and **never interrupt accounting**.

A third type, `repeated_denial`, is defined in code but nothing calls it yet.

---

## 15. Services and timers

| Unit | Runs as | Schedule / command | Role |
|---|---|---|---|
| `quota-accounting.service` | `printquota` (+ group `lp`) | `printquota-accounting --interval 15` | Reads `page_log`, reconciles jobs, sends alerts |
| `quota-api.service` | `printquota` (+ group `lpadmin`) | `uvicorn printquota.api.main:app --host 0.0.0.0 --port 8080 --proxy-headers` | Web console and portal |
| `quota-reset.timer` → `.service` | `printquota` | Daily 00:30 (+ up to 5 min random delay, `Persistent=true`) | `printquota-reset`: rolls elapsed periods and writes an audit row (actor `system`, source `timer`) |
| `quota-backup.timer` → `.service` | root | Daily 23:30 (+ up to 10 min, `Persistent=true`) | `backup.sh /var/backups/printquota` (enabled by `install.sh` since 0.2.0) |
| CUPS backend (not a unit) | root, run by `cupsd` | For each job | Pre-flight enforcement |

The long-running services are hardened with `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`,
kernel/cgroup protection, restricted address families and namespaces, and
`ReadWritePaths=/var/lib/printquota`. The accounting daemon can only read
`/var/log/cups` and `/etc/printquota`.

Accounting daemon options (for manual runs):

```bash
printquota-accounting [--page-log PATH] [--state PATH] [--interval SECONDS] [--once]
```

---

## 16. Data model

Eight tables, defined in `src/printquota/db/models.py`. All timestamps are
timezone-aware UTC and are written by Python, never by a database default,
so SQLite and PostgreSQL behave the same.

| Table | Key | Main columns |
|---|---|---|
| `groups` | `name` | `description`, `shared_quota` (NULL = no pool, ≥ 0), `pages_used`, `period_start` |
| `users` | `username` (= CUPS user name) | `display_name`, `email`, `group_name` → groups (SET NULL on delete), `quota_limit` ≥ 0, `pages_used`, `period_start`, `low_balance_threshold`, `is_active`, `is_admin`, `password_hash` |
| `printers` | `name` (= CUPS queue) | `description`, `real_device_uri`, `cost_per_page_mono`, `cost_per_page_color`, `supports_duplex`, `duplex_discount` (0 ≤ d < 1), `is_active` |
| `print_policies` | `id` | `scope_type` (user/group/printer/global), `scope_value`, `rule_type`, `rule_value`, `is_active` |
| `print_jobs` | `id` (surrogate) | `cups_job_id`, `username` → users (CASCADE), `printer` → printers (CASCADE), `title`, `copies`, `is_color`, `is_duplex`, `estimated_pages`, `actual_pages`, `charged_pages`, `cost`, `status` (allowed/denied/completed/error), `denial_reason`, `reconciled`, `submitted_at`, `completed_at` |
| `alerts_log` | `id` | `username`, `alert_type`, `channel`, `message`, `delivered`, `sent_at` |
| `admin_audit_log` | `id` | `admin_user`, `action`, `target`, `details` (JSON), `source` (cli/web/timer), `timestamp` |
| `app_settings` | `key` (dotted setting name) | `value` (JSON), `updated_by`, `updated_at`. Written by the Settings page. Only whitelisted keys are ever read |

Design notes:

- **`cups_job_id` is not the primary key**, because CUPS reuses job IDs after a restart.
  The daemon matches `page_log` lines to the newest unreconciled `allowed`
  job with the same (printer, CUPS job ID).
- **`charged_pages` is stored separately from `estimated_pages` and `actual_pages`.** It
  records what was actually taken from the balance, so reconciliation is a
  simple difference and is safe to repeat.
- **Job status flow:** `allowed` → `completed` (after reconciliation), or
  `denied`. `error` is used by the refund helper.
- **Deleting a user or printer cascades to its jobs.** Deleting a group sets its
  members' `group_name` to NULL.
- On SQLite, every connection enables `foreign_keys=ON`, `journal_mode=WAL`,
  `synchronous=NORMAL` and a 10-second `busy_timeout`, because three
  processes share one file.

### SQLite vs PostgreSQL

SQLite comfortably handles a few hundred users and a handful of busy queues.
Move to PostgreSQL when `database is locked` warnings appear in the
accounting log, or when you want the database on a different host from CUPS:

```yaml
database:
  url: "postgresql+psycopg://printquota:SECRET@localhost/printquota"
```

Then run `/opt/printquota/bin/pip install 'psycopg[binary]'`, run
`alembic upgrade head` (see §19), and restart the services. Data is not
migrated automatically.

---

## 17. Logging and monitoring

Logs are sent to journald (identifier `printquota`) when `systemd-python`
is installed, and to stderr otherwise, which journald captures anyway. Each
record carries the component name (`printquota.backend`,
`printquota.accounting.daemon`, …) and `key=value` fields.

```bash
journalctl -u cups -f                  # backend allow/deny decisions (+ -t printquota)
journalctl -t printquota -f            # everything printquota logs to journald
journalctl -u quota-accounting -f      # reconciliation passes, page_log rotation
journalctl -u quota-api -f             # sign-ins, failed sign-ins (with IP), errors
```

Each backend decision logs the job, user, printer, pages, estimation method,
colour, duplex, whether it was allowed, and the reason.

**What to monitor:**

- `GET /healthz`: liveness plus a database round-trip.
- `GET /admin/api/summary`: totals for dashboards.
- Whether `quota-accounting.service` is active. If it stops, jobs still print
  and are charged the estimate, but corrections and alerts stop.
- `page_log entry with no matching allowed job` warnings. These usually
  mean a queue was printing without being wrapped.

---

## 18. Backup and restore

`scripts/backup.sh [DEST]` reads `database.url` from
`$PRINTQUOTA_CONFIG` (default `/etc/printquota/settings.yaml`):

- **SQLite:** uses the online backup API, which is safe while the services
  run, then gzips the copy to `printquota-YYYYmmdd-HHMMSS.db.gz`.
- **PostgreSQL:** `pg_dump | gzip` to `printquota-YYYYmmdd-HHMMSS.sql.gz`.
- Keeps the newest `$KEEP` backups (default 14) and deletes older ones.

```bash
sudo ./scripts/backup.sh /var/backups/printquota
```

**Restore (SQLite):**

```bash
sudo systemctl stop quota-api quota-accounting
sudo gunzip -c /var/backups/printquota/printquota-<stamp>.db.gz \
     > /var/lib/printquota/printquota.db
sudo rm -f /var/lib/printquota/printquota.db-wal /var/lib/printquota/printquota.db-shm
sudo chown printquota:printquota /var/lib/printquota/printquota.db
sudo systemctl start quota-accounting quota-api
```

Losing this database loses every balance and all usage history. **Copy
backups off the VM as well.**

---

## 19. Upgrades and database migrations

```bash
cd /path/to/print-quota-system
git pull
sudo ./scripts/install.sh          # reinstalls the package, migrates, restarts units
```

`install.sh` keeps your existing `settings.yaml` and `env`. Schema changes
always ship as Alembic migrations in
`src/printquota/db/migrations/versions/`. **Never edit the schema by hand.**
The migration environment reads the database URL from printquota's own
settings, so it always targets the same database as the services.
SQLite migrations use Alembic's batch mode so that `ALTER TABLE` works.

To run migrations by hand:

```bash
cd /path/to/print-quota-system
sudo PRINTQUOTA_CONFIG=/etc/printquota/settings.yaml /opt/printquota/bin/alembic upgrade head
```

Creating a new migration during development:

```bash
PRINTQUOTA_DB_URL=sqlite:///./dev.db .venv/bin/alembic revision --autogenerate -m "describe change"
```

---

## 20. Security model

### Trusting the user name (required before relying on quotas)

Enforcement is only as trustworthy as `job-originating-user-name`. By
default CUPS takes this name from the client, which can set it to anything.
Require authentication in `/etc/cups/cupsd.conf`:

```apache
<Location /printers>
  Order allow,deny
  Allow @LOCAL
  AuthType Default
  Require valid-user
</Location>
```

With `Require valid-user`, CUPS replaces the job's user name with the
authenticated one. Without it, quotas are advisory at best. The installer
deliberately does not edit `cupsd.conf`. Restart CUPS after changing it.

### Built-in protections

- **No shell anywhere in the backend.** `pdfinfo` and the real backend are run
  with argument lists; `pdfinfo` also has a timeout.
- **Real backend path validation.** The backend scheme must match
  `[a-z0-9][a-z0-9+.-]*` and resolve to an executable directly inside
  `printing.real_backend_dir`, so a crafted device URI cannot run an
  arbitrary binary.
- **Backend permissions** are `0700 root:root`. CUPS runs 0700 backends as
  root (needed to read other users' spool files) and 0755 backends as `lp`.
- **Fail-safe defaults:** a database outage or backend crash **holds** the job;
  a broken URI **stops** the queue. Neither lets a job print without being counted.
- **Web app:** bcrypt password hashes, signed and expiring session cookies
  (`HttpOnly`, `SameSite=Lax`, optionally `Secure`), relative-only redirects
  after sign-in, failed sign-ins logged with the client IP.
- **Audit trail:** every privileged action from the CLI, console or timer is
  recorded with the actor, target, details and source.
- **Least privilege:** the services run as the unprivileged `printquota`
  user under systemd hardening. Configuration is `0640 root:printquota`.
- **First-run protection:** the setup wizard needs the one-time
  `PRINTQUOTA_SETUP_TOKEN` (compared in constant time) and closes for good
  once an administrator exists. Without a token it only accepts requests
  from the server itself.
- **CUPS administration:** the web service is in CUPS's `lpadmin` group so
  it can enforce, release and create queues. It never runs a shell. Queue
  names and URIs are validated against strict patterns (never starting with
  `-`), and every call is an argument list with a timeout. It is still an
  administrative privilege, so anyone with an admin account on the console
  can change CUPS queues.
- **Console settings** can never change the database URL, secret key, ports,
  sign-in backend or CUPS paths. The SMTP password is never shown back and is
  masked in the audit log.

### Recommendations

- Put the console behind a TLS reverse proxy (nginx/Caddy) and set
  `api.cookie_secure: true`. Or bind it to a management network by editing
  `--host` in `quota-api.service`.
- Keep `/etc/printquota/env` readable only by root and `printquota`. It
  holds the session secret.
- Rotating `PRINTQUOTA_SECRET_KEY` signs everyone out, and nothing else.
- Complete the setup wizard soon after installing. The setup link contains
  the token and may appear in proxy or access logs, but it stops working once
  the first administrator exists.

---

## 21. Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| Job disappears, user sees no reason | Denied by policy or quota | `quotactl usage --days 1 --status denied`; `journalctl -u cups -n 100 \| grep -i printquota`. `denial_reason` on the job is the definitive answer |
| Denied job not in the reports | Unknown or disabled user: these denials are **not** stored in `print_jobs` | CUPS journal / `error_log`; create or enable the account |
| Queue stopped | Bad `quota:` URI, missing real backend, or the real backend failed | `lpstat -v <queue>` should read `quota:<real-uri>`; `ls -l /usr/lib/cups/backend/quota` should be `0700 root:root`; then `cupsenable <queue>` |
| Jobs held instead of printing | Backend could not write to the database: DB down or unreadable, **printer not registered** (foreign key), or the backend crashed | `quotactl printer list`; DB file permissions; `journalctl -u cups`. Release held jobs with `lp -i <job> -H resume` |
| A job printed although it was over the quota, or was charged 1 page | The document couldn't be counted: an uncountable format (PCL6/XPS driver), or the spool couldn't be read | `journalctl -u cups -n 50 \| grep method=` shows how it was counted. `spool:postscript`/`spool:pdf` are exact; `…:fallback` means guessed. Use the Microsoft PS Class Driver on Windows (§10) |
| Balances don't change after printing | Daemon not running or cannot read `page_log` | `systemctl status quota-accounting`; `sudo -u printquota head /var/log/cups/page_log`; `PageLogFormat` must be the CUPS default |
| Estimates consistently wrong | PCL/raw drivers | See §10. Billing is still corrected by the daemon |
| User charged for a jammed job | Pages were imaged and logged | `quotactl user set-quota` (raise it) or `quotactl user reset`; both are audited |
| `database is locked` warnings | SQLite contention | Move to PostgreSQL (§16) |
| Everyone signed out after a restart | `secret_key` is empty | Set `PRINTQUOTA_SECRET_KEY` in `/etc/printquota/env` |
| Config change has no effect | File settings are cached per process, or an environment variable overrides it | Console settings apply within 15 s. After editing `settings.yaml`/`env`, restart `quota-accounting` and `quota-api`. A *set by environment* badge means `/etc/printquota/env` wins |
| **Turn on** / **Add printer** says *Forbidden* | The `printquota` account isn't in the `lpadmin` group, or `quota-api` was started before it was added | `id printquota` should list `lpadmin`. Fix with `sudo usermod -a -G lpadmin printquota && sudo systemctl restart quota-api` (re-running `install.sh` does both) |
| Printers page says *Could not read the CUPS queues* | CUPS not running, or `cups-client` missing | `systemctl status cups`; `lpstat -v` |
| Printers page says *quota backend is not installed* | `install.sh` hasn't been run on this server | `sudo ./scripts/install.sh` |
| IPP Everywhere queue creation fails | The printer is unreachable or doesn't speak IPP Everywhere | Check the URI with `ipptool -tv <uri> get-printer-attributes.test`, or choose *Raw queue* |
| Lost the setup link | — | The token is `PRINTQUOTA_SETUP_TOKEN` in `/etc/printquota/env`. Open `/setup` and paste it |
| Locked out (no administrator can sign in) | Forgotten password or disabled account | `sudo /opt/printquota/bin/quotactl db init --admin <name>` (§6) |

More detail is in [docs/operations.md](docs/operations.md), and every issue met so far, with its fix, is in [docs/setup-and-issues.md](docs/setup-and-issues.md).

---

## 22. Development and testing

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export PRINTQUOTA_DB_URL=sqlite:///./dev.db
.venv/bin/alembic upgrade head
.venv/bin/pytest                                   # full suite
.venv/bin/python scripts/seed_demo.py              # demo data; every password is 'printquota'
.venv/bin/uvicorn printquota.api.main:app --reload # http://127.0.0.1:8000
```

`seed_demo.py` creates the groups `finance` (budget 2000), `engineering`
(6000) and `reception` (unlimited), the printers `hp-mono` and `color-mfp`,
the users `ceejay` (admin), `ada`, `tunde` and `reception`, and 60
randomised jobs, most of them reconciled.

### Test suite (206 tests)

Every test runs against a throwaway SQLite file and settings file, so none
of them can touch a real deployment. Nothing calls the real CUPS: queue tests
replace `services.cups_queues.runner` with a fake. Two backend tests execute
a shell-script stand-in for the printer backend, so they only pass on Linux
or macOS.

| File | Covers |
|---|---|
| `unit/test_policy_engine.py` | Rule behaviour, scope precedence, soft vs strict, group pools, validation |
| `unit/test_pages.py` | PDF, PostScript, PCL and text estimation, fallbacks, copies, N-up |
| `unit/test_cost.py` | Rates, duplex discount, sheet counts, default fallbacks |
| `unit/test_config.py` | Defaults → YAML → console → env precedence, type conversion, whitelist of console keys |
| `integration/test_backend.py` | Runs the CUPS backend exactly as CUPS does, with a stand-in real backend: allow, deny, forced duplex, unknown user, broken URI → STOP, DB failure → HOLD, path traversal |
| `integration/test_quota_flow.py` | Immediate debit, double-spend protection, group pools, reconciliation up/down, idempotency, refunds, period rolling |
| `integration/test_daemon.py` | `page_log` parsing (per-page and `total` lines), cursor, rotation, partial lines, unmatched entries |
| `integration/test_spool_counting.py` | Counting the submitted documents from the CUPS spool when the backend receives uncountable driver output: over-quota refusal before printing, copies/number-up/page-ranges from the control file, multiple documents, gzip, fallbacks |
| `integration/test_alerts.py` | Thresholds, cooldown, disabled alerts, transport failures |
| `integration/test_cli.py` | Every `quotactl` command group |
| `integration/test_api.py` | Auth, portal, console CRUD, CSV, audit, health, redirects |
| `integration/test_identity.py` | Name matching: `DOMAIN\user`, capitals, `user@realm`, exact-match priority, ambiguity refusal, jobs charged to the matched account, sign-in, and the creation rules in the console, CLI, import and setup |
| `integration/test_console.py` | Setup wizard and its token, admin creation, password rules, self-protection, user filters, CSV import (preview/apply/upload/update), export, bulk actions, user and group deletion, Settings page (save, validation, env lock, secret handling, revert, live effect), queue enforce/release/add against a fake CUPS, input validation, permission errors |

### Code conventions

- `policies/engine.py` and `accounting/cost.py` are **pure functions** over
  dataclasses (no ORM, config or I/O). Keep them that way so they stay
  fully unit-testable.
- `services/quota.py` is the **only** module that changes balances.
- Every privileged change must call `record_audit`.
- Operational values come from settings, never from hard-coded constants.

---

## 23. Known limitations and gaps

These were found by reviewing the code as of 0.2.0 and are listed so that
future maintainers don't trip over them. The backup-install gap, the JSON
401 on `/admin/api/*`, and the failing user deletion from 0.1.1 are fixed
(see [CHANGELOG.md](CHANGELOG.md)).

1. **A printer rate of 0 falls back to the default rate**, so a free queue
   or a mono-only printer with colour set to 0 is still charged
   `printing.default_cost_per_page_*`. Workaround: set the defaults to 0 on
   the Settings page, or use a tiny non-zero rate.
2. **`block_filetype` rarely matches in practice.** It compares the spool
   file's extension, and CUPS spool files (`/var/spool/cups/dNNNNN-001`)
   usually have none.
3. **Soft enforcement skips the group budget check** as well as the user check.
4. **Denials for unknown or disabled users are not stored** in `print_jobs`,
   so they appear only in the CUPS logs and not in reports or on the dashboard.
5. **Some features exist in code but are not wired up:** the `repeated_denial`
   alert, per-job refunds (`refund_job`, which has no CLI or console command),
   and LDAP group sync (`lookup_groups`).
6. **`api.host`/`api.port` are ignored by the systemd unit**, which passes
   fixed uvicorn flags.
7. **Only the default CUPS `PageLogFormat` is supported** by the daemon's parser.
   Many drivers never report pages, so there may be no `page_log` at all. Charges
   then rely on the pre-print count, which is only exact for PDF, PostScript
   and Apple raster documents (§10).
8. **`cupsd.conf` is not managed from the console.** Requiring logins for
   printing (§20) is still a one-time edit on the server.
9. **Large imports with passwords are slow.** Each password is bcrypt-hashed
   (about 0.25 s each), so importing 1,000 users *with* passwords takes a few
   minutes in one request. Imports without passwords are fast. For very large
   batches, import without passwords and let people use PAM/LDAP sign-in, or
   split the file.
10. **Script executable bits:** a Windows checkout can drop the executable
    bit from `scripts/*.sh`, `scripts/seed_demo.py` and `quota_backend.py`
    (git shows a 100755 → 100644 mode change). Run `chmod +x scripts/*.sh`
    on the server before installing, and don't commit the mode change.

---

## 24. Roadmap

From [CHANGELOG.md](CHANGELOG.md):

- **Phase 9:** AD/LDAP group synchronisation using the existing hook.
- **Phase 10:** hardening pass once there is real traffic.
- **Phase 11 (stretch):** secure/pull printing and a client-side confirmation popup.

**Out of scope by design:** multi-tenancy, payment/billing integration,
native mobile apps, document watermarking, and MFP scan/copy tracking.

---

## 25. Further documentation

- [docs/architecture.md](docs/architecture.md): why a custom system, the
  enforcement mechanism, the data model, the policy engine, the security
  model, and scaling.
- [docs/setup-and-issues.md](docs/setup-and-issues.md): **start here for a new server or a problem.** A step-by-step setup runbook, plus a log of every issue met in the real deployment (symptom, cause, fix, version).
- [docs/operations.md](docs/operations.md): rollout, troubleshooting,
  backup and restore, upgrades, monitoring.
- [CHANGELOG.md](CHANGELOG.md): release history.

---

## 26. Licence

MIT. See [LICENSE](LICENSE).
