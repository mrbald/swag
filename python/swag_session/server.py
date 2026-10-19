from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import time
from urllib.parse import urlsplit
import uuid

from aiohttp import WSMsgType, web

from .processes import exclusive_lock


@dataclass(frozen=True)
class Config:
    proxy_key: str
    public_origin: str
    runtime: Path = Path("/run/swag")
    artifacts: Path = Path("/artifacts")
    backend_url: str = "http://bank:8081"
    service_token: str = ""
    lease_seconds: float = 30.0
    start_timeout: float = 20.0
    launcher_command: tuple[str, ...] = (sys.executable, "-m", "swag_session.launcher")

    def __post_init__(self) -> None:
        origin = urlsplit(self.public_origin)
        if (not self.proxy_key or origin.scheme not in {"http", "https"}
                or not origin.netloc or origin.username or origin.password
                or origin.path or origin.query or origin.fragment):
            raise ValueError("configure a proxy key and exact http(s) browser origin")
        if not 0 < self.lease_seconds <= 3600 or not 0 < self.start_timeout <= 300:
            raise ValueError("invalid session timeout")

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            proxy_key=os.environ["SWAG_PROXY_KEY"],
            public_origin=os.environ["SWAG_PUBLIC_ORIGIN"],
            runtime=Path(os.environ.get("SWAG_RUNTIME_DIR", "/run/swag")),
            artifacts=Path(os.environ.get("SWAG_ARTIFACT_DIR", "/artifacts")),
            backend_url=os.environ["SWAG_BACKEND_URL"],
            service_token=os.environ["SWAG_SERVICE_TOKEN"],
            lease_seconds=float(os.environ.get("SWAG_VIEWER_LEASE_SECONDS", "30")),
            start_timeout=float(os.environ.get("SWAG_START_TIMEOUT_SECONDS", "20")),
        )


class SessionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@dataclass(eq=False)
class Viewer:
    websocket: web.WebSocketResponse
    writer: asyncio.StreamWriter
    deadline: float

    async def close(self, code: int = 1001, message: bytes = b"workspace ended") -> None:
        self.writer.close()
        try:
            await self.websocket.close(code=code, message=message)
        except (ConnectionError, RuntimeError):
            pass


@dataclass
class Workspace:
    user: str
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    state: str = "absent"
    generation: str | None = None
    pid: int | None = None
    process: asyncio.subprocess.Process | None = None
    monitor: asyncio.Task[None] | None = None
    viewer: Viewer | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self) -> dict[str, object]:
        return {"user": self.user, "state": self.state, "generation": self.generation,
                "pid": self.pid, "csrf_token": self.csrf_token}


WORKSPACE_KEY = web.RequestKey("workspace", Workspace)


