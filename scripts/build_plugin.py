#!/usr/bin/env python3
"""Bundle the existing viewer sources inside the local plugin, using stdlib only."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
ASSETS = (("server.py", "viewer_server.py"), ("index.html", "index.html"))
PACKAGE = (".codex-plugin/plugin.json", ".mcp.json", "server.py", "viewer-bridge.js")


class BuildError(ValueError):
    pass


def safe_file(path, root, required=True):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise BuildError("Symbolic links are not package inputs or outputs: " + str(path))
        if part == root:
            break
    if path.exists() and not path.is_file():
        raise BuildError("Expected a regular file: " + str(path))
    if required and not path.is_file():
        raise BuildError("Missing package file: " + str(path))


def write_atomic(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build(project=PROJECT, check=False, archive=None):
    project = Path(project).resolve()
    plugin = project / "plugins" / "codex-trace-probe"
    source = {}
    for name, target in ASSETS:
        path = project / name
        safe_file(path, project)
        source[plugin / "assets" / target] = path.read_bytes()
    license_path = project / "LICENSE"
    safe_file(license_path, project)
    source[plugin / "LICENSE"] = license_path.read_bytes()
    files = [plugin / name for name in PACKAGE]
    for path in files:
        safe_file(path, project)
    manifest = json.loads((plugin / PACKAGE[0]).read_text(encoding="utf-8"))
    mcp = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("name") != "codex-trace-probe" or
            not isinstance(manifest.get("version"), str) or
            manifest.get("mcpServers") != "./.mcp.json" or
            not isinstance(mcp, dict) or not isinstance(mcp.get("mcpServers"), dict)):
        raise BuildError("Invalid plugin manifest or MCP configuration")
    for path in source:
        safe_file(path, project, required=False)
    archive = Path(archive).expanduser().absolute() if archive is not None else None
    if check and archive is not None:
        raise BuildError("--check does not write a ZIP archive")
    if archive is not None:
        if archive.resolve().is_relative_to(plugin.resolve()):
            raise BuildError("ZIP output must be outside the plugin directory")
        safe_file(archive, archive.parent, required=False)
    if check:
        for path, data in source.items():
            if not path.is_file() or path.read_bytes() != data:
                raise BuildError("Missing or stale generated asset; run scripts/build_plugin.py: " + str(path))
    else:
        (plugin / "assets").mkdir(exist_ok=True)
        for path, data in source.items():
            if not path.is_file() or path.read_bytes() != data:
                write_atomic(path, data)
    if archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=archive.parent, delete=False) as stream:
                temporary = Path(stream.name)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in files + list(source):
                    bundle.writestr(path.relative_to(plugin).as_posix(), path.read_bytes())
            os.replace(temporary, archive)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return {"plugin": str(plugin), "version": manifest["version"], "checked": check,
            "assets": [str(path) for path in source], "archive": str(archive) if archive else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate generated assets without writing")
    parser.add_argument("--zip", metavar="PATH", help="Write an installable plugin ZIP, including viewer assets")
    arguments = parser.parse_args(argv)
    try:
        result = build(check=arguments.check, archive=arguments.zip)
    except (BuildError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
