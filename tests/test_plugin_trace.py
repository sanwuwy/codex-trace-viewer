import base64
from contextlib import contextmanager
import errno
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("plugin_trace_server", PROJECT / "plugins/codex-trace-probe/server.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
A = "11111111-2222-4333-8444-555555555555"
B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
V = "bbbbbbbb-1111-4222-8333-444444444444"
W = "cccccccc-1111-4222-8333-444444444444"


class PluginTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="plugin trace ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "CODEX_THREAD_ID": B})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = plugin.TraceStore()
        self.server = plugin.ProbeServer()
        self.addCleanup(self.store.close)
        self.addCleanup(self.server.traces.close)

    def snapshot_bytes(self, snapshot, thread=A, view=V):
        return b"".join(base64.b64decode(self.store.chunk(thread, view, snapshot["snapshot_id"], index)["data"])
            for index in range(snapshot["total_chunks"]))

    def call(self, tool_name, thread=A, view=V, **arguments):
        return self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": tool_name, "arguments": {"view_id": view, "request_nonce": W, **arguments},
            "_meta": {"thread_id": thread, "threadId": thread}}})

    def save(self, thread=A, folder="sessions", text="hello"):
        root = self.home / folder
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("rollout-" + thread + ".jsonl")
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": str(self.home)}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": text}]}}, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def test_release_closes_only_owned_page_and_is_idempotent(self):
        self.save(A)
        self.save(B)
        self.store.snapshot(A, V, None)
        self.store.snapshot(B, W, None)
        released = self.store.views[V]["snapshot_file"]
        retained = self.store.views[W]["snapshot_file"]
        self.assertTrue(self.store.release(A, V))
        self.assertFalse(self.store.release(A, V))
        self.assertTrue(released.closed)
        self.assertFalse(retained.closed)
        self.assertNotIn(V, self.store.views)
        self.assertEqual(set(self.store.views), {W})
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, None)
        with self.assertRaises(ValueError):
            self.store.view(A, V)
        self.assertNotIn(V, self.store.views)

    def test_release_rejects_live_wrong_owner_without_refreshing_lease(self):
        self.save()
        self.store.snapshot(A, V, None)
        before = self.store.views[V]["seen"]
        frozen = self.store.views[V]["snapshot_file"]
        with self.assertRaises(ValueError):
            self.store.release(B, V)
        self.assertEqual(self.store.views[V]["seen"], before)
        self.assertFalse(frozen.closed)
        self.assertFalse(self.store.release(A, W))
        self.assertNotIn(W, self.store.views)

    def test_absent_release_blocks_late_request_without_extending_guard(self):
        self.save()
        self.assertFalse(self.store.release(A, V))
        first_guard = self.store.released[V]
        with patch.object(plugin.time, "monotonic", return_value=time.monotonic() + 20):
            self.assertFalse(self.store.release(A, V))
        self.assertEqual(self.store.released[V], first_guard)
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, None)
        with self.assertRaises(ValueError):
            self.store.release(B, V)
        self.assertEqual(self.store.views, {})

    def test_guard_capacity_reserves_active_page_release_and_expires(self):
        self.save()
        self.store.snapshot(A, V, None)
        with patch.object(plugin, "MAX_RELEASE_GUARDS", 2):
            self.assertFalse(self.store.release(A, W))
            with self.assertRaisesRegex(ValueError, "lifecycle limit"):
                self.store.view(A, A)
            with self.assertRaisesRegex(ValueError, "lifecycle limit"):
                self.store.release(A, A)
            self.assertTrue(self.store.release(A, V))
            self.assertEqual(len(self.store.released), 2)
            future = time.monotonic() + 301
            with patch.object(plugin.time, "monotonic", return_value=future):
                self.store.sweep()
                self.store.view(A, A)
            self.assertEqual(self.store.released, {})

    def test_long_active_copy_renews_idle_time_at_completion(self):
        self.save()
        self.store.locate(A, self.store.view(A, V))
        now = time.monotonic()
        original = plugin.open_record
        @contextmanager
        def long_copy(*arguments):
            with original(*arguments) as stream:
                with patch.object(plugin.time, "monotonic", return_value=now + 301):
                    yield stream
        with patch.object(plugin.time, "monotonic", side_effect=[now, now, now + 301, now + 301]), \
                patch.object(plugin, "open_record", side_effect=long_copy):
            self.store.snapshot(A, V, None)
        frozen = self.store.views[V]["snapshot_file"]
        with patch.object(plugin.time, "monotonic", return_value=now + 301):
            self.store.sweep()
        self.assertFalse(frozen.closed)
        self.assertIn(V, self.store.views)

    def test_shutdown_waits_for_active_copy_and_cannot_resurrect_store(self):
        self.save()
        entered, proceed = threading.Event(), threading.Event()
        original = plugin.open_record
        failures = []
        @contextmanager
        def paused(*arguments):
            with original(*arguments) as stream:
                entered.set()
                if not proceed.wait(2):
                    raise RuntimeError("test close barrier timed out")
                yield stream
        def copy():
            try:
                self.store.snapshot(A, V, None)
            except Exception as error:
                failures.append(error)
        with patch.object(plugin, "open_record", side_effect=paused):
            copier = threading.Thread(target=copy)
            copier.start()
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=self.store.close)
            closer.start()
            self.assertTrue(closer.is_alive())
            proceed.set()
            copier.join(2)
            closer.join(2)
        self.assertEqual(failures, [])
        self.assertFalse(closer.is_alive())
        self.assertEqual(self.store.views, {})
        self.assertFalse(self.store.release(A, V))
        self.assertEqual(self.store.released, {})
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, None)

    def test_rpc_release_is_app_only_and_checks_each_request_identity(self):
        self.save()
        self.call("trace_snapshot")
        frozen = self.server.traces.views[V]["snapshot_file"]
        self.assertIn("error", self.call("trace_release", thread=B))
        result = self.call("trace_release")["result"]["structuredContent"]
        self.assertIs(result["released"], True)
        self.assertTrue(frozen.closed)
        self.assertIs(self.call("trace_release")["result"]["structuredContent"]["released"], False)
        self.assertIn("error", self.call("trace_snapshot"))
        self.assertIn("error", self.call("trace_schema", name="exec"))
        self.assertEqual(self.call("trace_release", thread_id=A)["error"]["code"], -32602)
        tools = self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        release = next(tool for tool in tools if tool["name"] == "trace_release")
        self.assertEqual(release["_meta"]["ui"]["visibility"], ["app"])
        self.assertNotIn("resourceUri", release["_meta"]["ui"])

    def test_timer_reaps_idle_page_without_requests_and_preserves_active_page(self):
        self.save(A)
        self.save(B)
        self.store.snapshot(A, V, None)
        self.store.snapshot(B, W, None)
        idle = self.store.views[V]["snapshot_file"]
        active = self.store.views[W]["snapshot_file"]
        self.store.views[V]["seen"] = time.monotonic() - 31
        with patch.object(plugin, "SWEEP_INTERVAL", 0.01):
            self.store.start()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with self.store.lock:
                    if idle.closed:
                        break
                time.sleep(0.01)
        self.assertTrue(idle.closed)
        self.assertFalse(active.closed)
        self.assertEqual(set(self.store.views), {W})
        worker = self.store.worker
        self.store.close()
        self.store.close()
        self.assertFalse(worker.is_alive())
        self.assertTrue(active.closed)
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, None)

    def test_short_lease_closes_all_idle_files_and_fds_without_touching_renewed_page(self):
        self.save(A)
        self.save(B)
        self.store.snapshot(A, V, None)
        self.store.snapshot(B, W, None)
        files = {}
        for view_id in (V, W):
            view = self.store.views[view_id]
            view["outline_file"] = tempfile.TemporaryFile(mode="w+b")
            view["detail_file"] = tempfile.TemporaryFile(mode="w+b")
            files[view_id] = [view[key] for key in ("snapshot_file", "outline_file", "detail_file")]
        idle_fds = [stream.fileno() for stream in files[V]]
        worker = self.store.worker
        now = time.monotonic()
        with self.store.lock:
            self.store.views[V]["seen"] = now
            self.store.views[W]["seen"] = now
            with patch.object(plugin.time, "monotonic", return_value=now + 20):
                self.store.view(B, W)
            with patch.object(plugin.time, "monotonic", return_value=now + 29.999):
                self.store.sweep()
            self.assertEqual(set(self.store.views), {V, W})
            with patch.object(plugin.time, "monotonic", return_value=now + 30.001):
                self.store.sweep()
            self.assertEqual(set(self.store.views), {W})
            self.assertTrue(all(stream.closed for stream in files[V]))
            self.assertTrue(all(not stream.closed for stream in files[W]))
            for descriptor in idle_fds:
                with self.assertRaises(OSError) as raised:
                    os.fstat(descriptor)
                self.assertEqual(raised.exception.errno, errno.EBADF)
            with patch.object(plugin.time, "monotonic", return_value=now + 50.001):
                self.store.sweep()
        worker.join(1)
        self.assertTrue(all(stream.closed for stream in files[W]))
        self.assertEqual(self.store.views, {})
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.store.worker)

    def test_release_waits_for_copy_then_blocks_old_snapshot_requests(self):
        self.save(text="large" * 10000)
        entered, proceed = threading.Event(), threading.Event()
        original = plugin.open_record
        failures = []
        @contextmanager
        def paused(*arguments):
            with original(*arguments) as stream:
                entered.set()
                if not proceed.wait(2):
                    raise RuntimeError("test copy barrier timed out")
                yield stream
        def copy():
            try:
                self.store.snapshot(A, V, None)
            except Exception as error:
                failures.append(error)
        with patch.object(plugin, "open_record", side_effect=paused):
            copier = threading.Thread(target=copy)
            copier.start()
            self.assertTrue(entered.wait(1))
            releaser = threading.Thread(target=lambda: self.store.release(A, V))
            releaser.start()
            self.assertTrue(releaser.is_alive())
            proceed.set()
            copier.join(2)
            releaser.join(2)
        self.assertEqual(failures, [])
        self.assertFalse(copier.is_alive())
        self.assertFalse(releaser.is_alive())
        self.assertNotIn(V, self.store.views)
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, None)

    def test_absent_data_directories_wait_without_falling_back(self):
        result = self.store.snapshot(A, V, None)
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(result["snapshot_match"])
        self.assertIs(result["truncated"], False)

    def test_exact_snapshot_roundtrips_unicode_and_unchanged_skips_reads(self):
        path = self.save(text="中文🙂" * 35000)
        first = self.store.snapshot(A, V, None)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(self.snapshot_bytes(first), path.read_bytes())
        with patch.object(plugin, "open_record", side_effect=AssertionError("unchanged body read")), \
                patch.object(plugin.os, "walk", side_effect=AssertionError("unchanged directory scan")):
            same = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(same["status"], "unchanged")
        self.assertEqual(same["total_chunks"], 0)
        self.assertEqual(same["start_offset"], same["total_bytes"])

    def test_append_replaces_snapshot_and_old_chunks_are_rejected(self):
        path = self.save()
        first = self.store.snapshot(A, V, None)
        with path.open("a") as stream:
            stream.write('{"type":"event_msg","payload":{"type":"task_complete"}}\n')
        second = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(second["status"], "delta")
        self.assertEqual(second["base_revision"], first["session"]["revision"])
        self.assertEqual(second["start_offset"], first["total_bytes"])
        self.assertEqual(self.snapshot_bytes(second), path.read_bytes()[first["total_bytes"]:])
        self.assertNotEqual(second["snapshot_id"], first["snapshot_id"])
        with self.assertRaises(ValueError):
            self.store.chunk(A, V, first["snapshot_id"], 0)

    def test_delta_can_retry_but_unknown_client_revision_resets_to_full(self):
        path = self.save()
        first = self.store.snapshot(A, V, None)
        with path.open("ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{"type":"task_started"}}\n')
        second = self.store.snapshot(A, V, first["session"]["revision"])
        retry = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(retry["status"], "delta")
        self.assertEqual(retry["snapshot_id"], second["snapshot_id"])
        reset = self.store.snapshot(A, V, "different-client-revision")
        self.assertEqual(reset["status"], "ready")
        self.assertEqual(reset["start_offset"], 0)
        self.assertIsNone(reset["base_revision"])
        self.assertEqual(self.snapshot_bytes(reset), path.read_bytes())
        with self.assertRaises(ValueError):
            self.store.chunk(A, V, second["snapshot_id"], 0)

    def test_append_without_old_final_newline_returns_complete_snapshot(self):
        path = self.save()
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        first = self.store.snapshot(A, V, None)
        with path.open("ab") as stream:
            stream.write(b'\n{"type":"event_msg","payload":{}}\n')
        second = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(second["status"], "ready")
        self.assertEqual(self.snapshot_bytes(second), path.read_bytes())

    def test_larger_same_inode_rewrite_requires_prefix_hash_match(self):
        path = self.save(text="old")
        first = self.store.snapshot(A, V, None)
        self.save(text="rewritten and longer")
        second = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(second["status"], "ready")
        self.assertEqual(self.snapshot_bytes(second), path.read_bytes())

    def test_file_replacement_and_unknown_revision_cannot_use_append_delta(self):
        path = self.save()
        first = self.store.snapshot(A, V, None)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes() + b'{"type":"event_msg","payload":{}}\n')
        replacement.replace(path)
        second = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(second["status"], "ready")
        with path.open("ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{}}\n')
        third = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(third["status"], "ready")
        self.assertEqual(self.snapshot_bytes(third), path.read_bytes())

    def test_copy_change_or_disk_full_preserves_previous_snapshot_and_closes_candidate(self):
        path = self.save()
        first = self.store.snapshot(A, V, None)
        old_bytes = self.snapshot_bytes(first)
        old_file = self.store.views[V]["snapshot_file"]
        with path.open("ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{}}\n')
        for failure in ("disk-full", "concurrent-change"):
            candidate = tempfile.TemporaryFile(mode="w+b")
            write = candidate.write
            def failed_write(block):
                if failure == "disk-full":
                    raise OSError(errno.ENOSPC, "No space left")
                with path.open("ab") as source:
                    source.write(b"\n")
                return write(block)
            with patch.object(tempfile, "TemporaryFile", return_value=candidate), \
                    patch.object(candidate, "write", side_effect=failed_write):
                with self.assertRaises((OSError, ValueError)):
                    self.store.snapshot(A, V, first["session"]["revision"])
            self.assertTrue(candidate.closed)
            self.assertFalse(old_file.closed)
            self.assertEqual(self.snapshot_bytes(first), old_bytes)

    def test_successful_replacement_expiry_and_stdio_eof_close_snapshot_files(self):
        path = self.save()
        first = self.store.snapshot(A, V, None)
        replaced = self.store.views[V]["snapshot_file"]
        with path.open("ab") as stream:
            stream.write(b"\n")
        self.store.snapshot(A, V, first["session"]["revision"])
        self.assertTrue(replaced.closed)
        expired = self.store.views[V]["snapshot_file"]
        future = plugin.time.monotonic() + 31
        with patch.object(plugin.time, "monotonic", return_value=future):
            self.store.view(B, W)
        self.assertTrue(expired.closed)
        self.server.traces.snapshot(A, V, None)
        on_exit = self.server.traces.views[V]["snapshot_file"]
        with patch.object(plugin, "ProbeServer", return_value=self.server), \
                patch.object(plugin.sys, "stdin", io.TextIOWrapper(io.BytesIO(b""))):
            plugin.main()
        self.assertTrue(on_exit.closed)
        self.assertEqual(self.server.traces.views, {})

    def test_shared_process_keeps_page_thread_and_snapshot_ownership(self):
        self.save(A, text="only A")
        self.save(B, text="only B")
        first = self.store.snapshot(A, V, None)
        second = self.store.snapshot(B, W, None)
        self.assertIn(b"only A", self.snapshot_bytes(first))
        self.assertIn(b"only B", self.snapshot_bytes(second, B, W))
        for arguments in ((B, V, first["snapshot_id"], 0), (B, W, first["snapshot_id"], 0)):
            with self.assertRaises(ValueError):
                self.store.chunk(*arguments)

    def test_rpc_tools_scope_each_page_and_do_not_rescan_chunks(self):
        path = self.save()
        snapshot = self.call("trace_snapshot")["result"]["structuredContent"]
        self.assertEqual(snapshot["identity_source"], "_meta.thread_id, _meta.threadId")
        self.assertEqual(snapshot["identity_status"], "verified")
        self.assertEqual(snapshot["view_id"], V)
        self.assertEqual(snapshot["request_nonce"], W)
        with patch.object(plugin.os, "walk", side_effect=AssertionError("chunk directory scan")):
            chunk = self.call("trace_chunk", snapshot_id=snapshot["snapshot_id"], chunk_index=0)["result"]["structuredContent"]
        self.assertEqual(base64.b64decode(chunk["data"]), path.read_bytes())
        self.assertEqual(chunk["encoding"], "base64")
        self.assertEqual(chunk["byte_offset"], 0)
        self.assertEqual(chunk["byte_length"], path.stat().st_size)
        self.assertNotEqual(chunk["request_id"], snapshot["request_id"])
        self.assertIn("error", self.call("trace_chunk", thread=B, snapshot_id=snapshot["snapshot_id"], chunk_index=0))

    def test_rpc_identity_and_arguments_cannot_bypass_host_binding(self):
        self.save()
        for meta in ({}, {"openai/session": A}, {"thread_id": A, "threadId": B},
                {"thread_id": A, "x-codex-turn-metadata": "not JSON"}):
            reply = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "trace_snapshot", "arguments": {"view_id": V, "request_nonce": W}, "_meta": meta}})
            self.assertIn("error", reply)
            self.assertNotIn("hello", json.dumps(reply))
        for arguments in ({"thread_id": A}, {"path": str(self.home)}, {"known_revision": 2},
                {"known_revision": "x" * 1025}):
            self.assertEqual(self.call("trace_snapshot", **arguments)["error"]["code"], -32602)
        for index in (True, -1, 9007199254740992, 1.5):
            self.assertEqual(self.call("trace_chunk", snapshot_id=W, chunk_index=index)["error"]["code"], -32602)

    def test_waiting_detects_new_nested_record_without_ten_second_delay(self):
        self.assertEqual(self.store.snapshot(A, V, None)["status"], "waiting")
        path = self.save(folder="sessions/2026/10/12")
        snapshot = self.store.snapshot(A, V, None)
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["session"]["path"], str(path.resolve()))

    def test_duplicate_wrong_header_and_replaced_record_cannot_return_another_thread(self):
        path = self.save()
        duplicate = path.with_name("duplicate-" + A + ".jsonl")
        duplicate.write_bytes(path.read_bytes())
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, None)
        duplicate.unlink()
        first = self.store.snapshot(A, V, None)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": B}}) + "\nprivate B\n")
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, first["session"]["revision"])
        self.assertNotIn(b"private B", self.snapshot_bytes(first))

    def test_changed_cached_identity_invalidates_location_and_next_poll_recovers(self):
        path = self.save()
        first = self.store.snapshot(A, V, None)
        raw = path.read_bytes()
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": B}}) + "\nprivate B\n")
        replacement = path.with_name("replacement-" + A + ".jsonl")
        replacement.write_bytes(raw)
        with self.assertRaises(ValueError):
            self.store.snapshot(A, V, first["session"]["revision"])
        second = self.store.snapshot(A, V, first["session"]["revision"])
        self.assertEqual(second["session"]["path"], str(replacement.resolve()))
        self.assertNotIn(b"private B", self.snapshot_bytes(second))

    def test_large_snapshot_has_no_head_tail_slice_and_retains_no_body_in_memory(self):
        path = self.save(text="prefix" * (3 * 1024 * 1024))
        snapshot = self.store.snapshot(A, V, None)
        self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
        self.assertFalse(snapshot["truncated"])
        self.assertEqual(snapshot["total_bytes"], path.stat().st_size)
        self.assertEqual(self.snapshot_bytes(snapshot), path.read_bytes())
        retained = self.store.views[V]
        self.assertNotIn("chunks", retained)
        self.assertFalse(any(isinstance(value, (str, bytes)) and len(value) > plugin.MAX_HEADER for value in retained.values()))

    def test_snapshot_source_reads_are_bounded_and_chunk_reads_use_only_frozen_file(self):
        path = self.save(text="x" * (2 * 1024 * 1024))
        original_open = plugin.open_record
        calls = []
        @contextmanager
        def bounded_open(*arguments):
            with original_open(*arguments) as stream:
                read = stream.read
                def bounded_read(size=-1):
                    self.assertGreater(size, 0)
                    self.assertLessEqual(size, plugin.CHUNK_BYTES)
                    calls.append(size)
                    return read(size)
                with patch.object(stream, "read", side_effect=bounded_read):
                    yield stream
        with patch.object(plugin, "open_record", side_effect=bounded_open):
            snapshot = self.store.snapshot(A, V, None)
        self.assertGreater(len(calls), 1)
        original_bytes = path.read_bytes()
        path.write_bytes(b"source has changed")
        with patch.object(plugin, "open_record", side_effect=AssertionError("chunk source read")):
            self.assertEqual(self.snapshot_bytes(snapshot), original_bytes)

    def test_unsafe_file_byte_size_is_rejected_and_candidate_closed(self):
        path = self.save()
        self.store.locate(A, self.store.view(A, V))
        info = path.stat()
        oversized = SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
            st_size=plugin.MAX_SAFE_INTEGER + 1, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
        candidate = tempfile.TemporaryFile(mode="w+b")
        self.addCleanup(candidate.close)
        with patch.object(plugin.os, "fstat", return_value=oversized), \
                patch.object(tempfile, "TemporaryFile", return_value=candidate):
            with self.assertRaisesRegex(ValueError, "safe byte-offset"):
                self.store.snapshot(A, V, None)
        self.assertTrue(candidate.closed)
        self.assertNotIn("snapshot_file", self.store.views[V])

    def test_raw_byte_pages_cross_unicode_long_lines_and_exceed_256_pages(self):
        path = self.save(text="中文🙂" * 600)
        with patch.object(plugin, "CHUNK_BYTES", 17):
            snapshot = self.call("trace_snapshot")["result"]["structuredContent"]
            self.assertGreater(snapshot["total_chunks"], 256)
            pieces = []
            for index in range(snapshot["total_chunks"]):
                chunk = self.call("trace_chunk", snapshot_id=snapshot["snapshot_id"], chunk_index=index)["result"]["structuredContent"]
                raw = base64.b64decode(chunk["data"])
                self.assertEqual(chunk["byte_offset"], index * 17)
                self.assertEqual(chunk["byte_length"], len(raw))
                self.assertEqual(chunk["done"], index == snapshot["total_chunks"] - 1)
                pieces.append(raw)
        self.assertEqual(b"".join(pieces), path.read_bytes())

    def test_generated_resource_uses_existing_viewer_and_packaged_schema_reader(self):
        installed = self.home / "installed plugin"
        assets = installed / "assets"
        assets.mkdir(parents=True)
        for name in ("viewer-bridge.js",):
            shutil.copyfile(PROJECT / "plugins/codex-trace-probe" / name, installed / name)
        shutil.copyfile(PROJECT / "index.html", assets / "index.html")
        shutil.copyfile(PROJECT / "server.py", assets / "viewer_server.py")
        with patch.object(plugin, "__file__", str(installed / "server.py")):
            result = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": plugin.TRACE_RESOURCE}})
            resource = result["result"]["contents"][0]
            self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
            self.assertIn("const S =", resource["text"])
            self.assertIn("S.scope = 'plugin-pending'", resource["text"])
            self.assertEqual(resource["text"].count("bind();\nbindGutters();\nboot();"), 1)
            self.assertEqual(resource["_meta"]["ui"]["csp"]["connectDomains"], [])
            cache = self.home / "cache" / "codex_apps_tools"
            cache.mkdir(parents=True)
            (cache / "tools.json").write_text(json.dumps({"tools": [{"server_name": "fixture", "tool_name": "lookup",
                "tool_namespace": "fixture", "tool": {"name": "fixture.lookup", "inputSchema": {"type": "object"}}}]}))
            schema = self.call("trace_schema", name="lookup", ns="fixture")["result"]["structuredContent"]["schema"]
            self.assertTrue(schema["found"])
            self.assertEqual(schema["inputSchema"], {"type": "object"})

    def test_invalid_unicode_metadata_does_not_terminate_stdio_or_next_ping(self):
        path = self.save()
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": A, "cwd": "\ud800"}}) + "\n")
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "trace_snapshot",
            "arguments": {"view_id": V, "request_nonce": W}, "_meta": {"thread_id": A}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        process = subprocess.run([sys.executable, "-B", str(PROJECT / "plugins/codex-trace-probe/server.py")],
            input="".join(json.dumps(item) + "\n" for item in requests).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(replies[0]["result"]["structuredContent"]["session"]["cwd"], "\ud800")

    def test_one_emoji_does_not_widen_the_entire_cached_ascii_snapshot(self):
        path = self.save(text="a" * (1024 * 1024) + "🙂")
        snapshot = self.store.snapshot(A, V, None)
        def retained_size(value):
            if isinstance(value, dict):
                return sys.getsizeof(value) + sum(retained_size(item) for item in value.values())
            if isinstance(value, (tuple, list)):
                return sys.getsizeof(value) + sum(retained_size(item) for item in value)
            return sys.getsizeof(value)
        self.assertLess(retained_size(self.store.views[V]), path.stat().st_size * 2)
        self.assertEqual(self.snapshot_bytes(snapshot), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
