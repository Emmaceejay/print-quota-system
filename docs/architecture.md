# Architecture and design decisions

## 1. Why not an existing project

| Option | Why it was rejected |
|---|---|
| PaperCut NG/MF | Proprietary; free only to 5 users |
| PyKota | Upstream dead; Python 2 era; no security maintenance |
| SavaPage | Core licensing ambiguous across distributions |

The remaining option is a small, owned system built directly on CUPS's own
extension point. That is what this is.

## 2. The enforcement mechanism

CUPS logs usage; it does not enforce. There are two integration points and
printquota uses both:

**Pre-flight — a wrapper backend.** A CUPS queue's `DeviceURI` names the
program CUPS execs to move the job to the device. Setting it to
`quota:socket://10.0.0.5:9100` makes CUPS run `/usr/lib/cups/backend/quota`
with the real URI carried inside. The wrapper:

1. reads `job-originating-user-name` from `argv[2]`;
2. estimates the page count from the spool file (`pdfinfo` for PDF, the
   `%%Pages` trailer for PostScript, form feeds for PCL, line count for
   text), multiplied by copies and divided by N-up;
3. evaluates policy rules, then the user's quota and the group pool;
4. **ALLOW** → execs the real backend with the same argv and
   `DEVICE_URI` rewritten; **DENY** → exits `CUPS_BACKEND_CANCEL`.

**Post-print — the accounting daemon.** CUPS writes one `page_log` line per
imaged page. The daemon aggregates those per job and reconciles the charge.

### Why both halves

The pre-flight estimate can be wrong: the filter chain may add a banner
page, the driver may re-compose the job, duplex and N-up change the number
of sides, and a job may fail halfway. `page_log` is the only authoritative
count, but it exists only after the paper is out — too late to block
anything. So printquota charges the estimate up front (which is what stops
a burst of jobs all passing against the same balance) and corrects the
difference afterwards.

### Why a denial returns CANCEL, not FAILED

`CUPS_BACKEND_FAILED` **disables the queue** — one user hitting their quota
would take the printer offline for the whole office. `CUPS_BACKEND_CANCEL`
cancels that one job and leaves the queue running. The reason is written to
stderr with an `ERROR:` prefix, so it lands in `error_log` and in the job's
state message.

### Why a datastore failure returns HOLD

If the database is unreachable, the wrapper cannot know whether the job is
within quota. Printing it would be unaccounted; cancelling it would lose the
user's work. `CUPS_BACKEND_HOLD` keeps the job in the queue until an
administrator releases it.

## 3. Data model

Seven tables (`src/printquota/db/models.py`):

| Table | Holds |
|---|---|
| `groups` | Department, optional shared page pool, pool usage |
| `users` | Quota, usage, rolling period anchor, alert threshold, flags |
| `printers` | Queue name, real device URI, per-page costs, duplex discount |
| `print_policies` | One rule, scoped to user/group/printer/global |
| `print_jobs` | Every decision and its reconciliation |
| `alerts_log` | Every notification (also drives the cooldown) |
| `admin_audit_log` | Every privileged action, from console and CLI alike |

Design notes:

- **Timestamps are timezone-aware UTC, written from Python**, never by a
  database default, so SQLite and PostgreSQL behave identically.
- **`print_jobs.cups_job_id` is not the primary key.** CUPS reuses job ids
  after a restart; the surrogate `id` keeps history unambiguous.
- **`charged_pages` is separate from `estimated_pages` and
  `actual_pages`.** It records what the balance was actually debited, which
  is what makes reconciliation a difference (`actual - charged`) and
  therefore idempotent.
- **`reconciled` is a hard flag.** Replaying `page_log` can never
  double-charge.

### Quota semantics

Quota is counted in **pages**, over a **rolling period** anchored per user
(`period_start`). When the period elapses the anchor advances in whole
windows — a user who did not print for three months restarts aligned to
their original anchor, not to "now", so quotas do not drift across the
organisation.

Cost is tracked in money alongside pages, for reporting and chargeback. It
does not gate printing; mixing the two units in one enforcement decision
makes denials hard for users to reason about.

