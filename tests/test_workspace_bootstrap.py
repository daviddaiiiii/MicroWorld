"""A workspace lookup initializes the library without resolving the workspace again."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac-agent"))

import openmhs_workspace as workspace


class WorkspaceBootstrapTest(TestCase):
    def test_library_seed_is_nonrecursive_and_preserves_existing_files(self):
        with TemporaryDirectory() as folder, patch.dict("os.environ", {"OPENMHS_WORKSPACE": folder}):
            with patch.object(workspace, "workspace_root", wraps=workspace.workspace_root) as lookup:
                root = lookup()
                self.assertEqual(lookup.call_count, 1)

            readme = root / "lib" / "README.md"
            self.assertTrue(readme.is_file())
            self.assertTrue((root / "lib" / "algorithms" / "README.md").is_file())
            readme.write_text("Workspace notes", encoding="utf-8")
            self.assertEqual(workspace.workspace_root(), root)
            self.assertEqual(readme.read_text(encoding="utf-8"), "Workspace notes")
