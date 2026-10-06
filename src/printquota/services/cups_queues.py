"""CUPS queue administration for the web console.

This is the console's equivalent of ``scripts/register_backend.sh``: list
the queues CUPS knows about, put a queue behind the quota backend
(``lpadmin -p Q -v quota:<real-uri>``), take it back out, and create new
queues.

The web service runs as the unprivileged ``printquota`` user. CUPS lets
members of its ``SystemGroup`` (``lpadmin`` on Ubuntu) administer queues
over the local socket, so ``install.sh`` adds ``printquota`` to that group.
Nothing here needs root, and nothing runs through a shell: every call is an
argument list with a timeout, and queue names and URIs are validated first
so they can never be read as options.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..core.exceptions import PrintQuotaError

SCHEME = "quota"

#: Conservative subset of what CUPS accepts: no spaces, slashes, quotes or
#: '#', and it may not start with '-' (so it can never look like an option).
QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,126}$")
#: scheme:rest, no whitespace, scheme may not start with '-'.
DEVICE_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:\S+$")

DRIVERS = {
    "everywhere": "IPP Everywhere (driverless) - recommended for network printers",
    "raw": "Raw queue - the client's driver does all the work",
}

_PERMISSION_HINT = (
    "The web service is not allowed to administer CUPS. Make sure the "
    "'printquota' account is in the 'lpadmin' group (re-run scripts/install.sh, "
    "or: sudo usermod -a -G lpadmin printquota) and restart quota-api."
)


class CupsError(PrintQuotaError):
    """A CUPS command failed; the message is safe to show an administrator."""


@dataclass
class Queue:
    """One CUPS destination as the console shows it."""

    name: str
    device_uri: str
    state: str = "unknown"  # idle | printing | disabled | unknown

    @property
    def enforced(self) -> bool:
        return self.device_uri.startswith(f"{SCHEME}:")

    @property
    def real_uri(self) -> str:
        return self.device_uri[len(SCHEME) + 1 :] if self.enforced else self.device_uri


#: The function used to run commands. Tests replace it with a fake CUPS.
Runner = Callable[[list[str], int], "subprocess.CompletedProcess[str]"]


def _default_runner(args: list[str], timeout: int) -> "subprocess.CompletedProcess[str]":
    binary = shutil.which(args[0])
    if binary is None:
        raise CupsError(
            f"'{args[0]}' was not found. Install the CUPS client tools on this server "
            "(sudo apt install cups-client)."
        )
    try:
        return subprocess.run(
            [binary, *args[1:]], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise CupsError(f"'{args[0]}' did not answer within {timeout} seconds") from None
    except OSError as exc:
        raise CupsError(f"could not run '{args[0]}': {exc}") from None


runner: Runner = _default_runner


def _run(args: list[str], timeout: int = 20) -> str:
    proc = runner(args, timeout)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip() or f"{args[0]} exited with {proc.returncode}"
        lowered = message.lower()
        if "forbidden" in lowered or "not authorized" in lowered or "unauthorized" in lowered:
            raise CupsError(f"{message}. {_PERMISSION_HINT}")
        raise CupsError(message)
    return proc.stdout or ""


def validate_queue_name(name: str) -> str:
    name = (name or "").strip()
    if not QUEUE_NAME_RE.fullmatch(name):
        raise CupsError(
            "Queue names may use letters, digits, '.', '_' and '-' (up to 127 characters) "
            "and must not start with '-' or '.'."
        )
    return name


def validate_device_uri(uri: str) -> str:
    uri = (uri or "").strip()
    if not DEVICE_URI_RE.fullmatch(uri):
        raise CupsError(
            "Enter a device URI such as socket://10.0.0.5:9100, ipp://10.0.0.6/ipp/print "
            "or usb://HP/LaserJet?serial=..."
        )
    if uri.startswith(f"{SCHEME}:"):
        raise CupsError("Enter the printer's real device URI, not a quota: URI.")
    return uri


def list_queues() -> list[Queue]:
    """Every CUPS destination with its device URI and state."""
    proc = runner(["lpstat", "-v"], 20)
    output = proc.stdout or ""
    if proc.returncode != 0:
        message = (proc.stderr or "").strip()
        # "lpstat: No destinations added." is an empty system, not an error.
        if "no destinations" in message.lower():
            return []
        raise CupsError(message or "lpstat failed; is CUPS running?")

    queues: dict[str, Queue] = {}
    for line in output.splitlines():
        match = re.match(r"^device for (?P<name>\S+):\s*(?P<uri>\S+)\s*$", line.strip())
        if match:
            queues[match.group("name")] = Queue(match.group("name"), match.group("uri"))

    try:
        states = _run(["lpstat", "-p"])
    except CupsError:
        states = ""
    for line in states.splitlines():
        match = re.match(r"^printer (?P<name>\S+) (?:is (?P<state>\w+)|(?P<disabled>disabled))", line)
        if match and match.group("name") in queues:
            queues[match.group("name")].state = (
                "disabled" if match.group("disabled") else match.group("state") or "unknown"
            )
    return sorted(queues.values(), key=lambda q: q.name.lower())


def get_queue(name: str) -> Queue:
    for queue in list_queues():
        if queue.name == name:
            return queue
    raise CupsError(f"CUPS has no queue named '{name}'.")


def backend_installed(backend_dir: str) -> bool:
    """Whether the quota wrapper backend is present (install.sh puts it there)."""
    return (Path(backend_dir) / SCHEME).is_file()


def enforce(name: str, backend_dir: str) -> Queue:
    """Route a queue through the quota backend. Returns the queue *before* the change."""
    name = validate_queue_name(name)
    queue = get_queue(name)
    if queue.enforced:
        return queue
    if not backend_installed(backend_dir):
        raise CupsError(
            f"The quota backend is not installed at {Path(backend_dir) / SCHEME}. "
            "Run scripts/install.sh on the server first."
        )
    _run(["lpadmin", "-p", name, "-v", f"{SCHEME}:{queue.device_uri}"])
    return queue


def release(name: str) -> Queue:
    """Restore a queue's real device URI. Returns the queue *before* the change."""
    name = validate_queue_name(name)
    queue = get_queue(name)
    if not queue.enforced:
        return queue
    _run(["lpadmin", "-p", name, "-v", queue.real_uri])
    return queue


