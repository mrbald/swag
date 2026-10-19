"""Linux process ownership primitives used by the launcher and its children."""

from __future__ import annotations

import ctypes
import fcntl
import os
from pathlib import Path
import signal
import sys


def exclusive_lock(path: Path) -> int:
    """Keep a stable inode: unlinking a lock can admit two independent owners."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def parent_death_signal(parent_pid: int, signum: int) -> None:
    """Arm before exec, checking both sides of prctl's parent-death race."""
    if sys.platform != "linux":
        raise RuntimeError("desktop process ownership requires Linux PR_SET_PDEATHSIG")
    if os.getppid() != parent_pid:
        raise RuntimeError("owning parent already exited")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signum), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signum)
        raise RuntimeError("owning parent exited while arming death signal")


def group_alive(pgid: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return False


def child_main() -> None:
    parent_pid = int(sys.argv[1])
    command = sys.argv[2:]
    parent_death_signal(parent_pid, signal.SIGKILL)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    child_main()
