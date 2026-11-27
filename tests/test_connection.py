"""Ownership and hardware arbitration exercised through the Pi HTTP API."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from labthings import Schema, create_app
from labthings.views import ActionView

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi_setup/backend"))

SCAN = "/api/v2/extensions/org.microworld.scan-mosaic"
LEGACY = "/api/v2/extensions/org.openmhs.scan-mosaic"
HEADER = "X-MicroWorld-Connection"


class ConnectionTest(TestCase):
    def setUp(self):
        camera = ModuleType("openflexure_microscope.api.v2.views.actions.camera")
        camera.FullCaptureArgs = Schema
        with patch.dict(sys.modules, {camera.__name__: camera}):
            import org_microworld_scan_mosaic as scan

        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        with patch.object(scan, "PREVIEW_DIR", home.name), patch.object(scan.threading.Thread, "start"):
            self.extension = scan.ScanMosaicExtension()
        self.app, self.api = create_app(__name__, prefix="/api/v2", handle_cors=False)
        self.api.register_extension(self.extension)
        self.api.register_extension(scan.LABTHINGS_EXTENSIONS[1]())
        self.gate = self.extension.hardware
        self.assertEqual(self.app.test_client().get(SCAN + "/connection").status_code, 200)

    def request(self, method, path, token=None, **kwargs):
        client = self.app.test_client()
        headers = {HEADER: token} if token else {}
        return client.open(path, method=method, headers=headers,
                           environ_overrides={"REMOTE_ADDR": "192.168.42.5"}, **kwargs)

    def command(self, action, token=None, *, namespace=SCAN, **payload):
        return self.request("POST", namespace + "/connection", token, json={"action": action, **payload})

    def claim(self, computer, takeover=False):
        result = self.command("claim", computer=computer, takeover=takeover)
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        return result.get_json()["token"]

    def test_busy_hardware_blocks_handoff_and_preserves_owner(self):
        token = self.claim("Mac")
        with self.gate.hold("move"):
            denied = self.command("claim", computer="Ubuntu", takeover=True)
            self.assertEqual(denied.status_code, 409)
            self.assertIn("move", denied.get_json()["error"])
        self.assertEqual(self.command("renew", token).status_code, 200)

    def test_idle_handoff_revokes_reads_mutations_and_releases_on_both_namespaces(self):
        old = self.claim("Mac")
        self.assertEqual(self.command("claim", computer="Ubuntu").status_code, 409)
        new = self.claim("Ubuntu", takeover=True)
        self.assertEqual(self.request("GET", SCAN + "/hardware", old).status_code, 409)
        self.assertEqual(self.request("POST", SCAN + "/calibrate_scale", old, json={}).status_code, 409)
        self.assertEqual(self.command("release", old, namespace=LEGACY).status_code, 409)
        self.assertEqual(self.command("renew", new, namespace=LEGACY).status_code, 200)
        self.assertEqual(self.request("GET", "/api/v2/").status_code, 200)
        self.assertEqual(self.command("release", new).status_code, 200)
        self.assertEqual(self.request("POST", SCAN + "/calibrate_scale", new, json={}).status_code, 409)
        self.assertEqual(self.request("POST", SCAN + "/calibrate_scale", json={}).status_code, 409)

    def test_processing_blocks_takeover_until_matching_scan_finishes(self):
        token = self.claim("Mac")
        with self.app.test_request_context(headers={HEADER: token}):
            self.gate.mark_processing("scan-1")
        self.assertEqual(self.command("finish", token, session_id="previous").status_code, 200)
        self.assertEqual(self.command("claim", takeover=True).status_code, 409)
        self.assertEqual(self.command("finish", token, session_id="scan-1").status_code, 200)
        self.claim("Ubuntu", takeover=True)

    def test_competing_initial_claims_create_one_owner(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda n: self.command("claim", computer=str(n)), range(12)))
        self.assertEqual(sorted(answer.status_code for answer in results), [200] + [409] * 11)

    def test_stock_action_in_progress_blocks_handoff(self):
        started = Event()
        finish = Event()

        class StockCapture(ActionView):
            wait_for = 0

            def post(self):
                started.set()
                finish.wait(4)
                return {"ok": True}

        # Flask 1.1 forbids route changes after first use. Bind the fixture
        # before issuing its requests and keep the existing shared gate.
        app, api = create_app(__name__, prefix="/api/v2", handle_cors=False)
        api.register_extension(self.extension)
        api.add_view(StockCapture, "/capture_fixture")
        self.app = app
        token = self.claim("Mac")
        try:
            capture = self.request("POST", "/api/v2/capture_fixture", token, json={})
            self.assertEqual(capture.status_code, 201)
            self.assertTrue(started.wait(1))
            self.assertEqual(self.command("claim", takeover=True).status_code, 409)
        finally:
            finish.set()
            api.actions.join()
        self.claim("Ubuntu", takeover=True)
