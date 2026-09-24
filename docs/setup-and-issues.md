# Setup guide and issue log

This is a working record of how printquota was deployed, every problem hit
along the way, what caused it, and how it was solved. Use it when building a
new server or when something breaks.

- **Part A** is the setup runbook for a new server, in order, with the lessons
  below already built in.
- **Part B** is the issue log: symptom → cause → fix → status.
- **Part C** lists quick health checks.

The README is the full reference. This document is the practical, lived-in
version.

**Keep it current:** when a new problem is solved, add an entry to Part B
(next number), and if it changes the setup steps, update Part A too.

| | |
|---|---|
| Last updated | 2026-09-24 |
| Current version | 0.2.3 |
| Reference server | Ubuntu 22.04 LTS (jammy), Python 3.10, Hyper-V VM `printserver` |
| Reference printer | CUPS queue `Office_Printer` → `lpd://192.0.2.20/lp` (PPD driver) |
| Clients | Windows PCs on Active Directory |

---

## Part A: Setting up a new server

### A1. Get the code onto the server

```bash
git clone https://github.com/Emmaceejay/print-quota-system.git
cd print-quota-system
chmod +x scripts/*.sh          # harmless if already executable (see B1)
```

Always install from a **git clone on the server**. Don't copy the folder from
a Windows PC, because that can strip the executable bit from the scripts (B1).

### A2. Run the installer

```bash
sudo ./scripts/install.sh 2>&1 | tail -30
```

It must end with **"printquota is installed."** plus a setup link. If it
stops earlier, read the last lines (B19). Then confirm:

```bash
/opt/printquota/bin/pip show printquota | grep Version    # expected version
systemctl status quota-api --no-pager | grep Active       # "since" = just now
```

### A3. Create the first administrator (browser)

Open the link the installer printed:
`http://<server-ip>:8080/setup?token=…`. If you've lost it, run
`sudo grep SETUP_TOKEN /etc/printquota/env` and open `/setup`. See B2.

### A4. Share the printer queues on the network (one-time, server shell)

```bash
sudo cupsctl --share-printers --remote-any
sudo lpadmin -p <QUEUE> -o printer-is-shared=true      # for each queue
sudo ufw allow 631/tcp                                 # only if ufw is enabled
```

### A5. Clean up CUPS queues (see B9, B10)

```bash
lpstat -v                                  # list queues: "device for <QUEUE>: <URI>"
sudo systemctl disable --now cups-browsed  # stop auto-created implicitclass:// queues
sudo lpadmin -x <leftover-or-test-queue>   # e.g. a hp-mono queue pointing at /dev/null
```

### A6. Add printers and turn on enforcement (browser)

Go to **Printers & queues**:

- For an existing queue, click **Turn on** (quota enforcement).
- For a new printer, use **Add a new printer to CUPS**. Give it a simple name
  with no spaces (e.g. `Finance_M426`), the device URI
  `ipp://<printer-ip>/ipp/print`, driver **IPP Everywhere**, and tick
  **Turn on quota enforcement**.
- Give every printer a **fixed IP** (DHCP reservation).
- Set the **costs** with **Edit costs**.

The queue name is the name **before the colon** in `lpstat -v`, not the device
URI (B8).

### A7. Create users (browser)

- The printquota username must **exactly** match what the PC sends. On AD
  that's the `sAMAccountName`, capital letters included (B14).
- For many users: export from AD with PowerShell (B14), then use **Users →
  Import users…**, preview, and apply.

### A8. Settings (browser)

On **Settings**, set the default quota, period, enforcement mode, costs and
currency, and alerts/SMTP. Then click **Send test alert**. If the Webhook URL
box contains a username, clear it (B17).

### A9. Connect each Windows PC (B11)

1. Settings → Printers & scanners → **Add device** → **Add manually** →
   **Select a shared printer by name**:
   `http://<server-ip>:631/printers/<QUEUE>`. The queue name is
   case-sensitive.
