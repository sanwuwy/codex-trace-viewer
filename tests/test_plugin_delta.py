import base64
from contextlib import contextmanager
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import test_plugin_lazy as lazy

plugin, A, V, W = lazy.plugin, lazy.A, lazy.V, lazy.W


class OutlineDeltaTests(unittest.TestCase):
    setUp = lazy.LazyTraceTests.setUp
    save = lazy.LazyTraceTests.save
    payload = lazy.LazyTraceTests.payload

    def append(self, raw):
        with self.path.open("ab") as source:
            source.write(raw)

    def test_append_projects_only_suffix_and_reuses_frozen_files(self):
        first = self.store.snapshot(A, V, None, "outline")
        baseline = self.payload(first)
        view = self.store.views[V]
        old_raw, old_outline = view["snapshot_file"], view["outline_file"]
        suffix = b'\ninvalid\n{"type":"event_msg","payload":{"type":"task_complete"}}\n'
        self.append(suffix)
        raw_write, outline_write = old_raw.write, old_outline.write
        raw_writes, outline_writes, worker_sizes = [], [], []
        build = plugin.build_outline_isolated
        def project(source, *arguments, **keywords):
            worker_sizes.append(os.fstat(source.fileno()).st_size)
            return build(source, *arguments, **keywords)
        def write_raw(data):
            raw_writes.append(len(data))
            return raw_write(data)
        def write_outline(data):
            outline_writes.append(len(data))
            return outline_write(data)
        with patch.object(old_raw, "read", side_effect=AssertionError("Old frozen raw was reread")), \
                patch.object(old_outline, "read", side_effect=AssertionError("Old outline was reread")), \
                patch.object(old_raw, "write", side_effect=write_raw), \
                patch.object(old_outline, "write", side_effect=write_outline), \
                patch.object(plugin, "build_outline_isolated", side_effect=project):
            delta = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(delta["status"], "delta")
        self.assertEqual(worker_sizes, [len(suffix)])
        self.assertEqual(sum(raw_writes), len(suffix))
        self.assertEqual(sum(outline_writes), delta["total_bytes"] - first["total_bytes"])
        self.assertIs(view["snapshot_file"], old_raw)
        self.assertIs(view["outline_file"], old_outline)
        self.assertEqual(delta["start_offset"], len(baseline))
        self.assertEqual(delta["base_revision"], first["session"]["revision"])
        self.assertEqual(delta["record_count"], 6)
        self.assertEqual(delta["skipped_records"], 1)
        self.assertEqual(json.loads(self.payload(delta))["index"], 7)
        detail = self.store.detail(A, V, delta["snapshot_id"], [0, 4, 7])
        self.assertEqual([item["index"] for item in map(json.loads, self.payload(detail, True).splitlines())], [0, 4, 7])
        expected = self.store.snapshot(A, W, None, "outline")
        expected_bytes = b"".join(base64.b64decode(self.store.chunk(A, W, expected["snapshot_id"], i)["data"])
            for i in range(expected["total_chunks"]))
        self.assertEqual(baseline + self.payload(delta), expected_bytes)

    def test_blank_invalid_only_append_has_zero_page_delta_then_correct_next_index(self):
        first = self.store.snapshot(A, V, None, "outline")
        self.append(b'\ninvalid\n {"x":NaN}\n \t\r\n')
        delta = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(delta["status"], "delta")
        self.assertEqual(delta["total_bytes"], first["total_bytes"])
        self.assertEqual(delta["total_chunks"], 0)
        self.assertEqual(delta["skipped_records"], 2)
        self.assertEqual(delta["record_count"], 5)
        self.append('{"type":"event_msg","payload":{"text":"中文🙂"}}\n'.encode())
        second = self.store.snapshot(A, V, delta["session"]["revision"], "outline")
        self.assertEqual(json.loads(self.payload(second))["index"], 9)
        self.assertEqual(second["skipped_records"], 2)
        self.assertEqual(second["record_count"], 6)

    def test_rewrite_append_replacement_truncation_and_unknown_baseline_rebuild(self):
        for change in ("rewrite-append", "replace", "truncate", "unknown-baseline"):
            with self.subTest(change=change):
                first = self.store.snapshot(A, V, None, "outline")
                previous = self.store.views[V]["snapshot_file"]
                raw = self.path.read_bytes()
                if change == "rewrite-append":
                    self.path.write_bytes(raw.replace(b'"model": "fixture"', b'"model": "changed"') + b'{}\n')
                elif change == "replace":
                    replacement = self.path.with_suffix(".new")
                    replacement.write_bytes(raw + b'{}\n')
                    replacement.replace(self.path)
                elif change == "truncate":
                    self.path.write_bytes(raw.splitlines(keepends=True)[0])
                else:
                    self.append(b'{}\n')
                result = self.store.snapshot(A, V, "unknown" if change == "unknown-baseline" else first["session"]["revision"], "outline")
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["start_offset"], 0)
                self.assertIsNone(result["base_revision"])
                self.assertTrue(previous.closed)
                frozen = self.store.views[V]["snapshot_file"]
                frozen.seek(0)
                self.assertEqual(frozen.read(), self.path.read_bytes())

    def test_partial_utf8_final_line_requires_full_rebuild_and_recovers(self):
        first = self.store.snapshot(A, V, None, "outline")
        line = '{"type":"event_msg","payload":{"text":"中文🙂"}}\n'.encode()
        split = line.index('🙂'.encode()) + 2
        self.append(line[:split])
        partial = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(partial["status"], "ready")
        self.assertEqual(partial["skipped_records"], 1)
        self.append(line[split:])
        complete = self.store.snapshot(A, V, partial["session"]["revision"], "outline")
        self.assertEqual(complete["status"], "ready")
        self.assertEqual(complete["skipped_records"], 0)
        self.assertEqual(json.loads(self.payload(complete).splitlines()[-1])["record"]["payload"]["text"], "中文🙂")
        self.append(b'{}\n')
        delta = self.store.snapshot(A, V, complete["session"]["revision"], "outline")
        self.assertEqual(delta["status"], "delta")
        self.assertEqual(json.loads(self.payload(delta))["index"], 6)

    def test_delta_retry_unchanged_and_unknown_revision_do_not_read_source(self):
        first = self.store.snapshot(A, V, None, "outline")
        self.append(b'{}\n')
        delta = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        with patch.object(plugin, "open_record", side_effect=AssertionError("Unchanged source reopened")), \
                patch.object(plugin, "build_outline_isolated", side_effect=AssertionError("Unchanged outline rebuilt")):
            retry = self.store.snapshot(A, V, first["session"]["revision"], "outline")
            self.assertEqual(retry["status"], "delta")
            self.assertEqual(retry["snapshot_id"], delta["snapshot_id"])
            same = self.store.snapshot(A, V, delta["session"]["revision"], "outline")
            self.assertEqual(same["status"], "unchanged")
            full = self.store.snapshot(A, V, "unknown", "outline")
            self.assertEqual(full["status"], "ready")
            self.assertEqual(full["start_offset"], 0)
        self.assertEqual(len(self.payload(full).splitlines()), 6)
        with self.assertRaises(ValueError):
            self.store.chunk(A, V, delta["snapshot_id"], 0)

    def test_failed_inplace_append_rolls_back_bytes_index_detail_and_other_view(self):
        first = self.store.snapshot(A, V, None, "outline")
        baseline = self.payload(first)
        detail = self.store.detail(A, V, first["snapshot_id"], [4])
        baseline_detail = self.payload(detail, True)
        other = self.store.snapshot(A, W, None, "outline")
        view = self.store.views[V]
        raw, outline, old_detail = [view[key] for key in ("snapshot_file", "outline_file", "detail_file")]
        offsets = view["record_offsets"].copy()
        self.append(b'{"type":"event_msg","payload":{"type":"task_complete"}}\n')
        original_factory = tempfile.TemporaryFile
        for failed in (raw, outline):
            candidates = []
            def temporary(*arguments, **keywords):
                candidate = original_factory(*arguments, **keywords)
                candidates.append(candidate)
                return candidate
            original_write = failed.write
            def short_write(data):
                original_write(data[:max(1, len(data) // 2)])
                raise OSError("fixture disk full")
            with patch.object(tempfile, "TemporaryFile", side_effect=temporary), \
                    patch.object(failed, "write", side_effect=short_write):
                with self.assertRaisesRegex(OSError, "fixture disk full"):
                    self.store.snapshot(A, V, first["session"]["revision"], "outline")
            self.assertTrue(all(file.closed for file in candidates))
            self.assertEqual(os.fstat(raw.fileno()).st_size, first["source_bytes"])
            self.assertEqual(os.fstat(outline.fileno()).st_size, first["total_bytes"])
            self.assertEqual(view["record_offsets"], offsets)
            self.assertEqual(view["revision"], first["session"]["revision"])
            self.assertEqual(self.payload(first), baseline)
            self.assertEqual(self.payload(detail, True), baseline_detail)
            self.assertFalse(old_detail.closed)
            self.assertIn("data", self.store.chunk(A, W, other["snapshot_id"], 0))
        delta = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(delta["status"], "delta")
        self.assertTrue(old_detail.closed)
        self.store.release(A, V)
        self.assertTrue(raw.closed)
        self.assertTrue(outline.closed)
        self.assertIn("data", self.store.chunk(A, W, other["snapshot_id"], 0))

    def test_raw_prefix_verification_is_bounded_and_source_change_cannot_commit(self):
        first = self.store.snapshot(A, V, None, "outline")
        baseline = self.payload(first)
        self.append(b'{}\n')
        source_size = self.path.stat().st_size
        original_open = plugin.open_record
        reads, modify = [], False
        @contextmanager
        def observed(*arguments):
            with original_open(*arguments) as source:
                read = source.read
                def observed_read(size=-1):
                    self.assertGreater(size, 0)
                    self.assertLessEqual(size, plugin.CHUNK_BYTES)
                    data = read(size)
                    reads.append((source.tell() - len(data), len(data)))
                    if modify and len(reads) == 1:
                        self.append(b'{}\n')
                    return data
                with patch.object(source, "read", side_effect=observed_read):
                    yield source
        with patch.object(plugin, "open_record", side_effect=observed):
            modify = True
            with self.assertRaisesRegex(ValueError, "changed during the read"):
                self.store.snapshot(A, V, first["session"]["revision"], "outline")
            self.assertEqual(self.payload(first), baseline)
            modify = False
            reads.clear()
            delta = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(delta["status"], "delta")
        self.assertEqual(sum(length for _, length in reads), source_size + len(b'{}\n') + 1)

    def test_short_candidate_read_cannot_commit_incomplete_append(self):
        first = self.store.snapshot(A, V, None, "outline")
        baseline = self.payload(first)
        self.append(b'{}\n')
        build = plugin.build_outline_isolated
        patches = []
        def truncate_read(source, *arguments, **keywords):
            result = build(source, *arguments, **keywords)
            fault = patch.object(source, "read", return_value=b"")
            fault.start()
            patches.append(fault)
            return result
        try:
            with patch.object(plugin, "build_outline_isolated", side_effect=truncate_read):
                with self.assertRaisesRegex(OSError, "incremental snapshot read"):
                    self.store.snapshot(A, V, first["session"]["revision"], "outline")
        finally:
            for fault in patches:
                fault.stop()
        self.assertEqual(self.payload(first), baseline)
        self.assertEqual(os.fstat(self.store.views[V]["snapshot_file"].fileno()).st_size, first["source_bytes"])

    def test_flush_failure_rolls_back_both_files_without_flushing_dirty_tail_again(self):
        first = self.store.snapshot(A, V, None, "outline")
        baseline = self.payload(first)
        view = self.store.views[V]
        self.append(b'{}\n')
        for key in ("snapshot_file", "outline_file"):
            target = view[key]
            with patch.object(target, "flush", side_effect=OSError("fixture flush ENOSPC")) as flush:
                with self.assertRaisesRegex(OSError, "fixture flush ENOSPC"):
                    self.store.snapshot(A, V, first["session"]["revision"], "outline")
                self.assertEqual(flush.call_count, 1)
            self.assertEqual(os.fstat(view["snapshot_file"].fileno()).st_size, first["source_bytes"])
            self.assertEqual(os.fstat(view["outline_file"].fileno()).st_size, first["total_bytes"])
            self.assertEqual(self.payload(first), baseline)

    def test_rollback_attempts_both_files_and_reports_cleanup_failure(self):
        first = self.store.snapshot(A, V, None, "outline")
        baseline = self.payload(first)
        view = self.store.views[V]
        self.append(b'{}\n{}\n')
        truncate, attempted = plugin.os.ftruncate, []
        def failed_truncate(descriptor, size):
            attempted.append(descriptor)
            if descriptor == view["snapshot_file"].fileno():
                raise OSError("fixture truncate failure")
            return truncate(descriptor, size)
        with patch.object(view["outline_file"], "flush", side_effect=OSError("fixture flush ENOSPC")), \
                patch.object(plugin.os, "ftruncate", side_effect=failed_truncate):
            with self.assertRaisesRegex(OSError, "rollback failed"):
                self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(attempted, [view["snapshot_file"].fileno(), view["outline_file"].fileno()])
        self.assertEqual(self.payload(first), baseline)
        self.assertEqual(view["revision"], first["session"]["revision"])
        with self.path.open("r+b") as source:
            source.truncate(first["source_bytes"] + 3)
        recovered = self.store.snapshot(A, V, first["session"]["revision"], "outline")
        self.assertEqual(recovered["status"], "delta")
        self.assertEqual(os.fstat(view["snapshot_file"].fileno()).st_size, recovered["source_bytes"])


if __name__ == "__main__":
    unittest.main()
