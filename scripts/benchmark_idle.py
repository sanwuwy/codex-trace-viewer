"""macOS MCP idle audit; synthetic records only, no running Codex processes touched.

python3 scripts/benchmark_idle.py /path/to/unpacked/baseline --seconds 32 --repeats 3
RSS includes shared pages; physical footprint and interval CPU are reported separately.
"""
import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid


class Usage(ctypes.Structure):
    # rusage_info_v2, sys/resource.h in the macOS SDK.
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "user", "system", "idle_wakes", "interrupt_wakes", "pageins", "wired", "rss", "footprint",
        "start", "exit", "child_user", "child_system", "child_idle_wakes", "child_interrupt_wakes",
        "child_pageins", "child_elapsed", "disk_read", "disk_written")]


class TaskInfo(ctypes.Structure):
    # proc_taskinfo / PROC_PIDTASKINFO=4, sys/proc_info.h in the macOS SDK.
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "virtual", "resident", "user", "system", "threads_user", "threads_system")] + [
        (name, ctypes.c_int32) for name in ("policy", "faults", "pageins", "cow", "sent", "received",
        "syscalls_mach", "syscalls_unix", "context_switches", "threads", "running", "priority")]


class Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class Meter:
    def __init__(self):
        if sys.platform != "darwin":
            raise RuntimeError("This resource audit uses macOS libproc; Windows/Linux are not measured")
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.lib.proc_pid_rusage.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        system.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        system.mach_timebase_info.restype = ctypes.c_int
        clock = Timebase()
        if system.mach_timebase_info(ctypes.byref(clock)) != 0 or not clock.denom:
            raise RuntimeError("Could not read Mach timebase")
        self.timebase = [clock.numer, clock.denom]
        self.seconds = clock.numer / clock.denom / 1e9
        before, expected = self.read(os.getpid()), time.process_time()
        until = time.perf_counter() + 0.05
        while time.perf_counter() < until:
            pass
        measured = self.read(os.getpid())["cpu_seconds"] - before["cpu_seconds"]
        expected = time.process_time() - expected
        if abs(measured - expected) > 0.01:
            raise RuntimeError(f"CPU calibration failed: {measured} versus {expected}")
        self.validation = {"measured_seconds": measured, "process_time_seconds": expected}

    def read(self, pid):
        usage, task = Usage(), TaskInfo()
        if self.lib.proc_pid_rusage(pid, 2, ctypes.byref(usage)) != 0:
            raise OSError(ctypes.get_errno(), "proc_pid_rusage")
        if self.lib.proc_pidinfo(pid, 4, 0, ctypes.byref(task), ctypes.sizeof(task)) != ctypes.sizeof(task):
            raise OSError(ctypes.get_errno(), "proc_pidinfo")
        return {"cpu_seconds": (usage.user + usage.system) * self.seconds,
            "child_cpu_seconds": (usage.child_user + usage.child_system) * self.seconds,
            "rss_bytes": usage.rss, "footprint_bytes": usage.footprint, "threads": task.threads,
            "idle_wakes": usage.idle_wakes, "context_switches": task.context_switches}


class Peer:
    def __init__(self, directory, home, mode):
        self.mode, self.sequence = mode, 0
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, "-B", "-u", str(directory / "server.py")],
            env={**os.environ, "CODEX_HOME": str(home), "CODEX_THREAD_ID": "untrusted-environment"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def call(self, method, params=None, error=False):
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(request).encode() + b"\n")
        self.process.stdin.flush()
        if not self.selector.select(65):
            raise TimeoutError(f"{self.mode}: {method} timed out")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"{self.mode}: MCP exited before {method} response")
        response = json.loads(line)
        assert response["id"] == self.sequence, response
        assert ("error" in response) == error, response
        return response.get("error" if error else "result")

    def tool(self, name, thread, view, error=False, **arguments):
        result = self.call("tools/call", {"name": name, "arguments": {"view_id": view,
            "request_nonce": str(uuid.uuid4()), **arguments}, "_meta": {"thread_id": thread, "threadId": thread}}, error)
        return result if error else result["structuredContent"]

    def close(self):
        self.process.stdin.close()
        try:
            status = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("MCP failed to exit on stdin EOF")
        finally:
            self.selector.close()
            self.process.stdout.close()
            self.stderr.close()
        assert status == 0, status


def idle(peers, meter, seconds, phase):
    start = time.monotonic()
    first = {peer.mode: meter.read(peer.process.pid) for peer in peers}
    samples = {peer.mode: [first[peer.mode]] for peer in peers}
    while time.monotonic() - start < seconds:
        time.sleep(min(0.25, max(0, seconds - (time.monotonic() - start))))
        for peer in peers:
            samples[peer.mode].append(meter.read(peer.process.pid))
    elapsed = time.monotonic() - start
    result = {}
    for peer in peers:
        values, before = samples[peer.mode], first[peer.mode]
        after = values[-1]
        cpu = after["cpu_seconds"] - before["cpu_seconds"]
        result[peer.mode] = {"phase": phase, "elapsed_seconds": elapsed, "cpu_seconds": cpu,
            "cpu_percent_one_core": 100 * cpu / elapsed,
            "median_rss_mib": statistics.median(v["rss_bytes"] for v in values) / 1048576,
            "median_footprint_mib": statistics.median(v["footprint_bytes"] for v in values) / 1048576,
            "threads": sorted(set(v["threads"] for v in values)),
            "idle_wakes": after["idle_wakes"] - before["idle_wakes"],
            "context_switches": after["context_switches"] - before["context_switches"]}
    return result


