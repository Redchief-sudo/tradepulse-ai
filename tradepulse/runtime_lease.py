"""Account lease -- a host-local exclusive claim on one broker account.

Every order-capable command (`run`, `scan`, `monitor`) holds the lease for
its account for as long as any order-capable task runs, and the execution
gateway re-verifies it immediately before every submission.

Mutual exclusion holds among processes on **one host** that resolve the
**same lease directory** on a **local filesystem**. It does not cover another
host, another user or `TRADEPULSE_LEASE_DIR` (a different domain), or broker
API clients that are not TradePulse. Those remain the operator's
responsibility (one host per account, design section 3.8), and
reconciliation's integrity lock detects them after the fact.

The lock is ``flock`` on ``<dir>/<venue>-<account_number>.lock``: the kernel
releases it when the holder exits or dies, so a crash never strands it. Lease
files are never unlinked -- a deleted or replaced file would let a second
process lock a new inode -- and ``AccountLease.verify`` detects one by
comparing the held descriptor's inode with the path's.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from tradepulse.broker.port import AccountRef

LEASE_DIR_ENV = "TRADEPULSE_LEASE_DIR"
MOUNTINFO = Path("/proc/self/mountinfo")
PROC_LOCKS = Path("/proc/locks")
NETWORK_FILESYSTEMS = frozenset({"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"})


class AccountLeaseHeld(RuntimeError):
    """Another process holds the lease; the message names the recorded holder."""


class AccountLeaseUnavailable(RuntimeError):
    """The lease cannot be taken or is no longer verifiably held."""


def _unescape_mount_path(field: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as \\ooo."""
    out, i = [], 0
    while i < len(field):
        code = field[i + 1:i + 4]
        if field[i] == "\\" and len(code) == 3 and code.isdigit():
            out.append(chr(int(code, 8)))
            i += 4
            continue
        out.append(field[i])
        i += 1
    return "".join(out)


def filesystem_type(path: Path) -> str | None:
    """The filesystem type of the mount holding ``path``, from
    /proc/self/mountinfo (the longest mount point containing it); None where
    mountinfo is unavailable."""
    try:
        lines = MOUNTINFO.read_text().splitlines()
    except OSError:
        return None
    target = str(path.resolve())
    best: tuple[int, str] | None = None
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount_point = _unescape_mount_path(fields[4])
        inside = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if inside and (best is None or len(mount_point) >= best[0]):
            best = (len(mount_point), right.split()[0])
    return best[1] if best else None


def lease_directory() -> Path:
    """The one lease-directory resolver every caller uses:
    ``TRADEPULSE_LEASE_DIR``, else ``$XDG_STATE_HOME/tradepulse/leases``, else
    ``~/.local/state/tradepulse/leases``. Created with mode 0700. Refused on a
    network filesystem, where flock may not exclude across clients."""
    configured = os.environ.get(LEASE_DIR_ENV)
    if configured:
        directory = Path(configured).expanduser()
    else:
        state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        directory = Path(state_home).expanduser() / "tradepulse" / "leases"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    kind = filesystem_type(directory)
    if kind in NETWORK_FILESYSTEMS:
        raise AccountLeaseUnavailable(f"lease directory {directory} is on a network filesystem ({kind})")
    return directory


def lease_path(ref: AccountRef, directory: Path) -> Path:
    return directory / f"{ref.venue}-{ref.account_number}.lock"


def _same_file(fd: int, path: Path) -> bool:
    try:
        held, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _recorded_holder(path: Path) -> str:
    """The diagnostic record the holder wrote; best effort."""
    try:
        record = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return "an unknown holder"
    if not isinstance(record, dict) or not record:
        return "an unknown holder"
    return f"{record.get('holder', '?')} (pid {record.get('pid', '?')}, since {record.get('acquired_at', '?')})"


class AccountLease:
    """A held lease. ``verify()`` raises AccountLeaseUnavailable unless the
    descriptor is still open and still the file at ``path``."""

    def __init__(self, ref: AccountRef, path: Path, fd: int) -> None:
        self.ref = ref
        self.path = path
        self._fd: int | None = fd

    def verify(self) -> None:
        fd = self._fd
        if fd is None:
            raise AccountLeaseUnavailable(f"account lease for {self.ref.key} has been released")
        if not _same_file(fd, self.path):
            raise AccountLeaseUnavailable(f"account lease file {self.path} was deleted or replaced")

    def _release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


@contextmanager
def account_lease(ref: AccountRef, *, holder: str, directory: Path | None = None) -> Iterator[AccountLease]:
    """Hold the exclusive lease for ``ref`` for the duration of the block.
    Raises AccountLeaseHeld (naming the holder) if another process holds it."""
    directory = directory if directory is not None else lease_directory()
    path = lease_path(ref, directory)
    fd = -1
    for _ in range(2):  # one retry if the file was replaced between open and lock
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise AccountLeaseUnavailable(f"cannot open account lease file {path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AccountLeaseHeld(f"account {ref.key} is leased by {_recorded_holder(path)}") from None
        if _same_file(fd, path):
            break
        os.close(fd)
        fd = -1
    if fd < 0:
        raise AccountLeaseUnavailable(f"account lease file {path} keeps being replaced")
    record = json.dumps({"holder": holder, "pid": os.getpid(), "acquired_at": datetime.now(UTC).isoformat()})
    os.ftruncate(fd, 0)
    os.pwrite(fd, record.encode(), 0)
    lease = AccountLease(ref, path, fd)
    try:
        yield lease
    finally:
        lease._release()


def account_lease_in_use(ref: AccountRef, *, directory: Path | None = None) -> str | None:
    """Who holds the lease, or None -- without taking it. Reads the kernel's
    lock table (/proc/locks) instead of probing with a lock of its own, so a
    probe can never make a real acquisition fail."""
    directory = directory if directory is not None else lease_directory()
    path = lease_path(ref, directory)
    try:
        info = os.stat(path, follow_symlinks=False)
        table = PROC_LOCKS.read_text().splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AccountLeaseUnavailable(f"cannot inspect account lease {path}: {exc}") from exc
    wanted = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    for line in table:
        fields = line.split()
        if "->" in fields:  # a waiter, not a holder
            continue
        if len(fields) >= 6 and fields[1] == "FLOCK" and fields[5] == wanted:
            return _recorded_holder(path)
    return None
