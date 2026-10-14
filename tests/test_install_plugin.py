import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("trace_installer", PROJECT / "scripts/install_plugin.py")
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.tmp = tempfile.TemporaryDirectory(prefix="trace install with spaces ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "download with spaces"
        shutil.copytree(PROJECT / "plugins/codex-trace-probe", self.project / "plugins/codex-trace-probe")
        (self.project / "scripts").mkdir()
        for name in ("server.py", "index.html", "LICENSE"):
            source = PROJECT / name
            (self.project / name).write_bytes(source.read_bytes() if source.exists() else b"MIT License\n")
        shutil.copy2(PROJECT / "scripts/build_plugin.py", self.project / "scripts/build_plugin.py")
        self.home = self.root / "custom home with spaces"
        self.calls = []
        self.cli = self.root / "codex"
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o755)

    def runner(self, command, **kwargs):
        self.calls.append(command)
        if command[1:] == ["--version"]:
            return "codex-cli 0.160.0"
        if command[-1:] == ["--help"]:
            return "Usage: codex plugin marketplace add / plugin add / plugin list --json"
        if command[1:4] == ["plugin", "marketplace", "add"]:
            return json.dumps({"marketplaceName": "codex-trace-local", "installedRoot": str(self.home / "plugins/marketplace"), "alreadyAdded": False})
        if command[1:3] == ["plugin", "add"]:
            self.installed = kwargs["env"]["CODEX_HOME"] + "/plugins/cache/test"
            marketplace = Path(next(call[4] for call in reversed(self.calls) if call[1:4] == ["plugin", "marketplace", "add"] and "--help" not in call))
            shutil.copytree(marketplace / "plugins/codex-trace-probe", self.installed, dirs_exist_ok=True)
            return json.dumps({"pluginId": "codex-trace-probe@codex-trace-local", "version": "0.11.0", "installedPath": self.installed})
        if command[1:3] == ["plugin", "list"]:
            return json.dumps({"installed": [{"pluginId": "codex-trace-probe@codex-trace-local", "version": "0.11.0", "installed": True, "enabled": True}], "available": []})
        raise AssertionError(command)

    def install(self, **kwargs):
        with patch.object(self.installer.sys, "platform", "darwin"):
            return self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home), "PATH": os.environ.get("PATH", "")}, runner=self.runner, **kwargs)

    def test_check_does_not_write_or_install(self):
        assets = self.project / "plugins/codex-trace-probe/assets"
        if assets.exists():
            shutil.rmtree(assets)
        result = self.install(check=True)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(self.home.exists())
        self.assertFalse(assets.exists())
        self.assertFalse(any(command[1:3] == ["plugin", "add"] and "--help" not in command for command in self.calls))

    def test_install_is_idempotent_and_uses_current_python(self):
        first = self.install()
        second = self.install()
        self.assertEqual(first["marketplace_path"], second["marketplace_path"])
        mcp = json.loads((Path(first["marketplace_path"]) / "plugins/codex-trace-probe/.mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["codex-trace-probe"]["command"], str(Path(sys.executable).resolve()))
        original = json.loads((self.project / "plugins/codex-trace-probe/.mcp.json").read_text())
        self.assertEqual(original["mcpServers"]["codex-trace-probe"]["command"], "python3")
        self.assertTrue(first["enabled"])
        self.assertTrue((Path(first["marketplace_path"]) / "plugins/codex-trace-probe/LICENSE").is_file())

    def test_unsupported_platform_fails_before_cli_or_writes(self):
        with patch.object(self.installer.sys, "platform", "linux"):
            with self.assertRaisesRegex(self.installer.InstallError, "macOS"):
                self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=self.runner)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.home.exists())

    def test_explicit_missing_cli_does_not_fallback(self):
        with self.assertRaisesRegex(self.installer.InstallError, "CLI"):
            with patch.object(self.installer.sys, "platform", "darwin"):
                self.installer.install(project=self.project, codex=str(self.root / "missing"), environ={"CODEX_HOME": str(self.home)}, runner=self.runner)
        self.assertFalse(self.home.exists())

    def test_old_or_missing_python_fails_before_install(self):
        for attribute, value in (("version_info", (3, 8)), ("executable", str(self.root / "missing python"))):
            with self.subTest(attribute=attribute):
                with patch.object(self.installer.sys, attribute, value):
                    with self.assertRaisesRegex(self.installer.InstallError, "Python"):
                        self.install()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.home.exists())

    def test_missing_cli_reports_actionable_error(self):
        with patch.object(self.installer.shutil, "which", return_value=None), patch.object(self.installer, "APP_CLI", self.root / "absent app"):
            with patch.object(self.installer.sys, "platform", "darwin"):
                with self.assertRaisesRegex(self.installer.InstallError, "--codex"):
                    self.installer.install(project=self.project, environ={"CODEX_HOME": str(self.home)}, runner=self.runner)
        self.assertFalse(self.home.exists())

    def test_wrong_cli_on_path_never_falls_back_to_app(self):
        with patch.object(self.installer.shutil, "which", return_value=str(self.cli)):
            with patch.object(self.installer.sys, "platform", "darwin"):
                with self.assertRaisesRegex(self.installer.InstallError, "not Codex"):
                    self.installer.install(project=self.project, environ={"CODEX_HOME": str(self.home)}, runner=lambda command, **kwargs: "different tool 1.0")
        self.assertFalse(self.home.exists())

    def test_cli_without_plugin_json_support_fails_before_writes(self):
        original = self.runner
        def runner(command, **kwargs):
            return "Usage: codex plugin" if command[-1] == "--help" else original(command, **kwargs)
        with patch.object(self.installer.sys, "platform", "darwin"):
            with self.assertRaisesRegex(self.installer.InstallError, "does not support"):
                self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=runner)
        self.assertFalse(self.home.exists())

    def test_symlink_source_and_output_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.home.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(self.installer.InstallError, "Symbolic"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])
        self.home.unlink()
        source = self.project / "index.html"
        source.rename(outside / "index.html")
        source.symlink_to(outside / "index.html")
        with self.assertRaisesRegex(self.installer.InstallError, "Symbolic"):
            self.install()
        self.assertFalse(self.home.exists())

    def test_tampered_retained_installation_is_not_overwritten(self):
        result = self.install()
        output = Path(result["marketplace_path"]) / "plugins/codex-trace-probe/server.py"
        output.write_bytes(b"preserve unrelated content")
        with self.assertRaisesRegex(self.installer.InstallError, "differs"):
            self.install()
        self.assertEqual(output.read_bytes(), b"preserve unrelated content")

    def test_partial_cli_failure_is_reported_and_package_retained(self):
        original = self.runner
        def runner(command, **kwargs):
            if command[1:3] == ["plugin", "add"] and "--help" not in command:
                raise self.installer.InstallError("install denied")
            return original(command, **kwargs)
        with patch.object(self.installer.sys, "platform", "darwin"):
            with self.assertRaisesRegex(self.installer.InstallError, "plugin installation failed.*no automatic rollback"):
                self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=runner)
        self.assertEqual(len(list((self.home / "trace-viewer-install").glob("*/marketplace/plugins/codex-trace-probe/server.py"))), 1)
        self.assertFalse(any(command[1:3] == ["plugin", "remove"] for command in self.calls))

    def test_disabled_plugin_does_not_report_success(self):
        original = self.runner
        def runner(command, **kwargs):
            result = original(command, **kwargs)
            if command[1:3] == ["plugin", "list"] and "--help" not in command:
                result = result.replace('"enabled": true', '"enabled": false')
            return result
        with patch.object(self.installer.sys, "platform", "darwin"):
            with self.assertRaisesRegex(self.installer.InstallError, "not confirmed"):
                self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=runner)

    def test_check_does_not_generate_bytecode_or_modify_sources(self):
        before = {str(path.relative_to(self.project)): path.read_bytes() for path in self.project.rglob("*") if path.is_file()}
        self.install(check=True)
        after = {str(path.relative_to(self.project)): path.read_bytes() for path in self.project.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_installed_bytes_must_match_retained_package(self):
        original = self.runner
        def runner(command, **kwargs):
            result = original(command, **kwargs)
            if command[1:3] == ["plugin", "add"] and "--help" not in command:
                (Path(self.installed) / "server.py").write_bytes(b"wrong cached version")
            return result
        with patch.object(self.installer.sys, "platform", "darwin"):
            with self.assertRaisesRegex(self.installer.InstallError, "Installed package differs"):
                self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=runner)

    def test_cli_entry_works_from_unrelated_working_directory(self):
        import subprocess
        fake_cli = self.root / "fake CLI with spaces"
        fake_cli.write_text("#!" + sys.executable + "\nimport sys\nprint('codex-cli 0.160.0' if sys.argv[1:] == ['--version'] else 'Usage: codex plugin --json')\n")
        fake_cli.chmod(0o755)
        installer = self.project / "scripts/install_plugin.py"
        shutil.copy2(PROJECT / "scripts/install_plugin.py", installer)
        completed = subprocess.run([sys.executable, str(installer), "--check", "--codex", str(fake_cli)], cwd=self.root,
                                   env={**os.environ, "CODEX_HOME": str(self.home)}, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "ready")
        self.assertFalse(self.home.exists())

    def test_cli_probe_uses_disposable_home_and_cleans_it(self):
        self.install(check=True)
        observed = []
        def runner(command, **kwargs):
            observed.append(Path(kwargs["env"]["CODEX_HOME"]))
            self.assertNotEqual(kwargs["env"]["CODEX_HOME"], str(self.home))
            Path(kwargs["env"]["CODEX_HOME"], "alias-probe").write_text("probe")
            return self.runner(command, **kwargs)
        with patch.object(self.installer.sys, "platform", "darwin"):
            self.installer.install(project=self.project, check=True, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=runner)
        self.assertEqual(len(observed), 4)
        self.assertTrue(all(not directory.exists() for directory in observed))
        self.assertFalse(self.home.exists())

    def test_invalid_cli_json_is_an_explicit_partial_failure(self):
        original = self.runner
        def runner(command, **kwargs):
            return "not JSON" if command[1:4] == ["plugin", "marketplace", "add"] and "--help" not in command else original(command, **kwargs)
        with patch.object(self.installer.sys, "platform", "darwin"):
            with self.assertRaisesRegex(self.installer.InstallError, "marketplace registration failed.*invalid JSON"):
                self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": str(self.home)}, runner=runner)

    def test_invalid_source_configuration_fails_before_assets_or_state_write(self):
        assets = self.project / "plugins/codex-trace-probe/assets"
        if assets.exists():
            shutil.rmtree(assets)
        (self.project / "plugins/codex-trace-probe/.mcp.json").write_text('{"mcpServers":null}')
        with self.assertRaisesRegex(self.installer.InstallError, "Invalid MCP"):
            self.install()
        self.assertFalse(assets.exists())
        self.assertFalse(self.home.exists())

    def test_missing_license_is_not_installed(self):
        (self.project / "LICENSE").unlink()
        with self.assertRaisesRegex(self.installer.InstallError, "MIT LICENSE"):
            self.install()
        self.assertFalse(self.home.exists())

    def test_cli_error_and_timeout_are_explicit(self):
        import subprocess
        for failure in (OSError("cannot execute"), subprocess.TimeoutExpired(["cli"], 120)):
            with self.subTest(error=failure):
                with patch.object(self.installer.subprocess, "run", side_effect=failure):
                    with self.assertRaisesRegex(self.installer.InstallError, "CLI command failed"):
                        self.installer.run(["cli"], env={})
        with patch.object(self.installer.subprocess, "run", return_value=subprocess.CompletedProcess(["cli"], 2, "", "bad CLI")):
            with self.assertRaisesRegex(self.installer.InstallError, "exit 2.*bad CLI"):
                self.installer.run(["cli"], env={})

    def test_source_repository_cannot_be_used_as_codex_home(self):
        with patch.object(self.installer.sys, "platform", "darwin"):
            with self.assertRaisesRegex(self.installer.InstallError, "outside"):
                self.installer.install(project=self.project, check=True, codex=str(self.cli), environ={"CODEX_HOME": str(self.project)}, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_codex_home_with_trailing_space_is_preserved(self):
        tail_space = str(self.home) + " "
        with patch.object(self.installer.sys, "platform", "darwin"):
            result = self.installer.install(project=self.project, codex=str(self.cli), environ={"CODEX_HOME": tail_space}, runner=self.runner)
        self.assertEqual(result["codex_home"], tail_space)
        self.assertTrue(Path(tail_space, "trace-viewer-install").is_dir())
        self.assertFalse(self.home.exists())
