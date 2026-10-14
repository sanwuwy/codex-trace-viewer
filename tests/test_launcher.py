import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]
THREAD_A = "11111111-2222-4333-8444-555555555555"
THREAD_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
URL = "http://127.0.0.1:54321"


def copy_checkout(directory):
    checkout = Path(directory).resolve() / "source checkout with spaces"
    helper = checkout / ".agents" / "skills" / "codex-trace" / "scripts" / "viewer.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(PROJECT / ".agents" / "skills" / "codex-trace" / "scripts" / "viewer.py", helper)
    for name in ("trace.py", "server.py", "index.html"):
        shutil.copyfile(PROJECT / name, checkout / name)
    return checkout


def load_launcher(checkout):
    spec = importlib.util.spec_from_file_location("trace_launcher", checkout / "trace.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="trace launcher ")
        self.addCleanup(self.tmp.cleanup)
        self.checkout = copy_checkout(self.tmp.name)
        self.launcher = load_launcher(self.checkout)
        self.home = Path(self.tmp.name).resolve() / "isolated codex home"
        self.environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home),
                                                   "CODEX_THREAD_ID": THREAD_A})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, args, result=None, browser=True):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(self.launcher.viewer, "control", return_value=result or
                          {"thread_id": THREAD_A, "url": URL, "reused": False}) as control, \
             patch.object(self.launcher.webbrowser, "open", return_value=browser) as opened, \
             patch.object(sys, "stdout", output), patch.object(sys, "stderr", errors):
            code = self.launcher.main(args)
        return code, output.getvalue(), errors.getvalue(), control, opened

    def test_current_start_routes_identity_and_no_open_suppresses_dispatch(self):
        code, output, error, control, opened = self.invoke(["start", "--no-open"])
        self.assertEqual(code, 0, error)
        control.assert_called_once_with("start", THREAD_A)
        opened.assert_not_called()
        self.assertEqual(json.loads(output)["url"], URL)
        self.assertNotIn("control_token", output + error)

    def test_default_start_dispatches_returned_url(self):
        code, output, error, control, opened = self.invoke(["start"])
        self.assertEqual(code, 0, error)
        control.assert_called_once_with("start", THREAD_A)
        opened.assert_called_once_with(URL)
        self.assertEqual(json.loads(output)["browser_target"], "browser")

    def test_stop_and_status_do_not_open_a_browser(self):
        for command in ("stop", "status"):
            with self.subTest(command=command):
                code, output, error, control, opened = self.invoke([command])
                self.assertEqual(code, 0, error)
                control.assert_called_once_with(command, THREAD_A)
                opened.assert_not_called()
                self.assertEqual(json.loads(output)["thread_id"], THREAD_A)

    def test_missing_identity_has_no_controller_or_state_side_effect(self):
        os.environ.pop("CODEX_THREAD_ID", None)
        for command in ("start", "stop", "status", "shortcuts"):
            with self.subTest(command=command):
                code, output, error, control, opened = self.invoke([command])
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertIn("CODEX_THREAD_ID", json.loads(error)["error"])
                control.assert_not_called()
                opened.assert_not_called()
                self.assertFalse(self.home.exists())

    def test_all_operations_do_not_depend_on_current_identity(self):
        os.environ.pop("CODEX_THREAD_ID", None)
        code, output, error, control, opened = self.invoke(["start-all", "--no-open"],
                                                         {"thread_id": None, "url": URL})
        self.assertEqual(code, 0, error)
        control.assert_called_once_with("start", None)
        opened.assert_not_called()
        self.assertIsNone(json.loads(output)["thread_id"])
        result = {"results": [{"thread_id": THREAD_A, "stopped": True}], "errors": []}
        with patch.object(self.launcher.viewer, "stop_all", return_value=result) as stopped:
            code, output, error, control, opened = self.invoke(["stop-all"])
        self.assertEqual(code, 0, error)
        stopped.assert_called_once_with()
        control.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(json.loads(output), result)

    def test_partial_stop_all_reports_structured_failure(self):
        result = {"results": [{"thread_id": THREAD_A, "stopped": True}],
                  "errors": [{"service": THREAD_B, "error": "service identity mismatch"}]}
        with patch.object(self.launcher.viewer, "stop_all", return_value=result):
            code, output, error, control, opened = self.invoke(["stop-all"])
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(error), result)
        self.assertEqual(output, "")
        control.assert_not_called()
        opened.assert_not_called()

    def test_native_dispatch_binds_thread_and_stable_browser_tab(self):
        code, output, error, control, opened = self.invoke(["start", "--open-codex"])
        self.assertEqual(code, 0, error)
        control.assert_called_once_with("start", THREAD_A)
        opened.assert_called_once()
        target = urlsplit(opened.call_args.args[0])
        self.assertEqual((target.scheme, target.netloc, target.path),
                         ("codex", "threads", "/" + THREAD_A))
        query = parse_qs(target.query)
        self.assertEqual(query["browserUrl"], [URL])
        self.assertEqual(query["browserTabId"], ["codex-trace-" + THREAD_A])
        self.assertEqual(json.loads(output)["url"], URL)

    def test_native_failure_falls_back_to_browser_without_stopping_service(self):
        for failure in (False, OSError("protocol handler unavailable")):
            with self.subTest(failure=failure), \
                 patch.object(self.launcher.webbrowser, "open", side_effect=[failure, True]) as opened, \
                 patch.object(self.launcher.viewer, "control", return_value={"url": URL,
                              "thread_id": THREAD_A}) as control, \
                 patch.object(sys, "stdout", new_callable=io.StringIO) as output, \
                 patch.object(sys, "stderr", new_callable=io.StringIO) as error:
                self.assertEqual(self.launcher.main(["start", "--open-codex"]), 0, error.getvalue())
                control.assert_called_once_with("start", THREAD_A)
                self.assertEqual(opened.call_count, 2)
                self.assertEqual(opened.call_args.args[0], URL)
                result = json.loads(output.getvalue())
                self.assertEqual(result["browser_target"], "browser")
                self.assertTrue(result["warning"])

    def test_failed_browser_dispatch_preserves_url_and_healthy_service(self):
        for args in (["start"], ["start", "--open-codex"]):
            with self.subTest(args=args):
                code, output, error, control, opened = self.invoke(args, browser=False)
                self.assertNotEqual(code, 0)
                control.assert_called_once_with("start", THREAD_A)
                result = json.loads(error)
                self.assertEqual(result["url"], URL)
                self.assertTrue(result["error"])
                self.assertNotIn("control_token", output + error)

    def test_native_identity_rejected_before_control_or_shortcut_creation(self):
        for command in ("start", "shortcuts"):
            for identity in ("other-thread", "../invalid"):
                with self.subTest(command=command, identity=identity):
                    code, output, error, control, opened = self.invoke(
                        [command, "--thread", identity, "--open-codex"])
                    self.assertNotEqual(code, 0)
                    self.assertTrue(json.loads(error)["error"])
                    control.assert_not_called()
                    opened.assert_not_called()
                    self.assertFalse(self.home.exists())

    def test_generated_shortcuts_are_bound_private_and_platform_specific(self):
        result = self.launcher.generate_shortcuts(THREAD_A, open_codex=True)
        self.assertEqual(result["thread_id"], THREAD_A)
        self.assertTrue(Path(result["actions_file"]).exists())
        self.assertFalse(result["actions_installed"])
        directory = Path(result["directory"])
        self.assertTrue(directory.is_relative_to(self.home))
        files = {Path(name).stem: Path(name) for name in result["files"]}
        self.assertEqual(set(files), {"start", "stop", "start-all", "stop-all"})
        suffix = ".cmd" if os.name == "nt" else ".command" if sys.platform == "darwin" else ".sh"
        for command, file in files.items():
            self.assertEqual(file.suffix, suffix)
            content = file.read_text(encoding="utf-8")
            self.assertIn(str(self.checkout / "trace.py"), content)
            self.assertIn(str(self.home), content)
            self.assertNotIn("control_token", content)
            if command.endswith("-all"):
                self.assertNotIn("--thread", content)
                self.assertNotIn("--open-codex", content)
            else:
                self.assertIn(THREAD_A, content)
            if os.name == "posix":
                self.assertTrue(file.stat().st_mode & stat.S_IXUSR)
                self.assertEqual(stat.S_IMODE(file.stat().st_mode) & 0o077, 0)
        self.assertIn("--open-codex", files["start"].read_text(encoding="utf-8"))

    def test_actions_install_is_bound_idempotent_and_preserves_other_profiles(self):
        environments = self.checkout / ".codex" / "environments"
        environments.mkdir(parents=True)
        existing = environments / "environment.toml"
        existing.write_text('name = "existing project profile"\n', encoding="utf-8")
        result = self.launcher.generate_shortcuts(THREAD_A, install_actions=True)
        target = Path(result["actions_file"])
        self.assertEqual(target, environments / ("codex-trace-" + THREAD_A + ".toml"))
        original = target.read_bytes()
        self.assertEqual(existing.read_text(encoding="utf-8"), 'name = "existing project profile"\n')
        if sys.version_info >= (3, 11):
            import tomllib
            config = tomllib.loads(original.decode("utf-8"))
            self.assertIn(THREAD_A, config["name"])
            actions = config["actions"]
            self.assertEqual(len(actions), 4)
            commands = [action["command"] for action in actions]
            if os.name == "nt":
                commands = [Path(name).read_text(encoding="utf-8") for name in result["files"]]
            for command in commands:
                self.assertIn(str(self.checkout / "trace.py").replace("%", "%%") if os.name == "nt"
                              else str(self.checkout / "trace.py"), command)
                self.assertIn(str(self.home).replace("%", "%%") if os.name == "nt"
                              else str(self.home), command)
            bound = [command for command in commands if "--thread" in command]
            self.assertEqual(len(bound), 2)
            self.assertTrue(all(THREAD_A in command for command in bound))
        repeated = self.launcher.generate_shortcuts(THREAD_A, install_actions=True)
        self.assertEqual(repeated["actions_file"], str(target))
        self.assertEqual(target.read_bytes(), original)
        target.write_text('name = "user edited profile"\n', encoding="utf-8")
        with self.assertRaises(self.launcher.viewer.ControllerError):
            self.launcher.generate_shortcuts(THREAD_A, install_actions=True)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "user edited profile"\n')

    def test_actions_install_preserves_concurrent_user_edits_after_precheck(self):
        target = self.checkout / ".codex" / "environments" / ("codex-trace-" + THREAD_A + ".toml")
        directory = self.home / "trace-viewer" / "shortcuts" / THREAD_A
        marker = "concurrent user edit\n"
        original_mkdir = Path.mkdir
        for already_installed in (False, True):
            with self.subTest(already_installed=already_installed):
                if target.exists():
                    target.unlink()
                if already_installed:
                    self.launcher.generate_shortcuts(THREAD_A, install_actions=True)

                def concurrent_mkdir(path, *args, **kwargs):
                    if path == directory:
                        original_mkdir(target.parent, parents=True, exist_ok=True)
                        target.write_text(marker, encoding="utf-8")
                    return original_mkdir(path, *args, **kwargs)

                failure = None
                with patch.object(Path, "mkdir", new=concurrent_mkdir):
                    try:
                        self.launcher.generate_shortcuts(THREAD_A, install_actions=True)
                    except self.launcher.viewer.ControllerError as error:
                        failure = error
                self.assertTrue(target.read_text(encoding="utf-8") == marker,
                                "Actions installation overwrote a concurrent user edit")
                self.assertIsInstance(failure, self.launcher.viewer.ControllerError)

    def test_windows_actions_and_batch_preserve_non_ascii_shell_sensitive_paths(self):
        special = Path(self.tmp.name).resolve() / "中文 路径%百分号!单引号'"
        checkout = copy_checkout(special)
        launcher = load_launcher(checkout)
        home = special / "数据 目录%保留!用户'"
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
             patch.object(launcher.sys, "platform", "win32"):
            result = launcher.generate_shortcuts(THREAD_A)
        files = {Path(name).stem: Path(name) for name in result["files"]}
        text = Path(result["actions_file"]).read_text(encoding="utf-8")
        if sys.version_info >= (3, 11):
            import tomllib
            commands = [action["command"] for action in tomllib.loads(text)["actions"]]
        else:
            commands = [json.loads(line.partition(" = ")[2]) for line in text.splitlines()
                        if line.startswith("command = ")]
        self.assertEqual(len(commands), 4)
        prefix = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
        for command, file in zip(commands, files.values()):
            self.assertTrue(command.startswith(prefix),
                            "Windows Actions must run from either CMD or PowerShell")
            encoded = command[len(prefix):]
            self.assertRegex(encoded, r"\A[A-Za-z0-9+/]+={0,2}\Z")
            decoded = base64.b64decode(encoded, validate=True).decode("utf-16-le")
            self.assertEqual(decoded, "& '" + str(file).replace("'", "''") + "'")
            content = file.read_text(encoding="utf-8")
            self.assertEqual(file.suffix, ".cmd")
            self.assertIn("setlocal DisableDelayedExpansion", content)
            self.assertIn("chcp 65001", content)
            self.assertLess(content.index("chcp 65001"), content.index("CODEX_HOME="))
            self.assertIn(str(home).replace("%", "%%"), content)
            self.assertIn(str(checkout / "trace.py").replace("%", "%%"), content)


class LauncherLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="trace shortcut lifecycle ")
        self.addCleanup(self.tmp.cleanup)
        self.checkout = copy_checkout(self.tmp.name)
        self.launcher = self.checkout / "trace.py"
        self.home = Path(self.tmp.name).resolve() / "synthetic codex home"
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True)
        for thread in (THREAD_A, THREAD_B):
            (sessions / ("rollout-2026-10-10T08-00-00-" + thread + ".jsonl")).write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n",
                encoding="utf-8")
        hooks = Path(self.tmp.name) / "test hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            "import webbrowser\nwebbrowser.open = lambda *args, **kwargs: True\n", encoding="utf-8")
        self.unrelated = Path(self.tmp.name) / "unrelated working directory"
        self.unrelated.mkdir()
        self.environment = dict(os.environ, CODEX_HOME=str(self.home), CODEX_THREAD_ID=THREAD_A,
                                PYTHONPATH=str(hooks), PYTHONDONTWRITEBYTECODE="1")
        self.addCleanup(self.stop_isolated_services)

    def call(self, *args, expected=0):
        process = subprocess.run([sys.executable, str(self.launcher), *args],
                                 cwd=self.unrelated, env=self.environment,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, expected, process.stderr)
        return json.loads(process.stdout if expected == 0 else process.stderr)

    def stop_isolated_services(self):
        process = subprocess.run([sys.executable, str(self.launcher), "stop-all"],
                                 cwd=self.unrelated, env=self.environment,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)

    def run_shortcut(self, file, environment):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(file)] if os.name == "nt" else [str(file)]
        process = subprocess.run(command, cwd=self.unrelated, env=environment,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_bound_shortcut_lifecycle_isolated_from_changed_identity_and_home(self):
        first = self.call("start", "--no-open")
        self.assertEqual(first["thread_id"], THREAD_A)
        self.assertNotIn("control_token", first)
        second = self.call("start", "--thread", THREAD_B, "--no-open")
        generated = self.call("shortcuts")
        files = {Path(name).stem: Path(name) for name in generated["files"]}
        stale_environment = dict(self.environment, CODEX_THREAD_ID=THREAD_B,
                                 CODEX_HOME=str(Path(self.tmp.name) / "wrong home"))
        self.assertTrue(self.run_shortcut(files["stop"], stale_environment)["stopped"])
        self.assertFalse(self.call("status")["running"])
        self.assertTrue(self.call("status", "--thread", THREAD_B)["running"])
        stale_environment.pop("CODEX_THREAD_ID")
        started = self.run_shortcut(files["start"], stale_environment)
        self.assertEqual(started["thread_id"], THREAD_A)
        self.assertNotEqual(first["instance_id"], started["instance_id"])
        reused = self.run_shortcut(files["start"], stale_environment)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["instance_id"], started["instance_id"])
        with urlopen(started["url"] + "/api/sessions", timeout=2) as response:
            data = json.load(response)
        self.assertEqual(data["bound_thread"], THREAD_A)
        self.assertEqual({item["thread_id"] for item in data["items"]}, {THREAD_A})
        with urlopen(second["url"] + "/api/health", timeout=2) as response:
            self.assertEqual(json.load(response)["thread_id"], THREAD_B)
        overview = self.run_shortcut(files["start-all"], stale_environment)
        self.assertIsNone(overview["thread_id"])
        with urlopen(overview["url"] + "/api/sessions", timeout=2) as response:
            data = json.load(response)
        self.assertIsNone(data["bound_thread"])
        self.assertEqual({item["thread_id"] for item in data["items"]}, {THREAD_A, THREAD_B})
        result = self.run_shortcut(files["stop-all"], stale_environment)
        self.assertEqual(result["errors"], [])
        self.assertEqual({item["thread_id"] for item in result["results"]}, {None, THREAD_A, THREAD_B})
        self.assertFalse(self.call("status")["running"])
        self.assertFalse(self.call("status", "--thread", THREAD_B)["running"])
        self.assertFalse((Path(self.tmp.name) / "wrong home").exists())


if __name__ == "__main__":
    unittest.main()
