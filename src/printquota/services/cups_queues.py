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

import re
import shutil
import subprocess
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
