import os
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.state import file_lock  # noqa: E402


class StatePermissionTests(unittest.TestCase):
    def test_lock_file_is_private_on_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.lock"
            with file_lock(path) as acquired:
                self.assertTrue(acquired)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_lock_file_tightens_existing_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.lock"
            path.touch(mode=0o644)
            os.chmod(path, 0o644)
            with file_lock(path) as acquired:
                self.assertTrue(acquired)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
