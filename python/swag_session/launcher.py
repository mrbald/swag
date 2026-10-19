"""One workspace supervisor. Qt exit ends the workspace, never the bank service."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from .processes import exclusive_lock, group_alive, parent_death_signal


def resolve_desktop_mode(environment: Mapping[str, str]) -> tuple[str, str]:
    """Choose the desktop layout and its optional external window manager."""
    ui_mode = environment.get("SWAG_UI_MODE", "single")
    if ui_mode not in {"single", "mdi"}:
        raise ValueError("SWAG_UI_MODE must be single or mdi")
    window_manager = environment.get(
        "SWAG_WINDOW_MANAGER", "none" if ui_mode == "mdi" else "openbox",
    )
    if window_manager not in {"openbox", "none"}:
        raise ValueError("SWAG_WINDOW_MANAGER must be openbox or none")
    return ui_mode, window_manager


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def stop(child: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + 2.0
    while group_alive(child.pid) and time.monotonic() < deadline:
        child.poll()
        time.sleep(0.025)
    if group_alive(child.pid):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait()


def reserve_display(runtime: Path) -> tuple[int, int]:
    displays = runtime / "displays"
    displays.mkdir(mode=0o700, exist_ok=True)
    for number in range(100, 1000):
        try:
            descriptor = exclusive_lock(displays / f"{number}.lock")
        except BlockingIOError:
            continue
        if (Path(f"/tmp/.X11-unix/X{number}").exists()
                or Path(f"/tmp/.X{number}-lock").exists()):
            os.close(descriptor)
            continue
        return number, descriptor
    raise RuntimeError("no free X display")


def main() -> int:
    # Reject configuration errors before locks or child processes are created.
    ui_mode, window_manager = resolve_desktop_mode(os.environ)
    user = os.environ["SWAG_USER"]
    generation = os.environ["SWAG_GENERATION"]
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", user):
        raise ValueError("invalid immutable user")
    if not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise ValueError("invalid generation")
    runtime = Path(os.environ.get("SWAG_RUNTIME_DIR", "/run/swag"))
    user_runtime = runtime / "users" / user
    launch_runtime = user_runtime / generation
    artifacts = Path(os.environ["SWAG_LAUNCH_ARTIFACT_DIR"])
    manifest = artifacts / "launcher.json"
    state = Path(os.environ["SWAG_STATE_FILE"])
    rfb = launch_runtime / "rfb.sock"
    metadata: dict[str, object] = {
        "user": user, "generation": generation, "ready": False,
        "launcher_pid": os.getpid(), "started_at": time.time(),
        "rfb_socket": str(rfb),
        "ui_mode": ui_mode, "window_manager": window_manager, "helpers": {},
    }
    children: list[tuple[str, subprocess.Popen[bytes]]] = []
    logs = []
    locks: list[int] = []
    received_signal: int | None = None
    result = 1

    def signalled(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signalled)
    parent_death_signal(int(os.environ["SWAG_MANAGER_PID"]), signal.SIGTERM)

    def launch(name: str, command: list[str]) -> subprocess.Popen[bytes]:
        log = (artifacts / f"{name}.log").open("ab", buffering=0)
        logs.append(log)
        child = subprocess.Popen(
            [sys.executable, "-m", "swag_session.processes", str(os.getpid()), *command],
            env=os.environ, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        children.append((name, child))
        metadata["helpers"] = {name: child.pid for name, child in children}
        return child

    def check_children() -> None:
        if received_signal:
            raise InterruptedError(f"signal {received_signal}")
        for name, child in children:
            if child.poll() is not None:
                raise RuntimeError(f"{name} exited: {child.returncode}")

    def wait_for(check, description: str) -> None:
        deadline = time.monotonic() + float(os.environ.get("SWAG_START_TIMEOUT_SECONDS", "20"))
        while True:
            check_children()
            if check():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"waiting for {description}")
            time.sleep(0.05)

    try:
        locks.append(exclusive_lock(user_runtime / "launcher.lock"))
        display, display_lock = reserve_display(runtime)
        locks.append(display_lock)
        metadata["display"] = display
        os.environ["DISPLAY"] = f":{display}"
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        os.environ["XDG_RUNTIME_DIR"] = str(launch_runtime)
        os.environ["XDG_CONFIG_HOME"] = str(launch_runtime / "config")
        os.environ["XDG_CACHE_HOME"] = str(launch_runtime / "cache")
        os.environ["SWAG_UI_MODE"] = ui_mode
        launch("xvnc", [
            "Xvnc", f":{display}", "-geometry", "1280x800", "-depth", "24",
            "-rfbport", "-1", "-rfbunixpath", str(rfb), "-rfbunixmode", "0600",
            "-SecurityTypes", "None", "-NeverShared", "-DisconnectClients",
            "-nolisten", "tcp", "-ac", "-desktop", f"swag-{user}",
        ])
        wait_for(lambda: Path(f"/tmp/.X11-unix/X{display}").is_socket() and rfb.is_socket(),
                 "X and private RFB sockets")
        if window_manager == "openbox":
            launch("openbox", ["openbox", "--sm-disable"])
        launch("vncconfig", ["vncconfig", "-nowin"])
        app = launch("qt", ["/opt/swag/bin/swag-desktop"])
        metadata["app_pid"] = app.pid

        def app_ready() -> bool:
            try:
                observation = json.loads(state.read_text())
                return (observation.get("ready") is True and observation.get("user") == user
                        and observation.get("generation") == generation
                        and observation.get("pid") == app.pid)
            except (FileNotFoundError, json.JSONDecodeError):
                return False

        wait_for(app_ready, "Qt initialized observation")
        metadata.update(ready=True, helpers={name: child.pid for name, child in children})
        atomic_json(manifest, metadata)
        while not received_signal and app.poll() is None:
            for name, child in children:
                if name != "qt" and child.poll() is not None:
                    raise RuntimeError(f"{name} exited: {child.returncode}")
            time.sleep(0.05)
        if received_signal:
            stop(app)
        result = app.wait()
    except Exception as error:
        metadata["error"] = str(error)
        print(json.dumps({"event": "workspace-failed", "error": str(error)}), flush=True)
    finally:
        metadata["ready"] = False
        try:
            atomic_json(manifest, metadata)
        except OSError:
            pass  # Artifact failure must never skip process cleanup.
        # Stop Qt first, then helpers. Each direct child also has a kernel death
        # signal, so SIGKILL of this supervisor cannot leave its desktop alive.
        for _name, child in reversed(children):
            stop(child)
        if rfb.is_socket():
            rfb.unlink()
        metadata.update(ready=False, ended_at=time.time(), exit_code=result)
        try:
            atomic_json(manifest, metadata)
        except OSError:
            pass
        for log in logs:
            log.close()
        for descriptor in reversed(locks):
            os.close(descriptor)
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    sys.exit(main())
