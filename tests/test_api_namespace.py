"""Run in an OpenFlexure v2 Python environment or with LabThings 1.3 and its dependencies."""

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase, main
from unittest.mock import patch

from labthings import Schema, create_app

ROOT = Path(__file__).resolve().parents[1]


def load_module(directory, name):
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApiNamespaceTest(TestCase):
    def test_pi_aliases_share_the_scan_and_prompt_state(self):
        backend = ROOT / "pi_setup/backend"
        camera = ModuleType("openflexure_microscope.api.v2.views.actions.camera")
        camera.FullCaptureArgs = Schema
        with patch.dict(sys.modules, {camera.__name__: camera}):
            scan = load_module(backend, "org_microworld_scan_mosaic")
        prompts = load_module(backend, "org_microworld_visual_prompts")

        with TemporaryDirectory() as folder:
            scan.PREVIEW_DIR = str(Path(folder) / "scan")
            prompts.DEFAULT_ROOT = Path(folder) / "prompts"
            app, api = create_app(__name__, prefix="/api/v2", handle_cors=False)
            # Turn off the hardware discovery retry thread in this test process.
            with patch.object(scan.threading.Thread, "start"):
                extensions = [cls() for mod in (scan, prompts) for cls in mod.LABTHINGS_EXTENSIONS]
            for extension in extensions:
                api.register_extension(extension)

            current_scan = api.extensions["org.microworld.scan-mosaic"]
            legacy_scan = api.extensions["org.openmhs.scan-mosaic"]
            current_scan.state.tile_count = 7

            with app.test_client() as client:
                td = client.get("/api/v2/").get_json()
                base = "/api/v2/extensions/"
                for namespace in ("org.microworld", "org.openmhs"):
                    self.assertIn(namespace + ".scan-mosaic/tile", td["actions"])
                    stats = client.get(base + namespace + ".scan-mosaic/hardware").get_json()
                    self.assertEqual(stats["scan_tiles_seen"], 7)
                    response = client.get(base + namespace + ".visual-prompts/status")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()["extension"], "org.microworld.visual-prompts")

                response = client.post(base + "org.openmhs.visual-prompts/codex", json={"read": True})
                self.assertEqual(response.status_code, 200)
                current = client.get(base + "org.microworld.visual-prompts/codex").get_json()
                self.assertTrue(current["connected"])
                self.assertTrue(current["last_read"])

            current_tile = current_scan.views["tile"]["view"]
            legacy_tile = legacy_scan.views["tile"]["view"]
            self.assertIs(current_tile._deque, legacy_tile._deque)
            self.assertNotEqual(current_tile.endpoint, legacy_tile.endpoint)

    def test_discovery_prefers_current_and_falls_back_to_legacy(self):
        network = load_module(ROOT / "Runtime", "network")
        info = {"title": "openflexure:microscope:test", "@type": ["org.openflexure.microscope"]}
        for actions, expected, available in (
            ({"org.microworld.scan-mosaic/tile": {}, "org.openmhs.scan-mosaic/tile": {}}, "org.microworld", True),
            ({"org.openmhs.scan-mosaic/tile": {}}, "org.openmhs", True),
            ({}, "org.microworld", False),
        ):
            with self.subTest(actions=actions), patch.object(network, "read_json", return_value={**info, "actions": actions}):
                result = network.identify("127.0.0.1:5000")
                self.assertEqual(result["api_namespace"], expected)
                self.assertEqual(result["openmhs"], available)


if __name__ == "__main__":
    main()
