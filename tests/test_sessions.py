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

from swag_session.server import Config, SessionManager, create_app


FAKE_LAUNCHER = r'''
import asyncio, fcntl, json, os, pathlib, signal

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
    server = await asyncio.start_unix_server(echo, path=str(runtime / 'rfb.sock'))
    manifest = artifact / 'launcher.json'
    metadata = {'ready': True, 'user': user, 'generation': generation, 'app_pid': pid}
    manifest.write_text(json.dumps(metadata))
    end = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, end.set)
    await end.wait()
    for writer in connections.copy():
        writer.close()
    server.close()
    await server.wait_closed()
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


if __name__ == "__main__":
    unittest.main()
