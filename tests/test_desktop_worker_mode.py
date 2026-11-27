"""Exercise Worker selection shared by both desktop clients."""

from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Runtime"))
import desktop


class WorkerModeTest(TestCase):
    device = {"url": "http://microscope.local:5000", "api_namespace": "org.microworld",
              "openmhs": True}

    def select(self, mode, *, busy=False, owner="", online=False):
        replies = [
            {"busy": busy},
            {"agent": {"worker_online": online, "supervisor_url": owner}},
        ]
        with patch.object(desktop, "read_json", side_effect=replies):
            return desktop.worker_mode(self.device, mode, "192.168.42.5")

    def test_auto_keeps_external_worker(self):
        self.assertFalse(self.select("auto", owner="http://192.168.42.9:18766", online=True))
        with self.assertRaisesRegex(RuntimeError, "PC Worker"):
            self.select("worker", owner="http://192.168.42.9:18766", online=True)

    def test_existing_legacy_worker_is_local(self):
        self.assertTrue(self.select("auto", owner="http://192.168.42.5:18765", online=True))

    def test_idle_or_viewer(self):
        self.assertTrue(self.select("auto"))
        self.assertFalse(self.select("viewer"))
        self.assertFalse(self.select("auto", busy=True))
        with self.assertRaisesRegex(RuntimeError, "acquisition"):
            self.select("worker", busy=True)
