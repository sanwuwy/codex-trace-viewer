#!/usr/bin/env python3
"""Read-only session trace scoped by per-call identity; no HTTP service is started.

UI metadata: https://developers.openai.com/plugins/build/extensions
Anonymous IDs: https://developers.openai.com/plugins/reference
thread_id/threadId are observed host fields, not a public API guarantee.
"""
from contextlib import ExitStack, contextmanager
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time

TRACE_RESOURCE = "ui://codex-trace-probe/trace-v11.html"
MAX_MESSAGE = 1024 * 1024
MAX_HEADER = 256 * 1024
CHUNK_BYTES = 384 * 1024
MAX_SAFE_INTEGER = 9007199254740991
IDLE_TTL = 30
SWEEP_INTERVAL = 5
RELEASE_GUARD_TTL = 300
MAX_RELEASE_GUARDS = 1024
MAX_DETAIL_RECORDS = 8192
OUTLINE_TIMEOUT = 60
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class ArgumentError(ValueError):
    pass


def valid_id(value):
    return isinstance(value, str) and UUID.fullmatch(value) is not None


def signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@contextmanager
def open_record(root, path):
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("Platform cannot guarantee a no-follow metadata read")
    with ExitStack() as descriptors:
        folder = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.callback(os.close, folder)
        for part in root.parts[1:]:
            folder = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            descriptors.callback(os.close, folder)
        parts = path.relative_to(root).parts
        for part in parts[:-1]:
            folder = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            descriptors.callback(os.close, folder)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Record must be a regular file")
            yield stream
            after = os.fstat(stream.fileno())
            entry = os.stat(parts[-1], dir_fd=folder, follow_symlinks=False)
        if signature(before) != signature(after) or signature(after) != signature(entry):
            raise ValueError("Record changed during the read; retry the snapshot")


def read_metadata(root, path):
    with open_record(root, path) as stream:
        line = stream.readline(MAX_HEADER + 1)
        if len(line) > MAX_HEADER:
            raise ValueError("Metadata exceeded the limit")
        result = json.loads(line)
    return result