2. Driver: **Microsoft → Microsoft PS Class Driver**. If it's not listed,
   choose **Generic → MS Publisher Imagesetter**. Never choose PCL6 or XPS.
3. In Printing preferences, set the paper size to **A4**.

### A10. Test (see Part C for the commands)

1. Reset your test user's usage. Set the quota to **3**.
2. Print **2 pages**. They print, and the user shows used 2, remaining 1.
3. Print **4 pages**. **Nothing prints**, and the job is cancelled with
   "quota exceeded".
4. `sudo journalctl -u cups -n 50 --no-pager | grep -o "method='[^']*'"`
   should show `spool:postscript` (B15).

### A11. Before relying on the numbers (B21)

- On each printer's web admin page, allow printing **only from the server's
  IP**, and remove any direct printer connections from the PCs.
- Require a login for printing in `/etc/cups/cupsd.conf`
  (`Require valid-user`, README §20). On AD, that means joining the server to
  the domain (B14).

### Upgrading an existing server

```bash
cd ~/print-quota-system
git pull origin main
sudo ./scripts/install.sh 2>&1 | tail -25
/opt/printquota/bin/pip show printquota | grep Version    # must match the new version
```

If the version didn't change, see B18.

---

## Part B: Issue log

Status: **Fixed x.y.z** = fixed in code in that version. **Verified** =
confirmed on the real server. **Config** = solved by configuration, not code.
**Info** = expected behaviour, no action.

### B1. Scripts lose their executable bit when copied from Windows

- **Symptom:** `./scripts/install.sh: Permission denied`. On Windows, `git
  status` shows `scripts/*.sh` as modified with only a mode change
  (100755 → 100644).
- **Cause:** a Windows checkout doesn't keep Unix permissions.
- **Fix:** on the server, `chmod +x scripts/*.sh`. On the Windows development
  copy, `git config core.fileMode false`, so the mode change is never
  committed. The files are 100755 in git.
- **Status:** Config.

### B2. The first administrator had to be created on the command line

- **Symptom:** a fresh install needed `quotactl db init --admin`.
- **Fix:** a browser **setup wizard** at `/setup`, protected by a one-time
  `PRINTQUOTA_SETUP_TOKEN` that the installer generates and prints. It closes
  for good once an admin exists, and `/login` redirects to it until then.
- **Lost the link:** `sudo grep SETUP_TOKEN /etc/printquota/env`, then open
  `http://<server>:8080/setup` and paste the token.
- **Status:** Fixed 0.2.0.

### B3. Setting or resetting an administrator password

- **Normal:** **Users** → click the user → *New password* + *Confirm* →
  **Save**. To create another admin, tick **Administrator** on the Add form
  (a password is required).
- **Locked out (nobody can sign in):**
  ```bash
  sudo /opt/printquota/bin/quotactl db init --admin <name>   # new password + admin rights
  sudo /opt/printquota/bin/quotactl user enable <name>       # if it was disabled
  ```
- Changing a password doesn't end existing sessions. Those expire after 8 h,
  or immediately if you change `PRINTQUOTA_SECRET_KEY` and restart `quota-api`.
- **Status:** Fixed 0.2.0 (console). The CLI is only needed for recovery.

### B4. Everyday setup needed the command line

- **Symptom:** users, groups, printers, enforcement and settings all needed
  `quotactl` or `register_backend.sh`.
- **Fix:** the web console covers all of it: user CSV import/export, bulk
  actions, groups, **Printers & queues** (Turn on / Turn off / Add printer),
  **Settings**, and a "Getting started" checklist on the dashboard.
- **Status:** Fixed 0.2.0.

### B5. Deleting a user who had printed failed

- **Symptom:** `NOT NULL constraint failed: print_jobs.username`. This
  affected `quotactl user delete` too.
- **Fix:** the user's job history and user-scoped policies are deleted with
  the user.
- **Status:** Fixed 0.2.0.

### B6. Nightly backups never ran

- **Cause:** `install.sh` never copied `backup.sh` to `/opt/printquota/share`
  and never enabled `quota-backup.timer`.
