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
from urllib.error import URLError
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / ".agents" / "skills" / "codex-trace" / "scripts" / "viewer.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("trace_controller", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdentityTests(unittest.TestCase):
    def test_missing_thread_never_guesses_a_session(self):
        helper = load_helper()
        with patch.dict(os.environ), self.assertRaisesRegex(helper.ControllerError, "CODEX_THREAD_ID"):
            os.environ.pop("CODEX_THREAD_ID", None)
            helper.thread_id(None)

    def test_current_thread_or_explicit_thread_is_validated(self):
        helper = load_helper()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "current-thread"}):
            self.assertEqual(helper.thread_id(None), "current-thread")
            self.assertEqual(helper.thread_id("authorized-test"), "authorized-test")
        for value in ("../other", "", "a" * 129, "thread/other", "你好"):
            with self.subTest(value=value), self.assertRaises(helper.ControllerError):
                helper.thread_id(value)

    def test_leading_separator_is_rejected_before_starting_a_process(self):
        helper = load_helper()
        for value in ("_leading", "-leading"):
            with self.subTest(value=value), \
                 patch.object(helper.sys, "argv", ["viewer.py", "start", "--thread=" + value]), \
                 patch.object(helper.sys, "stderr", new_callable=io.StringIO) as error, \
                 patch.object(helper.subprocess, "Popen") as process, \
                 patch.object(helper, "state_dir") as state, \
                 patch.object(helper, "control", return_value={}) as control:
                self.assertEqual(helper.main(), 1)
                self.assertIn("CODEX_THREAD_ID", error.getvalue())
                process.assert_not_called()
                state.assert_not_called()
                control.assert_not_called()

    def test_source_is_located_from_helper_not_launch_directory(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory, patch("os.getcwd", return_value=directory):
            self.assertEqual(helper.PROJECT, PROJECT)
            self.assertEqual(len(helper.fingerprint()), 64)

    def test_registry_rejects_foreign_urls_and_identity(self):
        helper = load_helper()
        record = {"protocol": 1, "thread_id": "current-thread", "url": "http://127.0.0.1:54321",
                  "instance_id": "a" * 32, "control_token": "b" * 64, "fingerprint": "c" * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            for key, value in (("url", "https://example.com"), ("url", "http://127.0.0.1:4/path"),
                               ("thread_id", "other-thread"), ("control_token", "invalid")):
                path.write_text(json.dumps({**record, key: value}), encoding="utf-8")
                with self.subTest(key=key, value=value), self.assertRaises(helper.ControllerError):
                    helper.load_registry(path, "current-thread")

    def test_stop_all_continues_after_malformed_registry_field_types(self):
        helper = load_helper()
        record = {"protocol": 1, "thread_id": "a-broken", "url": "http://127.0.0.1:54321",
                  "instance_id": "a" * 32, "control_token": "b" * 64, "fingerprint": "c" * 64}
        malformed = [[], {**record, "url": 42}, {**record, "instance_id": []},
                     {**record, "control_token": None}, {**record, "fingerprint": {}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z-healthy.json").write_text("{}", encoding="utf-8")
            bad = root / "a-broken.json"
            for value in malformed:
                bad.write_text(json.dumps(value), encoding="utf-8")
                def stop(command, thread):
                    if thread == "a-broken":
                        return helper.load_registry(bad, thread)
                    return {"thread_id": thread, "stopped": True}
                with self.subTest(value=value), \
                     patch.object(helper, "state_dir", return_value=root), \
                     patch.object(helper, "control", side_effect=stop), \
                     patch.object(helper, "request_json") as network:
                    result = helper.stop_all()
                    self.assertEqual(result["results"], [{"thread_id": "z-healthy", "stopped": True}])
                    self.assertEqual(result["errors"][0]["service"], "a-broken")
                    self.assertTrue(bad.exists())
                    network.assert_not_called()

    def test_health_mismatch_is_not_considered_owned(self):
        helper = load_helper()
        record = {"thread_id": "current-thread", "url": "http://127.0.0.1:54321", "instance_id": "a" * 32}
        with patch.object(helper, "request_json", return_value={"app": "other-app"}), \
             self.assertRaisesRegex(helper.ControllerError, "身份"):
            helper.health(record)

    def test_only_connection_refusal_is_stale_not_timeout(self):
        helper = load_helper()
        with patch.object(helper, "request_json", side_effect=URLError(ConnectionRefusedError())):
            self.assertIsNone(helper.health({"url": "http://127.0.0.1:54321"}))
        with patch.object(helper, "request_json", side_effect=URLError(TimeoutError())), \
             self.assertRaisesRegex(helper.ControllerError, "不可确认"):
            helper.health({"url": "http://127.0.0.1:54321"})

    def test_stop_waits_through_connection_reset_after_authenticated_shutdown(self):
        helper = load_helper()
        record = {"protocol": 1, "thread_id": "current-thread", "url": "http://127.0.0.1:54321",
                  "instance_id": "a" * 32, "control_token": "b" * 64, "fingerprint": "c" * 64}
        healthy = {"app": "codex-trace-viewer", "protocol": 1, "thread_id": "current-thread", "instance_id": "a" * 32}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current-thread.json"
            helper.save_registry(path, record)
            with patch.object(helper, "state_dir", return_value=Path(directory)), \
                 patch.object(helper, "request_json", side_effect=[healthy, {"ok": True},
                              URLError(ConnectionResetError()), URLError(ConnectionRefusedError())]):
                self.assertTrue(helper.control("stop", "current-thread")["stopped"])
            self.assertFalse(path.exists())

    def test_start_all_uses_overview_without_current_thread(self):
        helper = load_helper()
        with patch.dict(os.environ), \
             patch.object(helper.sys, "argv", ["viewer.py", "start-all"]), \
             patch.object(helper.sys, "stdout", new_callable=io.StringIO), \
             patch.object(helper, "control", return_value={"thread_id": None}) as control:
            os.environ.pop("CODEX_THREAD_ID", None)
            self.assertEqual(helper.main(), 0)
            control.assert_called_once_with("start", None)

    def test_all_commands_reject_thread_override_before_mutation(self):
        helper = load_helper()
        for command in ("start-all", "stop-all"):
            with self.subTest(command=command), \
                 patch.object(helper.sys, "argv", ["viewer.py", command, "--thread", "other"]), \
                 patch.object(helper.sys, "stderr", new_callable=io.StringIO), \
                 patch.object(helper, "state_dir") as state, self.assertRaises(SystemExit) as error:
                helper.main()
            self.assertEqual(error.exception.code, 2)
            state.assert_not_called()

    def test_stop_all_reports_failures_and_continues_other_registered_services(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("_all", "thread-a", "thread-b", "_invalid"):
                (root / (name + ".json")).write_text("{}", encoding="utf-8")
            (root / "unregistered.log").write_text("", encoding="utf-8")
            def stop(command, thread):
                self.assertEqual(command, "stop")
                if thread == "thread-a":
                    raise helper.ControllerError("服务身份不匹配")
                return {"thread_id": thread, "stopped": True}
            with patch.object(helper, "state_dir", return_value=root), \
                 patch.object(helper, "control", side_effect=stop) as control:
                result = helper.stop_all()
            self.assertEqual({r["thread_id"] for r in result["results"]}, {None, "thread-b"})
            self.assertEqual({r["service"] for r in result["errors"]}, {"thread-a", "_invalid"})
            self.assertEqual(control.call_count, 3)

    def test_partial_stop_all_failure_exits_nonzero_with_structured_results(self):
        helper = load_helper()
        result = {"results": [{"thread_id": "thread-a", "stopped": True}],
                  "errors": [{"service": "broken", "error": "登记损坏"}]}
        with patch.object(helper.sys, "argv", ["viewer.py", "stop-all"]), \
             patch.object(helper, "stop_all", return_value=result), \
             patch.object(helper.sys, "stderr", new_callable=io.StringIO) as error:
            self.assertEqual(helper.main(), 1)
            self.assertEqual(json.loads(error.getvalue()), result)

    def test_stop_all_rejects_registry_symlink_without_reading_target(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "thread-a.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(helper, "state_dir", return_value=root), \
                 patch.object(Path, "is_symlink", return_value=True), \
                 patch.object(helper, "control") as control:
                result = helper.stop_all()
            control.assert_not_called()
            self.assertEqual(result["results"], [])
            self.assertEqual(result["errors"][0]["service"], "thread-a")
            self.assertTrue(path.exists())

    def test_stop_all_snapshots_keys_and_processes_current_instance_for_each_key(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a-first", "z-last"):
                (root / (name + ".json")).write_text('{"instance": "old"}', encoding="utf-8")
            def stop(command, thread):
                if thread == "a-first":
                    (root / "z-last.json").write_text('{"instance": "new"}', encoding="utf-8")
                    (root / "new-key.json").write_text('{}', encoding="utf-8")
                instance = json.loads((root / (thread + ".json")).read_text())["instance"]
                return {"thread_id": thread, "stopped": True, "instance": instance}
            with patch.object(helper, "state_dir", return_value=root), \
                 patch.object(helper, "control", side_effect=stop):
                result = helper.stop_all()
            self.assertEqual([r["thread_id"] for r in result["results"]], ["a-first", "z-last"])
            self.assertEqual(result["results"][1]["instance"], "new")
            self.assertEqual(result["errors"], [])
            self.assertTrue((root / "new-key.json").exists())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="trace skill ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "state with spaces"
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True)
        for thread in ("thread-a", "thread-b"):
            (sessions / ("rollout-2026-10-10T08-00-00-" + thread + ".jsonl")).write_text(
                json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n", encoding="utf-8")
        self.environment = dict(os.environ, CODEX_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        self.environment.pop("CODEX_THREAD_ID", None)
        self.helper = HELPER
        self.addCleanup(self.stop_started_threads)

    def call(self, command, thread=None, expected=0):
        args = [sys.executable, str(self.helper), command]
        if thread is not None:
            args += ["--thread", thread]
        result = subprocess.run(args, cwd=self.tmp.name, env=self.environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def stop_started_threads(self):
        for thread in ("thread-a", "thread-b"):
            result = subprocess.run([sys.executable, str(self.helper), "stop", "--thread", thread],
                                    cwd=self.tmp.name, env=self.environment,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, str(self.helper), "stop-all"],
                                cwd=self.tmp.name, env=self.environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_requires_current_thread_before_creating_control_state(self):
        self.assertIn("CODEX_THREAD_ID", self.call("start", expected=1)["error"])
        self.assertFalse((self.home / "trace-viewer").exists())

    def test_start_reuse_and_stop_are_isolated_between_threads(self):
        first = self.call("start", "thread-a")
        again = self.call("start", "thread-a")
        second = self.call("start", "thread-b")
        self.assertFalse(first["reused"])
        self.assertTrue(again["reused"])
        self.assertEqual(first["url"], again["url"])
        self.assertEqual(first["instance_id"], again["instance_id"])
        self.assertNotEqual(first["url"], second["url"])
        self.assertTrue(self.call("status", "thread-a")["running"])
        self.assertTrue(self.call("stop", "thread-a")["stopped"])
        self.assertFalse(self.call("status", "thread-a")["running"])
        self.assertTrue(self.call("status", "thread-b")["running"])

    def test_environment_identity_private_state_and_no_token_output(self):
        self.environment["CODEX_THREAD_ID"] = "thread-a"
        result = self.call("start")
        self.assertEqual(result["thread_id"], "thread-a")
        self.assertNotIn("control_token", result)
        registry = self.home / "trace-viewer" / "thread-a.json"
        record = json.loads(registry.read_text(encoding="utf-8"))
        self.assertNotIn(record["control_token"], json.dumps(result))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o600)
            for log in registry.parent.glob("*.log"):
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_helper_source_locator_and_version_guard_in_checkout_with_spaces(self):
        checkout = Path(self.tmp.name) / "source checkout with spaces"
        scripts = checkout / ".agents" / "skills" / "codex-trace" / "scripts"
        scripts.mkdir(parents=True)
        self.helper = scripts / "viewer.py"
        shutil.copyfile(HELPER, self.helper)
        for name in ("server.py", "index.html"):
            shutil.copyfile(PROJECT / name, checkout / name)
        first = self.call("start", "thread-a")
        with (checkout / "index.html").open("a", encoding="utf-8") as changed:
            changed.write("\n<!-- changed source for isolated test -->\n")
        self.assertIn("源码已更新", self.call("start", "thread-a", expected=1)["error"])
        self.assertTrue(self.call("stop", "thread-a")["stopped"])
        updated = self.call("start", "thread-a")
        self.assertNotEqual(first["instance_id"], updated["instance_id"])

    def test_concurrent_starts_reuse_one_service(self):
        args = [sys.executable, str(self.helper), "start", "--thread", "thread-a"]
        children = [subprocess.Popen(args, cwd=self.tmp.name, env=self.environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        records = []
        for child in children:
            out, error = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, error)
            records.append(json.loads(out))
        self.assertEqual(records[0]["instance_id"], records[1]["instance_id"])
        self.assertEqual(sorted(record["reused"] for record in records), [False, True])

    def test_overview_start_reuses_one_service_and_lists_all_synthetic_threads(self):
        first = self.call("start-all")
        again = self.call("start-all")
        self.assertIsNone(first["thread_id"])
        self.assertFalse(first["reused"])
        self.assertTrue(again["reused"])
        self.assertEqual(first["instance_id"], again["instance_id"])
        with urlopen(first["url"] + "/api/sessions", timeout=2) as response:
            data = json.load(response)
        self.assertIsNone(data["bound_thread"])
        self.assertEqual({row["thread_id"] for row in data["items"]}, {"thread-a", "thread-b"})
        self.assertNotIn("control_token", first)

    def test_stop_all_stops_overview_and_independent_services(self):
        self.call("start", "thread-a")
        self.call("start", "thread-b")
        self.call("start-all")
        result = self.call("stop-all")
        self.assertEqual(result["errors"], [])
        self.assertEqual({r["thread_id"] for r in result["results"]}, {None, "thread-a", "thread-b"})
        self.assertTrue(all(r["stopped"] for r in result["results"]))
        self.assertFalse(self.call("status", "thread-a")["running"])
        self.assertFalse(self.call("status", "thread-b")["running"])
        self.assertEqual(list((self.home / "trace-viewer").glob("*.json")), [])
        self.assertEqual(self.call("stop-all"), {"results": [], "errors": []})

    def test_current_thread_stop_leaves_overview_running(self):
        overview = self.call("start-all")
        self.environment["CODEX_THREAD_ID"] = "thread-a"
        current = self.call("start")
        self.assertNotEqual(current["instance_id"], overview["instance_id"])
        self.assertTrue(self.call("stop")["stopped"])
        with urlopen(overview["url"] + "/api/health", timeout=2) as response:
            health = json.load(response)
        self.assertIsNone(health["thread_id"])
        self.assertEqual(health["instance_id"], overview["instance_id"])

    def test_stop_all_keeps_broken_registry_and_stops_valid_services(self):
        self.call("start", "thread-a")
        self.call("start-all")
        broken = self.home / "trace-viewer" / "broken.json"
        broken.write_text("not JSON", encoding="utf-8")
        self.addCleanup(broken.unlink)
        result = self.call("stop-all", expected=1)
        self.assertEqual({r["thread_id"] for r in result["results"]}, {None, "thread-a"})
        self.assertEqual(result["errors"][0]["service"], "broken")
        self.assertTrue(broken.exists())
        self.assertFalse(self.call("status", "thread-a")["running"])

    def test_concurrent_overview_starts_reuse_one_service(self):
        args = [sys.executable, str(self.helper), "start-all"]
        children = [subprocess.Popen(args, cwd=self.tmp.name, env=self.environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        records = []
        for child in children:
            out, error = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, error)
            records.append(json.loads(out))
        self.assertEqual(records[0]["instance_id"], records[1]["instance_id"])
        self.assertEqual(sorted(record["reused"] for record in records), [False, True])


if __name__ == "__main__":
    unittest.main()
