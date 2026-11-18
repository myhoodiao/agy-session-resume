#!/usr/bin/env python3
"""
Antigravity CLI (`agy`) 极简会话恢复与模型记忆工具（零第三方依赖）。

职责：
1. `agy -r [query]` / `agy --resume [query]`：读取与原生 `/resume` 一致的有效 CLI
   会话列表，仅展示「Thread 名称」与「对应的模型与思考等级」，回车后自动带回
   该 Thread 最后一次使用的模型启动 `agy.exe`（会话级 `--settings` 覆盖，
   不改写全局配置）。
2. `agy -c` / `--continue` / `--conversation <id>`：未显式指定 `--model` 或
   `--settings` 中的 `"model"` 时，自动补回该会话上次使用的模型启动 `agy.exe`。
3. 其余参数（及 `-p` / `--print` / `--prompt` 等非交互模式）原样透传给原生 `agy.exe`。
"""

from __future__ import annotations

import ctypes
import json
import msvcrt
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from typing import Any, Callable

HOME = Path.home()
DATA_DIR = HOME / ".gemini" / "antigravity-cli"
SUMMARIES_DB = DATA_DIR / "conversation_summaries.db"
CONVERSATIONS_DIR = DATA_DIR / "conversations"
ANNOTATIONS_DIR = DATA_DIR / "annotations"
META_CACHE_FILE = DATA_DIR / "cache" / "conversation_metadata.json"
LAST_CONVS_FILE = DATA_DIR / "cache" / "last_conversations.json"
MODELS_CACHE_FILE = HOME / ".local" / "share" / "agy-guard" / "models_cache.json"