class SessionManager:
    def __init__(self, config: Config):
        self.config = config
        self.users: dict[str, Workspace] = {}
        self.closing = False
        self.manager_lock: int | None = None

    async def open(self) -> None:
        os.umask(0o077)
        self.config.runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.config.artifacts.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.manager_lock = exclusive_lock(self.config.runtime / "manager.lock")

    def workspace(self, user: str) -> Workspace:
        if self.closing:
            raise SessionError(503, "shutting_down", "workspace service is stopping")
        return self.users.setdefault(user, Workspace(user))

    def launch_paths(self, workspace: Workspace) -> tuple[Path, Path]:
        assert workspace.generation is not None
        return (self.config.runtime / "users" / workspace.user / workspace.generation,
                self.config.artifacts / "users" / workspace.user / workspace.generation)

    async def _ended(self, workspace: Workspace) -> None:
        workspace.state = "ended"
        workspace.pid = None
        viewer, workspace.viewer = workspace.viewer, None
        if viewer:
            await viewer.close()

    async def _refresh(self, workspace: Workspace) -> None:
        if (workspace.process is not None and workspace.process.returncode is not None
                and workspace.state != "ended"):
            await self._ended(workspace)
        elif workspace.state == "running":
            _runtime, artifacts = self.launch_paths(workspace)
            try:
                metadata = json.loads((artifacts / "launcher.json").read_text())
            except (OSError, ValueError):
                metadata = {}
            if metadata.get("ready") is not True:
                await self._ended(workspace)

    async def _watch(self, workspace: Workspace, process: asyncio.subprocess.Process) -> None:
        while process.returncode is None:
            async with workspace.lock:
                if workspace.process is not process:
                    return
                await self._refresh(workspace)
            await asyncio.sleep(0.05)
        await process.wait()
        async with workspace.lock:
            if workspace.process is process:
                await self._ended(workspace)

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            # Launcher stops direct children in parallel only on parent death;
            # normal shutdown allows its bounded, orderly helper cleanup.
            await asyncio.wait_for(process.wait(), timeout=12)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def start(self, workspace: Workspace) -> dict[str, object]:
        async with workspace.lock:
            if self.closing:
                raise SessionError(503, "shutting_down", "workspace service is stopping")
            await self._refresh(workspace)
            if workspace.state in {"running", "starting"}:
                return workspace.snapshot()
            if workspace.process is not None and workspace.process.returncode is None:
                await self._stop(workspace.process)
            workspace.generation = uuid.uuid4().hex
            workspace.state = "starting"
            workspace.pid = None
            workspace.process = None
            runtime, artifacts = self.launch_paths(workspace)
            try:
                runtime.mkdir(parents=True, mode=0o700)
                artifacts.mkdir(parents=True, mode=0o700)
                environment = dict(os.environ)
                environment.update({
                    "SWAG_USER": workspace.user,
                    "SWAG_GENERATION": workspace.generation,
                    "SWAG_RUNTIME_DIR": str(self.config.runtime),
                    "SWAG_LAUNCH_ARTIFACT_DIR": str(artifacts),
                    "SWAG_STATE_FILE": str(artifacts / "state.json"),
                    "SWAG_SESSION_LOCK_FILE": str(runtime.parent / "session.lock"),
                    "SWAG_BACKEND_URL": self.config.backend_url,
                    "SWAG_SERVICE_TOKEN": self.config.service_token,
                    "SWAG_MANAGER_PID": str(os.getpid()),
                    "SWAG_START_TIMEOUT_SECONDS": str(self.config.start_timeout),
                })
                with (artifacts / "launcher.log").open("ab", buffering=0) as log:
                    process = await asyncio.create_subprocess_exec(
                        *self.config.launcher_command, env=environment,
                        stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                    )
                workspace.process = process
                deadline = time.monotonic() + self.config.start_timeout
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("workspace launcher exited during startup")
                    try:
                        metadata = json.loads((artifacts / "launcher.json").read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        metadata = {}
                    if (metadata.get("ready") is True
                            and metadata.get("user") == workspace.user
                            and metadata.get("generation") == workspace.generation
                            and isinstance(metadata.get("app_pid"), int)
                            and (runtime / "rfb.sock").is_socket()):
                        workspace.pid = metadata["app_pid"]
                        workspace.state = "running"
                        workspace.monitor = asyncio.create_task(self._watch(workspace, process))
                        return workspace.snapshot()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("workspace startup timed out")
                    await asyncio.sleep(0.05)
            except BaseException as error:
                if workspace.process is not None:
                    await self._stop(workspace.process)
                await self._ended(workspace)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise SessionError(503, "launch_failed", "workspace failed to start; inspect launcher logs") from error

    async def end(self, workspace: Workspace) -> dict[str, object]:
        async with workspace.lock:
            await self._ended(workspace)
            if workspace.process:
                await self._stop(workspace.process)
            return workspace.snapshot()

    async def close(self) -> None:
        self.closing = True
        await asyncio.gather(*(self.end(workspace) for workspace in self.users.values()))
        await asyncio.gather(*(workspace.monitor for workspace in self.users.values()
                               if workspace.monitor is not None), return_exceptions=True)
        if self.manager_lock is not None:
            os.close(self.manager_lock)
            self.manager_lock = None

    async def viewer(self, request: web.Request, workspace: Workspace) -> web.WebSocketResponse:
        generation = request.match_info["generation"]
        reader: asyncio.StreamReader
        async with workspace.lock:
            await self._refresh(workspace)
            if (workspace.state != "running" or workspace.generation != generation
                    or workspace.process is None or workspace.process.returncode is not None):
                raise SessionError(404, "workspace_unavailable", "no live workspace for this generation")
            runtime, _artifacts = self.launch_paths(workspace)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(runtime / "rfb.sock"), limit=65536), 3,
                )
            except (OSError, TimeoutError) as error:
                raise SessionError(503, "viewer_unavailable", "workspace display is unavailable") from error
            websocket = web.WebSocketResponse(
                protocols=("binary",), max_msg_size=262144, heartbeat=10, compress=False,
                timeout=1, writer_limit=65536,
            )
            try:
                await websocket.prepare(request)
            except BaseException:
                writer.close()
                raise
            viewer = Viewer(websocket, writer, time.monotonic() + self.config.lease_seconds)
            previous, workspace.viewer = workspace.viewer, viewer
        if previous is not None:
            await previous.close(1008, b"viewer taken over")

        async def browser_to_rfb() -> None:
            async for message in websocket:
                if message.type == WSMsgType.BINARY:
                    writer.write(message.data)
                    await writer.drain()
                elif message.type == WSMsgType.TEXT:
                    await viewer.close(1003, b"binary RFB frames required")
                    break
                elif message.type == WSMsgType.ERROR:
                    break

        async def rfb_to_browser() -> None:
            while data := await reader.read(65536):
                await websocket.send_bytes(data)

        async def lease() -> None:
            while True:
                remaining = viewer.deadline - time.monotonic()
                if remaining <= 0:
                    await viewer.close(1008, b"viewer lease expired")
                    return
                await asyncio.sleep(min(remaining, 1.0))

        tasks = [asyncio.create_task(job()) for job in (browser_to_rfb, rfb_to_browser, lease)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await viewer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            async with workspace.lock:
                if workspace.viewer is viewer:
                    workspace.viewer = None
        return websocket


def create_app(config: Config, manager: SessionManager | None = None) -> web.Application:
    service = manager or SessionManager(config)

    @web.middleware
    async def authenticate(request: web.Request, handler):
        try:
            if request.path != "/health":
                key = request.headers.get("X-Swag-Proxy-Key", "")
                user = request.headers.get("X-Swag-User", "")
                if (not hmac.compare_digest(key.encode(), config.proxy_key.encode())
                        or not re.fullmatch(r"[A-Za-z0-9]{1,32}", user)):
                    raise SessionError(403, "forbidden", "trusted proxy identity required")
                workspace = service.workspace(user)
                request[WORKSPACE_KEY] = workspace
                if request.method in {"POST", "DELETE"}:
                    if (request.headers.get("Origin") != config.public_origin
                            or not hmac.compare_digest(
                                request.headers.get("X-Swag-CSRF", "").encode(), workspace.csrf_token.encode())):
                        raise SessionError(403, "csrf", "same-origin request and CSRF token required")
                elif request.path.startswith("/viewer/"):
                    if request.headers.get("Origin") != config.public_origin:
                        raise SessionError(403, "origin", "same-origin viewer required")
            response = await handler(request)
        except SessionError as error:
            response = web.json_response({"error": {"code": error.code, "message": error.message}},
                                         status=error.status)
        if not isinstance(response, web.WebSocketResponse):
            response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[authenticate], client_max_size=4096)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def get_session(request: web.Request) -> web.Response:
        workspace = request[WORKSPACE_KEY]
        async with workspace.lock:
            await service._refresh(workspace)
            return web.json_response(workspace.snapshot())

    async def start_session(request: web.Request) -> web.Response:
        return web.json_response(await service.start(request[WORKSPACE_KEY]))

    async def end_session(request: web.Request) -> web.Response:
        return web.json_response(await service.end(request[WORKSPACE_KEY]))

    async def renew_lease(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            raise SessionError(400, "invalid_json", "generation JSON object required")
        if not isinstance(payload, dict) or not isinstance(payload.get("generation"), str):
            raise SessionError(400, "invalid_generation", "generation is required")
        workspace = request[WORKSPACE_KEY]
        async with workspace.lock:
            await service._refresh(workspace)
            if workspace.state != "running" or workspace.generation != payload["generation"]:
                raise SessionError(404, "workspace_unavailable", "no live workspace for this generation")
            if workspace.viewer:
                workspace.viewer.deadline = time.monotonic() + config.lease_seconds
            return web.json_response({"generation": workspace.generation, "lease_seconds": config.lease_seconds})

    async def viewer(request: web.Request) -> web.WebSocketResponse:
        return await service.viewer(request, request[WORKSPACE_KEY])

    async def lifecycle(_app: web.Application):
        await service.open()
        try:
            yield
        finally:
            await service.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", health)
    app.router.add_get("/session", get_session)
    app.router.add_post("/session", start_session)
    app.router.add_delete("/session", end_session)
    app.router.add_post("/session/lease", renew_lease)
    app.router.add_get("/viewer/{generation}/websockify", viewer)
    return app


def main() -> None:
    web.run_app(create_app(Config.from_env()), host="0.0.0.0", port=8080,
                access_log=None, shutdown_timeout=15)
