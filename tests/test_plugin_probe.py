import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trace_identity_probe", PROJECT / "plugins/codex-trace-probe/server.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
A = "11111111-2222-4333-8444-555555555555"
B = "66666666-7777-4888-8999-aaaaaaaaaaaa"


class IdentityProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="trace probe ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "CODEX_THREAD_ID": B})
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.home / "sessions").mkdir()
        self.project = self.home / "non git project"
        self.project.mkdir()

    def save(self, thread, name=None):
        path = self.home / "sessions" / (name or ("rollout-" + thread + ".jsonl"))
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": str(self.project)}}) + "\n" + "private message that must not be parsed")
        return path

    def test_flat_metadata_matches_header_and_does_not_use_environment_identity(self):
        self.save(A)
        result, diagnostics = probe.inspect_identity({"_meta": {"thread_id": A, "threadId": A}})
        self.assertEqual(result["thread_id"], A)
        self.assertEqual(result["identity_status"], "verified")
        self.assertFalse(result["git_repository"])
        self.assertTrue(diagnostics["environment_thread_id_present"])
        self.assertNotIn(B, json.dumps([result, diagnostics]))

    def test_nested_metadata_accepts_object_and_encoded_object(self):
        self.save(A)
        for metadata in ({"thread_id": A}, json.dumps({"thread_id": A})):
            result, _ = probe.inspect_identity({"_meta": {"x-codex-turn-metadata": metadata}})
            self.assertEqual(result["thread_id"], A)

    def test_conflicting_or_invalid_aliases_fail_closed(self):
        self.save(A)
        for metadata in ({"thread_id": A, "threadId": B}, {"thread_id": A, "threadId": 3}):
            result, _ = probe.inspect_identity({"_meta": metadata})
            self.assertIsNone(result["thread_id"])
            self.assertEqual(result["identity_status"], "conflict")

    def test_missing_metadata_never_borrows_anonymous_or_environment_values(self):
        self.save(B)
        result, diagnostics = probe.inspect_identity({"arguments": {"thread_id": A}, "_meta": {"openai/session": B, "authorization": "secret"}})
        self.assertEqual(result["identity_status"], "missing")
        self.assertTrue(diagnostics["anonymous_session_present"])
        self.assertNotIn("secret", json.dumps([result, diagnostics]))

    def test_duplicate_and_symlinked_records_cannot_validate_identity(self):
        first = self.save(A)
        self.save(A, "duplicate-" + A + ".jsonl")
        result, _ = probe.inspect_identity({"_meta": {"thread_id": A}})
        self.assertEqual(result["identity_status"], "conflict")
        first.unlink()
        (self.home / "sessions" / ("duplicate-" + A + ".jsonl")).unlink()
        outside = self.home / ("outside-" + A + ".jsonl")
        outside.write_text(json.dumps({"type": "session_meta", "payload": {"id": A}}))
        first.symlink_to(outside)
        result, _ = probe.inspect_identity({"_meta": {"thread_id": A}})
        self.assertEqual(result["identity_status"], "candidate")

    def test_non_object_metadata_line_is_an_explicit_unverified_result(self):
        path = self.save(A)
        path.write_text("[]\n")
        result, _ = probe.inspect_identity({"_meta": {"thread_id": A}})
        self.assertEqual(result["identity_status"], "candidate")
        self.assertTrue(result["warnings"])

    def test_two_calls_keep_identity_and_nonce_independent_in_shared_server(self):
        self.save(A)
        self.save(B)
        server = probe.ProbeServer()
        results = []
        for index, thread in enumerate((A, B, A), 1):
            request = {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                "name": "trace_view", "arguments": {"view_id": thread, "request_nonce": A},
                "_meta": {"thread_id": thread}}}
            reply = server.dispatch(request)["result"]["structuredContent"]
            self.assertEqual(reply["thread_id"], thread)
            self.assertEqual(reply["view_id"], thread)
            self.assertEqual(reply["request_nonce"], A)
            self.assertEqual(reply["request_id"], index)
            results.append(reply)
        self.assertEqual(len({value["probe_id"] for value in results}), 3)
        self.assertEqual(len({value["server_instance_id"] for value in results}), 1)

    def test_input_identity_and_invalid_nonce_are_rejected(self):
        server = probe.ProbeServer()
        for arguments in ({"thread_id": A}, {"view_id": "invalid"}, []):
            response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "trace_view", "arguments": arguments, "_meta": {"thread_id": A}}})
            self.assertEqual(response["error"]["code"], -32602)

    def test_only_trace_page_is_discoverable_and_retired_probe_is_rejected(self):
        server = probe.ProbeServer()
        self.addCleanup(server.traces.close)
        tools = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        entries = [tool for tool in tools if tool.get("_meta", {}).get("openai/ui", {}).get("entrypoints")]
        self.assertEqual([tool["name"] for tool in entries], ["trace_view"])
        self.assertNotIn("trace_probe", [tool["name"] for tool in tools])
        resources = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["result"]["resources"]
        self.assertEqual([resource["uri"] for resource in resources], [probe.TRACE_RESOURCE])
        retired = server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "trace_probe", "arguments": {"view_id": A, "request_nonce": B}, "_meta": {"thread_id": A}}})
        self.assertEqual(retired["error"]["code"], -32602)
        retired = server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {
            "uri": "ui://codex-trace-probe/identity-v1.html"}})
        self.assertEqual(retired["error"]["code"], -32602)

    def test_stdio_negotiation_notifications_errors_and_tools(self):
        requests = [b"invalid json\n", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}).encode()+b"\n",
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
            b'{"jsonrpc":"2.0","id":3,"method":"not/a/method"}\n']
        process = subprocess.run([sys.executable, "-B", str(PROJECT / "plugins/codex-trace-probe/server.py")],
            input=b"".join(requests), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)
        output = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(output), 4)
        self.assertEqual(output[0]["error"]["code"], -32700)
        self.assertEqual(output[1]["result"]["protocolVersion"], "2025-03-26")
        tool = output[2]["result"]["tools"][0]
        self.assertEqual(tool["_meta"]["openai/ui"]["entrypoints"], [{"type": "thread"}])
        self.assertEqual(tool["_meta"]["ui"]["visibility"], ["app"])
        self.assertEqual(output[3]["error"]["code"], -32601)

    def test_malformed_present_nested_identity_cannot_be_discarded(self):
        self.save(A)
        for nested in ("not JSON", "[]", 1, "x" * 8193, "[" * 2000 + "]" * 2000):
            result, _ = probe.inspect_identity({"_meta": {"thread_id": A, "x-codex-turn-metadata": nested}})
            self.assertEqual(result["identity_status"], "conflict")
            self.assertIsNone(result["thread_id"])

    def test_link_swap_after_containment_check_cannot_match_outside_record(self):
        path = self.save(A)
        outside = self.home / "outside.jsonl"
        outside.write_text(path.read_text())
        original = Path.resolve
        swapped = []
        def resolve(value, *args, **kwargs):
            resolved = original(value, *args, **kwargs)
            if value == path and not swapped:
                path.unlink()
                path.symlink_to(outside)
                swapped.append(True)
            return resolved
        with patch.object(Path, "resolve", resolve):
            result, _ = probe.inspect_identity({"_meta": {"thread_id": A}})
        self.assertTrue(swapped)
        self.assertEqual(result["identity_status"], "candidate")

    def test_deep_record_is_explicitly_unverified(self):
        path = self.save(A)
        path.write_text("[" * 10000 + "]" * 10000 + "\n")
        result, _ = probe.inspect_identity({"_meta": {"thread_id": A}})
        self.assertEqual(result["identity_status"], "candidate")
        self.assertTrue(result["warnings"])

    def test_deep_or_oversized_stdio_input_does_not_kill_next_request(self):
        for invalid in (b"[" * 10000 + b"]" * 10000, b"x" * (probe.MAX_MESSAGE+1)):
            process = subprocess.run([sys.executable, "-B", str(PROJECT / "plugins/codex-trace-probe/server.py")],
                input=invalid+b'\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr)
            output = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual(len(output), 2)
            self.assertIn("error", output[0])
            self.assertEqual(output[1], {"jsonrpc":"2.0","id":7,"result":{}})

    def test_rpc_ids_reject_containers_booleans_and_nonfinite_numbers(self):
        server = probe.ProbeServer()
        for identity in ([], {}, True, float('nan'), float('inf'), 10 ** 600):
            result = server.dispatch({"jsonrpc":"2.0","id":identity,"method":"ping"})
            self.assertEqual(result["error"]["code"], -32600)
            self.assertIsNone(result["id"])

    @unittest.skipUnless(os.name == "posix", "Requires POSIX directory descriptors")
    def test_replaced_ancestor_of_data_root_is_rejected_before_reading(self):
        home = self.home / "nested-home"
        root = home / "sessions"
        root.mkdir(parents=True)
        path = root / ("rollout-" + A + ".jsonl")
        content = json.dumps({"type": "session_meta", "payload": {"id": A}}) + "\n"
        path.write_text(content)
        alternative = self.home / "alternative"
        (alternative / "sessions").mkdir(parents=True)
        (alternative / "sessions" / path.name).write_text(content)
        original = os.open
        opened = []
        def replaced(name, *args, **kwargs):
            if not opened:
                opened.append(True)
                home.rename(self.home / "saved-home")
                home.symlink_to(alternative, target_is_directory=True)
            return original(name, *args, **kwargs)
        with patch.object(probe.os, "open", side_effect=replaced):
            with self.assertRaises((OSError, ValueError)):
                probe.read_metadata(root, path)
        self.assertTrue(opened)


if __name__ == "__main__":
    unittest.main()
