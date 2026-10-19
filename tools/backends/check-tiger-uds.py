#!/usr/bin/env python3.12
"""Verify the packaged Xvnc accepts a Unix RFB connection with TCP disabled."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import tempfile
import time


def tcp_listeners() -> set[str]:
    listeners = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in Path(path).read_text().splitlines()[1:]:
            columns = row.split()
            if columns[3] == "0A":
                listeners.add(f"{path}:{columns[1]}")
    return listeners


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("/artifacts"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    help_run = subprocess.run(["Xvnc", "-help"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, check=False)
    (args.output_dir / "xvnc-help.txt").write_text(help_run.stdout)
    supports = all(option in help_run.stdout.lower() for option in ("rfbunixpath", "rfbport"))
    if not supports:
        print(json.dumps({"supportedFlags": False, "passed": False}))
        return 1
    with tempfile.TemporaryDirectory(prefix="swag-uds-") as temporary:
        path = Path(temporary) / "rfb.sock"
        command = ["Xvnc", ":198", "-geometry", "1280x800", "-depth", "24",
                   "-rfbport", "-1", "-rfbunixpath", str(path), "-rfbunixmode", "0600",
                   "-SecurityTypes", "None", "-nolisten", "tcp", "-ac"]
        before = tcp_listeners()
        with (args.output_dir / "xvnc-uds.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while not path.exists():
                    if process.poll() is not None:
                        raise RuntimeError(f"Xvnc exited {process.returncode}; see xvnc-uds.log")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Xvnc did not create the Unix socket")
                    time.sleep(0.1)
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    banner = client.recv(12).decode("ascii")
                mode = stat.S_IMODE(path.stat().st_mode)
                added = sorted(tcp_listeners() - before)
                result = {
                    "supportedFlags": True, "banner": banner,
                    "socketMode": oct(mode), "newTcpListeners": added,
                    "passed": banner.startswith("RFB ") and mode == 0o600 and not added,
                    "command": command,
                }
                (args.output_dir / "tiger-uds.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, sort_keys=True))
                return 0 if result["passed"] else 1
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