def lifecycle(peer, threads):
    views = [str(uuid.uuid4()), str(uuid.uuid4())]
    resource = peer.call("resources/list")["resources"][0]["uri"]
    assert "<html" in peer.call("resources/read", {"uri": resource})["contents"][0]["text"].lower()
    snapshots = [peer.tool("trace_snapshot", thread, view, format="outline")
        for thread, view in zip(threads, views)]
    def content(index, view=None, snapshot=None):
        source = snapshot or snapshots[index]
        return b"".join(base64.b64decode(peer.tool("trace_chunk", threads[index], view or views[index],
            snapshot_id=source["snapshot_id"], chunk_index=chunk)["data"])
            for chunk in range(source["total_chunks"]))
    original = [content(index) for index in range(2)]
    for index, data in enumerate(original):
        assert threads[index].encode() in data and threads[1 - index].encode() not in data
    assert peer.tool("trace_release", threads[0], views[0])["released"]
    peer.tool("trace_snapshot", threads[0], views[0], error=True, format="outline")
    peer.tool("trace_snapshot", threads[0], views[1], error=True, format="outline")
    assert threads[1].encode() in content(1)
    reopened = str(uuid.uuid4())
    fresh = peer.tool("trace_snapshot", threads[0], reopened, format="outline")
    assert fresh["snapshot_id"] != snapshots[0]["snapshot_id"]
    assert content(0, reopened, fresh) == original[0]
    assert peer.tool("trace_release", threads[0], reopened)["released"]
    assert peer.tool("trace_release", threads[1], views[1])["released"]
    peer.call("ping")
    return {"release_isolation": True, "late_request_rejected": True,
        "cross_owner_rejected": True, "reopen_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--current", type=Path, default=Path(__file__).resolve().parents[1] / "plugins/codex-trace-probe")
    parser.add_argument("--seconds", type=float, default=32)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("local-previews/idle-numeric-metrics.json"))
    args = parser.parse_args()
    if not 0 < args.seconds <= 300 or not 1 <= args.repeats <= 10:
        parser.error("seconds must be in (0,300], repeats must be in [1,10]")
    meter, runs = Meter(), []
    with tempfile.TemporaryDirectory(prefix="codex trace idle ") as directory:
        home = Path(directory)
        frozen = {}
        sources = {}
        for mode, source in (("old", args.baseline), ("new", args.current)):
            target = home / "plugins" / mode
            shutil.copytree(source.resolve(), target, ignore=shutil.ignore_patterns("__pycache__"))
            frozen[mode] = target
            sources[mode] = {"files_sha256": {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(target.rglob("*")) if path.is_file()}}
        (home / "sessions").mkdir()
        threads = [str(uuid.uuid4()), str(uuid.uuid4())]
        for thread in threads:
            records = [{"type": "session_meta", "payload": {"id": thread, "cwd": str(home)}},
                {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": thread}]}}]
            (home / "sessions" / ("rollout-" + thread + ".jsonl")).write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        for repeat in range(args.repeats):
            peers = []
            try:
                directories = list(frozen.items())
                for mode, path in (directories if repeat % 2 == 0 else directories[::-1]):
                    peer = Peer(path.resolve(), home, mode)
                    peers.append(peer)
                    initialized = peer.call("initialize", {"protocolVersion": "2025-11-25"})
                    peer.version = initialized["serverInfo"]["version"]
                    assert len(peer.call("tools/list")["tools"]) == 6
                    assert len(peer.call("resources/list")["resources"]) == 1
                initial = idle(peers, meter, args.seconds, "never_opened")
                checks = {peer.mode: lifecycle(peer, threads) for peer in peers}
                released = idle(peers, meter, args.seconds, "opened_then_released")
                for peer in peers:
                    value = {"repeat": repeat + 1, "mode": peer.mode, "version": peer.version,
                        "initial": initial[peer.mode], "released": released[peer.mode], **checks[peer.mode]}
                    runs.append(value)
                    print(json.dumps(value), flush=True)
            finally:
                for peer in peers:
                    peer.close()
    report = {"python": sys.version.split()[0], "mach_timebase": meter.timebase, "sources": sources,
        "cpu_validation": meter.validation, "runs": runs,
        "limits": ["macOS synthetic stdio MCP only; frontend, desktop and GUI are excluded",
            "RSS contains shared pages; physical footprint is reported separately",
            "Both versions idle concurrently; pair launch order alternates per repeat",
            "No schema requests: this measures startup and normal trace snapshot/release only"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
