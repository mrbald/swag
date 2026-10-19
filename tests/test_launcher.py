"""Launcher configuration and child selection, without starting X or Qt."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from swag_session import launcher


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


class LauncherChildren(unittest.TestCase):
    def launch_workspace(self, environment_overrides):
        """Exercise child selection and the persisted manifest with fake children."""
        with tempfile.TemporaryDirectory(prefix="swag-launcher-") as temporary:
            root = Path(temporary)
            generation = "a" * 32
            launch_runtime = root / "runtime" / "users" / "alice" / generation
            launch_runtime.mkdir(parents=True)
            rfb = launch_runtime / "rfb.sock"
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
                commands.append((command[4:], dict(kwargs["env"])))
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
            return commands, json.loads((artifacts / "launcher.json").read_text())

    def test_default_still_launches_openbox_and_records_actual_children(self):
        commands, manifest = self.launch_workspace({})
        self.assertEqual([command[0] for command, _ in commands],
                         ["Xvnc", "openbox", "vncconfig", "/opt/swag/bin/swag-desktop"])
        self.assertEqual(manifest["ui_mode"], "single")
        self.assertEqual(manifest["window_manager"], "openbox")
        self.assertEqual(set(manifest["helpers"]), {"xvnc", "openbox", "vncconfig", "qt"})
        self.assertEqual(commands[-1][1]["SWAG_UI_MODE"], "single")

    def test_mdi_omits_openbox_but_keeps_rfb_clipboard_and_qt(self):
        commands, manifest = self.launch_workspace({"SWAG_UI_MODE": "mdi"})
        self.assertEqual([command[0] for command, _ in commands],
                         ["Xvnc", "vncconfig", "/opt/swag/bin/swag-desktop"])
        self.assertEqual(manifest["ui_mode"], "mdi")
        self.assertEqual(manifest["window_manager"], "none")
        self.assertEqual(set(manifest["helpers"]), {"xvnc", "vncconfig", "qt"})
        self.assertEqual(commands[-1][1]["SWAG_UI_MODE"], "mdi")


if __name__ == "__main__":
    unittest.main()
