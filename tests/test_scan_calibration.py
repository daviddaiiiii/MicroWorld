"""Calibration keeps an existing grid auditable and derives motor-space steps from image motion."""

from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import sys

from labthings import Schema

from test_api_namespace import ROOT, load_module


def scan_module():
    capture = ModuleType("openflexure_microscope.api.v2.views.actions.camera")
    capture.FullCaptureArgs = Schema
    with patch.dict(sys.modules, {capture.__name__: capture}):
        return load_module(ROOT / "pi_setup/backend", "org_microworld_scan_mosaic")


class CalibrationTest(TestCase):
    def test_25_percent_overlap_can_require_more_than_4500_steps(self):
        scan = scan_module()
        home = (100, -200, 300)
        stage = SimpleNamespace(position=home)

        def move_abs(target):
            stage.position = tuple(target)

        stage.move_abs = move_abs

        def shift(reference, current):
            return (current[0] - reference[0]) * .075, -(current[1] - reference[1]) * .069, .80

        with patch.object(scan, "grab_gray_frame", side_effect=lambda _: stage.position), \
             patch.object(scan, "measure_frame_shift", side_effect=shift), \
             patch.object(scan.time, "sleep"):
            result = scan.calibrate_stride(SimpleNamespace(stage=stage))

        self.assertTrue(result["ok"])
        self.assertEqual(result["stride_xy"], [6400, 5217])
        self.assertEqual(result["axis_sign"], [-1, 1])
        self.assertAlmostEqual(result["overlap_xy"][1], .25, places=3)
        self.assertEqual(stage.position, home)

    def test_disagreeing_probes_return_warning_and_restore_stage(self):
        scan = scan_module()
        home = (100, 200, 300)
        stage = SimpleNamespace(position=home)
        stage.move_abs = lambda target: setattr(stage, "position", tuple(target))

        with patch.object(scan, "grab_gray_frame"), \
             patch.object(scan, "measure_frame_shift", side_effect=[(80., 2., .8), (120., 3., .8)]), \
             patch.object(scan.time, "sleep"):
            result = scan.calibrate_stride(SimpleNamespace(stage=stage))

        self.assertFalse(result["ok"])
        self.assertIsNone(result["stride_xy"])
        self.assertIn("disagreed", result["warning"])
        self.assertEqual(stage.position, home)
