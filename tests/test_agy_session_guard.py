#!/usr/bin/env python3
"""Offline sandbox unit tests for agy_session_guard.py (pure standard library)."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agy_session_guard as guard  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _encode_str_field(fnum: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    return _encode_varint((fnum << 3) | 2) + _encode_varint(len(raw)) + raw


def _encode_gen_meta(s19: str = "", s21: str = "") -> bytes:
    inner = b""
    if s19:
        inner += _encode_str_field(19, s19)
    if s21:
        inner += _encode_str_field(21, s21)
    return _encode_varint((1 << 3) | 2) + _encode_varint(len(inner)) + inner


def _create_conv_db(path: Path, meta_rows: list[tuple[int, bytes]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    for idx, blob in meta_rows:
        conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (idx, blob))
    conn.commit()
    conn.close()


class TestAgySessionGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.slug_to_label = dict(guard.FALLBACK_SLUG_TO_LABEL)

    def test_normalize_model_aliases(self) -> None:
        self.assertEqual(
            guard.normalize_model("gemini-3.8-flash-n", self.slug_to_label),
            ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        )
        self.assertEqual(
            guard.normalize_model("gemini-4-hydrogen", self.slug_to_label),
            ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
        )
        self.assertEqual(
            guard.normalize_model("Gemini 4 Hydrogen (Orchestrator)", self.slug_to_label),
            ("gemini-pro-orchestrator-g1-pro", "Orchestrator (Argon)"),
        )
        self.assertEqual(
            guard.normalize_model("gemini-flash-worker-agy", self.slug_to_label),
            ("gemini-pro-orchestrator-g1-pro", "Orchestrator (Argon)"),
        )
        self.assertEqual(
            guard.normalize_model("gemini-4-argon-trial", self.slug_to_label),
            ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
        )
        self.assertEqual(
            guard.normalize_model("unknown-future-model", self.slug_to_label),
            ("", "unknown-future-model"),
        )

    def test_get_thread_last_model_cross_row_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conv_dir = Path(tmp)
            cid_ok = "conv-fallback-ok"
            _create_conv_db(
                conv_dir / f"{cid_ok}.db",
                [
                    (1, _encode_gen_meta("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)")),
                    (2, _encode_gen_meta("internal-unmapped-slug-v9", "")),
                ],
            )
            cid_unknown = "conv-unknown-only"
            _create_conv_db(
                conv_dir / f"{cid_unknown}.db",
                [
                    (1, _encode_gen_meta("custom-old", "")),
                    (2, _encode_gen_meta("custom-new", "")),
                ],
            )
            with mock.patch.object(guard, "CONVERSATIONS_DIR", conv_dir):
                self.assertEqual(
                    guard.get_thread_last_model(cid_ok, self.slug_to_label),
                    ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
                )
                self.assertEqual(
                    guard.get_thread_last_model(cid_unknown, self.slug_to_label),
                    ("", "custom-new"),
                )
                self.assertEqual(
                    guard.get_thread_last_model("missing-id", self.slug_to_label),
                    ("", ""),
                )

    def test_load_sessions_filters_ghosts_and_applies_title_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summaries_db = root / "conversation_summaries.db"
            conv_dir = root / "conversations"
            ann_dir = root / "annotations"
            meta_file = root / "cache" / "conversation_metadata.json"
            conv_dir.mkdir(parents=True)
            ann_dir.mkdir(parents=True)
            meta_file.parent.mkdir(parents=True)

            conn = sqlite3.connect(summaries_db)
            conn.execute(
                """
                CREATE TABLE conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    preview TEXT,
                    step_count INTEGER,
                    nesting_depth INTEGER,
                    app_data_dir TEXT,
                    last_modified_time INTEGER
                )
                """
            )
            rows = [
                ("c1-ann", "Old DB Title", "Preview 1", 5, 0, "antigravity-cli", 300),
                ("c2-meta", "Old DB Title 2", "Preview 2", 3, 0, "antigravity-cli", 200),
                ("c3-db", "DB Title 3", "Preview 3", 2, 0, "antigravity-cli", 100),
                ("c4-ghost", "Ghost No DB", "Ghost", 4, 0, "antigravity-cli", 400),
                ("c5-zero", "Zero Steps", "Zero", 0, 0, "antigravity-cli", 500),
                ("c6-subagent", "Nested Subagent", "Sub", 5, 1, "antigravity-cli", 600),
                ("c7-ide", "IDE Thread", "IDE", 8, 0, "antigravity", 700),
            ]
            conn.executemany(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            conn.close()

            _create_conv_db(conv_dir / "c1-ann.db", [(1, _encode_gen_meta("gemini-4-hydrogen", ""))])
            _create_conv_db(conv_dir / "c2-meta.db", [(1, _encode_gen_meta("gemini-3.8-flash-n", ""))])
            _create_conv_db(conv_dir / "c3-db.db", [(1, _encode_gen_meta("claude-opus-5-5-high", ""))])
            # Create .db for non-eligible rows too to verify SQL filter works
            _create_conv_db(conv_dir / "c5-zero.db", [(1, _encode_gen_meta("gemini-4-argon-high", ""))])
            _create_conv_db(conv_dir / "c6-subagent.db", [(1, _encode_gen_meta("gemini-4-argon-high", ""))])
            _create_conv_db(conv_dir / "c7-ide.db", [(1, _encode_gen_meta("gemini-4-argon-high", ""))])

            (ann_dir / "c1-ann.pbtxt").write_text(
                'title: "注释真值标题 \\"C1\\""\n', encoding="utf-8"
            )
            meta_file.write_text(
                json.dumps(
                    {
                        "conversations": {
                            "c1-ann": {"summary": {"Title": "Cache Title 1"}},
                            "c2-meta": {"summary": {"Title": "缓存标题 C2"}},
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(guard, "SUMMARIES_DB", summaries_db),
                mock.patch.object(guard, "CONVERSATIONS_DIR", conv_dir),
                mock.patch.object(guard, "ANNOTATIONS_DIR", ann_dir),
                mock.patch.object(guard, "META_CACHE_FILE", meta_file),
            ):
                loaded = guard.load_sessions(self.slug_to_label)

            self.assertEqual([s["id"] for s in loaded], ["c1-ann", "c2-meta", "c3-db"])
            self.assertEqual(loaded[0]["title"], '注释真值标题 "C1"')
            self.assertEqual(loaded[0]["model_label"], "Gemini 4 Hydrogen (High)")
            self.assertEqual(loaded[1]["title"], "缓存标题 C2")
            self.assertEqual(loaded[1]["model_label"], "Gemini 3.8 Flash (High)")
            self.assertEqual(loaded[2]["title"], "DB Title 3")
            self.assertEqual(loaded[2]["model_label"], "Claude Opus 5.5 (High)")

    def test_inject_model_settings(self) -> None:
        args, inj = guard._inject_model_settings(
            ["--conversation", "abc"], "Gemini 4 Hydrogen (XHigh)"
        )
        self.assertTrue(inj)
        self.assertEqual(
            args, ["--conversation", "abc", "--settings", '{"model": "Gemini 4 Hydrogen (XHigh)"}']
        )

        args2, inj2 = guard._inject_model_settings(
            ["--conversation", "abc", "--settings", '{"effort":"high"}'],
            "Orchestrator (Argon)",
        )
        self.assertTrue(inj2)
        self.assertEqual(json.loads(args2[3]), {"effort": "high", "model": "Orchestrator (Argon)"})

        args3, inj3 = guard._inject_model_settings(
            ["--conversation", "abc", '--settings={"autoApprove":true}'],
            "Orchestrator (Argon)",
        )
        self.assertTrue(inj3)
        self.assertTrue(args3[2].startswith("--settings="))
        self.assertEqual(
            json.loads(args3[2].split("=", 1)[1]),
            {"autoApprove": True, "model": "Orchestrator (Argon)"},
        )

        # Should not override explicit model in --settings or --model
        _, inj4 = guard._inject_model_settings(
            ["--settings", '{"model":"Claude Opus 5.5 (High)"}'], "Orchestrator (Argon)"
        )
        self.assertFalse(inj4)

        _, inj5 = guard._inject_model_settings(
            ["--model", "gemini-4-argon-high"], "Orchestrator (Argon)"
        )
        self.assertFalse(inj5)

        _, inj6 = guard._inject_model_settings(["--conversation", "abc"], "-")
        self.assertFalse(inj6)

    def test_handle_cli_routing_and_passthrough(self) -> None:
        with (
            mock.patch.object(guard, "_launch_agy", return_value=0) as m_launch,
            mock.patch.object(guard, "run_picker") as m_picker,
        ):
            rc = guard.handle_cli(["-p", "hello", "-r"])
            self.assertEqual(rc, 0)
            m_picker.assert_not_called()
            m_launch.assert_called_once_with(["-p", "hello", "-r"])

        with (
            mock.patch.object(guard, "_launch_agy", return_value=0) as m_launch,
            mock.patch.object(guard, "_load_models_map", return_value=self.slug_to_label),
            mock.patch.object(guard, "load_sessions", return_value=[]),
            mock.patch.object(
                guard,
                "run_picker",
                return_value={"id": "cid-1", "title": "演示会话", "model_label": "Orchestrator (Argon)"},
            ) as m_picker,
        ):
            rc = guard.handle_cli(["--resume", "演示", "--settings", '{"theme":"dark"}'])
            self.assertEqual(rc, 0)
            m_picker.assert_called_once()
            called = m_launch.call_args[0][0]
            self.assertEqual(called[:2], ["--conversation", "cid-1"])
            idx = called.index("--settings")
            self.assertEqual(
                json.loads(called[idx + 1]),
                {"theme": "dark", "model": "Orchestrator (Argon)"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            work_dir = tmp_dir / "MyWork"
            work_dir.mkdir()
            conv_dir = tmp_dir / "conversations"
            conv_dir.mkdir()
            last_convs = tmp_dir / "last_conversations.json"
            last_convs.write_text(
                json.dumps({str(work_dir): "cid-continue"}), encoding="utf-8"
            )
            _create_conv_db(
                conv_dir / "cid-continue.db",
                [(1, _encode_gen_meta("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"))],
            )
            with (
                mock.patch.object(guard, "LAST_CONVS_FILE", last_convs),
                mock.patch.object(guard, "CONVERSATIONS_DIR", conv_dir),
                mock.patch.object(guard, "_load_models_map", return_value=self.slug_to_label),
                mock.patch.object(guard, "_launch_agy", return_value=0) as m_launch,
                mock.patch("os.getcwd", return_value=str(work_dir)),
            ):
                rc = guard.handle_cli(["-c"])
                self.assertEqual(rc, 0)
                called = m_launch.call_args[0][0]
                self.assertEqual(called[0], "-c")
                self.assertEqual(called[1], "--settings")
                self.assertEqual(json.loads(called[2]), {"model": "Gemini 4 Hydrogen (XHigh)"})

    def test_render_picker_frame_monochrome_and_alignment(self) -> None:
        sessions = [
            {
                "id": "11111111",
                "title": "Agy会话管家-修复退出回写覆盖与全量验证",
                "preview": "",
                "model_slug": "gemini-pro-orchestrator-g1-pro",
                "model_label": "Orchestrator (Argon)",
            },
            {
                "id": "22222222",
                "title": "Agy Session Resume Review",
                "preview": "",
                "model_slug": "gemini-4-hydrogen-xhigh",
                "model_label": "Gemini 4 Hydrogen (XHigh)",
            },
        ]
        frame, cursor, page_size, box_w = guard._render_picker_frame(
            sessions, sessions, "", 0, cols=140, lines=30
        )
        self.assertEqual(cursor, 0)
        self.assertGreaterEqual(page_size, 5)
        self.assertLess(box_w, 140)
        self.assertNotIn("\x1b[2J", frame)
        self.assertNotIn("\x1b[38;5;", frame)
        self.assertNotIn("\x1b[48;5;", frame)
        self.assertNotIn("\x1b[48;2;", frame)
        self.assertNotIn("\x1b[1;30;46m", frame)
        self.assertIn("\x1b[1;38;2;122;162;247m", frame)
        self.assertIn("\x1b[38;2;122;162;247m", frame)
        self.assertIn("\x1b[38;2;65;72;104m", frame)
        self.assertIn("❯ ", frame)
        self.assertIn("↑↓ Move", frame)

        lines_out = frame.split("\r\n")
        # 列表上方应保留一行留白
        self.assertEqual(ANSI_RE.sub("", lines_out[3]), "")
        for raw_line in lines_out[:-1]:
            plain = ANSI_RE.sub("", raw_line)
            if plain.strip():
                self.assertEqual(guard._display_width(plain), box_w)

        empty_frame, empty_cur, _, _ = guard._render_picker_frame(
            sessions, [], "nomatch", 0, cols=100, lines=20
        )
        self.assertEqual(empty_cur, 0)
        self.assertIn("No matching conversations.", empty_frame)


if __name__ == "__main__":
    unittest.main()
