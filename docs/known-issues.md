# Known issues and troubleshooting

Problems you may run into when deploying or running printquota, with the
cause and the resolution for each. Start with the section whose symptom
matches what you see.

**First, check the installed version** matches the latest release in the
[changelog](../CHANGELOG.md). Several problems below are fixed in newer
versions.

```bash
/opt/printquota/bin/pip show printquota | grep Version
```

For setup steps, see the [setup guide](setup.md). The [Health checks](setup.md#health-checks)
there are a good first diagnostic.

---

## Troubleshooting

### A user's jobs never reach the server

**Symptom.** One user can't print, although their account is active and has
pages left. `lpstat -W all -o <queue>` on the server shows no new job from
them, so printquota never sees the job. On their computer, the job sits in
the print queue with *Error*. The CUPS error log shows lines such as:

```
E [...] [Client 775] Unable to encrypt connection: A TLS fatal alert has been received.
```

**Cause.** The computer uses a printer that Windows discovered by itself
(e.g. *"Office_Printer @ printserver"*). Such printers connect over an
encrypted connection (`ipps`/`https`). CUPS presents a self-signed
certificate, Windows rejects it, and the job fails before it is sent.

**Resolution.**

1. On the computer, remove **every** entry for the printer, including the
   auto-discovered one, and cancel anything stuck in its queue.
2. Open `http://<server-ip>:631/printers/<queue>` in the computer's browser.
   If the page doesn't load, see
   [Adding a printer by address fails](#adding-a-printer-by-address-fails).
3. Add the printer by address, as described in
   [setup step 8](setup.md#8-connect-client-computers), with the
   **Microsoft PS Class Driver**.

To confirm which computer is failing, compare the IP addresses in
`/var/log/cups/access_log` at the time of the errors with the computer's
address (`ipconfig`).

### Adding a printer by address fails

**Symptom.** Windows reports that the printer name is incorrect or can't be
found when you enter `http://<server-ip>:631/printers/<queue>`.

**Cause.** Either the address is not exactly right, or the computer can't
reach the server on port 631.

**Resolution.**

- Open the same address in the computer's browser. If the CUPS page for the
  printer loads, copy the address from the browser. The queue name is
  case-sensitive, and the address must use `http`, not `https`.
- If it doesn't load, test the connection in PowerShell:
  `Test-NetConnection <server-ip> -Port 631`. If `TcpTestSucceeded` is
  `False`, a firewall, antivirus product or network segment is blocking the
  port.
- If the computer uses a web proxy, add the server's address to the proxy
  exceptions.
- Make sure the queue is shared ([setup step 3](setup.md#3-share-the-printer-queues-on-the-network-server-shell)).

### Jobs are cancelled with "no print account for this user"

**Symptom.** The CUPS error log shows
`[Job N] print job denied: no print account for this user`, followed by
`Backend returned status 5 (cancel job)`. The job does not appear in
**Reports**.

**Cause.** No printquota account matches the name the computer sent.
Printing through the server is working; only the account is missing.

**Resolution.** Find the name that was sent:

```bash
lpstat -W all -o <queue> | tail
```

The second column is the user name. Create the account with the plain
logon name. Capitals and a `DOMAIN\` prefix don't matter: jobs sent as
`CORP\J.Doe`, `J.Doe` or `j.doe@corp.example.com` all match the account
`j.doe` (version 0.2.4 and later).

### Jobs are refused with "matches more than one print account"

**Cause.** Two accounts differ only in capital letters (for example `j.doe`
and `J.Doe`), so printquota can't tell which one a job belongs to. It
refuses the job rather than guessing.

**Resolution.** Keep one account and delete the other. Move its quota
first if needed. Current versions don't allow creating such duplicates.

### Which Windows driver to use

**Symptom.** Jobs fail, print garbage, or are counted as a single page.

**Cause.** When a printer is added by address, Windows offers generic class
drivers. The CUPS queue converts the job for the printer, and printquota
counts its pages, so the job must arrive in a format both can read.

**Resolution.** Use **Microsoft PS Class Driver** (PostScript). If it isn't
listed, use **Generic → MS Publisher Imagesetter**. Don't use:

| Driver | Why not |
|---|---|
| Microsoft PCL6 Class Driver | CUPS can't convert PCL input |
| Microsoft MS-XPS / OpenXPS Class Driver | CUPS can't read XPS |

To check whether a queue has a driver on the server, run
`lpstat -l -p <queue> | grep -i interface`. A `.ppd` file means the server
converts jobs, so no printer-specific driver is needed on client computers.
No `.ppd` means a raw queue: either install the printer's own driver on
every client, or convert the queue to a driverless one:

```bash
sudo lpadmin -p <queue> -E -v ipp://<printer-ip>/ipp/print -m everywhere
```

Then click **Turn on** for the queue again in **Printers & queues**.

### Jobs print on one side only

**Symptom.** Jobs print on one side of the paper, even when the user chose
*Print on both sides*, or the option is missing on the computer.

Windows programs may offer only *Print on both sides manually*.

**Cause.** Two-sided printing is decided by the CUPS queue on the server.
Queues print one-sided unless set otherwise, and a Windows computer
connected by address doesn't pass its own two-sided choice to the server.
The Microsoft PS Class Driver has no two-sided option, which is why Windows
programs only offer the manual method. Before 0.3.0, the *Supports duplex*
tick in the console was only recorded and didn't change the queue.

The queue's driver on the server must have a two-sided option. Vendor
drivers come in one version per model, and the version for a model without
a duplex unit has no two-sided option. For example, the `RICOH MP 2014`
driver has none, while the printer may be an `MP 2014AD` with automatic
duplex. Check which driver a queue uses:

```bash
curl -s http://localhost:631/printers/<queue>.ppd | grep -E '^\*(NickName|OpenUI \*Duplex)'
```

A `*NickName` line without an `*OpenUI *Duplex` line means the driver has no
two-sided option.

**Resolution.**

1. Upgrade to 0.3.0 or later.
2. In **Printers & queues**, open **Edit costs** for the queue, tick
   **Print on both sides by default** and click **Save**. See
   [Two-sided printing](setup.md#two-sided-printing).
3. Read the message shown after saving:
   - *raw queue*: CUPS passes jobs through unchanged, so only the computer's
     driver can print two-sided. Convert the queue to a driverless one (see
     [Which Windows driver to use](#which-windows-driver-to-use)), then set
     it again.
   - *no two-sided option*: the queue's driver can't print two-sided. If the
     printer has a duplex unit, switch the queue to the driver for the
     duplex model. List the installed versions, then pick the one with
     `D` or `AD` in its name:
     ```bash
     lpinfo -m | grep -i '<model, e.g. MP 2014>'
     sudo lpadmin -p <queue> -m '<first column of the chosen line>'
     ```
     This keeps quota enforcement on: only the driver changes. If the
     printer supports driverless printing, use `-m everywhere` instead. If
     no duplex version is installed, get the full Linux driver package from
     the manufacturer. Then save **Print on both sides by default** again.
4. Check the queue with `lpoptions -p <queue> -l | grep -i duplex`. The
   starred choice should be `DuplexNoTumble`.
5. Print a 2-page document normally (not *manually*). It should come out on
   one sheet and use 1 page of quota.

A `force_duplex` policy can't fix this on `socket://`, `lpd://` or `usb://`
printers. It is applied after the job is rendered, and only IPP printers act
on it then. Use the queue setting.

### Jobs are counted as one page

**Symptom.** Multi-page jobs are charged as one page, and over-quota jobs
print.

**Cause.** The document couldn't be counted before printing: an
uncountable format (PCL6 or XPS driver), or the CUPS spool directory
couldn't be read. Versions before 0.2.1 also counted every job on a queue
with a driver as one page.

**Resolution.** Check how recent jobs were counted:

```bash
sudo journalctl -u cups -n 50 --no-pager | grep -o "method='[^']*'\|pages=[0-9]*"
```

`spool:postscript` and `spool:pdf` are exact. A method ending in
`fallback` is a guess: switch the client to the
[recommended driver](#which-windows-driver-to-use). If CUPS uses a
non-default spool directory (`RequestRoot`), set `printing.spool_dir` to
match.

### There is no page_log file

**Symptom.** `/var/log/cups/page_log` does not exist, and the accounting
service logs that it is missing.

**Cause.** Many printer drivers never report page counts to CUPS, so CUPS
never creates the file. This is normal.

**Resolution.** None needed. Jobs are charged at the page count measured
before printing, which is exact for PDF and PostScript documents. When CUPS
does write a `page_log`, charges are corrected from it automatically.

### A new price doesn't appear in Reports

**Cause.** A printer registered in printquota has its own price, copied from
the default when it was registered, and that price takes priority over the
**Settings** default. A job's cost is also fixed when it prints, so past
jobs keep their original price.

**Resolution.** Set the price on the printer: **Printers & queues → Edit
costs**. New jobs use it. A printer price of **0** means "use the Settings
default", not free.

### "Forbidden" when turning on a queue or adding a printer

**Cause.** The `printquota` service account isn't in the CUPS
administrators' group (`lpadmin`), or the web service was started before it
was added.

**Resolution.**

```bash
sudo usermod -a -G lpadmin printquota
sudo systemctl restart quota-api
id printquota     # must list lpadmin
```

Re-running `install.sh` also does this.

### Automatic "implicitclass" queues keep appearing

**Symptom.** `lpstat -v` lists queues with `implicitclass://` device URIs,
sometimes duplicated, and the CUPS log shows `CreateProfile failed …
AlreadyExists` messages.

**Cause.** The `cups-browsed` service creates queues for printers it
discovers. They aren't enforced, and CUPS can recreate them at any time.

**Resolution.** Create one permanent queue per printer
([setup step 5](setup.md#5-add-printers-and-turn-on-quota-enforcement-web-console)),
then run `sudo systemctl disable --now cups-browsed`.

### git pull fails with divergent branches

**Symptom.** `git pull` prints `(forced update)`, then
`fatal: Need to specify how to reconcile divergent branches` (or, with
`pull.ff only`, `fatal: Not possible to fast-forward, aborting`). If the
installer was run afterwards, it reinstalled the old version.

**Cause.** The repository's history was rewritten after the server cloned
it, so the server's copy and the repository no longer share a history. Git
won't guess which one to keep.

**Resolution.** Make the server's copy match the repository. Settings
(`/etc/printquota/`) and the database (`/var/lib/printquota/`) are outside
the checkout and aren't affected. First check that the server has nothing
of its own:

```bash
cd ~/print-quota-system
git fetch origin
git status --short                     # must print nothing
git log --oneline origin/main..main    # commits only on the server
```

If `git status` prints nothing and the log lists only older copies of
commits that are already in the repository, run:

```bash
git reset --hard origin/main
git config pull.ff only
sudo ./scripts/install.sh 2>&1 | tail -25
/opt/printquota/bin/pip show printquota | grep Version
```

If there are local changes or commits you don't recognise, save them first
(for example `git branch local-backup`), since `reset --hard` discards them.

**Prevention.** Set `git config pull.ff only` on every server (it's part of
[setup step 1](setup.md#1-install-printquota-server-shell)), and upgrade with
`git pull --ff-only origin main && sudo ./scripts/install.sh`, so the
installer never runs after a failed pull.

### An upgrade doesn't take effect

**Symptom.** After `git pull` and reinstalling, the old behaviour remains.

**Diagnosis.**

```bash
git log --oneline -1                                      # latest commit present?
/opt/printquota/bin/pip show printquota | grep Version    # new version installed?
systemctl status quota-api --no-pager | grep Active       # restarted after the upgrade?
```

**Cause.** Usually either `git pull` failed, so the new code never arrived
(see [git pull fails with divergent branches](#git-pull-fails-with-divergent-branches)),
or the installer stopped before installing the new version (see the next
entry). Either way, the old version is still installed and running.

**Resolution.** Re-run the installer and check that it finishes. Or, when a
release has no database changes, install and restart directly:

```bash
sudo /opt/printquota/bin/pip install --upgrade ~/print-quota-system
sudo systemctl restart quota-api quota-accounting
```

### Installer stops at "Installing OS dependencies"

**Symptom.** The installer ends after messages such as
`E: Failed to fetch … File has unexpected size … Mirror sync in progress?`.

**Cause.** `apt-get update` failed, typically because an Ubuntu mirror was
mid-sync or a third-party package repository is broken. Before 0.2.3 the
installer stopped at that point.

**Resolution.** Upgrade to 0.2.3 or later. The installer then skips the step
when all packages are present, and treats `apt-get update` errors as
warnings. On older versions, use the direct install in
[the previous entry](#an-upgrade-doesnt-take-effect), or retry later.

### The web console is unavailable after a reboot

**Symptom.** `http://<server-ip>:8080` stops answering after the server
restarts, and `systemctl is-enabled quota-api` prints `disabled`.

**Cause.** Installers from 0.2.0 to 0.2.4 started the web service but didn't
enable it at boot on new installations.

**Resolution.** `sudo systemctl enable --now quota-api`, or upgrade to 0.2.5
or later and re-run the installer.

### Nobody can sign in to the console

**Resolution.** On the server, reset a password and restore administrator
rights for an account. No other data changes:

```bash
sudo /opt/printquota/bin/quotactl db init --admin <username>
sudo /opt/printquota/bin/quotactl user enable <username>    # if the account was disabled
```

### "Permission denied" when running the scripts

**Cause.** The repository was copied from a Windows computer, which drops
the executable permission from the scripts.

**Resolution.** `chmod +x scripts/*.sh`, or clone the repository directly on
the server.

### Messages in the CUPS error log that are harmless

| Message | Meaning |
|---|---|
| `Printer drivers are deprecated and will stop working in a future version of CUPS` | A general notice about PPD drivers from CUPS |
| `Raw queues are deprecated …` | The same notice, for queues without a driver |
| `CreateProfile failed … AlreadyExists` | Colour-profile messages from automatic queues; they stop once `cups-browsed` is disabled |
| `Returning IPP client-error-bad-request for windows-ext` | A Windows computer probing the server while adding a printer |

`Scheduler shutting down due to program error` on its own only needs
attention if it repeats. Then check `sudo journalctl -u cups --since "<time>"`.

---

## Resolved in earlier releases

If you run an older version, these problems are fixed by upgrading. Details
are in the [changelog](../CHANGELOG.md).

| Problem | Fixed in |
|---|---|
| The web console wasn't enabled at boot on new installations | 0.2.5 |
| Jobs refused when the user name arrived as `DOMAIN\User` or with different capitals | 0.2.4 |
| Installer stopped when `apt-get update` failed | 0.2.3 |
| Settings page rejected every change when an unrelated field was invalid ("Nothing was saved") | 0.2.2 |
| Jobs on queues with a driver counted as one page, so over-quota jobs printed | 0.2.1 |
| Deleting a user who had printed failed | 0.2.0 |
| The nightly database backup never ran | 0.2.0 |
