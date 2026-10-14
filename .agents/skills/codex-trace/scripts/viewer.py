#!/usr/bin/env python3
"""Manage owned loopback viewers for a Codex thread or the session overview."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

PROJECT = Path(__file__).resolve().parents[4]
TIMEOUT = 10
OVERVIEW = "_all"


class ControllerError(Exception):
    pass


def thread_id(explicit):
    value = explicit if explicit is not None else os.environ.get("CODEX_THREAD_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ControllerError("需要有效 CODEX_THREAD_ID；禁止按最近记录或目录猜测会话。")
    return value


def fingerprint():
    digest = hashlib.sha256()
    for name in ("server.py", "index.html"):
        digest.update(name.encode())
        digest.update((PROJECT / name).read_bytes())
    return digest.hexdigest()


def state_dir():
    root = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser().resolve()
    directory = root / "trace-viewer"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


@contextmanager
def locked(directory, thread):
    descriptor = os.open(str(directory / (thread + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + TIMEOUT
        while True:
            stream.seek(0)
            try:
                acquire()
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise ControllerError("等待轨迹服务锁超时；请稍后重试。") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            release()


def request_json(url, token=None):
    request = Request(url, method="POST" if token else "GET")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    # Loopback control never uses network proxies configured on the host.
    with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
        content = response.read(16385)
    if len(content) > 16384:
        raise ControllerError("服务控制响应超过限制。")
    try:
        result = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise ControllerError("服务控制响应不是有效 JSON。") from error
    if not isinstance(result, dict):
        raise ControllerError("服务控制响应必须是对象。")
    return result


def load_registry(path, thread):
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise ControllerError("服务登记文件超过限制，无法安全使用。")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not all(isinstance(record.get(key), str)
                for key in ("url", "instance_id", "control_token", "fingerprint")):
            raise ValueError("invalid registry field types")
        if type(record.get("protocol")) is not int:
            raise ValueError("invalid protocol type")
        url = urlsplit(record["url"])
        valid = (record["protocol"] == 1 and record["thread_id"] == thread and
                 url.scheme == "http" and url.hostname == "127.0.0.1" and
                 url.port is not None and 1 <= url.port <= 65535 and
                 not url.path and not url.query and not url.fragment and not url.username and
                 re.fullmatch(r"[a-f0-9]{16,128}", record["instance_id"]) and
                 re.fullmatch(r"[a-f0-9]{64}", record["control_token"]) and
                 re.fullmatch(r"[a-f0-9]{64}", record["fingerprint"]))
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ControllerError("服务登记文件损坏，无法安全使用。") from error
    if not valid:
        raise ControllerError("服务登记文件身份或地址无效，无法安全使用。")
    return record


def health(record):
    try:
        result = request_json(record["url"] + "/api/health")
    except URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            return None
        raise ControllerError("服务不可确认；不会另起服务或按 PID 停止。") from error
    except (TimeoutError, OSError) as error:
        raise ControllerError("服务不可确认；不会另起服务或按 PID 停止。") from error
    expected = {"app": "codex-trace-viewer", "protocol": 1,
                "thread_id": record["thread_id"], "instance_id": record["instance_id"]}
    if any(result.get(key) != value for key, value in expected.items()):
        raise ControllerError("端口上的服务身份不匹配；不会停止该服务。")
    return result


def save_registry(path, record):
    descriptor, name = tempfile.mkstemp(prefix=path.stem + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def launch(directory, thread):
    token = secrets.token_hex(32)
    key = thread if thread is not None else OVERVIEW
    log_path = directory / (key + "." + secrets.token_hex(8) + ".log")
    descriptor = os.open(str(log_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    environment = dict(os.environ, CODEX_HOME=str(directory.parent), CTV_CONTROL_TOKEN=token)
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with os.fdopen(descriptor, "wb") as output:
        command = [sys.executable, "-u", str(PROJECT / "server.py"), "--port", "0"]
        if thread is not None:
            command += ["--thread", thread]
        process = subprocess.Popen(command,
                                   cwd=str(PROJECT), env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, **options)
    deadline = time.monotonic() + TIMEOUT
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ControllerError("服务启动失败；请检查轨迹服务日志：" + str(log_path))
            with log_path.open("rb") as stream:
                line = stream.readline(1024).decode("utf-8", errors="replace")
            found = re.search(r"http://127\.0\.0\.1:[0-9]+", line)
            if found:
                try:
                    status = request_json(found.group(0) + "/api/health")
                except URLError:
                    time.sleep(0.05)
                    continue
                instance = status.get("instance_id", "")
                if (status.get("app") != "codex-trace-viewer" or status.get("protocol") != 1 or
                        status.get("thread_id") != thread or not re.fullmatch(r"[a-f0-9]{16,128}", instance)):
                    raise ControllerError("新启动服务的身份不匹配。")
                return {"protocol": 1, "thread_id": thread, "url": found.group(0),
                        "instance_id": instance, "control_token": token, "fingerprint": fingerprint()}
            time.sleep(0.05)
        raise ControllerError("等待服务就绪超时；请检查轨迹服务日志：" + str(log_path))
    except BaseException:
        if process.poll() is None:
            process.terminate()  # Only this just-created child; never a registry PID or reused port.
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise


def control(command, thread):
    directory = state_dir()
    key = thread if thread is not None else OVERVIEW
    path = directory / (key + ".json")
    with locked(directory, key):
        record = load_registry(path, thread)
        running = health(record) if record else None
        if command == "status":
            return {"thread_id": thread, "running": running is not None,
                    "url": record["url"] if running else None,
                    "stale": record is not None and running is None}
        if command == "stop":
            if running:
                request_json(record["url"] + "/api/shutdown", record["control_token"])
                deadline = time.monotonic() + TIMEOUT
                while True:
                    try:
                        if health(record) is None:
                            break
                    except ControllerError as error:
                        # Listener shutdown can reset a request before refusing new ones.
                        if time.monotonic() >= deadline:
                            raise ControllerError("服务停止状态不可确认；保留登记信息供重试。") from error
                    if time.monotonic() >= deadline:
                        raise ControllerError("服务停止超时；保留登记信息供重试。")
                    time.sleep(0.05)
            if record:
                path.unlink()
            return {"thread_id": thread, "stopped": running is not None,
                    "stale_removed": record is not None and running is None}
        if running:
            if record["fingerprint"] != fingerprint():
                raise ControllerError("源码已更新；请先停止本会话服务，再重新启动。")
            reused = True
        else:
            if record:
                print("旧轨迹服务已不可达，重新启动；未停止任何其他进程。", file=sys.stderr)
                path.unlink()
            record = launch(directory, thread)
            try:
                save_registry(path, record)
            except OSError:
                request_json(record["url"] + "/api/shutdown", record["control_token"])
                raise
            reused = False
        return {"url": record["url"], "thread_id": thread,
                "instance_id": record["instance_id"], "reused": reused}


def stop_all():
    directory = state_dir()
    results, errors = [], []
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_symlink():
                raise ControllerError("登记文件是符号链接，无法安全使用。")
            thread = None if path.stem == OVERVIEW else thread_id(path.stem)
            results.append(control("stop", thread))
        except (ControllerError, OSError, ValueError) as error:
            errors.append({"service": path.stem, "error": str(error)})
    return {"results": results, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="打开、查看或停止会话轨迹服务")
    parser.add_argument("command", choices=("start", "status", "stop", "start-all", "stop-all"))
    parser.add_argument("--thread", help="显式指定已授权会话；默认使用 CODEX_THREAD_ID")
    args = parser.parse_args()
    if args.command.endswith("-all") and args.thread is not None:
        parser.error("全部操作禁止使用 --thread")
    try:
        if args.command == "start-all":
            result = control("start", None)
        elif args.command == "stop-all":
            result = stop_all()
        else:
            result = control(args.command, thread_id(args.thread))
    except (ControllerError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    failed = args.command == "stop-all" and bool(result["errors"])
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr if failed else sys.stdout)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
