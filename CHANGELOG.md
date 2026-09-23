# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-22

Initial release: Phases 1-8 of the build plan.

### Added

- SQLAlchemy data model with Alembic migrations (SQLite default,
  PostgreSQL-ready): users, groups, printers, policies, jobs, alerts, audit.
- Pure-function policy and quota engine with scope precedence
  (user > group > printer > global).
- Per-user page quotas over a rolling period, with group shared budgets
  enforced alongside them.
- CUPS wrapper backend (`quota:<real-uri>`): user identification, page
  estimation (PDF/PostScript/PCL/text), policy and quota evaluation,
  pass-through to the real backend, CANCEL on denial, HOLD on datastore
  failure.
- Accounting daemon that reconciles `page_log` against balances, handles log
  rotation and partial lines, and is idempotent per job.
- Mono/colour/duplex-aware cost model with a per-printer duplex discount.
- `quotactl` admin CLI covering users, groups, printers, policies, usage
  reporting, CSV export, period resets and the audit log.
- FastAPI web app: admin console (users, groups, printers, policies,
  reports, CSV export, audit log) and self-service portal with a JSON
  endpoint for client agents.
- Low-balance and over-quota alerts by email or webhook, with a cooldown.
- systemd units for the accounting daemon, the web app, the daily period
  reset and the nightly backup, with service hardening.
- Installer, backend registration script (reversible), backup script and a
  demo seed script.
- Unit and integration test suite, including tests that drive the CUPS
  backend exactly as CUPS does.
- Architecture and operations documentation.

### Security

- Real-backend path validated against the configured backend directory.
- No shell interpolation anywhere in the backend; `pdfinfo` runs with an
  argument list and a timeout.
- bcrypt password hashing, signed `HttpOnly` session cookies, relative-only
  post-login redirects.
- Every privileged action recorded in `admin_audit_log`.

## [0.1.1] - 2026-09-23

### Fixed

- Lowered the supported Python floor from 3.11 to 3.10 so the package
  installs on Ubuntu 22.04 LTS, whose stock interpreter is 3.10. The full
  test suite passes on 3.10 and 3.11; no 3.11-only syntax was in use.
- `install.sh` now checks the interpreter version up front and fails with a
  clear message instead of letting pip reject the package mid-install.

## [0.2.0] - 2026-09-23

Everything after installation can now be done in the web console; the CLI
is optional.

### Added

- **Setup wizard** (`/setup`): creates the first administrator in the
  browser. Guarded by a one-time `PRINTQUOTA_SETUP_TOKEN` that `install.sh`
  generates and prints; closes itself once an administrator exists.
- **Printers & queues** page: lists CUPS queues, turns quota enforcement on
  and off per queue, creates new CUPS queues (IPP Everywhere or raw), and
  edits each printer's cost model. The printer row is always written before
  a queue is wrapped, and rolled back if `lpadmin` fails.
- **User import** from an uploaded CSV or pasted rows, with a preview
  before anything is saved, a downloadable template, optional creation of
  missing groups and optional updates of existing users. **User export** to
  CSV in the same format.
- **Bulk user actions**: move to group, set quota, reset usage,
  enable/disable, grant/revoke admin, delete. The acting administrator
  can never disable, demote or delete themselves.
- Create administrators from the Users page; edit full name; password
  confirmation; delete a single user; delete a group.
- **Settings** page for quotas, enforcement mode, costs, currency and
  alert/SMTP delivery, stored in the new `app_settings` table (Alembic
  migration `5c1e7a9d2b44`), with a "send test alert" button. Values set by
  environment variables stay locked; infrastructure settings (database,
  secret key, ports, paths) stay file/env only.
- "Getting started" checklist on the dashboard.

### Changed

- Configuration precedence is now environment > console > file > defaults.
- The web service runs with the `lpadmin` group, which `install.sh` grants
  to the `printquota` account so the console can administer CUPS queues.
- Passwords set from the console must be at least 8 characters.
- `install.sh` restarts the services on upgrade and prints the setup link
  instead of CLI steps.

### Fixed

- Deleting a user who had printed failed with `NOT NULL constraint failed:
  print_jobs.username` (affected `quotactl user delete`). Their job history
  and user-scoped policies are now removed with them.
- The nightly backup never ran: `install.sh` now installs `backup.sh` and
  the docs under `/opt/printquota/share` and enables `quota-backup.timer`.
- Unauthenticated requests to `/admin/api/*` now get a JSON 401 instead of
  a redirect to the sign-in page.

## Unreleased

### Planned

- Phase 9: AD/LDAP group synchronisation driven by the existing hook.
- Phase 10: hardening pass once real traffic exists.
- Phase 11 (stretch): secure/pull printing, client-side confirmation popup.
