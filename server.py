#!/usr/bin/env python3
"""
Codex 轨迹查看器 —— 本地只读服务端。

只做四件事：
  1. 列出 Codex 数据目录中的 sessions 与 archived_sessions（含轻量 peek）
  2. 按需冻结并分块回传某个 rollout 的完整原文
  3. 缓存客户端算好的统计信息，供列表展示
  4. 读取本地 App 工具定义缓存，并返回有来源标记的 Schema

只监听 127.0.0.1，只读，不写任何 session 文件。
"""

import argparse
from contextlib import ExitStack, contextmanager
import datetime
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# https://learn.chatgpt.com/docs/config-file/environment-variables
CODEX_DIR = os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))
ROOTS = [
    os.path.join(CODEX_DIR, "sessions"),
    os.path.join(CODEX_DIR, "archived_sessions"),
]
CACHE_PATH = os.path.join(CODEX_DIR, "trace-viewer-cache.json")
INDEX_PATH = os.path.join(CODEX_DIR, "session_index.jsonl")       # {id, thread_name, updated_at}
STATE_PATH = os.path.join(CODEX_DIR, ".codex-global-state.json")  # local-projects / thread-project-assignments
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")

READ_BLOCK_BYTES = 1024 * 1024
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 128 * 1024
TAIL_TRIES = (TAIL_BYTES, 512 * 1024, 2 * 1024 * 1024)
SMALL_FILE = 512 * 1024
PEEK_BYTES = HEAD_BYTES
DEFAULT_PORT = 8765
NAME_RE = re.compile(r"^rollout-(\d{4}-\d{2}-\d{2})T(\d{2}-\d{2}-\d{2})-(.+)\.jsonl$")

# 反向扫尾（找「最后一条人类用户消息」）用
SCAN_CHUNK = 4 * 1024 * 1024
_USER_ROLE_RE = re.compile(rb'"role"\s*:\s*"user"')
_ENVELOPE_RE = re.compile(r"^<[a-z][a-z0-9]*(?:_[a-z0-9]+)+>")

INJECT_HINTS = ("AGENTS.md instructions", "<INSTRUCTIONS>", "environment_context", "image_resize_notice",
                "external_codex_apps", "user_instructions", "<system", "<permissions")

_lock = threading.Lock()
_list_lock = threading.Lock()
_cache = {"peek": {}, "stats": {}}
_cache_loaded = False
_cache_dirty = False
PEEK_VERSION = 2


