import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("viewer_server", Path(__file__).parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def entry(namespace, description):
    return {"server_name": "codex_apps", "tool_name": "_run", "tool_namespace": namespace,
            "tool": {"name": namespace.split("__")[-1] + ".run", "description": description,
                     "inputSchema": {"type": "object"}}}


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = server.TOOLS_CACHE_DIR
        server.TOOLS_CACHE_DIR = self.tmp.name
        server._tools_idx = {"sig": None, "entries": []}

    def tearDown(self):
        server.TOOLS_CACHE_DIR = self.previous
        self.tmp.cleanup()

    def cache(self, name, tools, mtime):
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps({"tools": tools}))
        os.utime(p, (mtime, mtime))

    def test_namespace_selects_the_right_tool(self):
        self.cache("both.json", [entry("codex_apps__alpha", "alpha"), entry("codex_apps__beta", "beta")], 10)
        found = server.lookup_tool_schema("_run", "codex_apps__beta")
        self.assertEqual(found["description"], "beta")

    def test_ambiguous_name_never_selects_the_first_tool(self):
        self.cache("both.json", [entry("codex_apps__alpha", "alpha"), entry("codex_apps__beta", "beta")], 10)
        found = server.lookup_tool_schema("_run", "")
        self.assertFalse(found["found"])
        self.assertIn("多个", found["error"])

    def test_latest_definition_has_provenance(self):
        self.cache("old.json", [entry("codex_apps__alpha", "old")], 10)
        self.cache("new.json", [entry("codex_apps__alpha", "new")], 20)
        found = server.lookup_tool_schema("_run", "codex_apps__alpha")
        self.assertEqual(found["description"], "new")
        self.assertTrue(found["cache_file"].endswith("new.json"))
        self.assertIn("cache_updated_at", found)

    def test_runtime_alias_and_unknown_namespace(self):
        self.cache("one.json", [entry("codex_apps__alpha", "alpha")], 10)
        self.assertTrue(server.lookup_tool_schema("mcp__codex_apps__alpha_run", "")["found"])
        self.assertFalse(server.lookup_tool_schema("_run", "unknown")["found"])

    def test_corrupt_cache_error_is_visible(self):
        (Path(self.tmp.name) / "bad.json").write_text("broken")
        found = server.lookup_tool_schema("_run", "")
        self.assertFalse(found["found"])
        self.assertIn("bad.json", found["error"])


if __name__ == "__main__":
    unittest.main()