- **Status:** Fixed 0.2.0. Check with `systemctl list-timers quota-backup.timer`.

### B7. How users print "through" printquota

- **Answer:** PCs never print to the printer directly. They print to the CUPS
  queue on the server. printquota checks each job against the user's quota,
  then passes it to the printer or cancels it.
- **Requirements:** the queue is shared (A4), enforcement is on (A6), the
  username matches (A7), and the PC is connected to the server queue (A9).
- **Status:** Config.

### B8. Queue name vs device URI

- **Confusion:** `lpd://192.0.2.20/lp` is a **device URI** (protocol, the
  printer's IP, and the printer's internal queue `lp`), not a queue name.
- **Queue name:** in `lpstat -v`, the part between `device for` and the colon:
  `device for Office_Printer: quota:lpd://192.0.2.20/lp` → **`Office_Printer`**.
  The `quota:` prefix means enforcement is on.
- The client address is `http://<server>:631/printers/Office_Printer`
  (case-sensitive).
- **Status:** Info.

### B9. A test queue pointing at `/dev/null`

- **Symptom:** `device for hp-mono: ///dev/null`.
- **Cause:** a leftover test queue named after the README example. Jobs to it
  go nowhere.
- **Fix:** `sudo lpadmin -x hp-mono`.
- **Status:** Config.

### B10. `implicitclass://` HP queues appear on their own

- **Symptom:** queues such as `HP_LaserJet_MFP_M426dw_CD736C` with
  `implicitclass://…` URIs, sometimes duplicated.
- **Cause:** `cups-browsed` auto-creates queues for printers it discovers.
  They are **not enforced** (printing to them bypasses the quota), and CUPS can
  recreate them at any time, which undoes enforcement.
- **Fix:** create a permanent queue per printer (A6), then
  `sudo systemctl disable --now cups-browsed`. Find printer addresses with
  `lpinfo --include-schemes dnssd,ipp,socket,lpd -v`.
- **Status:** Config.

### B11. Which Windows driver to choose

- **Symptom:** "Microsoft IPP Class Driver" isn't in the list when adding the
  printer by address. The list shows MS-XPS, OpenXPS, PCL6 and similar.
- **Cause:** Windows only offers the IPP class driver for printers it
  discovers itself.
- **Fix:** **Microsoft PS Class Driver** (PostScript), or if that's missing,
  **Generic → MS Publisher Imagesetter**. The server queue has a PPD driver,
  so CUPS converts PostScript for the printer. PostScript can also be counted
  exactly (B15).
