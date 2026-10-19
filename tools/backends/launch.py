#!/usr/bin/env python3.12
"""Private, single-probe supervisor. Application exit ends the container."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


APP = "/opt/swag/bin/swag-probe"
GEOMETRY = "1280x800"
VNC_PORT = 5900
WEB_PORT = 6080
STOP_TIMEOUT = 5.0


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def listening(port: int) -> bool:
    """Observe listeners without creating a VNC viewer or changing app state."""
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in Path(name).read_text().splitlines()[1:]:
            columns = row.split()
            if columns[3] == "0A" and int(columns[1].rsplit(":", 1)[1], 16) == port:
                return True
    return False


def process_exit_code(code: int) -> int:
    return code if code >= 0 else 128 - code


def group_has_live_members(pgid: int) -> bool:
    """A forked bridge worker can outlive its original group leader."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            # comm may contain spaces or parentheses; fields after its final ')'
            # begin with state, ppid, pgrp. Zombies need no further signalling.
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return False


def stop(process: subprocess.Popen[bytes], signum: int = signal.SIGTERM) -> None:
    # Signal the whole group even when poll() says its original leader exited.
    # websockify forks connection workers, which otherwise survive that leader.
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + STOP_TIMEOUT
    while group_has_live_members(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if group_has_live_members(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def main() -> int:
    backend = os.environ.get("SWAG_BACKEND", "qt")
    transport = os.environ.get("SWAG_RFB_TRANSPORT", "tcp")
    username = os.environ.get("SWAG_USER", "probe")
    generation = os.environ.get("SWAG_GENERATION", "1")
    if backend not in {"qt", "tiger"}:
        raise ValueError("SWAG_BACKEND must be qt or tiger")
    if transport not in {"tcp", "unix"}:
        raise ValueError("SWAG_RFB_TRANSPORT must be tcp or unix")
    if backend == "qt" and transport == "unix":
        raise ValueError("SWAG_BACKEND=qt does not support Unix RFB transport (unpatched Qt plugin)")
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", username):
        raise ValueError("SWAG_USER must be 1-32 alphanumeric characters")
    if not generation or len(generation) > 128:
        raise ValueError("SWAG_GENERATION must contain 1-128 characters")
    artifacts = Path(os.environ.get("SWAG_ARTIFACT_DIR", "/artifacts")).resolve()
    state_file = Path(os.environ.get("SWAG_STATE_FILE", "/artifacts/state.json")).resolve()
    for path in (artifacts, state_file):
        if not path.is_relative_to(Path("/artifacts")):
            raise ValueError("probe artifacts and state must be under /artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    os.environ["SWAG_STATE_FILE"] = str(state_file)
    if not Path(APP).is_file():
        raise FileNotFoundError(APP)
    rfb_socket: Path | None = None
    if transport == "unix":
        users = Path("/run/swag/users")
        users.mkdir(mode=0o700, exist_ok=True)
        user_directory = users / username
        user_directory.mkdir(mode=0o700, exist_ok=True)
        rfb_socket = user_directory / "vnc.sock"
        if rfb_socket.exists() or rfb_socket.is_symlink():
            raise FileExistsError(f"refusing existing RFB endpoint: {rfb_socket}")

    def rfb_ready() -> bool:
        return rfb_socket.is_socket() if rfb_socket is not None else listening(VNC_PORT)

    helpers: list[tuple[str, subprocess.Popen[bytes]]] = []
    logs = []
    app: subprocess.Popen[bytes] | None = None
    received_signal: int | None = None
    helper_failure: str | None = None
    metadata: dict[str, object] = {
        "backend": backend, "username": username, "generation": generation,
        "rfbTransport": transport,
        "rfbEndpoint": str(rfb_socket) if rfb_socket else f"127.0.0.1:{VNC_PORT}",
        "supervisorPid": os.getpid(), "serviceUid": os.getuid(),
        "geometry": GEOMETRY, "startedAt": time.time(), "ready": False,
    }

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, on_signal)

    def launch(name: str, command: list[str]) -> subprocess.Popen[bytes]:
        log = (artifacts / f"{name}.log").open("ab", buffering=0)
        logs.append(log)
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        emit("process-started", name=name, pid=child.pid, command=command)
        return child

    def wait_for(check, description: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not check():
            if received_signal:
                raise InterruptedError(f"signal {received_signal} during startup")
            if app is not None and app.poll() is not None:
                raise RuntimeError("Qt exited during startup")
            for name, child in helpers:
                if child.poll() is not None:
                    raise RuntimeError(f"{name} exited during startup: {child.returncode}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"waiting for {description}")
            time.sleep(0.1)

    result = 1
    try:
        if backend == "tiger":
            os.environ["DISPLAY"] = ":99"
            os.environ["QT_QPA_PLATFORM"] = "xcb"
            rfb_options = (["-rfbport", "-1", "-rfbunixpath", str(rfb_socket),
                            "-rfbunixmode", "0600"] if rfb_socket else
                           ["-rfbport", str(VNC_PORT), "-localhost", "yes"])
            xvnc = launch("xvnc", [
                "Xvnc", ":99", "-geometry", GEOMETRY, "-depth", "24",
                *rfb_options,
                "-SecurityTypes", "None", "-NeverShared", "-DisconnectClients",
                "-nolisten", "tcp", "-ac", "-desktop", "swag-probe",
            ])
            helpers.append(("xvnc", xvnc))
            wait_for(lambda: Path("/tmp/.X11-unix/X99").is_socket() and rfb_ready(),
                     "TigerVNC X and RFB listeners")
            helpers.append(("openbox", launch("openbox", ["openbox", "--sm-disable"])))
            # The packaged clipboard helper handles X11 selection ownership.
            helpers.append(("vncconfig", launch("vncconfig", ["vncconfig", "-nowin"])))
        else:
            os.environ.pop("DISPLAY", None)
            os.environ["QT_QPA_PLATFORM"] = f"vnc:size={GEOMETRY}:port={VNC_PORT}"

        app = launch("qt", [APP, *sys.argv[1:]])
        metadata["appPid"] = app.pid
        atomic_json(artifacts / "backend.json", metadata)
        wait_for(rfb_ready, "RFB endpoint")
        target_options = (["--unix-target", str(rfb_socket), f"0.0.0.0:{WEB_PORT}"]
                          if rfb_socket else
                          [f"0.0.0.0:{WEB_PORT}", f"127.0.0.1:{VNC_PORT}"])
        bridge = launch("websockify", [
            "/usr/bin/python3.12", "-m", "websockify",
            "--web", "/opt/novnc", *target_options,
        ])
        helpers.append(("websockify", bridge))
        wait_for(lambda: listening(WEB_PORT), "noVNC HTTP/WebSocket listener")
        metadata.update(ready=True, helpers={name: child.pid for name, child in helpers})
        atomic_json(artifacts / "backend.json", metadata)
        emit("ready", backend=backend, appPid=app.pid, rfbTransport=transport,
             rfbEndpoint=metadata["rfbEndpoint"], url=f"http://127.0.0.1:{WEB_PORT}/probe.html")
        while app.poll() is None:
            if received_signal:
                stop(app, received_signal)
                break
            for name, child in helpers:
                if child.poll() is not None:
                    helper_failure = f"{name} exited: {child.returncode}"
                    emit("helper-exited", name=name, code=child.returncode)
                    stop(app)
                    break
            if helper_failure:
                break
            time.sleep(0.1)
        result = process_exit_code(app.wait())
    except Exception as error:
        emit("startup-failed", error=str(error))
        metadata["error"] = str(error)
        if app is not None:
            if app.poll() is None:
                stop(app, received_signal or signal.SIGTERM)
            result = process_exit_code(app.wait())
        elif received_signal:
            result = 128 + received_signal
    finally:
        for _name, child in reversed(helpers):
            stop(child)
        if app is not None and app.poll() is None:
            stop(app)
        if rfb_socket is not None and rfb_socket.is_socket():
            rfb_socket.unlink()
        metadata.update(ready=False, endedAt=time.time(), exitCode=result,
                        helperFailure=helper_failure)
        atomic_json(artifacts / "backend.json", metadata)
        for log in logs:
            log.close()
        emit("session-ended", appExitCode=result, helperFailure=helper_failure)
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit("configuration-failed", error=str(error))
        sys.exit(2)
