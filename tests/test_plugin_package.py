import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trace_plugin_builder", PROJECT / "scripts" / "build_plugin.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PluginPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="trace plugin package ")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve() / "source with spaces"
        self.plugin = self.project / "plugins" / "codex-trace-probe"
        (self.plugin / ".codex-plugin").mkdir(parents=True)
        (self.project / "server.py").write_bytes(b"# viewer server\nINDEX = 'index.html'\n")
        (self.project / "index.html").write_bytes("<!doctype html><title>轨迹</title>".encode())
        (self.project / "LICENSE").write_bytes(b"MIT License\nCopyright contributors\n")
        manifest = {"name": "codex-trace-probe", "version": "0.2.0", "mcpServers": "./.mcp.json"}
        (self.plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest))
        (self.plugin / ".mcp.json").write_text('{"mcpServers":{"probe":{"command":"python3"}}}')
        (self.plugin / "server.py").write_bytes(b"# MCP server\n")
        (self.plugin / "viewer-bridge.js").write_bytes(b"// host bridge\n")

    def test_build_and_check_preserve_exact_sources_and_are_idempotent(self):
        result = builder.build(self.project)
        self.assertFalse(result["checked"])
        self.assertEqual(result["version"], "0.2.0")
        times = {}
        for source, target in builder.ASSETS:
            generated = self.plugin / "assets" / target
            self.assertEqual(generated.read_bytes(), (self.project / source).read_bytes())
            times[target] = generated.stat().st_mtime_ns
        builder.build(self.project, check=True)
        builder.build(self.project)
        self.assertEqual(times, {target: (self.plugin / "assets" / target).stat().st_mtime_ns
                                 for _, target in builder.ASSETS})

    def test_check_missing_and_stale_assets_never_writes(self):
        with self.assertRaisesRegex(builder.BuildError, "Missing or stale"):
            builder.build(self.project, check=True)
        self.assertFalse((self.plugin / "assets").exists())
        builder.build(self.project)
        generated = self.plugin / "assets" / "index.html"
        generated.write_bytes(b"stale")
        with self.assertRaisesRegex(builder.BuildError, "Missing or stale"):
            builder.build(self.project, check=True)
        self.assertEqual(generated.read_bytes(), b"stale")

    def test_missing_source_and_invalid_configuration_have_no_output(self):
        (self.project / "index.html").unlink()
        with self.assertRaises(builder.BuildError):
            builder.build(self.project)
        self.assertFalse((self.plugin / "assets").exists())
        (self.project / "index.html").write_bytes(b"html")
        (self.plugin / ".mcp.json").write_text('{"mcpServers":null}')
        with self.assertRaisesRegex(builder.BuildError, "Invalid plugin"):
            builder.build(self.project)
        self.assertFalse((self.plugin / "assets").exists())

    def test_zip_has_complete_package_and_excludes_local_files(self):
        (self.plugin / "ui.html").write_bytes(b"<!doctype html><title>Retired prototype</title>")
        (self.plugin / ".env").write_text("private")
        (self.plugin / "rollout.jsonl").write_text("private")
        (self.plugin / "private.js").write_text("private")
        (self.plugin / "__pycache__").mkdir()
        (self.plugin / "__pycache__" / "server.pyc").write_bytes(b"private")
        archive = Path(self.tmp.name) / "release" / "plugin.zip"
        builder.build(self.project, archive=archive)
        with zipfile.ZipFile(archive) as package:
            expected = set(builder.PACKAGE) | {"assets/index.html", "assets/viewer_server.py", "LICENSE"}
            self.assertEqual(set(package.namelist()), expected)
            self.assertNotIn("ui.html", package.namelist())
            self.assertEqual(package.read("assets/index.html"), (self.project / "index.html").read_bytes())
            self.assertEqual(package.read("assets/viewer_server.py"), (self.project / "server.py").read_bytes())

    def test_license_is_copied_and_verified_with_package(self):
        builder.build(self.project)
        self.assertEqual((self.plugin / "LICENSE").read_bytes(), (self.project / "LICENSE").read_bytes())
        (self.plugin / "LICENSE").write_bytes(b"wrong")
        with self.assertRaisesRegex(builder.BuildError, "Missing or stale"):
            builder.build(self.project, check=True)

    def test_missing_license_refuses_build_before_writing_assets(self):
        (self.project / "LICENSE").unlink()
        with self.assertRaisesRegex(builder.BuildError, "Missing package file"):
            builder.build(self.project)
        self.assertFalse((self.plugin / "assets").exists())

    def test_source_symlink_is_rejected_before_generating_assets(self):
        source = self.project / "server.py"
        external = Path(self.tmp.name) / "external.py"
        source.rename(external)
        source.symlink_to(external)
        with self.assertRaisesRegex(builder.BuildError, "Symbolic links"):
            builder.build(self.project)
        self.assertFalse((self.plugin / "assets").exists())
        self.assertEqual(external.read_bytes(), b"# viewer server\nINDEX = 'index.html'\n")

    def test_assets_directory_symlink_is_rejected(self):
        external = Path(self.tmp.name) / "external assets"
        external.mkdir()
        (self.plugin / "assets").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(builder.BuildError, "Symbolic links"):
            builder.build(self.project)
        self.assertEqual(list(external.iterdir()), [])

    def test_existing_generated_file_symlink_is_rejected(self):
        builder.build(self.project)
        generated = self.plugin / "assets" / "index.html"
        external = Path(self.tmp.name) / "external.html"
        external.write_bytes(b"preserve")
        generated.unlink()
        generated.symlink_to(external)
        with self.assertRaisesRegex(builder.BuildError, "Symbolic links"):
            builder.build(self.project)
        self.assertEqual(external.read_bytes(), b"preserve")

    def test_package_root_symlink_is_rejected(self):
        external = Path(self.tmp.name) / "external plugin"
        self.plugin.rename(external)
        self.plugin.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(builder.BuildError, "Symbolic links"):
            builder.build(self.project)
        self.assertFalse((external / "assets").exists())

    def test_output_directory_or_zip_symlink_is_rejected(self):
        (self.plugin / "assets").mkdir()
        (self.plugin / "assets" / "index.html").mkdir()
        with self.assertRaisesRegex(builder.BuildError, "regular file"):
            builder.build(self.project)
        self.assertFalse((self.plugin / "assets" / "viewer_server.py").exists())
        (self.plugin / "assets" / "index.html").rmdir()
        external = Path(self.tmp.name) / "external.zip"
        external.write_bytes(b"preserve")
        archive = Path(self.tmp.name) / "release.zip"
        archive.symlink_to(external)
        with self.assertRaisesRegex(builder.BuildError, "Symbolic links"):
            builder.build(self.project, archive=archive)
        self.assertEqual(external.read_bytes(), b"preserve")

    def test_check_with_zip_and_zip_inside_plugin_are_rejected_without_writes(self):
        for check, archive in ((True, Path(self.tmp.name) / "release.zip"),
                               (False, self.plugin / "archive.zip")):
            with self.subTest(check=check):
                with self.assertRaises(builder.BuildError):
                    builder.build(self.project, check=check, archive=archive)
                self.assertFalse(archive.exists())
                self.assertFalse((self.plugin / "assets").exists())

    def test_missing_bridge_fails_instead_of_packaging_an_incomplete_plugin(self):
        (self.plugin / "viewer-bridge.js").unlink()
        with self.assertRaisesRegex(builder.BuildError, "Missing package file"):
            builder.build(self.project, archive=Path(self.tmp.name) / "release.zip")
        self.assertFalse((self.plugin / "assets").exists())

    def test_cli_uses_script_directory_from_unrelated_working_directory(self):
        scripts = self.project / "scripts"
        scripts.mkdir()
        (scripts / "build_plugin.py").write_bytes((PROJECT / "scripts" / "build_plugin.py").read_bytes())
        argv = [sys.executable, "-B", str(scripts / "build_plugin.py")]
        result = subprocess.run(argv + ["--check"], cwd=self.tmp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing or stale", json.loads(result.stderr)["error"])
        result = subprocess.run(argv, cwd=self.tmp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["plugin"], str(self.plugin))
        result = subprocess.run(argv + ["--check"], cwd=self.tmp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["checked"])


if __name__ == "__main__":
    unittest.main()
