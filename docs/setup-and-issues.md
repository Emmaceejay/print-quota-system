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
(next number), and if it changes the setup steps, update Part A too. Then
regenerate the HTML copy with `python3 scripts/build_docs.py` and commit both.

**Easier to read in a browser:** open `docs/html/setup-and-issues.html`
(on the server: `/opt/printquota/share/docs/html/`). All the project
documents are there, with a contents sidebar you can filter, copy buttons on
commands, and status badges on every issue.

| | |
|---|---|
| Last updated | 2026-09-26 |
| Current version | 0.2.5 |
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

- Create each user with their **bare logon name**, e.g. `j.doe` (the AD
  `sAMAccountName`). Since 0.2.4, jobs sent as `CORP\J.Doe`,
  `J.Doe` or `j.doe@corp.example.com` are matched to it automatically
  (B22).
- For many users: export from AD with PowerShell (B14), then use **Users →
  Import users…**, preview, and apply.

### A8. Settings (browser)

On **Settings**, set the default quota, period, enforcement mode, costs and
currency, and alerts/SMTP. Then click **Send test alert**. If the Webhook URL
box contains a username, clear it (B17).

### A9. Connect each Windows PC (B11, B23)

1. **Remove** any existing entries for the printer first, especially ones
   Windows found by itself (e.g. *"Office_Printer @ printserver"*).
   **Don't use auto-discovered printers**: they connect with encryption that
   Windows refuses, and jobs never reach the server (B23).
2. Check the address works: open `http://<server-ip>:631/printers/<QUEUE>` in
   the PC's browser. The printer's CUPS page must load.
3. Settings → Printers & scanners → **Add device** → **Add manually** →
   **Select a shared printer by name**, and enter that same address:
   `http://<server-ip>:631/printers/<QUEUE>`. Use the **IP address**,
   **`http`** (not `https`), and the exact queue name (case-sensitive), e.g.
   `http://192.0.2.10:631/printers/Office_Printer`.
4. Driver: **Microsoft → Microsoft PS Class Driver**. If it's not listed,
   choose **Generic → MS Publisher Imagesetter**. Never choose PCL6 or XPS.
5. In Printing preferences, set the paper size to **A4**.
6. Print a test page and confirm a new job appears on the server
   (`lpstat -W all -o <QUEUE> | tail -2`).

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
  then create that user in **Users** with the bare logon name. Since 0.2.4,
  capitals and a `DOMAIN\` prefix don't matter (B22).
- **Status:** Config. Verified: fixed by creating the user, after which
  printing worked.

### B14. Active Directory usernames

- Domain PCs usually send the **`sAMAccountName`** (e.g. `jsmith`), but some
  send `DOMAIN\JSmith` (B22). Create the account as the bare logon name;
  since 0.2.4 every form matches it.
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

### B22. Jobs from one PC refused: the name arrives as `CORP\J.Doe`

- **Symptom:** one user's jobs never print. `lpstat -W all -o Office_Printer`
  shows the user as **`CORP\J.Doe`** (domain prefix plus capitals), while
  everyone else appears as `firstname.lastname`. In `error_log`:
  `print job denied: no print account for this user`.
- **Cause:** printquota matched names exactly. Windows sends the domain form
  on some PCs, depending on how the user signed in and how the printer was
  added. The account `j.doe` was never matched. Creating an account
  called `CORP\J.Doe` was a poor workaround: browsers turn `\` into
  `/` in web addresses, so that account couldn't be opened in the console,
  and the person would have two accounts with a split quota.
- **Fix:** names are now matched the way Active Directory does
  (`services/identity.py`). For the name as sent, then without `DOMAIN\`,
  then without `@realm`: an exact match first, then a match ignoring
  capitals, only if exactly one account qualifies.
  - Jobs are charged to, recorded under and checked against the policies of
    the matched account (`j.doe`). The name actually sent is logged:
    `journalctl -u cups | grep "print user matched"`.
  - Two accounts differing only in capitals are never guessed between: the
    job is refused with `'<name>' matches more than one print account (…)`.
  - Portal sign-in accepts every form.
  - New accounts may not contain `\` or differ from an existing one only in
    capitals (console, CLI, import, setup). A CSV row for `J.Doe`
    updates `j.doe`.
  - Console links URL-encode usernames, so an older account containing `\`
    can still be opened and deleted.
- **After upgrading:** make sure the user exists as `j.doe`. If a
  `CORP\J.Doe` workaround account was created, move its quota to
  `j.doe` and delete it. Exact matches win, so while it exists, jobs
  sent as `CORP\J.Doe` are still charged to it.
- **Status:** Fixed 0.2.4. **Verified 2026-09-25:** Jane Doe's jobs (sent as `CORP\J.Doe`) print and are charged to `j.doe`. Once his PC was reconnected, see B23.

### B23. A PC's jobs never reach the server (auto-discovered printer, TLS refused)

- **Symptom:** one user can't print, even though their printquota account is
  fine (active, pages remaining) and printquota is up to date. On the
  server, `lpstat -W all -o <QUEUE>` shows **no new jobs** from that user, so
  printquota never sees them. Often the user also got *"the printer name is
  not correct"* when adding the printer by address, then added it through
  Windows' automatic discovery instead. In `/var/log/cups/error_log`,
  repeated lines:
  ```
  E [...] [Client 775] Unable to encrypt connection: A TLS fatal alert has been received.
  ```
- **Cause:** a printer that Windows discovers by itself (e.g. *"Office_Printer @
  printserver"*) connects to CUPS over an **encrypted** connection
  (`ipps`/`https`, using the server's `.local` name). CUPS answers with its
  **self-signed certificate**, Windows rejects it (the "TLS fatal alert"), and
  every job fails on the PC before it is sent. The PC's own print queue shows
  the job as *Error*.
- **Diagnosis:**
  1. On the server, `lpstat -W all -o <QUEUE> | tail`. No job from the user
     after their attempt means the problem is between the PC and the server,
     not in printquota.
  2. `sudo grep "Unable to encrypt connection" /var/log/cups/error_log | tail`
     shows TLS failures at the times the user tried to print.
  3. Match them to the PC: `sudo grep "<dd/Mon/yyyy:HH:MM>" /var/log/cups/access_log | awk '{print $1}' | sort | uniq -c`,
     and compare with the PC's address (`ipconfig` on the PC).
- **Fix (on the PC):**
  1. Remove **every** entry for the printer, including the auto-discovered
     one, and cancel anything stuck in its queue.
  2. In the PC's browser, open `http://<server-ip>:631/printers/<QUEUE>`. It
     must load. If it doesn't, something blocks port 631 between the PC and
     the server (firewall, antivirus, proxy). Test with
     `Test-NetConnection <server-ip> -Port 631` in PowerShell.
  3. **Add manually → Select a shared printer by name** →
     `http://<server-ip>:631/printers/<QUEUE>`, with the IP address, `http`
     and the exact capitals. Use the **Microsoft PS Class Driver** (B11).
  4. Print a test page and confirm the job appears in `lpstat` and Reports.
