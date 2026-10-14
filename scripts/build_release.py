#!/usr/bin/env python3
"""Build allowlisted GitHub source and plugin archives; never include Git history or local data."""
import io
import json
from pathlib import Path
import re
import sys
import zipfile
import hashlib

import build_plugin

PROJECT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("README.md", "INSTALL.md", "ARCHITECTURE.md", "LICENSE", ".gitignore",
              "server.py", "index.html", "trace.py", "start.command")
SUPPORT_FILES = (".agents/plugins/marketplace.json", ".agents/skills/codex-trace/SKILL.md",
                 ".agents/skills/codex-trace/agents/openai.yaml",
                 ".agents/skills/codex-trace/scripts/viewer.py",
                 "scripts/build_plugin.py", "scripts/build_release.py", "scripts/install_plugin.py",
                 "scripts/benchmark_lazy.cjs", "scripts/benchmark_delta.cjs",
                 "scripts/benchmark_frontend.cjs", "scripts/benchmark_idle.py")


def source_files(project):
    files = {Path(name) for name in ROOT_FILES + SUPPORT_FILES}
    plugin = Path("plugins/codex-trace-probe")
    files.update(plugin / name for name in build_plugin.PACKAGE)
    files.update(plugin / "assets" / target for _, target in build_plugin.ASSETS)
    files.add(plugin / "LICENSE")
    tests = project / "tests"
    if tests.is_symlink() or not tests.is_dir():
        raise build_plugin.BuildError("Expected a real tests directory")
    files.update(path.relative_to(project) for pattern in ("*.py", "*.cjs") for path in tests.glob(pattern))
    for relative in files:
        build_plugin.safe_file(project / relative, project)
    return sorted(files)


def archive_bytes(project, files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            # Stable metadata; retain the executable macOS launcher bit.
            info = zipfile.ZipInfo("codex-trace-viewer/" + relative.as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if relative.name == "start.command" else 0o100644) << 16
            archive.writestr(info, (project / relative).read_bytes())
    return output.getvalue()


def build(project=PROJECT):
    project = Path(project).resolve()
    build_plugin.build(project)
    build_plugin.build(project, check=True)
    manifest = json.loads((project / "plugins/codex-trace-probe/.codex-plugin/plugin.json").read_text())
    version = manifest["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise build_plugin.BuildError("Release version must be major.minor.patch")
    if manifest.get("license") != "MIT":
        raise build_plugin.BuildError("This release requires the declared MIT license")
    files = source_files(project)
    source = archive_bytes(project, files)
    folder = project / "dist"
    build_plugin.safe_file(folder / "SHA256SUMS", project, required=False)
    names = ("codex-trace-viewer-" + version + "-source.zip", "codex-trace-plugin-" + version + ".zip")
    for name in names:
        build_plugin.safe_file(folder / name, project, required=False)
    folder.mkdir(exist_ok=True)
    build_plugin.build(project, archive=folder / names[1])
    build_plugin.write_atomic(folder / names[0], source)
    checksums = "".join(hashlib.sha256((folder / name).read_bytes()).hexdigest() + "  " + name + "\n"
                        for name in names)
    build_plugin.write_atomic(folder / "SHA256SUMS", checksums.encode("ascii"))
    return {"version": version, "source_files": len(files), "source_archive": str(folder / names[0]),
            "plugin_archive": str(folder / names[1]), "checksums": str(folder / "SHA256SUMS")}


def main():
    try:
        print(json.dumps(build(), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