DEFAULT_AGY_EXE = (
    Path(os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local")))
    / "agy"
    / "bin"
    / "agy.exe"
)

# `gen_metadata` 中出现过但 `agy models` 使用不同显示名的已知内置别名
MODEL_LABEL_ALIASES: dict[str, str] = {
    "Gemini 4 Hydrogen (Orchestrator)": "Orchestrator (Argon)",
    "gemini-flash-worker-agy": "Orchestrator (Argon)",
    "gemini-pro-orchestrator": "Orchestrator (Argon)",
    "gemini-4-argon-trial": "Gemini 4 Argon (High)",
    "gemini-3.8-flash-n": "Gemini 3.8 Flash (High)",
    "gemini-4-hydrogen": "Gemini 4 Hydrogen (High)",
}

FALLBACK_SLUG_TO_LABEL: dict[str, str] = {
    "gemini-4-argon-low": "Gemini 4 Argon (Low)",
    "gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
    "gemini-4-argon-high": "Gemini 4 Argon (High)",
    "gemini-4-hydrogen-low": "Gemini 4 Hydrogen (Low)",
    "gemini-4-hydrogen-medium": "Gemini 4 Hydrogen (Medium)",
    "gemini-4-hydrogen-high": "Gemini 4 Hydrogen (High)",
    "gemini-4-hydrogen-xhigh": "Gemini 4 Hydrogen (XHigh)",
    "gemini-pro-orchestrator-g1-pro": "Orchestrator (Argon)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "claude-opus-5-5-low": "Claude Opus 5.5 (Low)",
    "claude-opus-5-5-medium": "Claude Opus 5.5 (Medium)",
    "claude-opus-5-5-high": "Claude Opus 5.5 (High)",
    "claude-sonnet-5-5-low": "Claude Sonnet 5.5 (Low)",
    "claude-sonnet-5-5-medium": "Claude Sonnet 5.5 (Medium)",
    "claude-sonnet-5-5-high": "Claude Sonnet 5.5 (High)",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
}

MODELS_CACHE_TTL_SEC = 86400


# ============================================================================
# 1. 极简 Protobuf 解析（仅用于读取 gen_metadata 中的模型字段）
# ============================================================================

def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    val = 0
    shift = 0
    n = len(data)
    while offset < n:
        b = data[offset]
        offset += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint overflow")
    raise ValueError("varint truncated")


def _parse_proto(data: bytes) -> list[tuple[int, int, int | bytes]]:
    offset = 0
    n = len(data)
    out: list[tuple[int, int, int | bytes]] = []
    while offset < n:
        tag, offset = _decode_varint(data, offset)
        fnum = tag >> 3
        wtype = tag & 0x07
        if fnum <= 0:
            raise ValueError("invalid field number")
        if wtype == 0:
            val, offset = _decode_varint(data, offset)
            out.append((fnum, wtype, val))
        elif wtype == 1:
            if offset + 8 > n:
                raise ValueError("truncated 64-bit")
            out.append((fnum, wtype, data[offset : offset + 8]))
            offset += 8
        elif wtype == 2:
            length, offset = _decode_varint(data, offset)
            if offset + length > n:
                raise ValueError("truncated bytes")
            out.append((fnum, wtype, data[offset : offset + length]))
            offset += length
        elif wtype == 5:
            if offset + 4 > n:
                raise ValueError("truncated 32-bit")
            out.append((fnum, wtype, data[offset : offset + 4]))
            offset += 4
        else:
            raise ValueError(f"unsupported wire_type {wtype}")
    return out


# ============================================================================
# 2. 模型映射与单会话模型读取
# ============================================================================

def find_agy_exe() -> str:
    if DEFAULT_AGY_EXE.is_file():
        return str(DEFAULT_AGY_EXE)
    return shutil.which("agy.exe") or shutil.which("agy") or "agy.exe"


def _load_models_map(allow_refresh: bool = True) -> dict[str, str]:
    now = time.time()
    if MODELS_CACHE_FILE.is_file():
        try:
            cached = json.loads(MODELS_CACHE_FILE.read_text(encoding="utf-8"))
            mapping = cached.get("slug_to_label", {})
            updated_at = float(cached.get("updated_at", 0))
            if mapping and (not allow_refresh or (now - updated_at) < MODELS_CACHE_TTL_SEC):
                return mapping
        except Exception:
            pass

    if allow_refresh:
        try:
            res = subprocess.run(
                [find_agy_exe(), "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
            )
            if res.returncode == 0:
                parsed: dict[str, str] = {}
                for line in res.stdout.splitlines():
                    if "\t" in line:
                        slug, label = line.split("\t", 1)
                        slug, label = slug.strip(), label.strip()
                        if slug and label:
                            parsed[slug] = label
                if parsed:
                    MODELS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    MODELS_CACHE_FILE.write_text(
                        json.dumps({"updated_at": now, "slug_to_label": parsed}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    return parsed
        except Exception:
            pass

    if MODELS_CACHE_FILE.is_file():
        try:
            cached = json.loads(MODELS_CACHE_FILE.read_text(encoding="utf-8"))
            if cached.get("slug_to_label"):
                return cached["slug_to_label"]
        except Exception:
            pass
    return dict(FALLBACK_SLUG_TO_LABEL)


def normalize_model(raw: str | None, slug_to_label: dict[str, str]) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return "", ""
    text = MODEL_LABEL_ALIASES.get(text, text)
    label_to_slug = {v.lower(): k for k, v in slug_to_label.items()}
    if text in slug_to_label:
        return text, slug_to_label[text]
    if text.lower() in label_to_slug:
        slug = label_to_slug[text.lower()]
        return slug, slug_to_label[slug]
    if text.lower() in slug_to_label:
        return text.lower(), slug_to_label[text.lower()]
    return "", text


def get_thread_last_model(cid: str, slug_to_label: dict[str, str]) -> tuple[str, str]:
    db_path = CONVERSATIONS_DIR / f"{cid}.db"
    if not db_path.is_file():
        return "", ""

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 6").fetchall()
    except Exception:
        return "", ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    fallback_label = ""
    for (data,) in rows:
        if not data:
            continue
        try:
            top = _parse_proto(data)
        except Exception:
            continue
        s19 = ""
        s21 = ""
        for fnum, wtype, val in top:
            if fnum == 1 and wtype == 2 and isinstance(val, bytes):
                try:
                    sub = _parse_proto(val)
                except Exception:
                    continue
                for sfn, swt, sval in sub:
                    if sfn == 19 and swt == 2 and isinstance(sval, bytes):
                        s19 = sval.decode("utf-8", errors="ignore").strip()
                    elif sfn == 21 and swt == 2 and isinstance(sval, bytes):
                        s21 = sval.decode("utf-8", errors="ignore").strip()
        for cand in (s21, s19):
            if cand:
                slug, label = normalize_model(cand, slug_to_label)
                if slug:
                    return slug, label
                if label and not fallback_label:
                    fallback_label = label
    return "", fallback_label


# ============================================================================
# 3. 有效会话列表加载（与原生 `/resume` 对齐）
# ============================================================================

def _parse_annotation_title(cid: str) -> str:
    ann = ANNOTATIONS_DIR / f"{cid}.pbtxt"
    if not ann.is_file():
        return ""
    try:
        raw = ann.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'title\s*:\s*"((?:\\.|[^"\\])*)"', raw)
        if m:
            return m.group(1).replace('\\"', '"').replace("\\\\", "\\").strip()
    except Exception:
        pass
    return ""


def load_sessions(slug_to_label: dict[str, str]) -> list[dict[str, Any]]:
    if not SUMMARIES_DB.is_file():
        return []

    meta_map: dict[str, Any] = {}
    if META_CACHE_FILE.is_file():
        try:
            meta_map = json.loads(META_CACHE_FILE.read_text(encoding="utf-8")).get("conversations", {})
        except Exception:
            meta_map = {}

    sessions: list[dict[str, Any]] = []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=1.5)
        rows = conn.execute(
            """
            SELECT conversation_id, title, preview
            FROM conversation_summaries
            WHERE step_count > 0 AND nesting_depth = 0 AND app_data_dir = 'antigravity-cli'
            ORDER BY last_modified_time DESC
            """
        ).fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for cid, db_title, db_preview in rows:
        # 仅列出磁盘上实际存在 .db 实体文件的会话，过滤残留空壳记录，与原生 /resume 对齐
        if not (CONVERSATIONS_DIR / f"{cid}.db").is_file():
            continue

        cache_entry = meta_map.get(cid, {})
        cache_summary = cache_entry.get("summary", {}) if isinstance(cache_entry, dict) else {}
        ann_title = _parse_annotation_title(cid)
        title = (
            ann_title
            or (cache_summary.get("Title") or "").strip()
            or (db_title or "").strip()
            or (db_preview or "").strip()
            or (cache_summary.get("Preview") or "").strip()
            or cid[:8]
        )
        title = re.sub(r"\s+", " ", title).strip()
        preview = re.sub(r"\s+", " ", (db_preview or cache_summary.get("Preview") or "")).strip()

        model_slug, model_label = get_thread_last_model(cid, slug_to_label)
        sessions.append(
            {
                "id": cid,
                "title": title,
                "preview": preview,
                "model_slug": model_slug,
                "model_label": model_label or "-",
            }
        )
    return sessions


# ============================================================================
# 4. 极简终端选择器（仅显示 Thread 名称 + 完整模型与思考等级）
# ============================================================================

def _char_width(ch: str) -> int:
    if ord(ch) < 32:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    return sum(_char_width(c) for c in text)


def _fit_cell(text: str, width: int, align_right: bool = False) -> str:
    if width <= 0:
        return ""
    w = _display_width(text)
    if w > width:
        if width == 1:
            return "…"
        out: list[str] = []
        cur = 0
        for ch in text:
            cw = _char_width(ch)
            if cur + cw > width - 1:
                break
            out.append(ch)
            cur += cw
        text = "".join(out) + "…"
        w = cur + 1
    pad = " " * max(0, width - w)
    return (pad + text) if align_right else (text + pad)


def _enable_win_vt_mode() -> Callable[[], None]:
    """在传统 Windows 控制台下显式开启 VT ANSI 解析，退出时恢复原模式。"""
    if os.name != "nt":
        return lambda: None
    try:
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        if handle in (0, -1):
            return lambda: None
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return lambda: None
        orig_mode = mode.value
        if not (orig_mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            kernel32.SetConsoleMode(handle, orig_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            return lambda: kernel32.SetConsoleMode(handle, orig_mode)
    except Exception:
        pass
    return lambda: None


def _render_picker_frame(
    sessions: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
    query: str,
    cursor: int,
    cols: int,
    lines: int,
) -> tuple[str, int, int, int]:
    """渲染单帧选单输出（极简单色调、紧凑版心、0 闪烁重绘）。"""
    cols = max(50, cols)
    lines = max(12, lines)
    page_size = max(5, lines - 7)

    total = len(filtered)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total == 0:
        cursor = 0
        page = 0
    else:
        cursor = max(0, min(cursor, total - 1))
        page = cursor // page_size

    start = page * page_size
    end = min(total, start + page_size)

    max_model_len = max((_display_width(s["model_label"]) for s in sessions), default=25)
    max_title_len = max((_display_width(s["title"]) for s in sessions), default=40)
    model_w = min(max(max_model_len, 25), max(18, cols // 2 - 4))
    title_w = min(max(max_title_len + 2, 42), max(16, cols - model_w - 8))
    if 6 + title_w + model_w > cols:
        title_w = max(12, cols - model_w - 6)
    box_w = min(cols, 6 + title_w + model_w)

    divider = f"\x1b[90m{'─' * box_w}\x1b[0m\x1b[K\r\n"

    buf: list[str] = ["\x1b[H"]
    header = f" Conversations ({total})  Page {page + 1}/{total_pages}"
    buf.append(f"\x1b[1;36m{_fit_cell(header, box_w)}\x1b[0m\x1b[K\r\n")
    buf.append(f"\x1b[36m{_fit_cell(f' > Search: {query}_', box_w)}\x1b[0m\x1b[K\r\n")
    buf.append(divider)

    if not filtered:
        buf.append("   \x1b[90mNo matching conversations.\x1b[0m\x1b[K\r\n")
        for _ in range(page_size - 1):
            buf.append("\x1b[K\r\n")
    else:
        for idx in range(start, end):
            s = filtered[idx]
            selected = idx == cursor
            prefix = "❯ " if selected else "  "
            t_col = _fit_cell(s["title"], title_w)
            m_col = _fit_cell(s["model_label"], model_w)
            row_str = f" {prefix}{t_col}  {m_col} "
            if selected:
                buf.append(f"\x1b[1;30;46m{_fit_cell(row_str, box_w)}\x1b[0m\x1b[K\r\n")
            else:
                buf.append(f"{_fit_cell(row_str, box_w)}\x1b[0m\x1b[K\r\n")
        for _ in range(page_size - (end - start)):
            buf.append("\x1b[K\r\n")

    buf.append(divider)
    hint = " ↑↓ Move  ←→ Page  Enter Resume  Esc Clear/Quit"
    buf.append(f"\x1b[90m{_fit_cell(hint, cols)}\x1b[0m\x1b[K\x1b[J")
    return "".join(buf), cursor, page_size, box_w


def run_picker(sessions: list[dict[str, Any]], initial_query: str = "") -> dict[str, Any] | None:
    if not sessions:
        print("No Antigravity CLI conversations found.")
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("agy -r requires an interactive terminal.", file=sys.stderr)
        return None

    query = initial_query
    cursor = 0

    def get_filtered() -> list[dict[str, Any]]:
        q = query.strip().lower()
        if not q:
            return sessions
        return [
            s
            for s in sessions
            if q in s["title"].lower()
            or q in s["preview"].lower()
            or q in s["model_label"].lower()
            or q in s["id"].lower()
        ]

    def render() -> tuple[list[dict[str, Any]], int]:
        nonlocal cursor
        size = shutil.get_terminal_size((100, 30))
        filtered = get_filtered()
        frame, cursor, page_size, _ = _render_picker_frame(
            sessions, filtered, query, cursor, size.columns, size.lines
        )
        sys.stdout.write(frame)
        sys.stdout.flush()
        return filtered, page_size

    restore_vt = _enable_win_vt_mode()
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
    sys.stdout.flush()
    try:
        while True:
            filtered, page_size = render()
            ch = msvcrt.getwch()

            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                if code == "H" and filtered:  # Up
                    cursor = (cursor - 1) % len(filtered)
                elif code == "P" and filtered:  # Down
                    cursor = (cursor + 1) % len(filtered)
                elif code in ("K", "I") and filtered:  # Left / PageUp
                    cursor = max(0, cursor - page_size)
                elif code in ("M", "Q") and filtered:  # Right / PageDown
                    cursor = min(len(filtered) - 1, cursor + page_size)
                continue

            if ch in ("\r", "\n"):
                if filtered:
                    return filtered[cursor]
                continue
            if ch == "\x03":  # Ctrl+C
                return None
            if ch == "\x1b":  # Esc
                if query:
                    query = ""
                    cursor = 0
                else:
                    return None
                continue
            if ch == "\x08":  # Backspace
                if query:
                    query = query[:-1]
                    cursor = 0
                continue
            if ord(ch) >= 32:
                query += ch
                cursor = 0
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        restore_vt()


# ============================================================================
# 5. CLI 启动封装
# ============================================================================

def _extract_flag_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _has_any_flag(args: list[str], flags: set[str]) -> bool:
    return any(a.split("=", 1)[0] in flags for a in args)


def _normalize_win_path(p: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(p)).rstrip("\\/")
    except Exception:
        return p.lower().rstrip("\\/")


def _resolve_continue_cid(cwd: str) -> str:
    if not LAST_CONVS_FILE.is_file():
        return ""
    try:
        mapping = json.loads(LAST_CONVS_FILE.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            return ""
        norm_cwd = _normalize_win_path(cwd)
        for k, v in mapping.items():
            if _normalize_win_path(str(k)) == norm_cwd and isinstance(v, str):
                return v.strip()
    except Exception:
        pass
    return ""


def _split_resume_args(rest: list[str]) -> tuple[str, list[str]]:
    value_flags = {
        "--add-dir", "--agent", "--effort", "--mode",
        "--model", "--project", "--settings", "--log-file",
    }
    query_parts: list[str] = []
    extra_flags: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in value_flags and i + 1 < len(rest):
            extra_flags.extend([a, rest[i + 1]])
            i += 2
        elif a.startswith("-"):
            extra_flags.append(a)
            i += 1
        else:
            query_parts.append(a)
            i += 1
    return " ".join(query_parts), extra_flags


def _inject_model_settings(args: list[str], model_label: str) -> tuple[list[str], bool]:
    """将会话历史模型无损注入或合并进 `--settings` JSON，已有显式 model 时跳过。"""
    if not model_label or model_label == "-" or _has_any_flag(args, {"--model"}):
        return list(args), False

    new_args = list(args)
    for i, a in enumerate(new_args):
        if a == "--settings":
            if i + 1 >= len(new_args):
                return new_args, False
            try:
                parsed = json.loads(new_args[i + 1])
            except Exception:
                return new_args, False
            if not isinstance(parsed, dict) or str(parsed.get("model") or "").strip():
                return new_args, False
            parsed["model"] = model_label
            new_args[i + 1] = json.dumps(parsed, ensure_ascii=False)
            return new_args, True
        if a.startswith("--settings="):
            raw_val = a.split("=", 1)[1]
            try:
                parsed = json.loads(raw_val)
            except Exception:
                return new_args, False
            if not isinstance(parsed, dict) or str(parsed.get("model") or "").strip():
                return new_args, False
            parsed["model"] = model_label
            new_args[i] = "--settings=" + json.dumps(parsed, ensure_ascii=False)
            return new_args, True

    new_args.extend(["--settings", json.dumps({"model": model_label}, ensure_ascii=False)])
    return new_args, True


def _launch_agy(args: list[str]) -> int:
    return subprocess.run([find_agy_exe(), *args]).returncode


def handle_cli(argv: list[str]) -> int:
    if _has_any_flag(argv, {"-p", "--print", "--prompt", "-v", "--version", "-h", "--help"}):
        return _launch_agy(argv)

    if _has_any_flag(argv, {"-r", "--resume"}):
        rest = [a for a in argv if a not in ("-r", "--resume")]
        initial_query, extra_flags = _split_resume_args(rest)
        slug_to_label = _load_models_map(allow_refresh=True)
        sessions = load_sessions(slug_to_label)
        chosen = run_picker(sessions, initial_query=initial_query)
        if not chosen:
            return 0

        base_args = ["--conversation", chosen["id"], *extra_flags]
        launch_args, injected = _inject_model_settings(base_args, chosen["model_label"])
        if injected:
            print(f"Resuming [{chosen['title']}] with model: {chosen['model_label']}")
        else:
            print(f"Resuming [{chosen['title']}]")
        return _launch_agy(launch_args)

    is_continue = _has_any_flag(argv, {"-c", "--continue"})
    conv_id = _extract_flag_value(argv, "--conversation") or ""
    if not conv_id and is_continue:
        conv_id = _resolve_continue_cid(os.getcwd())

    if conv_id and not _has_any_flag(argv, {"--model"}):
        slug_to_label = _load_models_map(allow_refresh=False)
        _, model_label = get_thread_last_model(conv_id, slug_to_label)
        if model_label:
            launch_args, injected = _inject_model_settings(argv, model_label)
            if injected:
                return _launch_agy(launch_args)

    return _launch_agy(argv)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--run":
        args = args[1:]
    return handle_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