def add_queue(
    name: str,
    device_uri: str,
    driver: str = "everywhere",
    description: Optional[str] = None,
    location: Optional[str] = None,
) -> None:
    """Create and enable a new CUPS queue."""
    name = validate_queue_name(name)
    device_uri = validate_device_uri(device_uri)
    if driver not in DRIVERS:
        raise CupsError(f"Unknown driver '{driver}'.")
    if any(q.name.lower() == name.lower() for q in list_queues()):
        raise CupsError(f"CUPS already has a queue named '{name}'.")
    args = ["lpadmin", "-p", name, "-E", "-v", device_uri, "-m", driver]
    if description:
        args += ["-D", description.strip()[:127]]
    if location:
        args += ["-L", location.strip()[:127]]
    # IPP Everywhere queries the printer to build its driver, which can take a while.
    _run(args, timeout=90)


# ------------------------------------------------------------------ two-sided
#: IPP ``sides`` values. Long-edge binding is what "print on both sides"
#: means for portrait documents, and what every duplex printer supports.
SIDES_TWO = "two-sided-long-edge"
SIDES_ONE = "one-sided"

_PPD_OPTION_RE = re.compile(r"^(?P<key>[^/:\s]+)(?:/(?P<label>[^:]*))?:\s*(?P<choices>.*)$")
#: Choices a driver uses to say "the duplex unit is fitted".
_INSTALLED_CHOICES = ("True", "Installed")


@dataclass
class PpdOption:
    key: str
    label: str
    choices: list[str]
    default: Optional[str]


@dataclass
class DuplexResult:
    """What :func:`set_duplex_default` changed, and anything to tell the admin."""

    two_sided: bool
    ppd_settings: dict[str, str]
    warning: Optional[str] = None


def queue_options(name: str) -> dict[str, PpdOption]:
    """The driver (PPD) options of a queue, from ``lpoptions -l``.

    A raw queue has no driver and therefore no options; that is reported as
    an empty dict rather than an error.
    """
    name = validate_queue_name(name)
    try:
        output = _run(["lpoptions", "-p", name, "-l"])
    except CupsError:
        return {}
    options: dict[str, PpdOption] = {}
    for line in output.splitlines():
        match = _PPD_OPTION_RE.match(line.strip())
        if not match:
            continue
        default = None
        choices = []
        for choice in match.group("choices").split():
            if choice.startswith("*"):
                choice = choice[1:]
                default = choice
            choices.append(choice)
        key = match.group("key")
        options[key] = PpdOption(key, (match.group("label") or key).strip(), choices, default)
    return options


def _duplex_choice_option(options: dict[str, PpdOption]) -> Optional[PpdOption]:
    """The driver option that selects one- or two-sided printing, if any."""
    if "Duplex" in options and "DuplexNoTumble" in options["Duplex"].choices:
        return options["Duplex"]
    for option in options.values():
        if "duplex" in option.key.lower() and "DuplexNoTumble" in option.choices:
            return option
    return None


