from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("full_payload_server", PROJECT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FullPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="full payload ")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / "rollout.jsonl"
        roots = patch.object(server, "ROOTS", [str(self.path.parent)])
        roots.start()
        self.addCleanup(roots.stop)
        self.path.write_bytes(b'{"type":"session_meta","payload":{"id":"own"}}\n' +
            json.dumps({"type": "message", "text": "middle" + "a" * (2 * server.READ_BLOCK_BYTES)}).encode() +
            b'\n{"type":"message","text":"last"}\n')

    def test_frozen_payload_contains_every_byte_and_closes_temporary_stream(self):
        with server.read_payload(self.path) as (stream, size, head):
            self.assertEqual(size, self.path.stat().st_size)
            self.assertEqual(server.session_header(head)["id"], "own")
            self.assertEqual(stream.read(), self.path.read_bytes())
        self.assertTrue(stream.closed)

    def test_source_change_during_freeze_rejects_the_snapshot(self):
        original = os.fstat
        calls = []
        def changed(descriptor):
            result = original(descriptor)
            calls.append(descriptor)
            if len(calls) == 2:
                self.path.write_bytes(self.path.read_bytes() + b'\n')
                return original(descriptor)
            return result
        with patch.object(server.os, "fstat", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "发生变化"):
                with server.read_payload(self.path):
                    self.fail("Changed source cannot produce a usable snapshot")

    def test_response_streams_full_length_and_explicitly_marks_no_slice(self):
        handler = object.__new__(server.Handler)
        headers = {}
        handler.send_response = lambda status: self.assertEqual(status, 200)
        handler.send_header = lambda name, value: headers.update({name: value})
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        with server.read_payload(self.path) as (stream, size, _):
            handler._send_file(stream, size)
        self.assertEqual(headers["X-Truncated"], "0")
        self.assertEqual(int(headers["Content-Length"]), self.path.stat().st_size)
        self.assertEqual(handler.wfile.getvalue(), self.path.read_bytes())

    def test_mid_send_error_closes_connection_without_a_second_http_response(self):
        handler = object.__new__(server.Handler)
        handler.send_response = lambda status: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        class FailingSnapshot:
            def read(self, size):
                raise OSError("temporary snapshot read failed")
        errors = io.StringIO()
        with redirect_stderr(errors):
            handler._send_file(FailingSnapshot(), 100)
        self.assertTrue(handler.close_connection)
        self.assertIn("temporary snapshot read failed", errors.getvalue())
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_leaf_link_swap_after_authorization_cannot_copy_an_outside_file(self):
        authorized = server.safe_path(str(self.path))
        outside = self.path.parent.parent / (self.path.parent.name + "-outside.jsonl")
        outside.write_bytes(b'{"type":"session_meta","payload":{"id":"own"}}\n"outside secret"\n')
        self.addCleanup(outside.unlink)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            with server.read_payload(authorized):
                self.fail("Outside content cannot be authorized by a matching metadata ID")

    def test_directory_link_swap_after_authorization_cannot_copy_an_outside_file(self):
        directory = self.path.parent / "nested"
        directory.mkdir()
        nested = directory / "rollout.jsonl"
        nested.write_bytes(self.path.read_bytes())
        authorized = server.safe_path(str(nested))
        outside = self.path.parent.parent / (self.path.parent.name + "-outside")
        outside.mkdir()
        (outside / "rollout.jsonl").write_bytes(self.path.read_bytes())
        self.addCleanup(outside.rmdir)
        self.addCleanup((outside / "rollout.jsonl").unlink)
        nested.unlink()
        directory.rmdir()
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            with server.read_payload(authorized):
                self.fail("Directory replacement cannot bypass the configured data roots")

    @unittest.skipUnless(os.name == "posix", "Requires POSIX directory descriptors")
    def test_replaced_ancestor_of_data_root_is_rejected_before_reading(self):
        home = self.path.parent / "home"
        root = home / "sessions"
        root.mkdir(parents=True)
        path = root / "rollout.jsonl"
        path.write_bytes(self.path.read_bytes())
        alternative = self.path.parent / "alternative"
        (alternative / "sessions").mkdir(parents=True)
        (alternative / "sessions" / path.name).write_bytes(path.read_bytes())
        original = os.open
        opened = []
        def replaced(name, *args, **kwargs):
            if not opened and args[0] & os.O_DIRECTORY:
                opened.append(True)
                home.rename(self.path.parent / "saved-home")
                home.symlink_to(alternative, target_is_directory=True)
            return original(name, *args, **kwargs)
        with patch.object(server, "ROOTS", [str(root)]), patch.object(server.os, "open", side_effect=replaced):
            with self.assertRaises((OSError, ValueError)):
                with server.read_payload(str(path)):
                    self.fail("A changed ancestor cannot redirect an authorized snapshot")
        self.assertTrue(opened)


if __name__ == "__main__":
    unittest.main()
