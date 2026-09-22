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

## Unreleased

### Planned

- Phase 9: AD/LDAP group synchronisation driven by the existing hook.
- Phase 10: hardening pass once real traffic exists.
- Phase 11 (stretch): secure/pull printing, client-side confirmation popup.
