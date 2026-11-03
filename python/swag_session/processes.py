"""Linux process ownership primitives used by the launcher and its children."""

from __future__ import annotations

import ctypes
import fcntl
import os
from pathlib import Path
import signal
import socket
import stat
import sys


QT_WS_LISTEN_FD = "SWAG_QT_WS_LISTEN_FD"


def validate_unix_path(path: Path, *, suffix_bytes: int = 0) -> None:
    """Match the Qt listener's canonical filesystem path and parameter limits."""
    if (not path.is_absolute() or any(character in str(path) for character in "\0:=")
            or len(os.fsencode(path)) + suffix_bytes > (103 if sys.platform == "darwin" else 107)):
        raise ValueError("Unix listener requires a short canonical absolute path without ':', '=' or NUL")
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("Unix listener path cannot be resolved") from error
    if canonical != path:
        raise ValueError("Unix listener path must be canonical, without symlinks")


def unix_socket_identity(path: Path) -> tuple[int, int]:
    """Validate the private filesystem endpoint without following its final link."""
    validate_unix_path(path)
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("Unix listener directory must be owned by this user with mode 0700")
    entry = path.lstat()
    if (not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600):
        raise ValueError("Unix listener must be an owned mode-0600 socket")
    return entry.st_dev, entry.st_ino


def unlink_owned_socket(path: Path, identity: tuple[int, int]) -> None:
    """Never remove a replacement path when releasing a generation's socket."""
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if (stat.S_ISSOCK(entry.st_mode) and entry.st_uid == os.geteuid()
            and (entry.st_dev, entry.st_ino) == identity):
        path.unlink()


def reserve_unix_listener(path: Path) -> socket.socket:
    """Bind exclusively in an existing private generation directory."""
    validate_unix_path(path)
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("Unix listener directory must be owned by this user with mode 0700")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        # bind refuses occupied paths, including symlinks and stale sockets.
        listener.bind(str(path))
        entry = path.lstat()
        identity = entry.st_dev, entry.st_ino
        path.chmod(0o600)
        listener.listen(16)
        unix_socket_identity(path)
        return listener
    except BaseException:
        listener.close()
        if identity is not None:
            unlink_owned_socket(path, identity)
        raise


def reserve_ws_listener() -> socket.socket:
    """Reserve a generation's loopback endpoint before its renderer is launched."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        return listener
    except BaseException:
        listener.close()
        raise


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
