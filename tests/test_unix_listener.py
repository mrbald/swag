"""Filesystem ownership checks for the supervised raw-RFB endpoint."""

from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from swag_session.processes import reserve_unix_listener, unix_socket_identity, unlink_owned_socket


class UnixListener(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="swag-sock-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.path = self.root / "rfb.sock"

    def tearDown(self):
        self.temporary.cleanup()

    def test_reservation_permissions_identity_and_explicit_cleanup(self):
        with reserve_unix_listener(self.path) as listener:
            self.assertEqual(listener.family, socket.AF_UNIX)
            self.assertEqual(listener.getsockname(), str(self.path))
            entry = self.path.lstat()
            self.assertEqual(entry.st_mode & 0o777, 0o600)
            identity = unix_socket_identity(self.path)
            self.assertEqual(identity, (entry.st_dev, entry.st_ino))
        self.assertTrue(self.path.is_socket())
        unlink_owned_socket(self.path, identity)
        self.assertFalse(self.path.exists())
        unlink_owned_socket(self.path, identity)  # Repeated teardown is harmless.

    def test_existing_file_symlink_and_socket_are_never_removed(self):
        self.path.write_text("foreign data")
        with self.assertRaises(OSError):
            reserve_unix_listener(self.path)
        self.assertEqual(self.path.read_text(), "foreign data")
        self.path.unlink()
        target = self.root / "target"
        target.write_text("foreign target")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            reserve_unix_listener(self.path)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), "foreign target")
        self.path.unlink()
        with reserve_unix_listener(self.path):
            identity = unix_socket_identity(self.path)
            with self.assertRaises(OSError):
                reserve_unix_listener(self.path)
            self.assertEqual(unix_socket_identity(self.path), identity)

    def test_insecure_directory_and_parent_symlink_are_rejected(self):
        for mode in (0o755, 0o770):
            self.root.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                reserve_unix_listener(self.path)
            self.assertFalse(self.path.exists())
        self.root.chmod(0o700)
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            reserve_unix_listener(link / "rfb.sock")

    def test_malformed_paths_are_rejected_before_bind(self):
        for path in (Path("relative.sock"), Path("/tmp/with\0nul"), self.root / ("x" * 108)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                reserve_unix_listener(path)

    def test_identity_and_cleanup_do_not_follow_replacement_symlink(self):
        with reserve_unix_listener(self.path):
            identity = unix_socket_identity(self.path)
            self.path.unlink()
            target = self.root / "target"
            target.write_text("keep")
            self.path.symlink_to(target)
            with self.assertRaises(ValueError):
                unix_socket_identity(self.path)
            unlink_owned_socket(self.path, identity)
            self.assertTrue(self.path.is_symlink())
            self.assertEqual(target.read_text(), "keep")

    def test_reservation_failure_removes_its_own_socket(self):
        with patch.object(Path, "chmod", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                reserve_unix_listener(self.path)
        self.assertFalse(self.path.exists())

    def test_cleanup_rejects_mismatched_identity(self):
        with reserve_unix_listener(self.path):
            device, inode = unix_socket_identity(self.path)
            unlink_owned_socket(self.path, (device, inode + 1))
            self.assertTrue(self.path.is_socket())


if __name__ == "__main__":
    unittest.main()
