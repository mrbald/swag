"""One workspace supervisor. Qt exit ends the workspace, never the bank service."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

from .processes import exclusive_lock, group_alive, parent_death_signal


def resolve_render_backend(environment: Mapping[str, str]) -> str:
    backend = environment.get("SWAG_RENDER_BACKEND", "tiger")
    if backend not in {"tiger", "qt-ws"}:
        raise ValueError("SWAG_RENDER_BACKEND must be tiger or qt-ws")
    return backend


def resolve_desktop_mode(environment: Mapping[str, str],
                         render_backend: str = "tiger") -> tuple[str, str]:
    """Choose the desktop layout and its optional external window manager."""
    ui_mode = environment.get("SWAG_UI_MODE", "single")
    if ui_mode not in {"single", "mdi"}:
        raise ValueError("SWAG_UI_MODE must be single or mdi")
    if render_backend == "qt-ws" and ui_mode != "mdi":
        raise ValueError("qt-ws requires SWAG_UI_MODE=mdi")
    window_manager = environment.get("SWAG_WINDOW_MANAGER",
                                     "none" if ui_mode == "mdi" else "openbox")
    if window_manager not in {"openbox", "none"}:
        raise ValueError("SWAG_WINDOW_MANAGER must be openbox or none")
    if render_backend == "qt-ws" and window_manager != "none":
        raise ValueError("qt-ws requires SWAG_WINDOW_MANAGER=none")
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


def reserve_ws_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        return listener
    except BaseException:
        listener.close()
        raise


def main() -> int:
    # Reject configuration errors before locks or child processes are created.
    render_backend = resolve_render_backend(os.environ)
    ui_mode, window_manager = resolve_desktop_mode(os.environ, render_backend)
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
        "render_backend": render_backend,
        "ui_mode": ui_mode, "window_manager": window_manager, "helpers": {},
    }
    if render_backend == "tiger":
        metadata["rfb_socket"] = str(rfb)
    children: list[tuple[str, subprocess.Popen[bytes]]] = []
    logs = []
    locks: list[int] = []
    ws_listener: socket.socket | None = None
    received_signal: int | None = None
    result = 1

    def signalled(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signalled)
    parent_death_signal(int(os.environ["SWAG_MANAGER_PID"]), signal.SIGTERM)

    def launch(name: str, command: list[str],
               pass_fds: tuple[int, ...] = ()) -> subprocess.Popen[bytes]:
        log = (artifacts / f"{name}.log").open("ab", buffering=0)
        logs.append(log)
        child = subprocess.Popen(
            [sys.executable, "-m", "swag_session.processes", str(os.getpid()), *command],
            env=os.environ, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            pass_fds=pass_fds,
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
        os.environ["XDG_RUNTIME_DIR"] = str(launch_runtime)
        os.environ["XDG_CONFIG_HOME"] = str(launch_runtime / "config")
        os.environ["XDG_CACHE_HOME"] = str(launch_runtime / "cache")
        os.environ["SWAG_UI_MODE"] = ui_mode
        os.environ["SWAG_RENDER_BACKEND"] = render_backend
        if render_backend == "tiger":
            display, display_lock = reserve_display(runtime)
            locks.append(display_lock)
            metadata["display"] = display
            os.environ["DISPLAY"] = f":{display}"
            os.environ["QT_QPA_PLATFORM"] = "xcb"
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
        else:
            os.environ.pop("DISPLAY", None)
            ws_listener = reserve_ws_listener()
            metadata["ws_port"] = ws_listener.getsockname()[1]
            os.environ["QT_QPA_PLATFORM"] = (
                f"vncws:size=1280x800:fd={ws_listener.fileno()}")
            try:
                app = launch("qt", ["/opt/swag/bin/swag-desktop"],
                             pass_fds=(ws_listener.fileno(),))
            finally:
                ws_listener.close()
                ws_listener = None
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
        if ws_listener is not None:
            ws_listener.close()
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
