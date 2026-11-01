"""Session boundaries with real lightweight child processes and Unix streams.

These tests do not claim Qt/Xvnc or browser acceptance; those require the image.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest

from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from swag_session.server import Config, SessionManager, Viewer, create_app


FAKE_LAUNCHER = r'''
import asyncio, fcntl, json, os, pathlib, signal
from aiohttp import WSMsgType, web

async def main():
    user, generation = os.environ['SWAG_USER'], os.environ['SWAG_GENERATION']
    artifact = pathlib.Path(os.environ['SWAG_LAUNCH_ARTIFACT_DIR'])
    runtime = pathlib.Path(os.environ['SWAG_RUNTIME_DIR']) / 'users' / user / generation
    lock = open(os.environ['SWAG_SESSION_LOCK_FILE'], 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid = os.getpid()
    observation = {'ready': True, 'user': user, 'generation': generation, 'pid': pid,
                   'draft': 'unsaved draft stays here'}
    pathlib.Path(os.environ['SWAG_STATE_FILE']).write_text(json.dumps(observation))
    connections = set()
    async def echo(reader, writer):
        connections.add(writer)
        try:
            writer.write(b'RFB hello')
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            connections.discard(writer)
            writer.close()
    backend = os.environ.get('SWAG_RENDER_BACKEND', 'tiger')
    server = None
    runner = None
    ws_port = None
    if backend == 'tiger':
        server = await asyncio.start_unix_server(echo, path=str(runtime / 'rfb.sock'))
    else:
        async def echo_ws(request):
            ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
            await ws.prepare(request)
            await ws.send_bytes(b'RFB hello')
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
            return ws
        app = web.Application()
        app.router.add_get('/', echo_ws)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        ws_port = site._server.sockets[0].getsockname()[1]
    manifest = artifact / 'launcher.json'
    metadata = {'ready': True, 'user': user, 'generation': generation,
                'launcher_pid': pid, 'app_pid': pid, 'render_backend': backend,
                'helpers': {'qt': pid}}
    if backend == 'tiger':
        metadata['rfb_socket'] = str(runtime / 'rfb.sock')
    else:
        metadata['ws_port'] = ws_port
    manifest.write_text(json.dumps(metadata))
    end = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, end.set)
    await end.wait()
    for writer in connections.copy():
        writer.close()
    if server is not None:
        server.close()
        await server.wait_closed()
    if runner is not None:
        await runner.cleanup()
    metadata['ready'] = False
    manifest.write_text(json.dumps(metadata))

asyncio.run(main())
'''


class Sessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Unix paths must fit sockaddr_un even on macOS's 104-byte limit.
        self.temporary = tempfile.TemporaryDirectory(prefix="swag-", dir="/tmp")
        self.root = Path(self.temporary.name)
        launcher = self.root / "fake.py"
        launcher.write_text(FAKE_LAUNCHER)
        self.config = Config(
            proxy_key="private-proxy-key", public_origin="http://swag.test",
            runtime=self.root / "r", artifacts=self.root / "a",
            service_token="bank-secret", lease_seconds=0.4, start_timeout=3,
            launcher_command=(sys.executable, str(launcher)),
        )
        self.manager = SessionManager(self.config)
        self.client = TestClient(TestServer(create_app(self.config, self.manager)))
        self.addAsyncCleanup(self.client.close)
        self.addCleanup(self.temporary.cleanup)
        await self.client.start_server()
        self.tokens = {}

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    def headers(self, user="alice", mutation=False):
        value = {"X-Swag-Proxy-Key": self.config.proxy_key, "X-Swag-User": user,
                 "Origin": self.config.public_origin}
        if mutation:
            value["X-Swag-CSRF"] = self.tokens[user]
        return value

    async def get(self, user="alice"):
        response = await self.client.get("/session", headers=self.headers(user))
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.tokens[user] = body["csrf_token"]
        return body

    async def start(self, user="alice"):
        if user not in self.tokens:
            await self.get(user)
        response = await self.client.post("/session", headers=self.headers(user, True))
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def connect(self, session, user="alice"):
        return await self.client.ws_connect(
            f"/viewer/{session['generation']}/websockify", headers=self.headers(user),
            protocols=("binary",),
        )

    async def test_proxy_identity_and_mutation_boundaries(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        for headers in ({}, self.headers("../bob"), self.headers("a" * 33),
                        {**self.headers(), "X-Swag-Proxy-Key": "wrong"}):
            response = await self.client.get("/session", headers=headers)
            self.assertEqual(response.status, 403)
        absent = await self.get()
        self.assertEqual(absent["state"], "absent")
        self.assertIsNone(absent["generation"])
        for headers in (self.headers(), {**self.headers(mutation=True), "Origin": "http://evil.test"},
                        {**self.headers(mutation=True), "X-Swag-CSRF": "wrong"}):
            response = await self.client.post("/session", headers=headers)
            self.assertEqual(response.status, 403)
        self.assertFalse((self.root / "a" / "users").exists())

    async def test_concurrent_start_rejoin_and_separate_users(self):
        await self.get()
        sessions = await asyncio.gather(*(self.start() for _ in range(12)))
        self.assertEqual(len({value["generation"] for value in sessions}), 1)
        self.assertEqual(len({value["pid"] for value in sessions}), 1)
        self.assertEqual(len(list((self.root / "a" / "users" / "alice").iterdir())), 1)
        bob = await self.start("bob")
        self.assertNotEqual(bob["pid"], sessions[0]["pid"])
        self.assertNotEqual(bob["generation"], sessions[0]["generation"])
        alice = await self.start()
        self.assertEqual(alice, sessions[0])

    async def test_disconnect_preserves_draft_and_second_viewer_takes_over(self):
        session = await self.start()
        state_file = self.root / "a" / "users" / "alice" / session["generation"] / "state.json"
        original = state_file.read_bytes()
        first = await self.connect(session)
        self.assertEqual((await first.receive()).data, b"RFB hello")
        await first.send_bytes(b"opaque\x00RFB")
        self.assertEqual((await first.receive()).data, b"opaque\x00RFB")
        second = await self.connect(session)
        self.assertEqual((await second.receive()).data, b"RFB hello")
        self.assertEqual((await first.receive(timeout=1)).type, WSMsgType.CLOSE)
        await second.close()
        self.assertEqual((await self.get())["pid"], session["pid"])
        self.assertEqual(state_file.read_bytes(), original)
        third = await self.connect(session)
        self.assertEqual((await third.receive()).data, b"RFB hello")
        await third.close()

    async def test_dead_app_requires_explicit_restart_and_stale_or_cross_user_fails(self):
        session = await self.start()
        websocket = await self.connect(session)
        await websocket.receive()
        await self.get("bob")
        with self.assertRaises(WSServerHandshakeError) as denied:
            await self.connect(session, "bob")
        self.assertEqual(denied.exception.status, 404)
        os.kill(session["pid"], signal.SIGKILL)
        self.assertIn((await websocket.receive(timeout=2)).type,
                      (WSMsgType.CLOSE, WSMsgType.CLOSED))
        for _ in range(50):
            ended = await self.get()
            if ended["state"] == "ended":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(ended["state"], "ended")
        self.assertIsNone(ended["pid"])
        for _ in range(2):
            with self.assertRaises(WSServerHandshakeError) as denied:
                await self.connect(session)
            self.assertEqual(denied.exception.status, 404)
        self.assertEqual(len(list((self.root / "a" / "users" / "alice").iterdir())), 1)
        restarted = await self.start()
        self.assertNotEqual(restarted["generation"], session["generation"])
        with self.assertRaises(WSServerHandshakeError):
            await self.connect(session)
        response = await self.client.post("/session/lease", headers=self.headers(mutation=True),
                                          json={"generation": session["generation"]})
        self.assertEqual(response.status, 404)

    async def test_lease_renewal_expiry_and_origin(self):
        session = await self.start()
        with self.assertRaises(WSServerHandshakeError) as denied:
            await self.client.ws_connect(f"/viewer/{session['generation']}/websockify",
                                         headers={**self.headers(), "Origin": "http://evil.test"})
        self.assertEqual(denied.exception.status, 403)
        viewer = await self.connect(session)
        await viewer.receive()
        for _ in range(3):
            await asyncio.sleep(0.2)
            response = await self.client.post("/session/lease", headers=self.headers(mutation=True),
                                              json={"generation": session["generation"]})
            self.assertEqual(response.status, 200)
        await viewer.send_bytes(b"still live after original lease")
        self.assertEqual((await viewer.receive(timeout=1)).data, b"still live after original lease")
        self.assertEqual((await viewer.receive(timeout=1)).type, WSMsgType.CLOSE)
        self.assertEqual((await self.get())["state"], "running")

    async def test_delete_and_shutdown_stop_owned_children_without_unlinking_lock(self):
        alice, bob = await self.start(), await self.start("bob")
        lock = self.root / "r" / "users" / "alice" / "session.lock"
        inode = lock.stat().st_ino
        response = await self.client.delete("/session", headers=self.headers(mutation=True))
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["state"], "ended")
        with self.assertRaises(ProcessLookupError):
            os.kill(alice["pid"], 0)
        self.assertEqual(lock.stat().st_ino, inode)
        again = await self.start()
        self.assertNotEqual(again["generation"], alice["generation"])
        self.assertEqual(lock.stat().st_ino, inode)
        await self.manager.close()
        for session in (again, bob):
            with self.assertRaises(ProcessLookupError):
                os.kill(session["pid"], 0)

    async def test_failed_launch_ends_state_and_cleans_child(self):
        self.manager.config = Config(
            **{**self.config.__dict__, "launcher_command": (sys.executable, "-c", "raise SystemExit(7)")}
        )
        await self.get()
        response = await self.client.post("/session", headers=self.headers(mutation=True))
        self.assertEqual(response.status, 503)
        self.assertEqual((await self.get())["state"], "ended")
        self.assertIsNotNone(self.manager.users["alice"].process.returncode)

    async def test_launch_timeout_reaps_child(self):
        self.manager.config = Config(
            **{**self.config.__dict__, "start_timeout": 0.15,
               "launcher_command": (sys.executable, "-c", "import time; time.sleep(60)")}
        )
        await self.get()
        response = await self.client.post("/session", headers=self.headers(mutation=True))
        self.assertEqual(response.status, 503)
        self.assertEqual((await self.get())["state"], "ended")
        process = self.manager.users["alice"].process
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(process.pid, 0)

    async def test_only_bounded_binary_frames_are_accepted(self):
        session = await self.start()
        for payload in ("text cannot be RFB", b"x" * 262145):
            viewer = await self.connect(session)
            await viewer.receive()
            if isinstance(payload, str):
                await viewer.send_str(payload)
            else:
                await viewer.send_bytes(payload)
            self.assertEqual((await viewer.receive(timeout=2)).type, WSMsgType.CLOSE)
        self.assertEqual((await self.get())["pid"], session["pid"])


class QtWsSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="swag-ws-", dir="/tmp")
        self.root = Path(self.temporary.name)
        launcher = self.root / "fake.py"
        launcher.write_text(FAKE_LAUNCHER)
        self.config = Config(
            proxy_key="private-proxy-key", public_origin="http://swag.test",
            runtime=self.root / "r", artifacts=self.root / "a",
            service_token="bank-secret", render_backend="qt-ws",
            lease_seconds=0.4, start_timeout=3,
            launcher_command=(sys.executable, str(launcher)),
        )
        self.manager = SessionManager(self.config)
        self.client = TestClient(TestServer(create_app(self.config, self.manager)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    def headers(self, mutation=False):
        headers = {"X-Swag-Proxy-Key": self.config.proxy_key,
                   "X-Swag-User": "alice", "Origin": self.config.public_origin}
        if mutation:
            headers["X-Swag-CSRF"] = self.csrf
        return headers

    async def start(self):
        response = await self.client.get("/session", headers=self.headers())
        self.csrf = (await response.json())["csrf_token"]
        response = await self.client.post("/session", headers=self.headers(True))
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_binary_relay_takeover_and_manifest_identity(self):
        session = await self.start()
        manifest = self.root / "a" / "users" / "alice" / session["generation"] / "launcher.json"
        metadata = json.loads(manifest.read_text())
        self.assertEqual(metadata["render_backend"], "qt-ws")
        self.assertEqual(metadata["launcher_pid"], self.manager.users["alice"].process.pid)
        self.assertEqual(metadata["app_pid"], session["pid"])
        self.assertTrue(1 <= metadata["ws_port"] <= 65535)
        first = await self.client.ws_connect(
            f"/viewer/{session['generation']}/websockify?port=1",
            headers=self.headers(), protocols=("binary",))
        self.assertEqual((await first.receive()).data, b"RFB hello")
        await first.send_bytes(b"opaque\x00RFB")
        self.assertEqual((await first.receive()).data, b"opaque\x00RFB")
        second = await self.client.ws_connect(
            f"/viewer/{session['generation']}/websockify",
            headers=self.headers(), protocols=("binary",))
        self.assertEqual((await second.receive()).data, b"RFB hello")
        self.assertEqual((await first.receive(timeout=2)).type, WSMsgType.CLOSE)
        await second.send_bytes(b"new viewer")
        self.assertEqual((await second.receive()).data, b"new viewer")
        await second.close()
        self.assertEqual(self.manager.workspace("alice").state, "running")

    async def test_nonobject_or_wrong_pid_manifest_cannot_select_endpoint(self):
        for mutate in (lambda _m: [],
                       lambda m: {**m, "app_pid": m["app_pid"] + 1000},
                       lambda m: {**m, "ws_port": "5900"}):
            session = await self.start()
            manifest = self.root / "a" / "users" / "alice" / session["generation"] / "launcher.json"
            manifest.write_text(json.dumps(mutate(json.loads(manifest.read_text()))))
            with self.assertRaises(WSServerHandshakeError) as denied:
                await self.client.ws_connect(
                    f"/viewer/{session['generation']}/websockify", headers=self.headers())
            self.assertEqual(denied.exception.status, 404)
            self.assertEqual(self.manager.workspace("alice").state, "ended")

    async def test_inner_websocket_handshake_failure_maps_to_503(self):
        session = await self.start()
        async def reject(reader, writer):
            await reader.read(1024)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(reject, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            manifest = self.root / "a" / "users" / "alice" / session["generation"] / "launcher.json"
            metadata = json.loads(manifest.read_text())
            metadata["ws_port"] = port
            manifest.write_text(json.dumps(metadata))
            with self.assertRaises(WSServerHandshakeError) as denied:
                await self.client.ws_connect(
                    f"/viewer/{session['generation']}/websockify", headers=self.headers())
            self.assertEqual(denied.exception.status, 503)
        finally:
            server.close()
            await server.wait_closed()

    async def test_lease_expiry_closes_both_sockets_and_preserves_workspace(self):
        session = await self.start()
        outer = await self.client.ws_connect(
            f"/viewer/{session['generation']}/websockify",
            headers=self.headers(), protocols=("binary",))
        self.assertEqual((await outer.receive()).data, b"RFB hello")
        viewer = self.manager.workspace("alice").viewer
        self.assertIsNotNone(viewer)
        self.assertEqual((await outer.receive(timeout=2)).type, WSMsgType.CLOSE)
        for _ in range(50):
            if viewer.close_task is not None and viewer.close_task.done():
                break
            await asyncio.sleep(0.02)
        self.assertIsNotNone(viewer.close_task)
        self.assertTrue(viewer.close_task.done())
        self.assertTrue(viewer.upstream.closed)
        self.assertTrue(viewer.client_session.closed)
        self.assertEqual(self.manager.workspace("alice").state, "running")
        self.assertEqual(self.manager.workspace("alice").pid, session["pid"])


class ViewerCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_does_not_abort_memoized_close(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        calls = []
        class Outer:
            async def close(self, **_kwargs):
                entered.set()
                await resume.wait()
                calls.append("outer")
        class Upstream:
            async def close(self):
                calls.append("upstream")
        class Session:
            async def close(self):
                calls.append("session")
        viewer = Viewer(Outer(), 0, upstream=Upstream(), client_session=Session())
        waiting = asyncio.create_task(viewer.close())
        await entered.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        resume.set()
        await viewer.close()
        self.assertEqual(calls, ["outer", "upstream", "session"])


if __name__ == "__main__":
    unittest.main()
