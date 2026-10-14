import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]


def load_server(user_home, codex_home=None):
    expanduser = os.path.expanduser
    def expand(path):
        if path == "~":
            return str(user_home)
        if path.startswith("~/"):
            return str(user_home / path[2:])
        return expanduser(path)
    with patch.dict(os.environ), patch("os.path.expanduser", side_effect=expand):
        if codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = codex_home
        spec = importlib.util.spec_from_file_location("portable_server", PROJECT / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
    return server


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user_home = Path(self.tmp.name) / "different user"

    def assert_locations(self, server, expected):
        self.assertEqual(server.ROOTS, [str(expected / "sessions"), str(expected / "archived_sessions")])
        self.assertEqual(server.CACHE_PATH, str(expected / "trace-viewer-cache.json"))
        self.assertEqual(server.INDEX_PATH, str(expected / "session_index.jsonl"))
        self.assertEqual(server.STATE_PATH, str(expected / ".codex-global-state.json"))
        self.assertEqual(server.TOOLS_CACHE_DIR, str(expected / "cache" / "codex_apps_tools"))

    def test_default_locations_follow_current_user(self):
        self.assert_locations(load_server(self.user_home), self.user_home / ".codex")

    def test_codex_home_redirects_every_data_source(self):
        custom = Path(self.tmp.name) / "custom state with spaces"
        self.assert_locations(load_server(self.user_home, str(custom)), custom)

    def test_codex_home_expands_current_user_tilde(self):
        self.assert_locations(load_server(self.user_home, "~/other codex"), self.user_home / "other codex")

    def test_relative_codex_home_and_html_are_resolved_independently(self):
        previous = Path.cwd()
        try:
            os.chdir(self.tmp.name)
            expected = Path.cwd() / "relative state"
            server = load_server(self.user_home, "relative state")
        finally:
            os.chdir(previous)
        self.assert_locations(server, expected)
        self.assertEqual(server.INDEX, str(PROJECT / "index.html"))

    def test_launcher_has_no_machine_specific_python_executable(self):
        script = (PROJECT / "start.command").read_text(encoding="utf-8")
        self.assertNotRegex(script, r'(?m)^PY="/(?:Users|home)/')

    def test_system_assigned_port_is_reported_and_server_is_closed(self):
        server = load_server(self.user_home)
        srv = MagicMock(server_address=("127.0.0.1", 41234))
        srv.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(server, "ThreadingHTTPServer", return_value=srv), \
             patch.object(server.sys, "argv", ["server.py", "--port", "0"]), \
             patch.object(server.sys, "stdout", new_callable=io.StringIO) as out:
            server.main()
        self.assertIn("http://127.0.0.1:41234", out.getvalue())
        srv.server_close.assert_called_once()

    def test_invalid_cli_options_fail_before_binding(self):
        server = load_server(self.user_home)
        for args in (["--port", "-1"], ["--port", "65536"], ["--port", "abc"], ["--unknown"]):
            with self.subTest(args=args), patch.object(server, "ThreadingHTTPServer") as listener, \
                 patch.object(server.sys, "argv", ["server.py", *args]), \
                 patch.object(server.sys, "stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as failure:
                    server.main()
                self.assertEqual(failure.exception.code, 2)
                listener.assert_not_called()

    def test_custom_home_serves_sessions_metadata_schema_and_its_own_cache(self):
        custom = Path(self.tmp.name) / "custom state with spaces"
        (custom / "sessions").mkdir(parents=True)
        (custom / "archived_sessions").mkdir()
        (custom / "cache" / "codex_apps_tools").mkdir(parents=True)
        session = custom / "sessions" / "rollout-demo.jsonl"
        session.write_text(json.dumps({"type": "session_meta", "payload": {"id": "demo"}}) + "\n", encoding="utf-8")
        (custom / "archived_sessions" / "rollout-archived.jsonl").write_text("{}\n", encoding="utf-8")
        (custom / "session_index.jsonl").write_text(json.dumps({"id": "demo", "thread_name": "Portable demo"}) + "\n", encoding="utf-8")
        (custom / ".codex-global-state.json").write_text(json.dumps({"local-projects": {"p": {"name": "Demo"}},
            "thread-project-assignments": {"demo": {"projectId": "p"}}}), encoding="utf-8")
        (custom / "cache" / "codex_apps_tools" / "demo.json").write_text(json.dumps({"tools": [
            {"tool_name": "demo", "tool_namespace": "example", "tool": {"name": "example.demo"}}]}), encoding="utf-8")
        server = load_server(self.user_home, str(custom))
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(http.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(http.shutdown)
        base = "http://127.0.0.1:%d" % http.server_address[1]
        def get(route):
            with urlopen(base + route, timeout=3) as response:
                return response.read().decode("utf-8")
        data = json.loads(get("/api/sessions"))
        self.assertEqual(data["root"], server.ROOTS)
        self.assertEqual(data["count"], 2)
        item = next(item for item in data["items"] if item["thread_id"] == "demo")
        self.assertEqual((item["title"], item["project"]), ("Portable demo", "Demo"))
        self.assertEqual(get("/api/raw?" + urlencode({"path": str(session)})), session.read_text(encoding="utf-8"))
        schema = json.loads(get("/api/toolschema?name=demo&ns=example"))
        self.assertTrue(schema["found"])
        self.assertEqual(schema["source"], server.TOOLS_CACHE_DIR)
        self.assertTrue((custom / "trace-viewer-cache.json").exists())
        self.assertFalse(self.user_home.exists())
        with self.assertRaises(HTTPError) as denied:
            get("/api/raw?" + urlencode({"path": server.INDEX_PATH}))
        self.assertEqual(denied.exception.code, 403)

    @unittest.skipUnless(os.name == "posix" and shutil.which("zsh"), "macOS launcher requires POSIX zsh")
    def test_launcher_uses_path_python_from_a_checkout_with_spaces(self):
        checkout = Path(self.tmp.name) / "checkout with spaces"
        checkout.mkdir()
        shutil.copy(PROJECT / "start.command", checkout / "start.command")
        binaries = Path(self.tmp.name) / "bin with spaces"
        binaries.mkdir()
        fake_python = binaries / "python3"
        fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$CTV_LAUNCH_TRACE"\n', encoding="utf-8")
        fake_python.chmod(0o755)
        trace = Path(self.tmp.name) / "launch.txt"
        env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""), "CTV_LAUNCH_TRACE": str(trace)}
        subprocess.run([shutil.which("zsh"), str(checkout / "start.command"), "0"], cwd=self.tmp.name,
                       env=env, check=True, capture_output=True, text=True, timeout=3)
        values = trace.read_text(encoding="utf-8").splitlines()
        self.assertEqual(Path(values[0]).resolve(), checkout.resolve())
        self.assertEqual(values[1:], ["server.py", "--port", "0", "--open-browser"])

    def test_browser_failure_keeps_service_running_and_reports_actual_url(self):
        server = load_server(self.user_home)
        srv = MagicMock(server_address=("127.0.0.1", 41234))
        srv.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(server, "ThreadingHTTPServer", return_value=srv), \
             patch.object(server.sys, "argv", ["server.py", "--port", "0", "--open-browser"]), \
             patch.object(server.threading, "Timer") as timer, \
             patch.object(server.webbrowser, "open", return_value=False) as browser, \
             patch.object(server.sys, "stdout", new_callable=io.StringIO), \
             patch.object(server.sys, "stderr", new_callable=io.StringIO) as err:
            server.main()
            timer.call_args.args[1]()
        browser.assert_called_once_with("http://127.0.0.1:41234/")
        self.assertIn("请手动访问：http://127.0.0.1:41234/", err.getvalue())
        timer.return_value.cancel.assert_called_once()
        srv.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
