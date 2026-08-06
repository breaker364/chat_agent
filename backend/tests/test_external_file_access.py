import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from backend import tools as runtime_tools


class ExternalFileAccessTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "tmp" / "unittest" / f"external-file-access-{uuid4().hex}"
        self.workspace = root / "workspace"
        self.external = root / "external"
        self.workspace.mkdir(parents=True)
        self.external.mkdir()
        runtime_tools.set_allowed_root(
            self.workspace,
            external_read_roots=[self.external],
        )

    def tearDown(self):
        runtime_tools.set_allowed_root(Path.cwd(), external_read_roots=[])
        shutil.rmtree(self.workspace, ignore_errors=True)
        shutil.rmtree(self.external, ignore_errors=True)

    def test_external_read_and_directory_listing_require_configured_root(self):
        source = self.external / "download.md"
        source.write_text("downloaded evidence", encoding="utf-8")

        self.assertEqual(
            runtime_tools.read_file.invoke({"path": str(source)}),
            "downloaded evidence",
        )
        self.assertIn(
            "download.md",
            runtime_tools.list_directory.invoke({"path": str(self.external)}),
        )

        runtime_tools.set_allowed_root(self.workspace, external_read_roots=[])
        denied = runtime_tools.read_file.invoke({"path": str(source)})
        self.assertIn("Access denied", denied)

    def test_external_file_can_be_copied_into_workspace_only(self):
        source = self.external / "download.txt"
        source.write_text("copy me", encoding="utf-8")

        result = runtime_tools.copy_file.invoke(
            {
                "source_path": str(source),
                "destination_path": "tmp/copied.txt",
            }
        )

        target = self.workspace / "tmp" / "copied.txt"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "copy me")
        self.assertIn("Copied file", result)

        outside_target = self.external / "copied.txt"
        denied = runtime_tools.copy_file.invoke(
            {
                "source_path": str(source),
                "destination_path": str(outside_target),
            }
        )
        self.assertIn("Access denied", denied)
        self.assertFalse(outside_target.exists())


if __name__ == "__main__":
    unittest.main()
