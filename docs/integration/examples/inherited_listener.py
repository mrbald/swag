#!/usr/bin/env python3
"""Local Linux fd=N handoff demonstration, NOT an authenticated session manager.

No identity mapping, authentication, application-readiness protocol or recovery.
The JSON event describes a reserved listener, not a ready GUI. TCP is loopback;
Unix permissions exclude other UIDs, not processes sharing this UID. Do not
expose the viewer publicly. For production use the documented session manager.

Run a foreground application. Normal exit and SIGINT/SIGTERM/SIGHUP/SIGQUIT stop
its initial process group before releasing the reservation. Helpers that escape that group are not
contained. Killing this launcher with SIGKILL can leave Qt and socket files behind;
this example intentionally does not implement parent-death or service supervision.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


def identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def live_group(pgid: int) -> bool:
    # Keep the unreaped leader PID reserved until its original group is stopped.
    for entry in Path('/proc').iterdir():
        if entry.name.isdecimal():
            try:
                # getpgid does not read protected /proc metadata. Filter first
                # so hidepid=1 on unrelated UIDs cannot interrupt our cleanup.
                if os.getpgid(int(entry.name)) != pgid:
                    continue
                # A denial for an actual group member must remain visible; do
                # not treat an uninspectable owned process as already stopped.
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == pgid and fields[0] not in {'Z', 'X'}:
                    return True
            except (FileNotFoundError, ProcessLookupError):
                pass
    return False


def stop_group(child: subprocess.Popen) -> int:
    def send(signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
    send(signal.SIGTERM)
    deadline = time.monotonic() + 5
    forced = False
    while live_group(child.pid):
        if not forced and time.monotonic() >= deadline:
            send(signal.SIGKILL)
            forced = True
        # Even after SIGKILL, retain the reservation until the kernel completes
        # exit. This can wait if a process is stuck in uninterruptible kernel I/O.
        time.sleep(.05)
    return child.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport', choices=('tcp', 'unix'), required=True)
    parser.add_argument('--plugin-path', type=Path, required=True)
    parser.add_argument('--port', type=int, help='TCP port; default 5900, zero selects a free port')
    parser.add_argument('--runtime-dir', type=Path, help='Existing canonical owned mode 0700 directory for Unix')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='-- executable [arguments]')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('this example requires Linux')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('supply -- executable [arguments]')
    if not args.plugin_path.is_dir():
        parser.error('--plugin-path must name the installed plugins directory')
    if args.transport == 'tcp':
        if args.runtime_dir is not None or not 0 <= (args.port if args.port is not None else 5900) <= 65535:
            parser.error('TCP requires port 0..65535 and no --runtime-dir')
    else:
        root = args.runtime_dir
        if args.port is not None or root is None:
            parser.error('Unix requires --runtime-dir and no --port')
        try:
            info = root.lstat()
            valid = (root.is_absolute() and root.resolve(strict=True) == root
                     and not any(c in str(root) for c in '\0:=')
                     and stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                     and stat.S_IMODE(info.st_mode) == 0o700)
        except (OSError, RuntimeError, ValueError):
            valid = False
        if not valid:
            parser.error('--runtime-dir must be canonical, owned mode 0700, without symlinks, colon or equals')

    stopped_by = 0
    def stopping(signum, _frame):
        nonlocal stopped_by
        stopped_by = signum
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, stopping)

    listener = socket.socket(socket.AF_INET if args.transport == 'tcp' else socket.AF_UNIX)
    directory = path = child = None
    directory_id = socket_id = None
    try:
        if args.transport == 'tcp':
            # Permit restart after a closed connection enters TIME_WAIT; this
            # does not permit a second live listener on this Linux endpoint.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', args.port if args.port is not None else 5900))
            endpoint = f'ws://127.0.0.1:{listener.getsockname()[1]}'
        else:
            directory = Path(tempfile.mkdtemp(prefix='session-', dir=args.runtime_dir))
            directory_id = identity(directory)
            path = directory / 'rfb.sock'
            if len(os.fsencode(path)) > 107:
                raise ValueError('generated Unix socket path exceeds 107 bytes; use a shorter --runtime-dir')
            listener.bind(str(path))  # Never remove an occupied entry to make bind succeed.
            socket_id = identity(path)
            path.chmod(0o600)
            endpoint = str(path)
        listener.listen(16)  # Only the parent calls listen; Qt adopts, then accepts.
        env = dict(os.environ, QT_PLUGIN_PATH=str(args.plugin_path.resolve()),
                   QT_QPA_PLATFORM=f'vncws:size=1280x800:fd={listener.fileno()}',
                   QT_FORCE_STDERR_LOGGING='1')
        env.pop('DISPLAY', None)
        child = subprocess.Popen(command, env=env, pass_fds=(listener.fileno(),),
                                 start_new_session=True, stdout=sys.stderr)
        print(json.dumps(dict(event='listener_reserved', transport=args.transport,
                              endpoint=endpoint, pid=child.pid)), flush=True)
        while not stopped_by:
            # Unlike poll()/wait(), WNOWAIT does not release the leader PID.
            if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            time.sleep(.05)
    finally:
        result = stop_group(child) if child is not None else 1
        listener.close()
        if directory is not None and directory.exists() and identity(directory) == directory_id:
            if path is not None and socket_id is not None:
                try:
                    info = path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and identity(path) == socket_id:
                        path.unlink()
                except FileNotFoundError:
                    pass
            # No recursive deletion: preserve replacements and unexpected entries.
            try:
                directory.rmdir()
            except OSError:
                pass
    return 128 + stopped_by if stopped_by else (result if result >= 0 else 128 - result)


if __name__ == '__main__':
    raise SystemExit(main())
