#!/usr/bin/env python3
"""Install the local trace plugin on macOS using the official Codex CLI."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
PLUGIN = "codex-trace-probe"
MARKETPLACE = "codex-trace-local"
PLUGIN_ID = PLUGIN + "@" + MARKETPLACE
APP_CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")


class InstallError(ValueError):
    pass


def safe_path(path, kind=None):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError("Symbolic links are not installation inputs or outputs: " + str(part))
    if path.exists() and ((kind == "file" and not path.is_file()) or
                          (kind == "directory" and not path.is_dir())):
        raise InstallError("Expected " + kind + ": " + str(path))
    return path


def run(command, *, env, cwd=None):
    try:
        result = subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("CLI command failed: " + str(error)) from error
    if result.returncode:
        message = (result.stderr or result.stdout).strip()[:2000]
        raise InstallError("CLI command failed (exit %s): %s" % (result.returncode, message))
    return result.stdout


def json_result(output, phase):
    try:
        value = json.loads(output)
    except (ValueError, TypeError) as error:
        raise InstallError(phase + " returned invalid JSON") from error
    if not isinstance(value, dict):
        raise InstallError(phase + " returned an unexpected JSON structure")
    return value


def find_cli(codex, env):
    candidate = codex if codex is not None else shutil.which("codex", path=env.get("PATH", ""))
    if candidate is None and APP_CLI.is_file():
        candidate = str(APP_CLI)
    if candidate is None:
        raise InstallError("Codex CLI not found; install Codex Desktop or pass --codex PATH")
    path = Path(candidate).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise InstallError("Codex CLI is missing or not executable: " + str(path))
    return str(path.resolve())


def source_files(project):
    spec = importlib.util.spec_from_file_location("trace_plugin_builder", project / "scripts/build_plugin.py")
    safe_path(project / "scripts/build_plugin.py", "file")
    builder = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(builder)
    finally:
        sys.dont_write_bytecode = previous
    plugin = project / "plugins" / PLUGIN
    files = {}
    for name in builder.PACKAGE:
        path = safe_path(plugin / name, "file")
        if path.is_file():
            files[name] = path.read_bytes()
        elif name != "LICENSE":
            raise InstallError("Missing plugin source: " + str(path))
    for source, target in builder.ASSETS:
        path = safe_path(project / source, "file")
        if not path.is_file():
            raise InstallError("Missing viewer source: " + str(path))
        files["assets/" + target] = path.read_bytes()
    license_path = safe_path(project / "LICENSE", "file")
    if not license_path.is_file():
        raise InstallError("Missing MIT LICENSE")
    files["LICENSE"] = license_path.read_bytes()
    manifest = json_result(files[".codex-plugin/plugin.json"], "Plugin manifest")
    version = manifest.get("version")
    if manifest.get("name") != PLUGIN or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallError("Invalid plugin name or version")
    mcp = json_result(files[".mcp.json"], "MCP configuration")
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {PLUGIN} or not isinstance(servers[PLUGIN], dict):
        raise InstallError("Invalid MCP server configuration")
    for _, target in builder.ASSETS:
        safe_path(plugin / "assets" / target, "file")
    return builder, files, version


def prepare_marketplace(root, files, version, interpreter):
    mcp = json_result(files[".mcp.json"], "MCP configuration")
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {PLUGIN} or not isinstance(servers[PLUGIN], dict):
        raise InstallError("Invalid MCP server configuration")
    servers[PLUGIN]["command"] = interpreter
    files = {**files, ".mcp.json": (json.dumps(mcp, ensure_ascii=False, indent=2) + "\n").encode()}
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode() + b"\0" + data + b"\0")
    output = safe_path(root / (version + "-" + digest.hexdigest()[:16]) / "marketplace", "directory")
    marketplace = {"name": MARKETPLACE, "plugins": [{"name": PLUGIN,
        "source": {"source": "local", "path": "./plugins/" + PLUGIN},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools"}]}
    payload = {"plugins/" + PLUGIN + "/" + name: data for name, data in files.items()}
    payload["LICENSE"] = files["LICENSE"]
    payload[".agents/plugins/marketplace.json"] = (json.dumps(marketplace, indent=2) + "\n").encode()
    if output.exists():
        for name, data in payload.items():
            path = safe_path(output / name, "file")
            if not path.is_file() or path.read_bytes() != data:
                raise InstallError("Retained installation differs from this package: " + str(path))
        return output
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        target = staging / "marketplace"
        for name, data in payload.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Preserve existing versions and interpreter variants; never replace unrelated files.
        output.parent.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(output.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output


def install(project=PROJECT, check=False, codex=None, environ=None, runner=run):
    if sys.platform != "darwin":
        raise InstallError("This release supports verified macOS only")
    if sys.version_info < (3, 9):
        raise InstallError("Python 3.9 or newer is required")
    interpreter = str(Path(sys.executable).resolve())
    if not sys.executable or not Path(interpreter).is_file() or not os.access(interpreter, os.X_OK):
        raise InstallError("Current Python interpreter is missing or not executable")
    env = dict(os.environ if environ is None else environ)
    home = safe_path(env.get("CODEX_HOME", "") or Path.home() / ".codex", "directory")
    env["CODEX_HOME"] = str(home)
    project = safe_path(project, "directory")
    if home == project or project in home.parents:
        raise InstallError("CODEX_HOME must be outside the source repository")
    root = safe_path(home / "trace-viewer-install", "directory")
    cli = find_cli(codex, env)
    # CLI initialization can create PATH aliases even for --help. Probe in a disposable home.
    with tempfile.TemporaryDirectory(prefix="codex-trace-cli-check-") as probe_directory:
        probe_env = {**env, "CODEX_HOME": probe_directory}
        version_output = runner([cli, "--version"], env=probe_env, cwd=probe_directory).strip()
        if not re.match(r"^codex-cli\s+\S+", version_output):
            raise InstallError("The selected CLI is not Codex CLI: " + cli)
        for command in (["plugin", "marketplace", "add"], ["plugin", "add"], ["plugin", "list"]):
            help_output = runner([cli, *command, "--help"], env=probe_env, cwd=probe_directory)
            if "Usage:" not in help_output or "--json" not in help_output:
                raise InstallError("The selected Codex CLI does not support " + " ".join(command))
    builder, files, version = source_files(project)
    plugin = project / "plugins" / PLUGIN
    assets_present = all((plugin / "assets" / target).is_file() and
                         (plugin / "assets" / target).read_bytes() == files["assets/" + target]
                         for _, target in builder.ASSETS)
    result = {"status": "ready", "version": version, "cli": cli, "cli_version": version_output,
        "python": interpreter, "codex_home": str(home), "source_assets_present": assets_present}
    if check:
        return result
    try:
        builder.build(project)
        builder.build(project, check=True)
    except (ValueError, OSError) as error:
        raise InstallError("Source asset build failed: " + str(error)) from error
    marketplace_path = prepare_marketplace(root, files, version, interpreter)
    result["marketplace_path"] = str(marketplace_path)
    phase = "marketplace registration"
    try:
        registered = json_result(runner([cli, "plugin", "marketplace", "add", str(marketplace_path), "--json"], env=env), phase)
        if registered.get("marketplaceName") != MARKETPLACE:
            raise InstallError("CLI registered an unexpected marketplace")
        phase = "plugin installation"
        added = json_result(runner([cli, "plugin", "add", PLUGIN_ID, "--json"], env=env), phase)
        installed_path = added.get("installedPath")
        if added.get("pluginId") != PLUGIN_ID or added.get("version") != version or not isinstance(installed_path, str) or not Path(installed_path).is_absolute():
            raise InstallError("CLI returned an unexpected plugin installation")
        phase = "installation verification"
        listed = json_result(runner([cli, "plugin", "list", "--marketplace", MARKETPLACE, "--json"], env=env), phase)
        installed = listed.get("installed")
        match = [item for item in installed if isinstance(item, dict) and item.get("pluginId") == PLUGIN_ID] if isinstance(installed, list) else []
        if len(match) != 1 or match[0].get("enabled") is not True or match[0].get("installed") is not True or match[0].get("version") != version:
            raise InstallError("The plugin is not confirmed installed and enabled")
        installed_root = safe_path(installed_path, "directory")
        retained_root = marketplace_path / "plugins" / PLUGIN
        for name in files:
            installed_file = safe_path(installed_root / name, "file")
            if not installed_file.is_file() or installed_file.read_bytes() != (retained_root / name).read_bytes():
                raise InstallError("Installed package differs from the verified local package: " + name)
    except InstallError as error:
        raise InstallError(phase + " failed: " + str(error) + "; retained local package: " + str(marketplace_path) + "; no automatic rollback was performed") from error
    result.update(status="installed", installed_path=installed_path, enabled=True, source_assets_present=True, requires_ui_verification=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Diagnose prerequisites without writing or installing")
    parser.add_argument("--codex", metavar="PATH", help="Use this Codex CLI executable; do not fall back")
    options = parser.parse_args(argv)
    try:
        result = install(check=options.check, codex=options.codex)
    except (InstallError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
