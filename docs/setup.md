# Setup guide

This guide takes you from a fresh Ubuntu server to working print quotas, in
order. Each step says where it is done: the **server shell** (a few one-time
commands) or the **web console** (everything else).

- For the full reference (configuration, console, CLI, security), see the
  [README](../README.md).
- If something doesn't behave as described, see
  [Known issues and troubleshooting](known-issues.md).

**Before you start**, you need:

- Ubuntu Server 22.04 LTS or newer, with CUPS and your printers reachable on
  the network. Python 3.10+ is the stock interpreter, so nothing extra is
  needed.
- `sudo` access on the server.
- A fixed IP address for the server and for each printer (e.g. a DHCP
  reservation).

In the examples below, the server is `192.0.2.10`, the CUPS queue is
`Office_Printer`, and a user signs in to Windows as `CORP\J.Doe`.
Substitute your own values.

---

## 1. Install printquota (server shell)

Install from a clone of the repository on the server itself. Copying the
folder from a Windows PC can strip the scripts' executable permissions.

```bash
git clone https://github.com/Emmaceejay/print-quota-system.git
cd print-quota-system
git config pull.ff only
chmod +x scripts/*.sh
sudo ./scripts/install.sh 2>&1 | tail -30
```

`git config pull.ff only` makes a later `git pull` stop with an error,
instead of merging or asking how to reconcile, when the server's copy
doesn't match the repository
([details](known-issues.md#git-pull-fails-with-divergent-branches)).

The installer must finish with **"printquota is installed."** and a setup
link. Confirm the installation:

```bash
/opt/printquota/bin/pip show printquota | grep Version    # the version you installed
systemctl is-enabled quota-api quota-accounting            # "enabled" twice
systemctl status quota-api --no-pager | grep Active       # "active (running)"
```

If the installer stops early, see
[Installer stops at "Installing OS dependencies"](known-issues.md#installer-stops-at-installing-os-dependencies).

## 2. Create the first administrator (web console)

Open the link printed at the end of the installation:

```
http://192.0.2.10:8080/setup?token=…
```

Enter a username, your name, an email address for alerts (optional) and a
password of at least 8 characters, then click **Create administrator and
sign in**.

- The link contains a one-time token, and works only until the first
  administrator exists.
- If you've lost it, run `sudo grep SETUP_TOKEN /etc/printquota/env`, open
  `http://192.0.2.10:8080/setup` and paste the token.

## 3. Share the printer queues on the network (server shell)

CUPS only accepts connections from the server itself by default. Share it,
once:

```bash
sudo cupsctl --share-printers --remote-any
sudo lpadmin -p Office_Printer -o printer-is-shared=true   # repeat for each queue
sudo ufw allow 631/tcp                                     # only if the ufw firewall is enabled
```

## 4. Tidy up the CUPS queues (server shell)

List the queues CUPS knows about:

```bash
lpstat -v
```

Each line reads `device for <queue name>: <device URI>`. The **queue name**
is what users connect to; the **device URI** (for example
`lpd://192.0.2.20/lp` or `ipp://192.0.2.20/ipp/print`) is the printer's
own address.

- **Stop automatic queues.** Queues with `implicitclass://` URIs are created
  automatically by the `cups-browsed` service. They bypass quotas and can
  reappear at any time. Turn the service off:
  ```bash
  sudo systemctl disable --now cups-browsed
  ```
- **Remove test or unused queues**, e.g. one whose device URI is
  `/dev/null`: `sudo lpadmin -x <queue name>`.

## 5. Add printers and turn on quota enforcement (web console)

Go to **Printers & queues**.

- **Existing queue:** click **Turn on**. printquota registers the printer and
  puts the queue behind its quota check.
- **New printer:** use **Add a new printer to CUPS**. Give it a short name
  without spaces (e.g. `Office_Printer`), the device URI
  `ipp://<printer-ip>/ipp/print`, and the driver **IPP Everywhere**. Tick
  **Turn on quota enforcement straight away**.
- **Prices:** use **Edit costs** on each printer. A printer's own price takes
  priority over the default on the Settings page, and applies to jobs
  printed after the change.
- **Two-sided printing:** see below.

When a queue is enforced, `lpstat -v` shows its device URI with a `quota:`
prefix.

### Two-sided printing

To save paper, make queues print on both sides by default. New printers
added from the console are two-sided by default (the **Print on both sides
by default** tick). For an existing queue, open **Edit costs**, tick **Print
on both sides by default** and click **Save**. The **Sides** column then
shows *two-sided*. From the server shell, the equivalent is
`sudo /opt/printquota/bin/quotactl printer sides <queue> two-sided`.

This is set on the CUPS queue, so it applies to every computer and driver.
The client's own setting isn't enough: a Windows computer connected as in
[step 8](#8-connect-client-computers) doesn't pass its *Print on both sides*
choice to the server.

- **One-sided when needed.** A job that asks for one-sided still prints
  one-sided (macOS, Linux, and other clients that send the IPP `sides`
  option). For Windows users who sometimes need one-sided pages (forms,
  labels), add a second queue for the same printer, e.g.
  `Office_Printer_1-sided`. Leave it one-sided, turn on enforcement for it,
  and connect it on the computers that need it.
- **Check the result** with `lpoptions -p <queue> -l | grep -i duplex`. The
  starred value should be `DuplexNoTumble`. If the console warns that the
  driver has no two-sided option, the printer has no duplex unit, or the
  queue's driver doesn't know about it
  ([details](known-issues.md#jobs-print-on-one-side-only)).
- **Prices:** two-sided jobs are recorded as duplex and get the printer's
  duplex discount. Quotas still count printed sides.

## 6. Create users (web console)

Create each account with the user's **plain logon name**, e.g. `j.doe`,
without a `DOMAIN\` prefix. Print jobs sent as `CORP\J.Doe`, `J.Doe` or
`j.doe@corp.example.com` are all matched to `j.doe`.

- **A few users:** **Users → Add a user or administrator**.
- **Many users:** **Users → Import users…** (upload a CSV or paste rows,
  preview, then apply). To export Active Directory users in the right
  format, run this on a domain controller or a PC with the AD PowerShell
  module:

  ```powershell
  Get-ADUser -Filter 'Enabled -eq $true' -Properties mail,Department |
    Select-Object @{n='username';e={$_.SamAccountName}},
                  @{n='display_name';e={$_.Name}},
                  @{n='email';e={$_.mail}},
                  @{n='group';e={$_.Department}} |
    Export-Csv users.csv -NoTypeInformation -Encoding UTF8
  ```

  Tick **Create groups that do not exist yet** so each department becomes a
  group.

Imported users don't need a printquota password to print. A password is
only needed to view their balance in the self-service portal. To use
directory passwords for the portal, see README §13.

## 7. Configure settings and alerts (web console)

On **Settings**, set the default quota and period, the enforcement mode,
the currency and default prices, and alert delivery (SMTP server or
webhook). Then click **Send test alert**.

## 8. Connect client computers

Always connect client computers **by address**, using the server's IP and
plain `http`. Don't use printers that Windows discovers by itself: they use
an encrypted connection that Windows rejects, and jobs never reach the
server ([details](known-issues.md#a-users-jobs-never-reach-the-server)).

**Windows 10/11**

1. Remove any existing entries for the printer, especially auto-discovered
   ones such as *"Office_Printer @ printserver"*.
2. Check the address in the computer's browser:
   `http://192.0.2.10:631/printers/Office_Printer`. The printer's CUPS page
   must load.
3. **Settings → Bluetooth & devices → Printers & scanners → Add device →
   Add manually → Select a shared printer by name**, and enter the same
   address. The queue name is case-sensitive.
4. Driver: **Microsoft → Microsoft PS Class Driver**. If it isn't listed, use
   **Generic → MS Publisher Imagesetter**. Don't use the PCL6 or XPS class
   drivers ([why](known-issues.md#which-windows-driver-to-use)).
5. In **Printing preferences**, set the paper size (e.g. A4).

**macOS:** System Settings → Printers & Scanners → **Add Printer** → **IP**:
address `192.0.2.10`, protocol **IPP**, queue `printers/Office_Printer`.

**Linux:**
`sudo lpadmin -p Office_Printer -E -v ipp://192.0.2.10/printers/Office_Printer -m everywhere`

## 9. Test the setup

1. In **Users**, open a test user, click **Reset usage and restart period**
   and set the quota to **3**.
2. From that user's computer, print **2 pages**. They print, and the user
   shows 2 used and 1 remaining.
3. Print **4 pages**. **Nothing prints**, and the job appears in **Reports**
   as denied: *quota exceeded*.
4. On the server, check how pages were counted:
   ```bash
   sudo journalctl -u cups -n 50 --no-pager | grep -o "method='[^']*'\|pages=[0-9]*"
   ```
   `spool:postscript` or `spool:pdf` means an exact count; `fallback` means
   a guess ([see here](known-issues.md#jobs-are-counted-as-one-page)).

## 10. Secure the deployment

Before relying on the numbers, close the two ways around the quota:

- **Printing directly to the printer.** A computer that can reach a
  printer's IP address can add it directly and skip printquota. On each
  printer's web admin page, restrict printing to the server's IP address
  (often called *IP filtering* or *access control*), or block it with your
  network firewall. Remove any direct printer connections from client
  computers.
- **Printing under someone else's name.** By default CUPS trusts the user
  name each computer sends. Require a login for printing in
  `/etc/cups/cupsd.conf` (README §20) and restart CUPS. In an Active
  Directory domain, join the server to the domain first (`realm join`), so
  users authenticate with their domain accounts.

---

## Upgrading

```bash
cd ~/print-quota-system
git config pull.ff only                    # once per server; harmless to repeat
git pull --ff-only origin main && sudo ./scripts/install.sh 2>&1 | tail -25
/opt/printquota/bin/pip show printquota | grep Version    # must show the new version
```

The `&&` runs the installer only if the pull succeeded. Otherwise the
installer would reinstall the old version. If the pull fails with
*divergent branches* or *Not possible to fast-forward*, see
[git pull fails with divergent branches](known-issues.md#git-pull-fails-with-divergent-branches).
If the version doesn't change, see
[An upgrade doesn't take effect](known-issues.md#an-upgrade-doesnt-take-effect).

## Health checks

```bash
# Version and services
/opt/printquota/bin/pip show printquota | grep Version
systemctl is-enabled quota-api quota-accounting
systemctl status quota-api quota-accounting --no-pager | grep -E "●|Active"
systemctl list-timers quota-reset.timer quota-backup.timer --no-pager

# Queues (a quota: prefix means enforced) and recent jobs with user names
lpstat -v
lpstat -W all -o | tail -20

# Why a job was allowed or refused, and how its pages were counted
sudo journalctl -u cups -n 50 --no-pager | grep -i printquota
sudo tail -n 30 /var/log/cups/error_log

# Which computers are connecting, and any failed encrypted connections
sudo tail -n 20 /var/log/cups/access_log | awk '{print $1, $4, $6, $7}'
sudo grep -c "Unable to encrypt connection" /var/log/cups/error_log

# Web console and database
curl -s http://localhost:8080/healthz                     # {"status":"ok"}

# Permissions the web console needs to manage queues
id printquota                                              # includes lp and lpadmin
ls -l /usr/lib/cups/backend/quota                          # -rwx------ root root
```
