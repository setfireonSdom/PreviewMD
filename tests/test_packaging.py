import re
import unittest
from pathlib import Path

import app_metadata


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MetadataTests(unittest.TestCase):
    def test_metadata_is_a_single_conventional_source(self):
        self.assertEqual(app_metadata.APP_NAME, "PreviewMD")
        self.assertRegex(app_metadata.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(app_metadata.BUNDLE_IDENTIFIER, r"^[a-z0-9.]+$")


class SpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = (PROJECT_ROOT / "PreviewMD.spec").read_text(encoding="utf-8")

    def test_spec_is_portable_and_reads_shared_metadata(self):
        self.assertIn("SPECPATH", self.spec)
        self.assertIn("app_metadata.py", self.spec)
        self.assertNotIn("/Users/", self.spec)
        for key in ("APP_NAME", "APP_VERSION", "BUNDLE_IDENTIFIER"):
            self.assertIn(f"metadata['{key}']", self.spec)

    def test_spec_declares_markdown_and_text_document_types(self):
        self.assertIn("CFBundleDocumentTypes", self.spec)
        for document_type in ("'md'", "'txt'", "net.daringfireball.markdown", "public.plain-text"):
            self.assertIn(document_type, self.spec)
        self.assertIn("UTImportedTypeDeclarations", self.spec)


class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (PROJECT_ROOT / "build.sh").read_text(encoding="utf-8")

    def test_build_script_uses_project_python_and_checked_in_spec(self):
        self.assertIn("PYTHON_BIN", self.script)
        self.assertIn(".venv/bin/python", self.script)
        self.assertIn("PreviewMD.spec", self.script)
        self.assertIn("app_metadata.py", self.script)
        self.assertNotIn("pip install", self.script)

    def test_build_script_signs_and_packages_a_dmg(self):
        for tool in ("codesign", "hdiutil", "ditto"):
            self.assertIn(tool, self.script)
        self.assertIn("--noconfirm", self.script)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = PROJECT_ROOT / ".github" / "workflows" / "build-release.yml"
        cls.workflow = workflow.read_text(encoding="utf-8")

    def test_workflow_tests_builds_and_uploads_without_publishing(self):
        self.assertIn("macos-", self.workflow)
        self.assertIn("unittest discover", self.workflow)
        self.assertIn("bash build.sh", self.workflow)
        self.assertIn("upload-artifact", self.workflow)
        self.assertIn("dist/*/*.dmg", self.workflow)

    def test_publishing_requires_an_explicit_dispatch_input(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertRegex(self.workflow, re.compile(r"publish:.*\n(?:.*\n)*?.*default: false"))
        self.assertIn("inputs.publish == true", self.workflow)


if __name__ == "__main__":
    unittest.main()