def _clean_text(s):
    s = re.sub(r"<[^<>]{0,90}>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_noise(t):
    if not t or len(t) < 2:
        return True
    if t.startswith("<"):
        return True
    head = t[:220]
    for h in INJECT_HINTS:
        if h in head:
            return True
    return False


def _scan_lines(text, info):
    """从文本块提取元信息、用户/助手消息，以及 agent 的收尾消息。"""
    first_user, last_user, first_asst, last_agent = None, None, None, None
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        t = d.get("type")
        if t == "session_meta" and not info.get("session_id"):
            info["cwd"] = p.get("cwd") or ""
            info["model_provider"] = p.get("model_provider") or ""
            info["originator"] = p.get("originator") or ""
            info["cli_version"] = p.get("cli_version") or ""
            info["session_id"] = p.get("session_id") or p.get("id") or ""
            info["thread_id"] = p.get("id") or ""
            info["parent_thread_id"] = p.get("parent_thread_id") or ""
            info["forked_from_id"] = p.get("forked_from_id") or ""
            info["thread_source"] = p.get("thread_source") or ""
            g = p.get("git")
            g = g if isinstance(g, dict) else {}
            info["git"] = {"branch": g.get("branch") or "", "commit": (g.get("commit_hash") or "")[:8]}
            src = p.get("source")
            if isinstance(src, dict) and src.get("subagent"):
                info["context_kind"] = "subagent"
            else:
                info["context_kind"] = p.get("thread_source") or ""
            info["agent"] = p.get("agent_nickname") or p.get("agent_path") or ""
        elif t == "response_item" and p.get("type") == "message":
            role = p.get("role") or ""
            txt = _clean_text("\n".join(c.get("text", "") for c in (p.get("content") or []) if isinstance(c, dict)))
            if _is_noise(txt):
                continue
            if role == "user":
                if first_user is None:
                    first_user = txt
                last_user = txt
            elif role == "assistant" and first_asst is None:
                first_asst = txt
        elif t == "response_item" and p.get("type") == "agent_message":
            txt = _clean_text("\n".join(c.get("text", "") for c in (p.get("content") or []) if isinstance(c, dict)))
            if not _is_noise(txt):
                last_agent = txt
        elif t == "event_msg" and p.get("type") == "task_complete":
            txt = _clean_text(p.get("last_agent_message") or "")
            if not _is_noise(txt):
                last_agent = txt
    return first_user, last_user, first_asst, last_agent


def _human_user_ts(d):
    """某条 rollout 记录是「人类用户消息」时返回其时间戳（epoch 秒），否则 None。

    判据与前端 classifyMessage 保持一致：
      · 完整 content_item_kinds 中包含 user.* = 真实输入（含附件/纯图片）
      · <send_user_message…> 信封 = 用户通过工具作答 → 算人类输入
      · 其余任意信封标签 / # AGENTS.md / # Files … / 代理历史 / guardian_sources → 注入
      · 剩下的 role=user 一律视为人类输入（旧格式没有 metadata 也算）
    """
    if not isinstance(d, dict) or d.get("type") != "response_item":
        return None
    p = d.get("payload")
    if not isinstance(p, dict) or p.get("type") != "message" or p.get("role") != "user":
        return None
    raw = p.get("content")
    content = raw if isinstance(raw, list) else [{"text": raw}] if isinstance(raw, str) else []
    md = p.get("internal_chat_message_metadata_passthrough")
    kinds = md.get("content_item_kinds") if isinstance(md, dict) else None
    attributed = (isinstance(content, list) and isinstance(kinds, list) and len(kinds) == len(content)
                  and all(isinstance(k, str) for k in kinds) and any(k.startswith("user.") for k in kinds))
    txt = "\n".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
    if not txt and not attributed:
        return None
    head = txt[:140]
    if not attributed and not head.startswith("<send_user_message"):
        if head.startswith("<") or head.startswith("# AGENTS.md"):
            return None
        if txt.startswith("The following is the Codex agent history") or txt.startswith("# Files "):
            return None
        md = d.get("metadata")
        if isinstance(md, dict) and md.get("guardian_sources") is not None:
            return None
    ts = d.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def scan_last_user_ts(path):
    """从文件尾部向前分块扫描，返回 (epoch 秒 | None, 扫描字节数)。

    必须倒着扫：会话越活跃，最后一条用户消息越靠近文件末尾。
    实测 276MB 的会话只需读尾部 16MB、0.08s 就能命中；
    用「候选行预筛 (bytes 正则) + 只解析命中行」避免全量 JSON 解析。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, 0
    pos, carry, scanned = size, b"", 0
    try:
        with open(path, "rb") as f:
            while pos > 0:
                step = min(SCAN_CHUNK, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + carry
                scanned += step
                lines = chunk.split(b"\n")
                if pos > 0:
                    carry = lines[0]          # 首行不完整，留给下一块拼接
                    lines = lines[1:]
                else:
                    carry = b""
                for line in reversed(lines):
                    if not _USER_ROLE_RE.search(line):
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = _human_user_ts(d)
                    if ts is not None:
                        return ts, scanned
    except Exception:
        pass
    return None, scanned


def peek_file(path):
    """只读文件头尾，提取元信息与用户消息预览（优先最近一条）。"""
    info = {"cwd": "", "model_provider": "", "originator": "", "cli_version": "",
            "session_id": "", "git": {}, "preview": "", "preview_role": "",
            "context_kind": "", "agent": "", "thread_id": "", "parent_thread_id": "",
            "forked_from_id": "", "thread_source": "", "last_user_ts": None}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(size if size <= SMALL_FILE else HEAD_BYTES)
    except Exception:
        return info
    fu, lu, fa, la = _scan_lines(head.decode("utf-8", "ignore"), info)
    if not lu and size > SMALL_FILE:
        for tb in TAIL_TRIES:
            if size <= tb:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(size - tb)
                    tail = f.read(tb).decode("utf-8", "ignore")
                nl = tail.find("\n")
                if nl > 0:
                    tail = tail[nl + 1:]
                if not tail:
                    continue
                _, t_lu, t_fa, t_la = _scan_lines(tail, info)
                lu, la = (t_lu or lu), (t_la or la)
                if not fa and t_fa:
                    fa = t_fa
                if lu or la:
                    break
            except Exception:
                continue
    # 最后一条人类用户消息的时间（列表排序主键）。
    # 只对「根会话」反向扫尾：子线程（守护审查/子代理，600+ 个）里的 user 消息多是
    # 转发内容或程序注入，且全量扫一遍要几十秒；它们交给 updated_at / mtime 回退。
    if not info.get("parent_thread_id"):
        info["last_user_ts"], _scanned = scan_last_user_ts(path)

    for text, role in ((lu, "user"), (la, "agent"), (fa, "assistant"), (fu, "user")):
        if text:
            info["preview"], info["preview_role"] = text[:180], role
            break
    return info


# ---------- 缓存 ----------

def load_cache():
    global _cache_loaded
    with _lock:
        if _cache_loaded:
            return
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in ("peek", "stats"):
                    _cache[k] = data[k] if isinstance(data.get(k), dict) else {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            print("读取侧栏缓存失败，重新建立：%s" % e, file=sys.stderr)
        _cache_loaded = True


def save_cache():
    global _cache_dirty
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
        _cache_dirty = False
    except (OSError, ValueError) as e:
        print("写入侧栏缓存失败，将在下次请求重试：%s" % e, file=sys.stderr)


# ---------- 路径校验 ----------

def safe_path(raw):
    """只允许读取 ROOTS 下的 .jsonl 文件。"""
    if not raw:
        return None
    p = os.path.realpath(os.path.expanduser(raw))
    for root in ROOTS:
        rr = os.path.realpath(root)
        if p == rr or p.startswith(rr + os.sep):
            if p.endswith(".jsonl") and os.path.isfile(p):
                return p
    return None


def session_header(text):
    for line in text[:HEAD_BYTES].splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # 正在追加的未完成行不是可用于绑定的记录。
        if isinstance(record, dict) and record.get("type") == "session_meta":
            meta = record.get("payload")
            return meta if isinstance(meta, dict) else None
    return None


class ThreadScope:
    """一个服务实例只绑定一个明确的 session_meta.id。"""
    def __init__(self, thread_id):
        if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread_id):
            raise ValueError("会话 ID 必须为 1–128 个字母、数字、下划线或连字符")
        self.thread_id = thread_id
        self.path = None
        self.stats = None
        self.lock = threading.RLock()
        self.headers = {}
        self.directories = {}
        self.scan_after = 0

    def _meta(self, path):
        if not safe_path(path):
            return None
        try:
            st = os.stat(path)
            signature = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)
            cached = self.headers.get(path)
            if cached and cached[0] == signature:
                return cached[1]
            with open(path, "rb") as source:
                head = source.readline(HEAD_BYTES)
                meta = session_header(head)
                if meta is None and len(head) < HEAD_BYTES:
                    meta = session_header(head + source.read(HEAD_BYTES - len(head)))
            if meta and meta.get("id") != self.thread_id:
                meta = {"id": meta.get("id")}
            self.headers[path] = (signature, meta)
            return meta
        except FileNotFoundError:
            return None  # 文件可能正在从 sessions 移到 archived_sessions。

    def _directory_signature(self, path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)
        except FileNotFoundError:
            return None

    def resolve(self):
        with self.lock:
            had_path = self.path is not None
            if self.path:
                meta = self._meta(self.path)
                if meta and meta.get("id") == self.thread_id:
                    return self.path, meta
                self.path = None
            if not had_path and time.monotonic() < self.scan_after and all(
                    self._directory_signature(path) == signature for path, signature in self.directories.items()):
                return None, None
            matches, seen, directories = [], set(), {}
            def fail(error):
                raise error
            for root in ROOTS:
                directories[root] = self._directory_signature(root)
                if not os.path.isdir(root):
                    continue
                for directory, _, names in os.walk(root, onerror=fail):
                    directories[directory] = self._directory_signature(directory)
                    for name in names:
                        if not name.endswith(".jsonl"):
                            continue
                        path = os.path.realpath(os.path.join(directory, name))
                        if path in seen:
                            continue
                        seen.add(path)
                        meta = self._meta(path)
                        if meta and meta.get("id") == self.thread_id:
                            matches.append((path, meta))
            if len(matches) > 1:
                raise ValueError("此会话 ID 对应多个轨迹文件，无法确定当前记录")
            self.headers = {path: entry for path, entry in self.headers.items() if path in seen}
            self.directories = directories
            self.scan_after = time.monotonic() + 10
            self.path = matches[0][0] if matches else None
            return matches[0] if matches else (None, None)

    def allowed_path(self, raw):
        if not isinstance(raw, str):
            return None
        with self.lock:
            path, _ = self.resolve()
            return path if path and safe_path(raw) == path else None

    def items(self):
        with self.lock:
            path, meta = self.resolve()
            if not path:
                return []
            st = os.stat(path)
            info = {}
            _scan_lines(json.dumps({"type": "session_meta", "payload": meta}), info)
            tid = self.thread_id
            match = NAME_RE.match(os.path.basename(path))
            return [{"path": path, "name": os.path.basename(path), "session_id": tid,
                "date": match.group(1) if match else "", "clock": match.group(2).replace("-", ":") if match else "",
                "size": st.st_size, "mtime": int(st.st_mtime), "archived": "archived_sessions" in path,
                "revision": "|".join(str(x) for x in (path, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)),
                "stats": self.stats, **info}]


# ---------- 会话列举 ----------

# 标题与项目归属来自 Codex 自己的两个索引文件，按 mtime 失效重载
_meta = {"sig": None, "titles": {}, "updated": {}, "projects": {}, "proj_created": {},
         "proj_roots": [], "assign": {}, "projectless": set()}


def load_thread_meta():
    try:
        sig = (os.path.getmtime(INDEX_PATH), os.path.getmtime(STATE_PATH))
    except Exception:
        sig = None
    if sig is not None and _meta["sig"] == sig:
        return
    titles, updated, projects, proj_roots, assign = {}, {}, {}, [], {}
    proj_created = {}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    titles[d["id"]] = d.get("thread_name") or ""
                    updated[d["id"]] = d.get("updated_at") or ""
    except Exception:
        pass
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        for pid, v in (st.get("local-projects") or {}).items():
            if not isinstance(v, dict):
                continue
            name = v.get("name") or ""
            projects[pid] = name
            created = v.get("createdAt")
            if isinstance(created, (int, float)) and created > 0:
                proj_created[pid] = int(created)          # 毫秒时间戳，用于项目段排序
            for r in (v.get("rootPaths") or []):
                if isinstance(r, str) and r:
                    proj_roots.append((r, name, pid))
        proj_roots.sort(key=lambda x: len(x[0]), reverse=True)   # 最长前缀优先
        for tid, v in (st.get("thread-project-assignments") or {}).items():
            if isinstance(v, dict) and v.get("projectId"):
                assign[tid] = v["projectId"]
        projectless = set(x for x in (st.get("projectless-thread-ids") or []) if isinstance(x, str))
    except Exception:
        projectless = set()
    _meta.update({"sig": sig, "titles": titles, "updated": updated, "projects": projects,
                  "proj_created": proj_created, "proj_roots": proj_roots,
                  "assign": assign, "projectless": projectless})


def resolve_project(tid, cwd):
    """只认 Codex 的显式归属，保证与客户端侧栏一致。

    不要用 cwd 路径兜底：Codex 会把某些目录下的会话显式标为「无项目」
    （projectless-thread-ids），路径匹配会把它们错误地挂到同名项目下。
    proj_roots 仅在完全拿不到归属信息时（本地模式）才由前端使用。"""
    if not tid:
        return "", ""
    if tid in _meta["projectless"]:
        return "", ""
    pid = _meta["assign"].get(tid)
    if pid and _meta["projects"].get(pid):
        return _meta["projects"][pid], pid
    return "", ""


# ---------- 工具 Schema 查询（右栏「Schema」页签的数据源之一） ----------
# Codex 把 App/连接器工具的完整定义缓存在数据目录的 cache/codex_apps_tools/*.json：
#   {tools:[{server_name, tool_name, tool_namespace, tool:{name,title,description,inputSchema,outputSchema,…}}]}
# 缓存是当前工具定义，不能作为某次历史调用的 Schema 快照。

TOOLS_CACHE_DIR = os.path.join(CODEX_DIR, "cache", "codex_apps_tools")
_tools_idx = {"sig": None, "entries": [], "errors": []}
_tools_lock = threading.Lock()


def _load_tools_cache():
    files, errors = [], []
    try:
        for name in os.listdir(TOOLS_CACHE_DIR):
            if name.endswith(".json"):
                path = os.path.join(TOOLS_CACHE_DIR, name)
                try:
                    st = os.stat(path)
                    files.append((path, st.st_mtime_ns, st.st_size))
                except OSError as e:
                    errors.append(str(e))
    except FileNotFoundError:
        pass
    except OSError as e:
        errors.append(str(e))
    sig = (tuple(sorted(files)), tuple(errors))
    if _tools_idx["sig"] == sig:
        return
    entries, seen = [], set()
    for path, mtime_ns, _ in sorted(files, key=lambda x: (x[1], x[0]), reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
                raise ValueError("缺少 tools 数组")
        except (OSError, ValueError) as e:
            errors.append("%s: %s" % (os.path.basename(path), e))
            continue
        for item in data["tools"]:
            if not isinstance(item, dict) or not isinstance(item.get("tool"), dict):
                continue
            identity = (item.get("server_name"), item.get("tool_namespace"), item.get("tool_name") or item["tool"].get("name"))
            if identity in seen:
                continue
            seen.add(identity)
            entries.append({**item, "cache_file": path,
                            "cache_updated_at": datetime.datetime.fromtimestamp(mtime_ns / 1e9, datetime.timezone.utc).isoformat()})
    _tools_idx.update({"sig": sig, "entries": entries, "errors": errors})


def _name_candidates(name, ns):
    names = {name, name.replace("__", ".")}
    if ns:
        names.update((ns + "." + name, ns + "__" + name))
    return {x for x in names if x}


def lookup_tool_schema(name, ns):
    name, ns = (name or "").strip(), (ns or "").strip()
    if not name or len(name) > 256 or len(ns) > 256:
        return {"found": False, "error": "工具名为空或查询字段超过 256 字符"}
    with _tools_lock:
        _load_tools_cache()
        entries, errors = _tools_idx["entries"], _tools_idx["errors"]
    cands, matches = _name_candidates(name, ns), []
    for item in entries:
        tool = item["tool"]
        namespace = item.get("tool_namespace") or ""
        if ns and ns.replace("__", ".") != namespace.replace("__", "."):
            continue
        full_name, short_name = tool.get("name") or "", item.get("tool_name") or ""
        aliases = {full_name, short_name, namespace + "." + short_name,
                   "mcp__%s__%s" % (item.get("server_name") or "", full_name.replace(".", "_"))}
        if cands & aliases:
            matches.append(item)
    if len(matches) != 1:
        reason = "多个命名空间存在同名工具，无法唯一匹配" if matches else "未找到匹配的缓存定义"
        return {"found": False, "source": TOOLS_CACHE_DIR, "error": "; ".join([reason, *errors])}
    item = matches[0]
    tool = item["tool"]
    return {"found": True, "source": TOOLS_CACHE_DIR,
            "server_name": item.get("server_name") or "", "tool_namespace": item.get("tool_namespace") or "",
            "connector_name": item.get("connector_name") or "", "name": tool.get("name") or item.get("tool_name") or "",
            "title": tool.get("title") or "", "description": tool.get("description") or "",
            "inputSchema": tool.get("inputSchema"), "outputSchema": tool.get("outputSchema"),
            "annotations": tool.get("annotations"), "cache_file": item["cache_file"],
            "cache_updated_at": item["cache_updated_at"], "cache_warnings": errors}


def activity_ts(item):
    """活动时间（epoch 秒），与前端 actTime 同一条回退链：

      ① last_user_ts —— 最后一条人类用户消息的发送时间（根会话反向扫尾得到）
      ② updated_at   —— Codex 索引里的时间。注意它可能停在「会话创建时刻」，
                        对长期复用的会话不准（实测某会话停在 9/14，实际用到 10/09）
      ③ mtime        —— 文件最后写入时间

    统一转成数值再比较，避免字符串字典序在时区/精度不一致时给出错误顺序。"""
    lu = item.get("last_user_ts")
    if isinstance(lu, (int, float)) and lu > 0:
        return lu
    u = item.get("updated_at") or ""
    if u:
        try:
            return datetime.datetime.fromisoformat(u.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return item.get("mtime") or 0


def list_sessions(limit=0):
    # 并发刷新共用一次缓存填充；统计写入仍仅占用 _lock。
    with _list_lock:
        return _list_sessions(limit)


def _list_sessions(limit):
    global _cache_dirty
    load_cache()
    load_thread_meta()
    items = []
    live_keys = set()
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except Exception:
                    continue
                m = NAME_RE.match(fn)
                date, clock, sid = ("", "", "")
                if m:
                    date, clock, sid = m.group(1), m.group(2).replace("-", ":"), m.group(3)
                key = "%d|%s|%d|%d|%d|%d|%d" % (PEEK_VERSION, full, st.st_size, st.st_mtime_ns,
                                                   st.st_ctime_ns, st.st_ino, st.st_dev)
                live_keys.add(key)
                with _lock:
                    info = _cache["peek"].get(key)
                if not isinstance(info, dict) or "last_user_ts" not in info:
                    info = peek_file(full)
                    with _lock:
                        _cache["peek"][key] = info
                        _cache_dirty = True
                with _lock:
                    stats = _cache["stats"].get(full)
                tid = info.get("thread_id", "")
                proj, projid = resolve_project(tid, info.get("cwd") or "")
                items.append({
                    "path": full,
                    "name": fn,
                    "session_id": sid,
                    "date": date,
                    "clock": clock,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "revision": key,
                    "archived": "archived_sessions" in full,
                    "stats": stats,
                    "title": _meta["titles"].get(tid, ""),
                    "updated_at": _meta["updated"].get(tid, ""),
                    "project": proj,
                    "project_id": projid,
                    "project_created_at": _meta["proj_created"].get(projid),   # 项目创建时间（毫秒）
                    **info,
                })
    items.sort(key=activity_ts, reverse=True)
    with _lock:
        stale = _cache["peek"].keys() - live_keys
        for key in stale:
            del _cache["peek"][key]
        if stale:
            _cache_dirty = True
        if _cache_dirty:
            save_cache()
    if limit:
        items = items[:limit]
    return items


# ---------- 完整文件快照 ----------

@contextmanager
def _open_payload(path):
    """Open the authorized canonical name without following a replacement link."""
    path = os.path.abspath(path)
    roots = [os.path.realpath(root) for root in ROOTS]
    root = next((value for value in roots if os.path.normcase(path).startswith(
        os.path.normcase(value + os.sep))), None)
    if root is None or not path.endswith(".jsonl"):
        raise ValueError("路径不在允许范围内")
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    with ExitStack() as cleanup:
        if os.name == "nt":
            source = cleanup.enter_context(open(path, "rb"))
            # Verify the opened handle, not a second path lookup that can race.
            # https://docs.python.org/3.13/library/msvcrt.html#msvcrt.get_osfhandle
            # https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew
            import ctypes
            from ctypes import wintypes
            import msvcrt
            final_name = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            final_name.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            final_name.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = final_name(msvcrt.get_osfhandle(source.fileno()), buffer, len(buffer), 0)
            if not length:
                raise ctypes.WinError(ctypes.get_last_error())
            if length >= len(buffer):
                raise ValueError("打开的轨迹路径超过系统长度限制")
            actual = buffer.value
            if actual.startswith("\\\\?\\UNC\\"):
                actual = "\\\\" + actual[8:]
            elif actual.startswith("\\\\?\\"):
                actual = actual[4:]
            if os.path.normcase(actual) != os.path.normcase(path):
                raise ValueError("轨迹路径在打开期间发生变化，请重试")
            entry = lambda: os.stat(path, follow_symlinks=False)
        else:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
                raise OSError("当前平台无法保证禁止跟随轨迹链接")
            folder = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            cleanup.callback(os.close, folder)
            for part in root.split(os.sep)[1:]:
                folder = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                cleanup.callback(os.close, folder)
            parts = os.path.relpath(path, root).split(os.sep)
            for part in parts[:-1]:
                folder = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                cleanup.callback(os.close, folder)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            source = cleanup.enter_context(os.fdopen(descriptor, "rb"))
            entry = lambda: os.stat(parts[-1], dir_fd=folder, follow_symlinks=False)
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("轨迹必须是普通文件")
        yield source, before.st_size
        if signature(before) != signature(os.fstat(source.fileno())) or signature(before) != signature(entry()):
            raise ValueError("轨迹读取期间发生变化，请重试")


@contextmanager
def read_payload(path):
    """Freeze all bytes on disk before sending; source files are never modified."""
    with tempfile.TemporaryFile() as snapshot:
        with _open_payload(path) as (source, size):
            remaining = size
            while remaining:
                chunk = source.read(min(READ_BLOCK_BYTES, remaining))
                if not chunk:
                    raise ValueError("轨迹读取期间被截断，请重试")
                snapshot.write(chunk)
                remaining -= len(chunk)
        snapshot.seek(0)
        head = snapshot.read(HEAD_BYTES).decode("utf-8", errors="replace")
        snapshot.seek(0)
        yield snapshot, size, head


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "CodexTraceViewer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("CTV_VERBOSE"):
            sys.stderr.write("[ctv] " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

    def _send_file(self, source, size):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Truncated", "0")
        self.end_headers()
        try:
            remaining = size
            while remaining:
                chunk = source.read(min(READ_BLOCK_BYTES, remaining))
                if not chunk:
                    raise ValueError("完整快照发送期间被截断")
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # The client cancelled this snapshot.
        except (OSError, ValueError) as error:
            self.close_connection = True
            print("发送完整轨迹失败：%s" % error, file=sys.stderr)

    def _allowed_path(self, raw):
        scope = getattr(self.server, "scope", None)
        return scope.allowed_path(raw) if scope else safe_path(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            try:
                with open(INDEX, "r", encoding="utf-8") as f:
                    page = f.read()
                scope = getattr(self.server, "scope", None)
                config = json.dumps({"thread_id": scope.thread_id if scope else None})
                page = page.replace('<script type="application/json" id="viewer-config">{"thread_id":null}</script>',
                    '<script type="application/json" id="viewer-config">' + config + '</script>')
                self._send(200, page, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html 缺失", "text/plain; charset=utf-8")
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if path == "/api/health":
            scope = getattr(self.server, "scope", None)
            self._json({"app": "codex-trace-viewer", "protocol": 1,
                "thread_id": scope.thread_id if scope else None,
                "instance_id": getattr(self.server, "instance_id", None)})
            return

        if path == "/api/sessions":
            try:
                scope = getattr(self.server, "scope", None)
                limit = int((q.get("limit") or ["0"])[0] or 0)
                items = scope.items() if scope else list_sessions(limit)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            self._json({"root": ROOTS, "count": len(items), "items": items,
                "bound_thread": scope.thread_id if scope else None})
            return

        if path == "/api/toolschema":
            name = (q.get("name") or [""])[0]
            ns = (q.get("ns") or [""])[0]
            try:
                self._json(lookup_tool_schema(name, ns))
            except Exception as e:
                self._json({"found": False, "error": str(e)})
            return

        if path == "/api/raw":
            raw = (q.get("path") or [""])[0]
            p = self._allowed_path(raw)
            if not p:
                self._json({"error": "路径不在允许范围内"}, 403)
                return
            try:
                with read_payload(p) as (source, size, head):
                    scope = getattr(self.server, "scope", None)
                    if scope and (session_header(head) or {}).get("id") != scope.thread_id:
                        self._json({"error": "轨迹身份已变化，未返回其他会话内容"}, 403)
                        return
                    self._send_file(source, size)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        global _cache_dirty
        u = urlparse(self.path)
        if u.path == "/api/shutdown":
            token = getattr(self.server, "control_token", "")
            supplied = self.headers.get("Authorization", "")
            if not token or not secrets.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                self._json({"error": "无权停止此服务"}, 403)
                return
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if u.path != "/api/stats":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("bad body")
        except Exception:
            self._json({"error": "bad body"}, 400)
            return
        p = self._allowed_path(payload.get("path") or "")
        if not p:
            self._json({"error": "路径不在允许范围内"}, 403)
            return
        scope = getattr(self.server, "scope", None)
        if scope:
            with scope.lock:
                scope.stats = payload.get("stats") or {}
            self._json({"ok": True})
            return
        load_cache()
        with _lock:
            stats = payload.get("stats") or {}
            if _cache["stats"].get(p) != stats:
                _cache["stats"][p] = stats
                _cache_dirty = True
            if _cache_dirty:
                save_cache()
        self._json({"ok": True})


def main():
    # https://docs.python.org/3.13/library/argparse.html
    parser = argparse.ArgumentParser(description="本地查看 Codex JSONL 会话记录")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8765；0 由系统选择空闲端口")
    parser.add_argument("--open-browser", action="store_true", help="启动后打开当前系统的默认浏览器")
    parser.add_argument("--thread", help="只展示指定 Codex 会话 ID；省略时列出全部会话")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port 必须在 0 到 65535 之间")
    try:
        scope = ThreadScope(args.thread) if args.thread is not None else None
    except ValueError as e:
        parser.error(str(e))
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        parser.exit(1, "无法启动本地服务：%s。可使用 --port 0 选择空闲端口。\n" % e)
    srv.scope = scope
    srv.instance_id = secrets.token_hex(16)
    srv.control_token = os.environ.get("CTV_CONTROL_TOKEN") or ""
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("Codex 轨迹查看器已启动：" + url, flush=True)
    print("读取目录：")
    for r in ROOTS:
        print("  " + r + ("" if os.path.isdir(r) else "  (不存在)"))
    print("按 Ctrl+C 退出")
    browser_timer = None
    if args.open_browser:
        def open_browser():
            # https://docs.python.org/3.13/library/webbrowser.html
            try:
                if not webbrowser.open(url):
                    print("无法自动打开浏览器，请手动访问：" + url, file=sys.stderr)
            except (webbrowser.Error, OSError) as e:
                print("打开浏览器失败：%s；请手动访问：%s" % (e, url), file=sys.stderr)
        browser_timer = threading.Timer(0.5, open_browser)
        browser_timer.daemon = True
        browser_timer.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        if browser_timer:
            browser_timer.cancel()
        srv.server_close()


if __name__ == "__main__":
    main()