### Group budgets

A job must fit **both** the user's own quota and their group's shared pool
(when one is set). This is the behaviour administrators expect from a
department budget: an individual allowance stops one person monopolising the
pool, and the pool stops the department as a whole overspending. It can be
turned off with `quota.enforce_group_budget: false`.

## 4. Policy engine

`src/printquota/policies/engine.py` is pure functions over plain
dataclasses: no ORM, no configuration lookups, no I/O. That is what makes
the quota and policy logic exhaustively unit-testable without a printer.

Rules resolve by scope specificity — **user > group > printer > global** —
and a more specific rule *replaces* the less specific one for that rule type
rather than stacking with it, which is what an administrator means when they
add an exception for one person.

Policies are evaluated **before** quota so that a policy violation is
reported as such, rather than surfacing to the user as a confusing balance
message.

`force_duplex` is the one rule that modifies rather than denies: it appends
`sides=two-sided-long-edge` to the options handed to the real backend. The
wrapper runs after CUPS has rendered the job, so only the IPP backends act on
it, by sending `sides` as a job attribute. On other device URIs the engine
skips the rule (`JobContext.can_force_sides`) rather than charge as duplex a
job that prints one-sided.

Paper saving for a whole queue is therefore done in CUPS, not in the
wrapper. *Print on both sides by default* sets the queue's `sides-default`
and the driver's `Duplex` default with `lpadmin`. CUPS applies those to every
job before rendering and passes them to the backend in the job options, so
the wrapper sees the job as duplex and prices it accordingly.

## 5. Security model

### Trusting the user name

Enforcement is only as trustworthy as `job-originating-user-name`. By
default CUPS takes it from the client, where it can be set to anything. In
`/etc/cups/cupsd.conf`:

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
does not edit `cupsd.conf` for you — that file is too important to rewrite
behind an administrator's back.

### Other measures

- The wrapper never passes anything to a shell. `pdfinfo` is invoked with an
  argument list and a timeout.
- The real backend path is resolved and checked to be inside
  `printing.real_backend_dir`, so a crafted device URI cannot exec an
  arbitrary binary.
- The backend is installed `0700 root:root`. CUPS runs `0700` backends as
  root (needed to read the spool file) and `0755` ones as `lp`.
- The web app stores bcrypt password hashes, issues signed session cookies
  (`HttpOnly`, `SameSite=Lax`), and only follows relative redirect targets
  after sign-in.
- Every privileged action is written to `admin_audit_log` with the actor,
  target and source (`cli` or `web`).
- The services run as an unprivileged `printquota` user under systemd
  hardening (`ProtectSystem=strict`, `NoNewPrivileges`, a minimal
  `ReadWritePaths`).

## 6. Observability

Logging is structured and goes to journald when `systemd-python` is present,
otherwise to stderr (which journald captures anyway). Every allow/deny
decision logs the user, printer, page count, estimation method, the rule
that fired and the reason.

```bash
journalctl -u cups -f                 # backend decisions
journalctl -u quota-accounting -f     # reconciliation
journalctl -u quota-api -f            # console and portal
```

## 7. Scaling past SQLite

SQLite is the default: one VM, low write concurrency, WAL enabled, foreign
keys on, a busy timeout for the three processes that share the file. The
schema uses no SQLite-specific types, so moving to PostgreSQL is one setting
plus `alembic upgrade head`:

```yaml
database:
  url: "postgresql+psycopg://printquota:...@localhost/printquota"
```

Rough guidance: SQLite is comfortable to a few hundred users and a handful
of busy queues. Move to PostgreSQL when you start seeing `database is
locked` warnings in the accounting log, or when you want the database on a
different host from CUPS.

## 8. Deliberate non-goals

Multi-tenancy, payment/billing integration, native mobile apps, document
watermarking and MFP scan/copy tracking are out of scope. Secure/pull
printing and a client-side confirmation popup are plausible later additions
— both need per-device or per-OS agents, which is a different project from
this one.