def _duplex_unit_options(options: dict[str, PpdOption]) -> list[PpdOption]:
    """Installable-option switches such as ``OptionDuplex: *False True``."""
    return [
        option
        for option in options.values()
        if ("duplex" in option.key.lower() or "duplex" in option.label.lower())
        and any(choice in option.choices for choice in _INSTALLED_CHOICES)
        and "DuplexNoTumble" not in option.choices
    ]


def set_duplex_default(name: str, two_sided: bool) -> DuplexResult:
    """Make a queue print two-sided (or one-sided) unless a job asks otherwise.

    Sets the IPP default ``sides-default``, which CUPS applies to every job
    that does not carry its own ``sides`` and maps onto the driver. For
    queues with a driver it also sets the driver's own duplex option and,
    when turning two-sided on, marks an optional duplex unit as installed,
    because many vendor drivers ship with it "not installed".
    """
    name = validate_queue_name(name)
    get_queue(name)  # a clear error if CUPS has no such queue
    options = queue_options(name)
    ppd_settings: dict[str, str] = {}
    warning: Optional[str] = None

    choice_option = _duplex_choice_option(options)
    if two_sided:
        for unit in _duplex_unit_options(options):
            ppd_settings[unit.key] = next(c for c in _INSTALLED_CHOICES if c in unit.choices)
    if choice_option is not None:
        if two_sided:
            ppd_settings[choice_option.key] = "DuplexNoTumble"
        elif "None" in choice_option.choices:
            ppd_settings[choice_option.key] = "None"

    if two_sided and choice_option is None:
        if options:
            warning = (
                f"CUPS's driver for {name} has no two-sided option, so the printer will keep "
                "printing one-sided. If the printer has a duplex unit, the queue is usually "
                "using the driver for a model without one: switch it to the variant with "
                "duplex (often a name ending in D or AD; list them with lpinfo -m), or to "
                "IPP Everywhere if the printer supports it. Then save this setting again."
            )
        else:
            warning = (
                f"{name} is a raw queue: CUPS passes jobs through unchanged, so two-sided "
                "printing depends on each computer's own printer driver."
            )

    args = ["lpadmin", "-p", name, "-o", f"sides-default={SIDES_TWO if two_sided else SIDES_ONE}"]
    for key, value in ppd_settings.items():
        args += ["-o", f"{key}={value}"]
    _run(args)
    return DuplexResult(two_sided, ppd_settings, warning)


# ------------------------------------------------------- one- and two-sided copies
#: Where CUPS serves each queue's driver (PPD) file. Local requests need no
#: authentication, and it avoids needing read access to /etc/cups/ppd.
CUPS_URL = "http://localhost:631"


def _default_ppd_fetcher(name: str) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(f"{CUPS_URL}/printers/{name}.ppd", timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # a raw queue has no driver
            return None
        raise CupsError(f"CUPS would not send the driver for {name}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise CupsError(f"could not reach CUPS at {CUPS_URL}: {exc}") from None


#: Fetches a queue's PPD, or None for a raw queue. Tests replace it.
ppd_fetcher: Callable[[str], Optional[bytes]] = _default_ppd_fetcher


def copy_queue(source: str, name: str, backend_dir: str, description: Optional[str] = None) -> Queue:
    """Create queue ``name`` for the same printer as ``source``, with the same driver.

    The copy is shared and enforced from the start, so it can never be a
    way around the quota. Returns the source queue.
    """
    source = validate_queue_name(source)
    name = validate_queue_name(name)
    queues = list_queues()
    original = next((q for q in queues if q.name == source), None)
    if original is None:
        raise CupsError(f"CUPS has no queue named '{source}'.")
    if any(q.name.lower() == name.lower() for q in queues):
        raise CupsError(f"CUPS already has a queue named '{name}'.")
    if not backend_installed(backend_dir):
        raise CupsError(
            f"The quota backend is not installed at {Path(backend_dir) / SCHEME}. "
            "Run scripts/install.sh on the server first."
        )

    ppd = ppd_fetcher(source)
    args = [
        "lpadmin", "-p", name, "-E", "-v", f"{SCHEME}:{original.real_uri}",
        "-o", "printer-is-shared=true",
    ]
    if description:
        args += ["-D", description.strip()[:127]]
    temp: Optional[str] = None
    try:
        if ppd:
            handle, temp = tempfile.mkstemp(prefix="printquota-", suffix=".ppd")
            with os.fdopen(handle, "wb") as out:
                out.write(ppd)
            args += ["-P", temp]
        else:
            args += ["-m", "raw"]
        _run(args, timeout=60)
    finally:
        if temp:
            Path(temp).unlink(missing_ok=True)
    return original
