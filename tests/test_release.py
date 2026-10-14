import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
spec = importlib.util.spec_from_file_location("release", PROJECT / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="release fixture with spaces ")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        files = release.ROOT_FILES + release.SUPPORT_FILES
        for name in files:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic file\n")
        plugin = self.project / "plugins/codex-trace-probe"
        for name in release.build_plugin.PACKAGE:
            path = plugin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic file\n")
        (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
            "name": "codex-trace-probe", "version": "0.11.0", "license": "MIT", "mcpServers": "./.mcp.json"}))
        (plugin / ".mcp.json").write_text('{"mcpServers": {}}')
        (self.project / "tests").mkdir()
        (self.project / "tests/test_synthetic.py").write_text("# synthetic regression\n")

    def test_archive_is_complete_reproducible_and_excludes_local_data(self):
        for name in (".env", ".git/config", ".verification/secret.txt", "private.jsonl",
                     "local-previews/private.png", "dist/old.zip", "plugins/codex-trace-probe/private.txt"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("DO_NOT_PUBLISH")
        result = release.build(self.project)
        before = Path(result["source_archive"]).read_bytes()
        with zipfile.ZipFile(result["source_archive"]) as archive:
            names = set(archive.namelist())
            for name in ("INSTALL.md", "LICENSE", "scripts/install_plugin.py", "plugins/codex-trace-probe/assets/index.html",
                         "plugins/codex-trace-probe/LICENSE"):
                self.assertIn("codex-trace-viewer/" + name, names)
            self.assertFalse(any(b"DO_NOT_PUBLISH" in archive.read(name) for name in names))
        with zipfile.ZipFile(result["plugin_archive"]) as archive:
            self.assertIn("LICENSE", archive.namelist())
        for line in Path(result["checksums"]).read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((self.project / "dist" / name).read_bytes()).hexdigest())
        release.build(self.project)
        self.assertEqual(before, Path(result["source_archive"]).read_bytes())

    def test_symlinked_source_is_rejected(self):
        readme = self.project / "README.md"
        readme.unlink()
        readme.symlink_to(self.project / "LICENSE")
        with self.assertRaisesRegex(release.build_plugin.BuildError, "Symbolic links"):
            release.build(self.project)

    def test_unlicensed_or_invalid_version_release_is_rejected(self):
        path = self.project / "plugins/codex-trace-probe/.codex-plugin/plugin.json"
        manifest = json.loads(path.read_text())
        manifest["license"] = "unknown"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(release.build_plugin.BuildError, "MIT"):
            release.build(self.project)
        manifest["license"] = "MIT"
        manifest["version"] = "../../private"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(release.build_plugin.BuildError, "major.minor.patch"):
            release.build(self.project)


if __name__ == "__main__":
    unittest.main()
