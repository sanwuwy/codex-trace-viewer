import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("sessions_server", Path(__file__).parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def message(text, kinds=None, timestamp="2026-10-10T02:00:00Z"):
    content = [{"type": "input_text", "text": text}]
    return {"type": "response_item", "timestamp": timestamp,
            "payload": {"type": "message", "role": "user", "content": content,
                        "internal_chat_message_metadata_passthrough": {"content_item_kinds": kinds}}}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = patch.multiple(server, ROOTS=[str(root)], CACHE_PATH=str(root / "cache.json"),
                                       INDEX_PATH=str(root / "index"), STATE_PATH=str(root / "state"),
                                       _cache={"peek": {}, "stats": {}}, _cache_loaded=False, _cache_dirty=False)
        self.settings.start()
        self.file = root / "rollout-2026-10-10T02-00-00-id.jsonl"
        self.write("first")

    def tearDown(self):
        self.settings.stop()
        self.tmp.cleanup()

    def write(self, text):
        meta = {"type": "session_meta", "payload": {"id": "id", "cwd": "/project"}}
        self.file.write_text(json.dumps(meta) + "\n" + json.dumps(message(text)) + "\n")

    def test_attached_user_request_is_an_activity(self):
        self.assertIsNotNone(server._human_user_ts(message("# Files mentioned by the user:\n## My request:\ninspect", ["user.text"])))

    def test_image_only_input_is_an_activity(self):
        d = message("")
        d["payload"]["content"] = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]
        d["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = ["user.image"]
        self.assertIsNotNone(server._human_user_ts(d))

    def test_injection_and_invalid_attribution_are_excluded(self):
        for kinds in (None, [], ["user.text", "user.image"], [1]):
            with self.subTest(kinds=kinds):
                self.assertIsNone(server._human_user_ts(message("<environment_context>data</environment_context>", kinds)))
        self.assertIsNotNone(server._human_user_ts(message("<send_user_message_question_reply>[]</send_user_message_question_reply>")))

    def test_unchanged_listing_neither_peeks_nor_writes_cache(self):
        server.list_sessions()
        with patch.object(server, "peek_file", wraps=server.peek_file) as peek, patch.object(server, "save_cache") as save:
            self.assertEqual(len(server.list_sessions()), 1)
        peek.assert_not_called()
        save.assert_not_called()

    def test_equal_size_edit_within_same_second_invalidates_cache(self):
        ns = 1791597600123456000
        os.utime(self.file, ns=(ns, ns))
        self.assertEqual(server.list_sessions()[0]["preview"], "first")
        self.write("later")
        os.utime(self.file, ns=(ns + 1, ns + 1))
        self.assertEqual(server.list_sessions()[0]["preview"], "later")

    def test_cache_keeps_one_version_and_removes_deleted_files(self):
        server.list_sessions()
        self.write("changed")
        server.list_sessions()
        self.assertEqual(len(server._cache["peek"]), 1)
        self.file.unlink()
        self.assertEqual(server.list_sessions(), [])
        self.assertEqual(server._cache["peek"], {})

    def test_index_changes_update_title_and_sort_without_repeeking(self):
        server.list_sessions()
        Path(server.INDEX_PATH).write_text(json.dumps({"id": "id", "thread_name": "renamed", "updated_at": "2026-10-10T03:00:00Z"}) + "\n")
        Path(server.STATE_PATH).write_text("{}")
        with patch.object(server, "peek_file", wraps=server.peek_file) as peek:
            item = server.list_sessions()[0]
        self.assertEqual(item["title"], "renamed")
        self.assertEqual(item["updated_at"], "2026-10-10T03:00:00Z")
        peek.assert_not_called()

    def test_legacy_cache_is_rebuilt_but_stats_are_preserved(self):
        stats = {"turns": 3}
        Path(server.CACHE_PATH).write_text(json.dumps({"peek": {"legacy": {"last_user_ts": None}},
                                                      "stats": {str(self.file): stats}}))
        item = server.list_sessions()[0]
        self.assertIsNotNone(item["last_user_ts"])
        self.assertEqual(item["stats"], stats)
        self.assertNotIn("legacy", server._cache["peek"])

    def test_failed_cache_write_is_visible_and_retried(self):
        with patch.object(server.os, "replace", side_effect=OSError("disk full")), patch.object(server.sys, "stderr") as err:
            self.assertEqual(len(server.list_sessions()), 1)
            self.assertTrue(server._cache_dirty)
            self.assertTrue(err.write.called)
        server.list_sessions()
        self.assertFalse(server._cache_dirty)
        self.assertTrue(Path(server.CACHE_PATH).exists())

    def test_concurrent_refreshes_share_cache_fill(self):
        entered, release = threading.Event(), threading.Event()
        original = server.peek_file
        errors = []
        def peek(path):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("cache fill did not resume")
            return original(path)
        def listing():
            try:
                server.list_sessions()
            except Exception as e:
                errors.append(e)
        with patch.object(server, "peek_file", side_effect=peek) as spy:
            first = threading.Thread(target=listing)
            second = threading.Thread(target=listing)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(spy.call_count, 1)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