def record_identity(thread):
    """Only inspect the first metadata line of matching filenames, not messages."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    matches, warnings = [], []
    for folder in (home / "sessions", home / "archived_sessions"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*" + thread + "*.jsonl"):
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                warnings.append("拒绝读取目录之外或符号链接记录")
                continue
            try:
                item = read_metadata(folder, path)
                payload = item.get("payload") if isinstance(item, dict) else None
                if (isinstance(item, dict) and item.get("type") == "session_meta" and isinstance(payload, dict) and
                        payload.get("id") == thread):
                    matches.append(payload.get("cwd"))
            except (OSError, ValueError, RecursionError) as error:
                warnings.append("首条元信息读取失败：" + type(error).__name__)
    return matches, warnings


def inspect_identity(params, records=True):
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    fields, candidates, warnings = [], [], []
    nested = meta.get("x-codex-turn-metadata")
    invalid_container = False
    if isinstance(nested, str) and len(nested) <= 8192:
        try:
            nested = json.loads(nested)
        except (ValueError, RecursionError):
            warnings.append("嵌套身份元数据不是有效 JSON")
            invalid_container = True
    if not isinstance(nested, dict):
        if "x-codex-turn-metadata" in meta:
            fields.append("_meta.x-codex-turn-metadata")
            invalid_container = True
            warnings.append("已提供的身份容器无效或超过长度边界，拒绝绑定")
        nested = {}
    for source, container, key in (("_meta.thread_id", meta, "thread_id"),
                                   ("_meta.threadId", meta, "threadId"),
                                   ("_meta.x-codex-turn-metadata.thread_id", nested, "thread_id")):
        if key in container:
            fields.append(source)
            candidates.append(container[key])
    ids = {value.lower() for value in candidates if valid_id(value)}
    conflict = invalid_container or len(ids) > 1 or len(candidates) != sum(valid_id(value) for value in candidates)
    thread = next(iter(ids)) if len(ids) == 1 and not conflict else None
    matches, record_warnings = record_identity(thread) if thread and records else ([], [])
    warnings += record_warnings
    if len(matches) > 1:
        conflict, thread = True, None
        warnings.append("存在多个同 ID 元信息记录，未确认绑定")
    cwd = matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None
    if cwd and len(cwd) > 4096:
        cwd = None
        warnings.append("记录中的工作目录过长，未确认项目类型")
    git = any((parent / ".git").exists() for parent in (Path(cwd), *Path(cwd).parents)) if cwd and Path(cwd).is_absolute() else None
    status = "conflict" if conflict else "verified" if matches else "candidate" if thread else "missing"
    if status != "verified":
        warnings.append("尚未取得与保存记录一致的每请求会话 ID，禁止接入轨迹")
    warnings.append("记录匹配仅验证元信息，不等同于页面功能验收")
    return {"thread_id": thread, "identity_status": status,
            "identity_source": ", ".join(fields) or None, "cwd": cwd,
            "git_repository": git, "snapshot_match": bool(matches) if thread else None,
            "warnings": warnings}, {"sources": fields, "fields": fields,
            "environment_thread_id_present": bool(os.environ.get("CODEX_THREAD_ID")),
            "anonymous_session_present": "openai/session" in meta}


def synchronized(method):
    @wraps(method)
    def invoke(self, *arguments, **keywords):
        with self.lock:
            return method(self, *arguments, **keywords)
    return invoke


def project_record(record):
    """Remove large detail-only bodies; retain every field used by round statistics."""
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return record
    payload = record["payload"]
    if record.get("type") == "compacted":
        payload.pop("replacement_history", None)
        payload.pop("retained_context", None)
    if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict):
            if item.get("type") == "CommandExecution" or isinstance(item.get("command"), list):
                for key in ("stdout", "stderr", "aggregated_output", "formatted_output"):
                    item.pop(key, None)
            if item.get("type") == "McpToolCall" and isinstance(item.get("result"), dict):
                item["result"] = {key: value for key, value in item["result"].items() if key == "isError"}
    return record


def build_outline(source, start_index=0, source_offset=0):
    """Index physical source lines while writing a disk-backed lightweight projection."""
    import tempfile
    output = tempfile.TemporaryFile(mode="w+b")
    offsets, skipped = {}, 0
    def invalid_constant(value):
        raise ValueError("Non-JSON numeric constant: " + value)
    decoder = json.JSONDecoder(parse_constant=invalid_constant)
    try:
        source.seek(0)
        index = start_index
        while True:
            offset = source.tell()
            line = source.readline()
            if not line:
                break
            length = len(line)
            text = line.decode("utf-8", errors="replace")
            del line
            start, end = 0, len(text)
            while start < end and text[start] in " \t\r\n":
                start += 1
            while end > start and text[end - 1] in " \t\r\n":
                end -= 1
            record = None
            try:
                if end - start >= 2:
                    record, parsed_end = decoder.raw_decode(text, start)
                    if parsed_end != end:
                        raise ValueError("Trailing content after JSON record")
            except (ValueError, RecursionError):
                skipped += 1
                index += 1
                del text, record
                continue
            del text
            if end - start >= 2:
                envelope = {"index": index, "record": project_record(record)}
                data = (json.dumps(envelope, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
                del record, envelope
                if output.write(data) != len(data):
                    raise OSError("Incomplete outline write")
                offsets[index] = (source_offset + offset, length)
                del data
            else:
                del record
            index += 1
        size = output.tell()
        if size > MAX_SAFE_INTEGER:
            raise ValueError("Outline exceeds the safe byte-offset range")
        output.seek(0)
        return output, offsets, size, skipped, index - start_index
    except BaseException:
        output.close()
        raise


def outline_worker(start_index=0, source_offset=0):
    """A short-lived process owns the transient heap of large JSON records."""
    try:
        output, offsets, size, skipped, line_count = build_outline(sys.stdin.buffer, start_index, source_offset)
        with output:
            while True:
                block = output.read(CHUNK_BYTES)
                if not block:
                    break
                if sys.stdout.buffer.write(block) != len(block):
                    raise OSError("Incomplete outline transfer")
            sys.stdout.buffer.flush()
        peak = None
        try:
            import resource
        except ImportError:
            pass
        else:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform != "darwin":
                peak *= 1024
        metadata = {"offsets": [[index, *position] for index, position in offsets.items()],
            "total_bytes": size, "source_bytes": os.fstat(sys.stdin.fileno()).st_size,
            "skipped_records": skipped, "line_count": line_count, "start_index": start_index,
            "source_offset": source_offset, "worker_pid": os.getpid(), "worker_peak_rss_bytes": peak,
            "worker_cpu_seconds": time.process_time()}
        sys.stderr.write(json.dumps(metadata, ensure_ascii=True, separators=(",", ":")))
        sys.stderr.flush()
        return 0
    except (OSError, ValueError, RecursionError) as error:
        sys.stderr.write(json.dumps({"error": type(error).__name__}))
        sys.stderr.flush()
        return 1


def build_outline_isolated(source, start_index=0, source_offset=0):
    import subprocess
    import tempfile
    output = tempfile.TemporaryFile(mode="w+b", buffering=0)
    try:
        with tempfile.TemporaryFile(mode="w+b") as metadata_file:
            source.seek(0)
            try:
                # run() kills and waits for its child before raising TimeoutExpired.
                # https://docs.python.org/3.9/library/subprocess.html#subprocess.run
                completed = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--outline-worker",
                    str(start_index), str(source_offset)],
                    stdin=source, stdout=output, stderr=metadata_file, timeout=OUTLINE_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise OSError("Outline worker timed out after " + str(OUTLINE_TIMEOUT) + " seconds") from error
            metadata_file.seek(0)
            try:
                metadata = json.load(metadata_file)
            except (ValueError, UnicodeError) as error:
                raise OSError("Incomplete outline worker metadata; exit code " + str(completed.returncode)) from error
        if not isinstance(metadata, dict):
            raise ValueError("Invalid outline worker metadata")
        if completed.returncode != 0:
            raise OSError("Outline worker failed: " + str(metadata.get("error", "exit code " + str(completed.returncode))))
        size = os.fstat(output.fileno()).st_size
        source_size = os.fstat(source.fileno()).st_size
        entries = metadata.get("offsets")
        skipped = metadata.get("skipped_records")
        line_count = metadata.get("line_count")
        if (not isinstance(entries, list) or metadata.get("source_bytes") != source_size or
                metadata.get("total_bytes") != size or type(skipped) is not int or skipped < 0 or
                type(line_count) is not int or not 0 <= line_count <= MAX_SAFE_INTEGER - start_index or
                skipped + len(entries) > line_count or metadata.get("start_index") != start_index or
                metadata.get("source_offset") != source_offset):
            raise ValueError("Invalid outline worker metadata")
        offsets, previous_index, previous_end = {}, start_index - 1, source_offset
        for item in entries:
            if (not isinstance(item, list) or len(item) != 3 or any(type(value) is not int for value in item) or
                    not previous_index < item[0] < start_index + line_count or
                    not previous_end <= item[1] < item[1] + item[2] <= source_offset + source_size):
                raise ValueError("Invalid outline worker record index")
            index, offset, length = item
            offsets[index] = (offset, length)
            previous_index, previous_end = index, offset + length
        output.seek(0)
        return output, offsets, size, skipped, metadata
    except BaseException:
        output.close()
        raise


class TraceStore:
    """Disk-backed snapshots; the lock covers complete reads, copies and teardown."""
    def __init__(self):
        self.home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        self.views = {}
        self.released = {}
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.worker = None
        self.closed = False

    @synchronized
    def start(self):
        if self.closed:
            raise ValueError("Trace store is closed")
        if self.views and self.worker is None:
            self.worker = threading.Thread(target=self._cleanup_loop, name="trace-snapshot-cleanup", daemon=True)
            try:
                self.worker.start()
            except BaseException as error:
                self.worker = None
                if isinstance(error, RuntimeError):
                    raise OSError("Could not start trace cleanup thread") from error
                raise
        self.changed.notify()

    def _cleanup_loop(self):
        with self.changed:
            try:
                while self.views and not self.closed:
                    self.changed.wait(SWEEP_INTERVAL)
                    if self.closed:
                        break
                    try:
                        self.sweep()
                    except OSError as error:
                        print(json.dumps({"event": "trace_cleanup_error", "type": type(error).__name__}), file=sys.stderr)
            finally:
                self.worker = None

    @synchronized
    def sweep(self):
        now = time.monotonic()
        for key, value in list(self.views.items()):
            if now - value["seen"] > IDLE_TTL:
                self._drop(key)
        self._expire_guards(now)

    def _expire_guards(self, now):
        for key, (_, expires) in list(self.released.items()):
            if now >= expires:
                del self.released[key]

    def _drop(self, view_id):
        view = self.views[view_id]
        for key in ("snapshot_file", "outline_file", "detail_file"):
            if view.get(key):
                view[key].close()
        del self.views[view_id]
        if not self.views:
            self.changed.notify()

    @synchronized
    def view(self, thread, view_id):
        if self.closed:
            raise ValueError("Trace store is closed")
        self.sweep()
        now = time.monotonic()
        if view_id in self.released:
            raise ValueError("Trace page was released; reopen with a new page ID")
        created = view_id not in self.views
        if created:
            if len(self.views) >= 8:
                raise ValueError("Eight active trace pages already exist; close a page and retry")
            # Reserve one guard slot for every active page. Never evict a live
            # release guard to make room for an old queued request.
            if len(self.views) + len(self.released) >= MAX_RELEASE_GUARDS:
                raise ValueError("Trace page lifecycle limit reached; retry after five minutes")
            self.views[view_id] = {"thread_id": thread, "seen": now}
        view = self.views[view_id]
        if view["thread_id"] != thread:
            raise ValueError("Page identity changed; reopen the trace tab")
        view["seen"] = now
        if self.worker is None:
            try:
                self.start()
            except BaseException:
                if created:
                    self._drop(view_id)
                raise
        return view

    @synchronized
    def release(self, thread, view_id):
        if self.closed:
            return False
        self._expire_guards(time.monotonic())
        view = self.views.get(view_id)
        guard = self.released.get(view_id)
        owner = view["thread_id"] if view else guard[0] if guard else None
        if owner is not None and owner != thread:
            raise ValueError("Page belongs to another thread; no snapshot was released")
        if guard is not None:
            return False
        if view is None:
            if len(self.views) + len(self.released) >= MAX_RELEASE_GUARDS:
                raise ValueError("Trace page lifecycle limit reached; retry after five minutes")
        else:
            self._drop(view_id)
        self.released[view_id] = (thread, time.monotonic() + RELEASE_GUARD_TTL)
        return view is not None

    @synchronized
    def schema(self, thread, view_id, lookup):
        view = self.view(thread, view_id)
        try:
            return lookup()
        finally:
            view["seen"] = time.monotonic()

    def close(self):
        with self.lock:
            self.closed = True
            self.changed.notify()
            for view_id in list(self.views):
                self._drop(view_id)
            self.released.clear()
            worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    @synchronized
    def locate(self, thread, view):
        if view.get("path"):
            try:
                entry = view["path"].lstat()
                if not stat.S_ISREG(entry.st_mode):
                    view.pop("path")
                    view["scan_after"] = 0
                    raise ValueError("Record path is no longer a regular file")
                return view["root"], view["path"], entry
            except FileNotFoundError:
                view.pop("path")
        directories = view.get("directories", {})
        def directory_signature(path):
            try:
                return signature(path.stat())
            except FileNotFoundError:
                return None
        if (time.monotonic() < view.get("scan_after", 0) and
                all(directory_signature(path) == value for path, value in directories.items())):
            return None, None, None
        matches, directories = [], {}
        def fail(error):
            raise error
        for root in (self.home / "sessions", self.home / "archived_sessions"):
            directories[root] = directory_signature(root)
            if directories[root] is None:
                continue
            for folder, _, names in os.walk(root, onerror=fail):
                directories[Path(folder)] = directory_signature(Path(folder))
                for name in names:
                    if thread not in name or not name.endswith(".jsonl"):
                        continue
                    path = Path(folder) / name
                    item = read_metadata(root, path)
                    meta = item.get("payload") if isinstance(item, dict) else None
                    if isinstance(meta, dict) and item.get("type") == "session_meta" and meta.get("id") == thread:
                        matches.append((root, path, path.lstat()))
        if len(matches) > 1:
            raise ValueError("Multiple files match this thread; no trace was returned")
        view.update(directories=directories, scan_after=time.monotonic() + 10)
        if not matches:
            return None, None, None
        root, path, entry = matches[0]
        view.update(root=root, path=path)
        return root, path, entry

    @synchronized
    def snapshot(self, thread, view_id, known_revision, snapshot_format="full"):
        import hashlib
        import tempfile
        import uuid
        if snapshot_format not in ("full", "outline"):
            raise ValueError("Snapshot format must be full or outline")
        view = self.view(thread, view_id)
        root, path, entry = self.locate(thread, view)
        if path is None:
            return {"status": "waiting", "identity_status": "candidate", "snapshot_match": False, "truncated": False}
        format_changed = view.get("format", "full") != snapshot_format
        if view.get("file_signature") != signature(entry) or format_changed:
            snapshot_file = tempfile.TemporaryFile(mode="w+b", buffering=0)
            outline_file = None
            try:
                with open_record(root, path) as stream:
                    entry = os.fstat(stream.fileno())
                    if not 0 <= entry.st_size <= MAX_SAFE_INTEGER:
                        raise ValueError("File size exceeds the safe byte-offset range")
                    header = stream.readline(MAX_HEADER + 1)
                    if len(header) > MAX_HEADER:
                        raise ValueError("Metadata exceeded the limit")
                    record = json.loads(header)
                    meta = record.get("payload") if isinstance(record, dict) else None
                    if not isinstance(meta, dict) or record.get("type") != "session_meta" or meta.get("id") != thread:
                        view.pop("path", None)
                        view["scan_after"] = 0
                        raise ValueError("Record identity changed; no trace was returned")
                    old_size = view.get("source_bytes", view.get("total_bytes", 0))
                    append = (not format_changed and
                        known_revision == view.get("revision") and view.get("ends_newline") and
                        view.get("session", {}).get("path") == str(path) and
                        view.get("file_signature", ())[:2] == signature(entry)[:2] and
                        0 < old_size < entry.st_size)
                    if append and snapshot_format == "outline":
                        stream.seek(entry.st_size - 1)
                        append = stream.read(1) == b"\n"
                    digest, prefix = hashlib.sha256(), hashlib.sha256()
                    copied, ends_newline = 0, False
                    stream.seek(0)
                    # Verify the source prefix without parsing or copying its outline/raw snapshot.
                    if append and snapshot_format == "outline":
                        while copied < old_size:
                            block = stream.read(min(CHUNK_BYTES, old_size - copied))
                            if not block:
                                raise ValueError("Record shortened during the read; retry the snapshot")
                            digest.update(block)
                            copied += len(block)
                        append = digest.hexdigest() == view.get("digest")
                        if not append:
                            digest, copied = hashlib.sha256(), 0
                            stream.seek(0)
                    while copied < entry.st_size:
                        block = stream.read(min(CHUNK_BYTES, entry.st_size - copied))
                        if not block:
                            raise ValueError("Record shortened during the read; retry the snapshot")
                        if snapshot_file.write(block) != len(block):
                            raise OSError("Incomplete snapshot write")
                        digest.update(block)
                        if append and snapshot_format == "full" and copied < old_size:
                            prefix.update(block[:min(len(block), old_size - copied)])
                        copied += len(block)
                        ends_newline = block.endswith(b"\n")
                snapshot_file.seek(0)
                append = bool(append and (snapshot_format == "outline" or prefix.hexdigest() == view.get("digest")))
                revision = hashlib.sha256(repr((str(path), signature(entry), digest.hexdigest())).encode()).hexdigest()
                viewer_meta = {"path": str(path), "name": path.name, "session_id": thread, "thread_id": thread,
                    "revision": revision, "size": entry.st_size, "mtime": entry.st_mtime,
                    "cwd": meta.get("cwd") or "", "model_provider": meta.get("model_provider") or "",
                    "originator": meta.get("originator") or "", "cli_version": meta.get("cli_version") or "",
                    "archived": root.name == "archived_sessions"}
                offsets, skipped, total_bytes = {}, 0, copied
                worker = {}
                outline_append = append and snapshot_format == "outline"
                if snapshot_format == "outline":
                    outline_file, offsets, total_bytes, skipped, worker = build_outline_isolated(snapshot_file,
                        view["source_line_count"] if outline_append else 0, old_size if outline_append else 0)
                old_outline_size = view.get("total_bytes", 0)
                line_count = worker.get("line_count", 0)
                if outline_append:
                    offsets = {**view["record_offsets"], **offsets}
                    skipped += view["skipped_records"]
                    total_bytes += old_outline_size
                    line_count += view["source_line_count"]
                    if total_bytes > MAX_SAFE_INTEGER:
                        raise ValueError("Outline exceeds the safe byte-offset range")
                    targets = ((snapshot_file, view["snapshot_file"], old_size, copied - old_size),
                        (outline_file, view["outline_file"], old_outline_size, total_bytes - old_outline_size))
                    try:
                        for source, target, start, length in targets:
                            source.seek(0)
                            target.seek(start)
                            written = 0
                            while written < length:
                                block = source.read(min(CHUNK_BYTES, length - written))
                                if not block:
                                    raise OSError("Incomplete incremental snapshot read")
                                if target.write(block) != len(block):
                                    raise OSError("Incomplete incremental snapshot write")
                                written += len(block)
                            target.truncate(start + length)
                            target.flush()
                    except BaseException as error:
                        failures = []
                        for _, target, start, _ in targets:
                            try:
                                # Files are unbuffered, so truncation cannot retry a dirty-tail flush.
                                os.ftruncate(target.fileno(), start)
                                target.seek(start)
                            except OSError as cleanup_error:
                                failures.append(str(cleanup_error))
                        if failures:
                            raise OSError("Incremental snapshot rollback failed: " + "; ".join(failures)) from error
                        raise
                previous = [view.get(key) for key in ("snapshot_file", "outline_file", "detail_file")]
                view.update(file_signature=signature(entry), snapshot_file=previous[0] if outline_append else snapshot_file,
                    snapshot_id=str(uuid.uuid4()), revision=revision, session=viewer_meta,
                    total_bytes=total_bytes, source_bytes=copied, format=snapshot_format,
                    outline_file=previous[1] if outline_append else outline_file,
                    record_offsets=offsets, source_line_count=line_count, skipped_records=skipped, detail_file=None, detail_id=None,
                    outline_worker_pid=worker.get("worker_pid"), outline_worker_peak_rss_bytes=worker.get("worker_peak_rss_bytes"),
                    outline_worker_cpu_seconds=worker.get("worker_cpu_seconds"),
                    chunk_bytes=CHUNK_BYTES, digest=digest.hexdigest(), ends_newline=ends_newline,
                    start_offset=(old_outline_size if outline_append else old_size) if append else 0,
                    base_revision=view.get("revision") if append else None)
                if not outline_append:
                    snapshot_file = None
                    outline_file = None
                for item in previous[2:] if outline_append else previous:
                    if item:
                        item.close()
            finally:
                if snapshot_file is not None:
                    snapshot_file.close()
                if outline_file is not None:
                    outline_file.close()
        status = "unchanged" if known_revision == view["revision"] and not format_changed else "ready"
        if status != "unchanged" and view["start_offset"]:
            if known_revision == view["base_revision"]:
                status = "delta"
            else:
                view.update(snapshot_id=str(uuid.uuid4()), start_offset=0, base_revision=None)
        start = view["total_bytes"] if status == "unchanged" else view["start_offset"]
        count = (view["total_bytes"] - start + view["chunk_bytes"] - 1) // view["chunk_bytes"]
        view["seen"] = time.monotonic()
        return {"status": status,
            "identity_status": "verified", "snapshot_match": True, "session": view["session"],
            "snapshot_id": view["snapshot_id"], "total_chunks": count, "total_bytes": view["total_bytes"],
            "source_bytes": view["source_bytes"], "format": view["format"], "skipped_records": view["skipped_records"],
            "record_count": len(view["record_offsets"]) if view["format"] == "outline" else None,
            "start_offset": start, "chunk_bytes": view["chunk_bytes"],
            "base_revision": None if status == "unchanged" else view["base_revision"], "truncated": False}

    @synchronized
    def detail(self, thread, view_id, snapshot_id, record_indices):
        import tempfile
        import uuid
        view = self.view(thread, view_id)
        if snapshot_id != view.get("snapshot_id") or view.get("format") != "outline":
            raise ValueError("Outline snapshot expired or belongs to another page; refresh the trace")
        if (not isinstance(record_indices, list) or not 1 <= len(record_indices) <= MAX_DETAIL_RECORDS or
                any(type(index) is not int or not 0 <= index <= MAX_SAFE_INTEGER for index in record_indices) or
                len(set(record_indices)) != len(record_indices)):
            raise ValueError("Detail records must be unique non-negative safe integers within the request limit")
        if any(index not in view["record_offsets"] for index in record_indices):
            raise ValueError("Detail record does not exist in the outline")
        detail_file = tempfile.TemporaryFile(mode="w+b")
        try:
            for index in record_indices:
                offset, length = view["record_offsets"][index]
                view["snapshot_file"].seek(offset)
                raw = view["snapshot_file"].read(length)
                if len(raw) != length:
                    raise OSError("Detail source read was incomplete")
                parts = ('{"index":' + str(index) + ',"record":').encode("ascii"), raw.strip(), b"}\n"
                for data in parts:
                    if detail_file.write(data) != len(data):
                        raise OSError("Incomplete detail write")
            size = detail_file.tell()
            if size > MAX_SAFE_INTEGER:
                raise ValueError("Detail exceeds the safe byte-offset range")
            detail_file.seek(0)
            previous = view.get("detail_file")
            view.update(detail_file=detail_file, detail_id=str(uuid.uuid4()), detail_bytes=size)
            detail_file = None
            if previous:
                previous.close()
        finally:
            if detail_file is not None:
                detail_file.close()
        view["seen"] = time.monotonic()
        return {"snapshot_id": snapshot_id, "revision": view["revision"], "detail_id": view["detail_id"],
            "total_bytes": size, "total_chunks": (size + view["chunk_bytes"] - 1) // view["chunk_bytes"],
            "chunk_bytes": view["chunk_bytes"], "record_indices": record_indices, "record_count": len(record_indices)}

    @synchronized
    def chunk(self, thread, view_id, snapshot_id, index, detail_id=None):
        import base64
        view = self.view(thread, view_id)
        if snapshot_id != view.get("snapshot_id"):
            raise ValueError("Snapshot expired or belongs to another page; refresh the trace")
        if detail_id is not None:
            if detail_id != view.get("detail_id"):
                raise ValueError("Detail expired or belongs to another page; select the round again")
            source, total, start = view["detail_file"], view["detail_bytes"], 0
        else:
            source = view["outline_file"] if view["format"] == "outline" else view["snapshot_file"]
            total, start = view["total_bytes"], view["start_offset"]
        count = (total - start + view["chunk_bytes"] - 1) // view["chunk_bytes"]
        if type(index) is not int or not 0 <= index <= MAX_SAFE_INTEGER or index >= count:
            raise ValueError("Chunk index exceeds the snapshot")
        offset = start + index * view["chunk_bytes"]
        length = min(view["chunk_bytes"], total - offset)
        source.seek(offset)
        data = source.read(length)
        if len(data) != length:
            raise OSError("Snapshot read was incomplete")
        view["seen"] = time.monotonic()
        return {"snapshot_id": snapshot_id, "revision": view["revision"], "chunk_index": index,
            **({"detail_id": detail_id} if detail_id is not None else {}),
            "encoding": "base64", "data": base64.b64encode(data).decode("ascii"),
            "byte_offset": offset, "byte_length": length, "done": index == count - 1}


class ProbeServer:
    def __init__(self):
        self.instance = None
        self.sequence = 0
        self.traces = TraceStore()
        self.core = None

    def viewer_core(self):
        if self.core is None:
            import importlib.util
            source = Path(__file__).with_name("assets") / "viewer_server.py"
            spec = importlib.util.spec_from_file_location("codex_trace_viewer_core", source)
            core = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(core)
            self.core = core
        return self.core

    def trace_call(self, params):
        name, arguments = params.get("name"), params.get("arguments", {})
        keys = {"trace_view": set(), "trace_snapshot": {"known_revision", "format"},
            "trace_chunk": {"snapshot_id", "chunk_index", "detail_id"},
            "trace_detail": {"snapshot_id", "record_indices"}, "trace_schema": {"name", "ns"}, "trace_release": set()}
        required = {"trace_view": set(), "trace_snapshot": set(),
            "trace_chunk": {"snapshot_id", "chunk_index"}, "trace_detail": {"snapshot_id", "record_indices"},
            "trace_schema": {"name"}, "trace_release": set()}
        common = {"view_id", "request_nonce"}
        if name not in keys or not isinstance(arguments, dict) or set(arguments) - (keys[name] | common):
            raise ArgumentError("Unknown trace tool or arguments")
        if name != "trace_view" and not (required[name] | common) <= set(arguments):
            raise ArgumentError("Trace tool arguments are incomplete")
        if any(not valid_id(arguments[key]) for key in common | {"snapshot_id", "detail_id"} if key in arguments):
            raise ArgumentError("Page, request and snapshot IDs must be UUIDs")
        if "known_revision" in arguments and (not isinstance(arguments["known_revision"], str) or len(arguments["known_revision"]) > 1024):
            raise ArgumentError("Revision must be a string of at most 1024 characters")
        if "chunk_index" in arguments and (type(arguments["chunk_index"]) is not int or not 0 <= arguments["chunk_index"] <= MAX_SAFE_INTEGER):
            raise ArgumentError("Chunk index must be a non-negative safe integer")
        if "format" in arguments and arguments["format"] not in ("full", "outline"):
            raise ArgumentError("Snapshot format must be full or outline")
        if "record_indices" in arguments:
            indices = arguments["record_indices"]
            if (not isinstance(indices, list) or not 1 <= len(indices) <= MAX_DETAIL_RECORDS or
                    any(type(index) is not int or not 0 <= index <= MAX_SAFE_INTEGER for index in indices) or
                    len(set(indices)) != len(indices)):
                raise ArgumentError("Detail records must be unique non-negative safe integers within the request limit")
        if name == "trace_schema" and (not isinstance(arguments["name"], str) or not 1 <= len(arguments["name"].strip()) <= 256 or
                not isinstance(arguments.get("ns", ""), str) or len(arguments.get("ns", "")) > 256):
            raise ArgumentError("Schema query fields must be strings of at most 256 characters")
        result, diagnostics = inspect_identity(params, records=False)
        thread = result["thread_id"]
        if thread is None:
            raise ValueError("Missing or conflicting per-call host identity; no trace was returned")
        result["warnings"] = []
        if name == "trace_snapshot":
            result.update(self.traces.snapshot(thread, arguments["view_id"], arguments.get("known_revision"), arguments.get("format", "full")))
        elif name == "trace_chunk":
            result.update(self.traces.chunk(thread, arguments["view_id"], arguments["snapshot_id"], arguments["chunk_index"], arguments.get("detail_id")))
        elif name == "trace_detail":
            result.update(self.traces.detail(thread, arguments["view_id"], arguments["snapshot_id"], arguments["record_indices"]))
        elif name == "trace_schema":
            result["schema"] = self.traces.schema(thread, arguments["view_id"],
                lambda: self.viewer_core().lookup_tool_schema(arguments["name"], arguments.get("ns", "")))
        elif name == "trace_release":
            result["released"] = self.traces.release(thread, arguments["view_id"])
        return result, diagnostics

    def dispatch(self, request):
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or
                not isinstance(request.get("method"), str)):
            return self.error(None, -32600, "Invalid JSON-RPC request")
        if "id" not in request:
            return None
        identity, method = request["id"], request["method"]
        if not (identity is None or isinstance(identity, str) or
                (type(identity) is int and abs(identity) <= 9007199254740991) or
                (type(identity) is float and math.isfinite(identity) and abs(identity) <= 9007199254740991)):
            return self.error(None, -32600, "Invalid JSON-RPC id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(identity, -32602, "Params must be an object")
        try:
            if method == "initialize":
                # Versions verified against the bundled MCP server (2026-10-12).
                versions = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
                requested = params.get("protocolVersion")
                result = {"protocolVersion": requested if requested in versions else versions[0],
                          "capabilities": {"tools": {}, "resources": {}},
                          "serverInfo": {"name": "codex-trace-probe", "version": "0.11.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": []}
                common = {key: {"type": "string", "format": "uuid"} for key in ("view_id", "request_nonce")}
                definitions = (
                    ("trace_view", "会话轨迹", {}, [], TRACE_RESOURCE),
                    ("trace_snapshot", "读取会话快照", {"known_revision": {"type": "string", "maxLength": 1024},
                        "format": {"type": "string", "enum": ["full", "outline"]}}, list(common), None),
                    ("trace_chunk", "读取快照分页", {"snapshot_id": {"type": "string", "format": "uuid"},
                        "detail_id": {"type": "string", "format": "uuid"},
                        "chunk_index": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER}}, [*common, "snapshot_id", "chunk_index"], None),
                    ("trace_detail", "读取所选轮次详情", {"snapshot_id": {"type": "string", "format": "uuid"},
                        "record_indices": {"type": "array", "minItems": 1, "maxItems": MAX_DETAIL_RECORDS, "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER}}},
                        [*common, "snapshot_id", "record_indices"], None),
                    ("trace_schema", "查询本地工具定义", {"name": {"type": "string", "minLength": 1, "maxLength": 256},
                        "ns": {"type": "string", "maxLength": 256}}, [*common, "name"], None),
                    ("trace_release", "释放本页轨迹快照", {}, list(common), None))
                for name, title, properties, required, resource in definitions:
                    metadata = {"ui": {"visibility": ["app"]}}
                    if resource:
                        metadata["ui"]["resourceUri"] = resource
                        metadata["openai/ui"] = {"entrypoints": [{"type": "thread"}]}
                    result["tools"].append({"name": name, "title": title,
                        "description": "Local read-only trace data scoped by per-call host identity.",
                        "inputSchema": {"type": "object", "properties": {**common, **properties},
                            "required": required, "additionalProperties": False},
                        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "openWorldHint": False, "idempotentHint": True}, "_meta": metadata})
            elif method == "tools/call":
                arguments = params.get("arguments", {})
                result, diagnostics = self.trace_call(params)
                import datetime
                import uuid
                if self.instance is None:
                    self.instance = str(uuid.uuid4())
                self.sequence += 1
                result.update(probe_id=str(uuid.uuid4()), view_id=arguments.get("view_id"),
                    request_nonce=arguments.get("request_nonce"), server_instance_id=self.instance,
                    request_id=self.sequence, received_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                result = {"structuredContent": result, "content": [{"type": "text",
                    "text": "本地只读会话数据已返回插件页面。"}],
                    "_meta": {"diagnostics": diagnostics}}
            elif method == "resources/list":
                result = {"resources": [{"uri": uri, "name": title, "mimeType": "text/html;profile=mcp-app"}
                    for uri, title in ((TRACE_RESOURCE, "会话轨迹"),)]}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "resources/read":
                resource = params.get("uri")
                if resource != TRACE_RESOURCE:
                    return self.error(identity, -32602, "Unknown resource")
                html = (Path(__file__).with_name("assets") / "index.html").read_text(encoding="utf-8")
                bridge = Path(__file__).with_name("viewer-bridge.js").read_text(encoding="utf-8")
                marker = "bind();\nbindGutters();\nboot();"
                if html.count(marker) != 1:
                    raise ValueError("Viewer initialization marker changed; rebuild the plugin")
                html = html.replace(marker, bridge + "\n" + marker)
                result = {"contents": [{"uri": resource, "mimeType": "text/html;profile=mcp-app",
                    "text": html, "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
                                            "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]}}}]}
            else:
                return self.error(identity, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": identity, "result": result}
        except ArgumentError as error:
            return self.error(identity, -32602, str(error))
        except (OSError, ValueError, RecursionError, TypeError, AttributeError) as error:
            print(json.dumps({"event": "probe_error", "type": type(error).__name__}), file=sys.stderr)
            return self.error(identity, -32603, "Trace request failed (" + type(error).__name__ + "): " + str(error))

    @staticmethod
    def error(identity, code, message):
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}}


def main():
    server = ProbeServer()
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
                response = server.error(None, -32600, "Message exceeds 1 MiB limit")
            else:
                try:
                    response = server.dispatch(json.loads(line))
                except (ValueError, UnicodeError, RecursionError):
                    response = server.error(None, -32700, "Invalid JSON")
            if response is not None:
                print(json.dumps(response, ensure_ascii=True), flush=True)
    finally:
        server.traces.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--outline-worker"]:
        sys.exit(outline_worker())
    if len(sys.argv) == 4 and sys.argv[1] == "--outline-worker":
        sys.exit(outline_worker(int(sys.argv[2]), int(sys.argv[3])))
    main()
