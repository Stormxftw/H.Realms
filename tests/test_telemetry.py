import tempfile
import unittest
from pathlib import Path

import app


class TelemetryTests(unittest.TestCase):
    def test_find_process_pid_reads_proc_cmdline_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp)
            wanted = proc_root / "123"
            unrelated = proc_root / "456"
            wanted.mkdir()
            unrelated.mkdir()
            (wanted / "cmdline").write_bytes(
                b"/srv/Pal/Binaries/Linux/PalServer-Linux-Shipping\x00Pal\x00-port=8211\x00"
            )
            (unrelated / "cmdline").write_bytes(b"python3\x00app.py\x00")

            pid = app.find_process_pid("PalServer-Linux-Shipping", proc_root=proc_root)

            self.assertEqual(123, pid)


if __name__ == "__main__":
    unittest.main()