- **Never use:** the PCL6 class driver (CUPS can't convert it) or the XPS
  drivers (CUPS can't read them).
- **Check the queue type:** `lpstat -l -p <QUEUE> | grep -i interface`. A
  `.ppd` means a driver is on the server, so no HP driver is needed on the
  PCs. No `.ppd` means a raw queue, which needs the HP driver on every PC, or
  switch it: `sudo lpadmin -p <QUEUE> -E -v ipp://<ip>/ipp/print -m everywhere`,
  then click **Turn on** again.
- **Status:** Config. Verified: the PS Class Driver prints on `Office_Printer`.

### B12. CUPS `error_log` messages that are harmless

| Message | Meaning |
|---|---|
| `Printer drivers are deprecated and will stop working…` | General CUPS notice about PPD drivers. Ignore |
| `CreateProfile failed … AlreadyExists` | Colour-profile noise from the `implicitclass` queues. It stops after B10 |
| `Returning IPP client-error-bad-request for windows-ext … from <PC-IP>` | A Windows PC probing while it adds the printer. Ignore |
| `Scheduler shutting down due to program error` (once) | CUPS restarted itself. Only investigate if it repeats: `sudo journalctl -u cups --since "<time>"` |

- **Status:** Info.

### B13. Job cancelled: "no print account for this user"

- **Symptom:** in `error_log`: `[Job N] print job denied: no print account for
  this user` and `Backend returned status 5 (cancel job)`.
- **Meaning:** printing through the server **works**. The username sent by the
  PC just has no printquota account. These jobs don't appear in Reports.
- **Fix:** find the name with `lpstat -W all -o <QUEUE>` (second column),
  then create that user in **Users** with the exact same spelling and
  capitals.
- **Status:** Config. Verified: fixed by creating the user, after which
  printing worked.

### B14. Active Directory usernames

- Domain PCs send the **`sAMAccountName`** (e.g. `jsmith`, with no `DOMAIN\`).
  Use it exactly, with the same capital letters.
- If the server is later joined to the domain (`realm join`) and CUPS requires
  a login, names may become `jsmith@corp.example.com`. Set
  `use_fully_qualified_names = False` in `/etc/sssd/sssd.conf`, or create
  users in that form. Use one form consistently.
- **Bulk import from AD** (run on a DC or a PC with the AD tools), then use
  **Users → Import users…** with *Create groups that do not exist yet* ticked:
  ```powershell
  Get-ADUser -Filter 'Enabled -eq $true' -Properties mail,Department |
    Select-Object @{n='username';e={$_.SamAccountName}},
                  @{n='display_name';e={$_.Name}},
                  @{n='email';e={$_.mail}},
                  @{n='group';e={$_.Department}} |
    Export-Csv users.csv -NoTypeInformation -Encoding UTF8
  ```
- Portal passwords are separate. To use AD passwords for the portal, set
  `api.auth_backend: ldap` plus an `ldap:` section (README §13).
- **Status:** Config.

### B15. Over-quota job printed; a 4-page job counted as 1 page

- **Symptom:** quota 3, a 4-page document printed in full, and the user showed
  used 1, remaining 2.
- **Cause 1:** on a queue with a driver (PPD), CUPS converts the job to the
  printer's language (e.g. PCL XL) **before** printquota sees it. That can't
  be counted, so the estimate fell back to 1 page.
- **Cause 2:** this driver never reports pages to CUPS, so
  `/var/log/cups/page_log` **doesn't exist** and the after-print correction
  never happened (see B16).
- **Fix:** printquota now counts the **original document** in the CUPS spool
  (`/var/spool/cups/d<job>-NNN`), and reads copies, N-up and page ranges from
  `c<job>`. A job that doesn't fit is refused **before** printing; nothing is
  ever printed partially.
- **Verify:** `sudo journalctl -u cups -n 50 --no-pager | grep -o "method='[^']*'\|pages=[0-9]*"`.
  `spool:postscript` / `spool:pdf` means exact. `…:fallback` means guessed:
  check the Windows driver (B11).
- **Setting:** `printing.spool_dir` (default `/var/spool/cups`).
- **Status:** Fixed 0.2.1. Awaiting verification on the server (test A10).

### B16. No `page_log` file

- **Symptom:** `tail: cannot open '/var/log/cups/page_log'`. The accounting
  log shows only "started" lines.
- **Meaning:** normal for drivers that don't report pages. Since 0.2.1 the
  accounting service logs this once, and charges stay at the pre-print count,
  which is exact for PDF and PostScript (B15). When CUPS does write the file,
  both per-page lines and `total` lines are read.
- **Status:** Info (daemon handling fixed in 0.2.1).

### B17. Settings: "Nothing was saved. Webhook URL: must start with http://…"

- **Symptom:** changing costs failed with an error about a field that wasn't
  touched.
- **Cause:** the whole form was re-validated on every save, and the Webhook
  URL box contained a value from **browser autofill** (the page has a
  password box, the SMTP password). A second bug: the page could show and
  cache old values for 15 s after a save.
- **Fix:** only changed fields are validated, every valid change is saved,
  and a bad field is outlined in red with the typed value kept and a plain
  message. Autofill is off. Numbers accept `1,000` / `0,5` / `500.0`. The
  save is committed before the page is re-read.
- **If you still see it:** you're on old code (B18). If a box is red, clear
  it and save.
- **Status:** Fixed 0.2.2. Awaiting verification on the server.

### B18. Reinstalled, but the old behaviour is still there

- **Diagnosis:**
  ```bash
  cd ~/print-quota-system && git log --oneline -1        # latest commit?
  /opt/printquota/bin/pip show printquota | grep Version  # installed version?
  systemctl status quota-api --no-pager | grep Active     # restarted?
  ```
  When this happened: the code was at `7a0e9c1` (0.2.2), but the installed
  version was **0.2.0** and `quota-api` had been running since boot. The pull
  worked, but the install never completed (B19).
- **Quick fix (no installer):**
  ```bash
  sudo /opt/printquota/bin/pip install --upgrade ~/print-quota-system
  sudo systemctl restart quota-api quota-accounting
  ```
  This is enough when there are no new database migrations. Otherwise run
  `install.sh` or `alembic upgrade head`.
- **Status:** Config.

### B19. `install.sh` stops at "Installing OS dependencies"

- **Symptom:** `E: Failed to fetch … File has unexpected size … Mirror sync
  in progress?`, then the output ends. Nothing is upgraded or restarted.
- **Cause:** `apt-get update` failed (an Ubuntu mirror mid-sync; a broken
  third-party repo such as Docker's `focal` entry can do the same), and the
  script aborted.
- **Fix:** the installer skips the package step when everything is already
  installed, and treats `apt-get update` errors as warnings. It only fails if
  a missing package can't be installed. Workaround on older versions: B18's
  quick fix.
- **Status:** Fixed 0.2.3. Awaiting verification on the server.

### B20. Console buttons (Turn on / Add printer) say "Forbidden"

- **Cause:** the `printquota` account isn't in CUPS's `lpadmin` group, or
  `quota-api` started before it was added.
- **Fix:** `sudo usermod -a -G lpadmin printquota && sudo systemctl restart quota-api`.
  Re-running `install.sh` also does this. Check with `id printquota`.
- **Status:** Config (the installer handles it since 0.2.0). Not yet seen on
  the real server.

### B21. Loopholes to close before relying on the quotas

- **Direct printing:** PCs that can reach a printer's IP can add it directly
  and bypass printquota. Restrict each printer (its web admin → IP filtering
  / access control) to the server's IP, and remove direct connections from
  the PCs.
- **Name spoofing:** by default CUPS trusts the username the PC sends. Add
  `Require valid-user` for `/printers` in `/etc/cups/cupsd.conf`
  (README §20) and restart CUPS. On AD, join the server to the domain first
  (B14).
- **Status:** Config. Not done yet.

---

## Part C: Quick health checks

```bash
# Versions and services
/opt/printquota/bin/pip show printquota | grep Version
systemctl status quota-api quota-accounting --no-pager | grep -E "●|Active"
systemctl list-timers quota-reset.timer quota-backup.timer --no-pager

# CUPS queues (quota: prefix = enforced) and recent jobs with usernames
lpstat -v
lpstat -W all -o

# Why a job was allowed or denied, and how its pages were counted
sudo journalctl -u cups -n 50 --no-pager | grep -i printquota
sudo tail -n 30 /var/log/cups/error_log

# Web console reachable and database OK
curl -s http://localhost:8080/healthz          # {"status":"ok"}

# Permissions the console needs
id printquota                                   # must include lp and lpadmin
ls -l /usr/lib/cups/backend/quota               # must be -rwx------ root root
```

---

## Version history

| Version | Date | Main change |
|---|---|---|
| 0.1.0 | 2026-09-22 | Initial release |
| 0.1.1 | 2026-09-23 | Python 3.10 support (Ubuntu 22.04) |
| 0.2.0 | 2026-09-23 | Web console for everything (setup wizard, import, bulk actions, queues, settings). B2–B6 |
| 0.2.1 | 2026-09-24 | Count pages from the submitted document. B15, B16 |
| 0.2.2 | 2026-09-24 | Settings page saves reliably. B17 |
| 0.2.3 | 2026-09-24 | Installer survives `apt-get update` errors. B19 |

Full details are in [CHANGELOG.md](../CHANGELOG.md).
