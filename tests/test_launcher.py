"""Launcher configuration and child selection, without starting X or Qt."""

from __future__ import annotations

import json
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from swag_session import launcher
from swag_session.processes import reserve_unix_listener


class DesktopMode(unittest.TestCase):
    def test_defaults_preserve_single_window_desktop(self):
        self.assertEqual(launcher.resolve_desktop_mode({}), ("single", "openbox"))

    def test_mdi_owns_its_windows_without_an_external_manager(self):
        self.assertEqual(
            launcher.resolve_desktop_mode({"SWAG_UI_MODE": "mdi"}), ("mdi", "none"),
        )

    def test_window_manager_override_is_independent_of_layout(self):
        for ui_mode, window_manager in (("single", "none"), ("mdi", "openbox")):
            with self.subTest(ui_mode=ui_mode, window_manager=window_manager):
                self.assertEqual(launcher.resolve_desktop_mode({
                    "SWAG_UI_MODE": ui_mode, "SWAG_WINDOW_MANAGER": window_manager,
                }), (ui_mode, window_manager))

    def test_invalid_configuration_fails_before_creating_a_child(self):
        for environment in ({"SWAG_UI_MODE": ""}, {"SWAG_UI_MODE": "MDI"},
                            {"SWAG_WINDOW_MANAGER": ""},
                            {"SWAG_WINDOW_MANAGER": "metacity"},
                            {"SWAG_UI_MODE": "mdi", "SWAG_WINDOW_MANAGER": "None"}):
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(launcher.subprocess, "Popen") as spawn:
                    with self.assertRaises(ValueError):
                        launcher.main()
                    spawn.assert_not_called()

    def test_qt_ws_requires_mdi_without_external_window_manager(self):
        self.assertEqual(launcher.resolve_render_backend({}), "tiger")
        self.assertEqual(launcher.resolve_render_backend({"SWAG_RENDER_BACKEND": "qt-ws"}),
                         "qt-ws")
        self.assertEqual(launcher.resolve_desktop_mode({"SWAG_UI_MODE": "mdi"}, "qt-ws"),
                         ("mdi", "none"))
        for environment in ({"SWAG_RENDER_BACKEND": "other"},
                            {"SWAG_RENDER_BACKEND": "qt-ws"},
                            {"SWAG_RENDER_BACKEND": "qt-ws", "SWAG_UI_MODE": "mdi",
                             "SWAG_WINDOW_MANAGER": "openbox"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment,
                                                                      clear=True):
                with patch.object(launcher.subprocess, "Popen") as spawn:
                    with self.assertRaises(ValueError):
                        launcher.main()
                    spawn.assert_not_called()

    def test_qt_unix_requires_mdi_without_external_window_manager(self):
        self.assertEqual(launcher.resolve_render_backend({"SWAG_RENDER_BACKEND": "qt-unix"}),
                         "qt-unix")
        self.assertEqual(launcher.resolve_desktop_mode({"SWAG_UI_MODE": "mdi"}, "qt-unix"),
                         ("mdi", "none"))
        for environment in ({}, {"SWAG_UI_MODE": "mdi", "SWAG_WINDOW_MANAGER": "openbox"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                launcher.resolve_desktop_mode(environment, "qt-unix")


class LauncherChildren(unittest.TestCase):
    def launch_workspace(self, environment_overrides):
        """Exercise child selection and the persisted manifest with fake children."""
        with tempfile.TemporaryDirectory(prefix="swag-l-", dir="/tmp") as temporary, ExitStack() as stack:
            root = Path(temporary)
            generation = "a" * 32
            launch_runtime = root / "runtime" / "users" / "alice" / generation
            launch_runtime.mkdir(parents=True, mode=0o700)
            rfb = launch_runtime / "rfb.sock"
            reservation = None
            if environment_overrides.get("SWAG_RENDER_BACKEND") == "qt-unix":
                reservation = stack.enter_context(reserve_unix_listener(rfb))
                environment_overrides = {**environment_overrides,
                                         launcher.QT_WS_LISTEN_FD: str(os.dup(reservation.fileno()))}
            else:
                rfb.touch()
            artifacts = root / "artifacts"
            artifacts.mkdir()
            state = artifacts / "state.json"
            environment = {
                "SWAG_USER": "alice", "SWAG_GENERATION": generation,
                "SWAG_RUNTIME_DIR": str(root / "runtime"),
                "SWAG_LAUNCH_ARTIFACT_DIR": str(artifacts),
                "SWAG_STATE_FILE": str(state), "SWAG_MANAGER_PID": "999",
                **environment_overrides,
            }
            commands = []

            def spawn(command, **kwargs):
                child = MagicMock()
                child.pid = 2000 + len(commands)
                child.wait.return_value = 0
                child.poll.return_value = None
                commands.append((command[4:], dict(kwargs["env"]), kwargs["pass_fds"]))
                if command[4] == "/opt/swag/bin/swag-desktop":
                    state.write_text(json.dumps({
                        "ready": True, "user": "alice", "generation": generation,
                        "pid": child.pid,
                    }))
                    # Stay alive through readiness, then exit normally.
                    child.poll.side_effect = [None, 0]
                return child

            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(launcher, "parent_death_signal"), \
                    patch.object(launcher, "exclusive_lock", return_value=123), \
                    patch.object(launcher, "reserve_display", return_value=(110, 456)), \
                    patch.object(launcher.os, "close"), \
                    patch.object(launcher.signal, "signal"), \
                    patch.object(launcher, "stop"), \
                    patch.object(launcher.subprocess, "Popen", side_effect=spawn), \
                    patch.object(Path, "is_socket", return_value=True):
                self.assertEqual(launcher.main(), 0)
            if reservation is not None:
                self.assertGreaterEqual(reservation.fileno(), 0)
                self.assertTrue(rfb.is_socket())  # Only the manager removes the pathname.
            return commands, json.loads((artifacts / "launcher.json").read_text())

    def test_default_still_launches_openbox_and_records_actual_children(self):
        commands, manifest = self.launch_workspace({})
        self.assertEqual([command[0] for command, _, _ in commands],
                         ["Xvnc", "openbox", "vncconfig", "/opt/swag/bin/swag-desktop"])
        self.assertEqual(manifest["ui_mode"], "single")
        self.assertEqual(manifest["window_manager"], "openbox")
        self.assertEqual(set(manifest["helpers"]), {"xvnc", "openbox", "vncconfig", "qt"})
        self.assertEqual(commands[-1][1]["SWAG_UI_MODE"], "single")

    def test_mdi_omits_openbox_but_keeps_rfb_clipboard_and_qt(self):
        commands, manifest = self.launch_workspace({"SWAG_UI_MODE": "mdi"})
        self.assertEqual([command[0] for command, _, _ in commands],
                         ["Xvnc", "vncconfig", "/opt/swag/bin/swag-desktop"])
        self.assertEqual(manifest["ui_mode"], "mdi")
        self.assertEqual(manifest["window_manager"], "none")
        self.assertEqual(set(manifest["helpers"]), {"xvnc", "vncconfig", "qt"})
        self.assertEqual(commands[-1][1]["SWAG_UI_MODE"], "mdi")

    def test_qt_ws_passes_only_reserved_listener_to_qt(self):
        commands, manifest = self.launch_workspace({
            "SWAG_UI_MODE": "mdi", "SWAG_RENDER_BACKEND": "qt-ws",
        })
        self.assertEqual([command[0] for command, _, _ in commands],
                         ["/opt/swag/bin/swag-desktop"])
        command, environment, passed = commands[0]
        self.assertEqual(manifest["render_backend"], "qt-ws")
        self.assertEqual(manifest["helpers"], {"qt": 2000})
        self.assertNotIn("display", manifest)
        self.assertNotIn("rfb_socket", manifest)
        self.assertNotIn("DISPLAY", environment)
        self.assertEqual(environment["QT_QPA_PLATFORM"],
                         f"vncws:size=1280x800:fd={passed[0]}")
        self.assertEqual(len(passed), 1)
        self.assertTrue(1 <= manifest["ws_port"] <= 65535)
        with self.assertRaises(OSError):
            os.fstat(passed[0])  # Parent copy closed immediately after spawn.

    def test_qt_ws_adopts_manager_listener_and_preserves_managers_copy(self):
        with launcher.reserve_ws_listener() as reservation:
            inherited = os.dup(reservation.fileno())
            commands, manifest = self.launch_workspace({
                "SWAG_UI_MODE": "mdi", "SWAG_RENDER_BACKEND": "qt-ws",
                launcher.QT_WS_LISTEN_FD: str(inherited),
            })
            self.assertEqual(commands[0][2], (inherited,))
            self.assertNotIn(launcher.QT_WS_LISTEN_FD, commands[0][1])
            self.assertEqual(manifest["ws_port"], reservation.getsockname()[1])
            with self.assertRaises(OSError):
                os.fstat(inherited)
            with socket.socket() as replacement:
                with self.assertRaises(OSError):
                    replacement.bind(reservation.getsockname())

    def test_tiger_does_not_forward_inherited_qt_ws_descriptor_environment(self):
        commands, _ = self.launch_workspace({launcher.QT_WS_LISTEN_FD: "999999"})
        for _, environment, passed in commands:
            self.assertNotIn(launcher.QT_WS_LISTEN_FD, environment)
            self.assertEqual(passed, ())

    def test_qt_unix_forwards_reserved_descriptor_and_records_socket_identity(self):
        commands, manifest = self.launch_workspace({
            "SWAG_UI_MODE": "mdi", "SWAG_RENDER_BACKEND": "qt-unix",
        })
        self.assertEqual(len(commands), 1)
        command, environment, passed = commands[0]
        self.assertEqual(command[0], "/opt/swag/bin/swag-desktop")
        self.assertEqual(len(passed), 1)
        self.assertEqual(environment["QT_QPA_PLATFORM"], f"vncws:size=1280x800:fd={passed[0]}")
        self.assertNotIn(launcher.QT_WS_LISTEN_FD, environment)
        self.assertNotIn("DISPLAY", environment)
        self.assertNotIn("ws_port", manifest)
        self.assertEqual(manifest["render_backend"], "qt-unix")
        self.assertEqual(manifest["helpers"], {"qt": 2000})
        self.assertIsInstance(manifest["rfb_device"], int)
        self.assertGreater(manifest["rfb_inode"], 0)
        with self.assertRaises(OSError):
            os.fstat(passed[0])

    def test_qt_unix_rejects_missing_invalid_and_wrong_family_descriptors(self):
        with tempfile.TemporaryDirectory(prefix="swag-fd-", dir="/tmp") as temporary:
            path = Path(temporary) / "rfb.sock"
            for value in (None, "garbage", "-1", "0", "2"):
                environment = {} if value is None else {launcher.QT_WS_LISTEN_FD: value}
                with self.subTest(value=value), patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        launcher.workspace_unix_listener(path)
            with launcher.reserve_ws_listener() as reservation:
                inherited = os.dup(reservation.fileno())
                with patch.dict(os.environ, {launcher.QT_WS_LISTEN_FD: str(inherited)}):
                    with self.assertRaises(ValueError):
                        launcher.workspace_unix_listener(path)
                with self.assertRaises(OSError):
                    os.fstat(inherited)

    def test_qt_unix_rejects_wrong_path_and_insecure_socket(self):
        with tempfile.TemporaryDirectory(prefix="swag-fd-", dir="/tmp") as temporary:
            path = Path(temporary) / "rfb.sock"
            with reserve_unix_listener(path) as reservation:
                for expected, mode in ((path.with_name("other.sock"), 0o600), (path, 0o666)):
                    path.chmod(mode)
                    inherited = os.dup(reservation.fileno())
                    with patch.dict(os.environ, {launcher.QT_WS_LISTEN_FD: str(inherited)}):
                        with self.assertRaises(ValueError):
                            launcher.workspace_unix_listener(expected)
                    with self.assertRaises(OSError):
                        os.fstat(inherited)

    def test_inherited_listener_rejects_unbound_or_non_loopback_socket(self):
        for address in (None, ("0.0.0.0", 0)):
            with self.subTest(address=address), socket.socket() as reservation:
                if address is not None:
                    reservation.bind(address)
                    reservation.listen()
                inherited = os.dup(reservation.fileno())
                with patch.dict(os.environ, {launcher.QT_WS_LISTEN_FD: str(inherited)}):
                    with self.assertRaises(ValueError):
                        launcher.workspace_ws_listener()
                with self.assertRaises(OSError):
                    os.fstat(inherited)


if __name__ == "__main__":
    unittest.main()