- **Prevention:** always connect PCs by the plain `http://<IP>` address (A9).
  Disabling `cups-browsed` (B10) also removes the automatic HP queues that
  kept reappearing in the log.
- **Status:** Config. **Verified 2026-09-25:** Jane Doe's PC. After
  removing the auto-discovered printer and re-adding
  `http://192.0.2.10:631/printers/Office_Printer`, his jobs arrived as
  `CORP\J.Doe` and were charged to `j.doe` (B22).

### B24. Changed the price, but Reports still shows ₦1.00 per page

- **Symptom:** after setting a new price, every job in **Reports** still costs
  ₦1.00 per page.
- **Cause 1:** when a printer is registered, it gets **its own price**,
  copied from the default at that moment (₦1). A printer's own price always
  wins, so changing *Default cost per mono page* on the **Settings** page
  doesn't affect it. That default is only used for printers with no price of
  their own.
- **Cause 2:** a job's cost is recorded **when it prints** and never changes
  afterwards, like a receipt. Jobs printed before a price change keep the old
  price.
- **Fix:** **Printers & queues** → the printer → **Edit costs** → set
  *Cost / mono page* (and *Cost / colour page*) → **Save**. The table's
  *Mono* column shows the price that applies. Print one page, and the new job
  in Reports has the new cost.
- **Note:** a printer price of **0** means "use the Settings default", not
  free (README §23). For a free printer, set the default to 0 as well.
- **Status:** Config.

### B25. The web console doesn't come back after rebooting a new server

- **Symptom:** on a freshly installed server, `http://<server>:8080` works
  until the first reboot, then doesn't answer.
  `systemctl is-enabled quota-api` says `disabled`.
- **Cause:** found in a code review on 2026-09-26, before it hit a real
  server. Since 0.2.0, `install.sh` *restarted* `quota-api` but no longer
  *enabled* it, so it wasn't set to start at boot. Servers first installed
  with an older version (like the reference server) were enabled then, and
  are unaffected.
- **Fix:** the installer enables every unit again
  (`systemctl enable --now quota-accounting quota-api quota-reset.timer quota-backup.timer`).
  On an affected server: `sudo systemctl enable --now quota-api`.
- **Check:** `systemctl is-enabled quota-api quota-accounting` should print
  `enabled` twice.
- **Status:** Fixed 0.2.5.

---

## Part C: Quick health checks

```bash
# Versions and services
/opt/printquota/bin/pip show printquota | grep Version
systemctl status quota-api quota-accounting --no-pager | grep -E "●|Active"
systemctl is-enabled quota-api quota-accounting            # both "enabled" (B25)
systemctl list-timers quota-reset.timer quota-backup.timer --no-pager

# CUPS queues (quota: prefix = enforced) and recent jobs with usernames
lpstat -v
lpstat -W all -o

# Why a job was allowed or denied, and how its pages were counted
sudo journalctl -u cups -n 50 --no-pager | grep -i printquota
sudo tail -n 30 /var/log/cups/error_log

# Which PCs are connecting, and any TLS (encryption) failures (B23)
sudo tail -n 20 /var/log/cups/access_log | awk '{print $1, $4, $6, $7}'
sudo grep -c "Unable to encrypt connection" /var/log/cups/error_log

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
| 0.2.5 | 2026-09-26 | HTML docs (`docs/html/`); installer enables `quota-api` again. B25 |
| 0.2.4 | 2026-09-25 | Match `DOMAIN\user` and different capitals to one account. B22 (verified). Client setup documented: B23, B24 |

Full details are in [CHANGELOG.md](../CHANGELOG.md).
