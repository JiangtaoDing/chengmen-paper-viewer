"""Comprehensive automated test suite for paper_lock.py & start_viewer.ps1.

Covers all audit findings from Issues #2, #3, and #5:
- Dual-protocol PreToolUse hooks (Antigravity + Claude Code)
- edit_file / write_file / MultiEdit anti-bypass (\iffalse, % comment, CRLF, BOM, cwd, post-end-document)
- run_command anti-bypass (safe_only chaining, no-space redirects, PS cmdlets/aliases, globs, subdirs)
  AND zero false positives on read-only commands
- Atomic concurrent lock writes (WinError 32 / lost-update regression)
- Action/mode validation (no unknown-action silent unlock)
- Non-ICML \title{} support, no phantom locks, LaTeX parser & caption edge cases, generic section_group_key
- .paper_viewer/ lock placement & companion `"tex"` hint resolution
- start_viewer.ps1 exit-code propagation, pre-flight checks, and parameter sets
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import paper_lock  # noqa: E402


def _run_hook(payload: dict | str, cwd: Path | None = None) -> tuple[int, dict, str]:
    raw_in = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    p = subprocess.run(
        [sys.executable, str(REPO_ROOT / "paper_lock.py"), "--hook"],
        input=raw_in,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(cwd) if cwd else None,
        timeout=20,
    )
    out = json.loads(p.stdout) if p.stdout.strip() else {}
    return p.returncode, out, p.stderr


class TestPathAndContractHelpers(unittest.TestCase):
    def test_suffix_stripping_and_case_insensitivity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(paper_lock.tex_path_for(root / "main.locks.json").name, "main.tex")
            self.assertEqual(paper_lock.tex_path_for(root / "main.LOCKS.JSON").name, "main.tex")
            self.assertEqual(paper_lock.lock_path_for(root / "main.LOCKS.JSON").name.lower(), "main.locks.json")
            # .paper_viewer/ lock file resolves back to parent dir's .tex
            pv = root / ".paper_viewer"
            pv.mkdir()
            self.assertEqual(paper_lock.tex_path_for(pv / "main.locks.json"), (root / "main.tex").resolve())
            # When .paper_viewer/ exists, lock_path_for prefers it
            self.assertEqual(paper_lock.lock_path_for(root / "main.pdf"), (pv / "main.locks.json").resolve())
            # Explicit lock_dir overrides default
            custom = root / "custom_locks"
            self.assertEqual(
                paper_lock.lock_path_for(root / "main.pdf", lock_dir=custom),
                (custom / "main.locks.json").resolve(),
            )

    def test_tex_hint_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dir = root / "src"
            build_dir = root / "build"
            src_dir.mkdir()
            build_dir.mkdir()
            src_tex = src_dir / "paper.tex"
            src_tex.write_text("\\documentclass{article}\\begin{document}Hi\\end{document}\n", encoding="utf-8")
            pdf_path = build_dir / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            resolved = paper_lock.tex_path_for(pdf_path, tex_hint="../src/paper.tex", base_dir=build_dir)
            self.assertEqual(resolved, src_tex.resolve())
            # Non-existent hint falls back cleanly to sibling .tex
            fallback = paper_lock.tex_path_for(pdf_path, tex_hint="../src/missing.tex", base_dir=build_dir)
            self.assertEqual(fallback, (build_dir / "paper.tex").resolve())

    def test_action_and_mode_validation_and_both_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tex = root / "main.tex"
            pdf = root / "main.pdf"
            tex.write_text(
                "\\documentclass{article}\n\\begin{document}\n"
                "\\section{Intro}\nFirst locked paragraph content here.\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            pdf.write_bytes(b"%PDF-1.4\n")
            items = [{"id": "1-P1", "section": "§1 Intro", "title": "Intro", "anchor": "1. Intro", "en_preview": "First locked paragraph"}]

            # Contract positional signature: (pdf_path, action, ids, mode, items)
            res = paper_lock.update_locks_for_pdf(pdf, "lock", ["1-P1"], None, items)
            self.assertEqual(res["locked"], ["1-P1"])
            self.assertEqual(res["unmatched"], [])

            # Unknown action MUST raise ValueError, never silently unlock
            with self.assertRaises(ValueError):
                paper_lock.update_locks_for_pdf(pdf, "unknown_noop", ["1-P1"], None, items)
            self.assertEqual(paper_lock.get_locks_for_pdf(pdf)["locked"], ["1-P1"])

            # Invalid mode raises ValueError
            with self.assertRaises(ValueError):
                paper_lock.update_locks_for_pdf(pdf, "set_mode", mode="invalid_mode")

            # Legacy positional signature: (pdf_path, items, action, ids, mode)
            res2 = paper_lock.update_locks_for_pdf(pdf, items, "unlock", ["1-P1"])
            self.assertEqual(res2["locked"], [])

            # Unlock works even when items=[] (e.g., companion JSON temporarily broken)
            paper_lock.update_locks_for_pdf(pdf, action="lock", ids=["1-P1"], items=items)
            res3 = paper_lock.update_locks_for_pdf(pdf, action="unlock", ids=["1-P1"], items=[])
            self.assertEqual(res3["locked"], [])


class TestLaTeXParserAndSectionGrouping(unittest.TestCase):
    def test_non_icml_title_and_no_phantom_locks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tex = root / "neurips.tex"
            pdf = root / "neurips.pdf"
            tex.write_text(
                "\\documentclass{article}\n"
                "\\title[Short]{A General Non-ICML Research Title}\n"
                "\\begin{document}\n"
                "\\maketitle\n"
                "\\begin{abstract}\nAbstract text here.\n\\end{abstract}\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            pdf.write_bytes(b"%PDF-1.4\n")
            items = [
                {"id": "Title", "section": "标题", "title": "Title", "anchor": "A General Non-ICML Research Title"},
                {"id": "MissingItem", "section": "§9", "title": "Ghost", "anchor": "Totally nonexistent paragraph xyz"},
            ]
            res = paper_lock.update_locks_for_pdf(pdf, action="lock", ids=["Title", "MissingItem"], items=items)
            self.assertIn("Title", res["locks"])
            self.assertIn("\\title[Short]{A General Non-ICML Research Title}", res["locks"]["Title"]["tex_snippet"])
            # Phantom lock MUST NOT be created for unmatched item
            self.assertNotIn("MissingItem", res["locks"])
            self.assertEqual(res["unmatched"], ["MissingItem"])
            self.assertTrue(res["warnings"])

    def test_comments_appendixname_optional_section_multiline_and_captions(self) -> None:
        tex = (
            "% Note: define macros before \\begin{document}\n"
            "\\documentclass{article}\n"
            "\\newcommand{\\mytag}{foo\\1}\n"
            "\\begin{document}\n"
            "\\section[Short Intro]{Full Introduction Title}\n"
            "See \\appendixname~A and \\mytag for more details.\n\n"
            "\\section{\nSecond Main Section\n}\n"
            "Body of second section.\n\n"
            "\\begin{figure}[t]\n"
            "\\captionsetup{font=small}\n"
            "% \\end{figure}\n"
            "\\includegraphics{plot.pdf}\n"
            "\\caption[Short \\textbf{Summary}]{Real caption with set $\\left\\{x\\right.$ and $\\}$ plus tail words.}\n"
            "\\end{figure}\n\n"
            "\\section*{Acknowledgements}\n"
            "We thank the reviewers.\n"
            "\\end{document}\n"
        )
        # Must not crash on macro backref \1
        parsed = paper_lock._parse_tex_structure(tex)
        # Preamble comment must not pull \documentclass into prose blocks
        self.assertNotIn("\\documentclass", parsed["blocks"][0]["raw"])
        # \section[Short]{Full} must be section 1, and \appendixname must NOT trigger appendix mode!
        sec1_norm = parsed["blocks"][0]["norm"]
        self.assertTrue(sec1_norm.startswith("1 "), f"Expected section 1 prefix, got: {sec1_norm!r}")
        self.assertIn("appendixname", parsed["blocks"][0]["raw"])
        # Multiline \section must be section 2 (not appendix 'a')
        sec2_block = [b for b in parsed["blocks"] if "second main section" in b["norm"]][0]
        self.assertTrue(sec2_block["norm"].startswith("2 "), f"Expected section 2 prefix, got: {sec2_block['norm']!r}")
        # Figure must NOT terminate early at commented `% \end{figure}` and caption must skip \captionsetup & optional [...]
        self.assertEqual(len(parsed["floats"]), 1)
        fl = parsed["floats"][0]
        self.assertEqual(fl["label_num"], "1")
        self.assertIn("\\includegraphics{plot.pdf}", fl["raw"])
        self.assertIn("real caption with set", fl["norm_cap"])
        self.assertIn("plus tail words", fl["norm_cap"])
        self.assertNotIn("font small", fl["norm_cap"])

        # Last matched prose item must NOT swallow unlisted \section*{Acknowledgements}
        items = [
            {"id": "1-P1", "anchor": "1. Full Introduction Title", "en_preview": "See appendixname"},
            {"id": "2-P1", "anchor": "2. Second Main Section", "en_preview": "Body of second section"},
        ]
        spans = paper_lock.locate_tex_spans(tex, items)
        self.assertIn("2-P1", spans)
        self.assertNotIn("Acknowledgements", spans["2-P1"]["tex_snippet"])

    def test_fallback_span_does_not_mislock_earlier_table_or_embedded_float(self) -> None:
        tex = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{table}[t]\n\\caption{Table One Unlocked}\nRow 1\n\\end{table}\n\n"
            "\\begin{table}[t]\n\\caption{Table Two Unlocked}\nRow 2\n\\end{table}\n\n"
            "\\begin{table}[t]\n\\caption{Table Three Locked}\nRow 3 slightly edited\n\\end{table}\n"
            "\\end{document}\n"
        )
        entry = {
            "id": "Tab3",
            "tex_snippet": "\\begin{table}[t]\n\\caption{Table Three Locked}\nRow 3 original\n\\end{table}",
            "tex_chunks": ["\\begin{table}[t]\n\\caption{Table Three Locked}\nRow 3 original\n\\end{table}"],
            "tex_start_line": 12,
            "tex_end_line": 15,
        }
        spans = paper_lock._find_lock_char_spans(tex, entry)
        self.assertEqual(len(spans), 1)
        locked_text = tex[spans[0][0]:spans[0][1]]
        self.assertIn("Table Three Locked", locked_text)
        self.assertNotIn("Table One Unlocked", locked_text)

        # Embedded float inside multi-chunk prose span must not be locked on line fallback
        ftex = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\section{Intro}\nFirst half.\n\n"
            "\\begin{figure}[t]\n\\caption{Figure One.}\n\\end{figure}\n\n"
            "Second half.\n"
            "\\section{Method}\nMethod body.\n"
            "\\end{document}\n"
        )
        fitems = [
            {"id": "1-P1", "anchor": "1. Intro", "en_preview": "First half"},
            {"id": "Fig1", "kind": "figure", "anchor": "Figure 1.", "en_preview": "Figure One"},
            {"id": "2-P1", "anchor": "2. Method", "en_preview": "Method body"},
        ]
        fspans = paper_lock.locate_tex_spans(ftex, fitems)
        self.assertEqual(len(fspans["1-P1"]["tex_chunk_lines"]), 2)
        fb_entry = {
            "id": "1-P1",
            "tex_start_line": fspans["1-P1"]["start_line"],
            "tex_end_line": fspans["1-P1"]["end_line"],
            "tex_chunk_lines": fspans["1-P1"]["tex_chunk_lines"],
            "tex_snippet": "NON_MATCHING_SNIPPET",
            "tex_chunks": ["NON_MATCHING_SNIPPET"],
        }
        fb_spans = paper_lock._find_lock_char_spans(ftex, fb_entry)
        covered = "\n".join(ftex[a:b] for a, b in fb_spans)
        self.assertNotIn("Figure One", covered)

    def test_generic_section_group_key(self) -> None:
        self.assertEqual(paper_lock.section_group_key({"id": "Fig1", "section": "图 1", "kind": "figure"}), "图表")
        self.assertEqual(paper_lock.section_group_key({"id": "Tab2", "section": "表 2", "kind": "table"}), "图表")
        self.assertEqual(paper_lock.section_group_key({"id": "Fig1", "section": "§1 Overview", "kind": "figure"}), "§1")
        self.assertEqual(paper_lock.section_group_key({"id": "MAIN-P1", "section": "Main Results"}), "Main Results")
        self.assertEqual(paper_lock.section_group_key({"id": "TRAIN-1", "section": "Training"}), "Training")
        self.assertEqual(paper_lock.section_group_key({"id": "TabAll", "section": "All Results"}), "All Results")
        self.assertEqual(paper_lock.section_group_key({"id": "Impact", "section": "Broader Impact"}), "声明与影响")
        self.assertEqual(paper_lock.section_group_key({"id": "AppB-P1", "section": "Appendix B"}), "附录 B")


class TestConcurrencyAndEncoding(unittest.TestCase):
    def test_concurrent_lock_updates_no_errors_no_lost_updates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tex = root / "conc.tex"
            pdf = root / "conc.pdf"
            lines = ["\\documentclass{article}\n\\begin{document}\n"]
            items = []
            for k in range(1, 13):
                lines.append(f"\\section{{Section {k}}}\nUnique paragraph number {k} content.\n\n")
                items.append({
                    "id": f"{k}-P1",
                    "section": f"§{k}",
                    "title": f"Sec {k}",
                    "anchor": f"{k}. Section {k}",
                    "en_preview": f"Unique paragraph number {k}",
                })
            lines.append("\\end{document}\n")
            tex.write_text("".join(lines), encoding="utf-8")
            pdf.write_bytes(b"%PDF-1.4\n")

            errors: list[Exception] = []
            barrier = threading.Barrier(12)

            def worker(idx: int) -> None:
                barrier.wait()
                try:
                    paper_lock.update_locks_for_pdf(pdf, action="lock", ids=[f"{idx}-P1"], items=items)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 13)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            final = paper_lock.get_locks_for_pdf(pdf)
            self.assertEqual(len(final["locked"]), 12)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_latin1_tex_and_utf8_bom_locks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tex = root / "lat.tex"
            pdf = root / "lat.pdf"
            tex.write_bytes(
                b"\\documentclass{article}\n% Copyright \xa9 2026\n\\begin{document}\n"
                b"\\section{Intro}\nSafe text.\n\\end{document}\n"
            )
            pdf.write_bytes(b"%PDF-1.4\n")
            items = [{"id": "1-P1", "anchor": "1. Intro", "en_preview": "Safe text"}]
            res = paper_lock.update_locks_for_pdf(pdf, action="lock", ids=["1-P1"], items=items)
            self.assertEqual(res["locked"], ["1-P1"])

            # Prepend UTF-8 BOM to .locks.json -> load_locks MUST still preserve locks!
            lp = paper_lock.lock_path_for(pdf)
            lp.write_bytes(b"\xef\xbb\xbf" + lp.read_bytes())
            loaded = paper_lock.load_locks(pdf, strict=True)
            self.assertEqual(loaded["locked"], ["1-P1"])


class TestPreToolUseHooks(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="lock_hook_test_")
        self.root = Path(self._td.name)
        self.paper_dir = self.root / "paper"
        self.paper_dir.mkdir()
        self.hooks_cwd = self.root / "hooks_cwd"
        self.hooks_cwd.mkdir()

        self.tex = self.paper_dir / "main.tex"
        self.pdf = self.paper_dir / "main.pdf"
        self.tex_text = (
            "\\documentclass{article}\n"
            "\\title{Locked Paper Title}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\section{Introduction}\n"
            "Large language models have revolutionized reasoning in science.\n\n"
            "Second paragraph is unlocked and freely editable.\n"
            "\\end{document}\n"
        )
        self.tex.write_text(self.tex_text, encoding="utf-8", newline="\n")
        self.pdf.write_bytes(b"%PDF-1.4\n")
        self.items = [
            {"id": "Title", "section": "标题", "title": "Title", "anchor": "Locked Paper Title"},
            {"id": "1-P1", "section": "§1 Intro", "title": "Intro P1", "anchor": "1. Introduction", "en_preview": "Large language models"},
            {"id": "1-P2", "section": "§1 Intro", "title": "Intro P2", "anchor": "Second paragraph", "en_preview": "Second paragraph"},
        ]
        self.ldata = paper_lock.update_locks_for_pdf(self.pdf, action="lock", ids=["Title", "1-P1"], items=self.items)
        self.locks_path = paper_lock.lock_path_for(self.pdf)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_claude_code_protocol_blocks_edit_write_multiedit_bash(self) -> None:
        # 1. Claude Edit on locked paragraph
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(self.tex),
                "old_string": "Large language models have revolutionized reasoning in science.",
                "new_string": "HACKED",
            },
            "cwd": str(self.root),
        })
        self.assertEqual(rc, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 2. Claude Edit on unlocked paragraph -> allowed ({})
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(self.tex),
                "old_string": "Second paragraph is unlocked and freely editable.",
                "new_string": "Second paragraph revised cleanly.",
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out, {})

        # 3. Claude Write overwriting .locks.json -> denied
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.locks_path), "content": "{}"},
            "cwd": str(self.root),
        })
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 4. Claude MultiEdit touching locked title -> denied
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(self.tex),
                "edits": [{"old_string": "\\title{Locked Paper Title}", "new_string": "\\title{Changed}"}],
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 4b. Claude MultiEdit sequential chained edit targeting locked span -> denied
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(self.tex),
                "edits": [
                    {
                        "old_string": "\\maketitle",
                        "new_string": "\\maketitle\n% CHAIN_MARKER",
                    },
                    {
                        "old_string": "% CHAIN_MARKER\n\\section{Introduction}\nLarge language models have revolutionized reasoning in science.",
                        "new_string": "% CHAIN_MARKER\n\\section{Introduction}\nHACKED BY CHAIN",
                    },
                ],
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 4c. Claude MultiEdit sequential chained edit on unlocked paragraph -> allowed
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(self.tex),
                "edits": [
                    {
                        "old_string": "Second paragraph is unlocked and freely editable.",
                        "new_string": "% NOTE\nSecond paragraph is unlocked and freely editable.",
                    },
                    {
                        "old_string": "% NOTE\nSecond paragraph is unlocked and freely editable.",
                        "new_string": "% NOTE\nSecond paragraph cleanly edited.",
                    },
                ],
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out, {})

        # 5. Claude Bash deleting lock file -> denied
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -f {self.locks_path}"},
            "cwd": str(self.root),
        })
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 6. force_ask maps to Claude 'ask'
        paper_lock.update_locks_for_pdf(self.pdf, action="set_mode", mode="force_ask")
        rc, out, _ = _run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(self.tex),
                "old_string": "Large language models have revolutionized reasoning in science.",
                "new_string": "HACKED",
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_edit_and_write_anti_bypass_iffalse_comment_crlf_cwd_corrupt(self) -> None:
        snip = self.ldata["locks"]["1-P1"]["tex_snippet"]

        # 1. \iffalse wrap bypass in edit_file -> DENIED
        _, out, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {
                    "Path": str(self.tex),
                    "Target": snip,
                    "Replacement": "\\iffalse\n" + snip + "\n\\fi\nREWRITTEN",
                },
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["decision"], "deny")

        # 2. Two-step unclosed \iffalse inserted above locked span -> DENIED
        _, out, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {
                    "Path": str(self.tex),
                    "Target": "\\maketitle\n",
                    "Replacement": "\\maketitle\n\\iffalse\n",
                },
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["decision"], "deny")

        # 3. Single-line % comment bypass on locked title -> DENIED
        _, out, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {
                    "Path": str(self.tex),
                    "Target": "\\title{Locked Paper Title}",
                    "Replacement": "% \\title{Locked Paper Title}\n\\title{Hacked}",
                },
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["decision"], "deny")

        # 4. write_file \iffalse wrap -> DENIED
        _, out, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "write_file",
                "args": {
                    "Path": str(self.tex),
                    "Content": "\\documentclass{article}\n\\begin{document}\n\\iffalse\n\\title{Locked Paper Title}\n" + snip + "\n\\fi\nNEW\n\\end{document}\n",
                    "Overwrite": True,
                },
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out["decision"], "deny")

        # 5. Relative path resolved against data['cwd'] even when hook process CWD differs -> DENIED
        _, out, _ = _run_hook(
            {
                "hookEventName": "PreToolUse",
                "toolCall": {
                    "name": "edit_file",
                    "args": {
                        "Path": "paper/main.tex",
                        "Target": "Large language models have revolutionized reasoning in science.",
                        "Replacement": "HACKED",
                    },
                },
                "cwd": str(self.root),
                "workspacePaths": [str(self.root)],
            },
            cwd=self.hooks_cwd,
        )
        self.assertEqual(out["decision"], "deny")

        # 6. CRLF file: edit_file with CRLF Target -> DENIED; write_file with identical CRLF -> ALLOWED
        crlf_tex = self.paper_dir / "crlf.tex"
        crlf_pdf = self.paper_dir / "crlf.pdf"
        crlf_raw = self.tex_text.replace("\n", "\r\n")
        crlf_tex.write_bytes(crlf_raw.encode("utf-8"))
        crlf_pdf.write_bytes(b"%PDF-1.4\n")
        paper_lock.update_locks_for_pdf(crlf_pdf, action="lock", ids=["1-P1"], items=self.items)

        _, out_crlf_edit, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {
                    "Path": str(crlf_tex),
                    "Target": "\\section{Introduction}\r\nLarge language models have revolutionized reasoning in science.",
                    "Replacement": "HACKED",
                },
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out_crlf_edit["decision"], "deny")

        _, out_crlf_write, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {"name": "write_file", "args": {"Path": str(crlf_tex), "Content": crlf_raw, "Overwrite": True}},
            "cwd": str(self.root),
        })
        self.assertEqual(out_crlf_write["decision"], "allow")

        # 7. Corrupted .locks.json fails closed (deny) + logs diagnostic to stderr
        bad_tex = self.paper_dir / "bad.tex"
        bad_pdf = self.paper_dir / "bad.pdf"
        bad_tex.write_text(self.tex_text, encoding="utf-8")
        bad_pdf.write_bytes(b"%PDF-1.4\n")
        paper_lock.lock_path_for(bad_pdf).write_text("{corrupt json", encoding="utf-8")
        _, out_bad, err_bad = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {"Path": str(bad_tex), "Target": "Large language models", "Replacement": "X"},
            },
            "cwd": str(self.root),
        })
        self.assertEqual(out_bad["decision"], "deny")
        self.assertIn("Corrupted lock file", err_bad)

    def test_run_command_guard_blocks_bypasses_and_allows_readonly(self) -> None:
        # Also create a locked paper in non-`paper/` subdir `manuscript/`
        ms_dir = self.root / "manuscript"
        ms_dir.mkdir()
        (ms_dir / "draft.tex").write_text(self.tex_text, encoding="utf-8")
        (ms_dir / "draft.pdf").write_bytes(b"%PDF-1.4\n")
        paper_lock.update_locks_for_pdf(ms_dir / "draft.pdf", action="lock", ids=["1-P1"], items=self.items)

        must_deny = [
            "python -c \"open('paper/main.tex','w').write('hacked')\" && pdflatex paper/main.tex",
            "python -c \"open('paper/main.tex', encoding='utf-8', mode='w').close()\"",
            "python -c \"open('paper/main.tex', 'r+').truncate(0)\"",
            "Copy-Item evil.tex paper/main.tex; pdflatex paper/main.tex",
            "echo hacked > paper/main.tex && pdflatex paper/main.tex",
            "echo hacked 1>paper/main.tex",
            "echo hacked 1>>paper/main.tex",
            "echo hacked >|paper/main.tex",
            "echo hacked 1>|paper/main.tex",
            "echo hacked &>paper/main.tex",
            "echo hacked >&paper/main.tex",
            "echo {} >paper/main.locks.json",
            "echo {} 1>paper/main.locks.json",
            "echo {} >|paper/main.locks.json",
            "python -c \"import os; os.remove('paper/main.locks.json')\"",
            "Rename-Item paper/main.locks.json paper/x.bak",
            "Clear-Content paper/main.locks.json",
            "Remove-Item paper/*.locks.*",
            "mv evil.tex paper/main.tex",
            "Set-Content manuscript/draft.tex 'hacked'",
            "cat evil.tex | tee paper/main.tex",
        ]
        for cmd in must_deny:
            _, out, _ = _run_hook({
                "hookEventName": "PreToolUse",
                "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
                "cwd": str(self.root),
                "workspacePaths": [str(self.root)],
            })
            self.assertEqual(out["decision"], "deny", f"Expected DENY for: {cmd}")

        must_allow = [
            "python -c \"print(open('paper/main.tex').read())\"",
            "python -c \"print(open('paper/main.tex', 'r', encoding='utf-8').read())\"",
            "grep Introduction paper/main.tex > hits.txt",
            "grep Introduction paper/main.tex 1>hits.txt",
            "pdflatex -interaction=nonstopmode paper/main.tex > build.log 2>&1",
            "pdflatex -interaction=nonstopmode paper/main.tex 2>&1",
            "Select-String -Path paper/main.tex -Pattern Introduction",
        ]
        for cmd in must_allow:
            _, out, _ = _run_hook({
                "hookEventName": "PreToolUse",
                "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
                "cwd": str(self.root),
                "workspacePaths": [str(self.root)],
            })
            self.assertEqual(out["decision"], "allow", f"Expected ALLOW for read-only command: {cmd}")

    def test_paper_viewer_subdir_and_out_of_dir_tex_hook_protection(self) -> None:
        # 1. `.paper_viewer/main.locks.json` automatically protects `main.tex`
        pv_root = self.root / "pv_proj"
        (pv_root / ".paper_viewer").mkdir(parents=True)
        pv_tex = pv_root / "paper.tex"
        pv_pdf = pv_root / "paper.pdf"
        pv_tex.write_text(self.tex_text, encoding="utf-8")
        pv_pdf.write_bytes(b"%PDF-1.4\n")
        res = paper_lock.update_locks_for_pdf(pv_pdf, action="lock", ids=["1-P1"], items=self.items)
        self.assertEqual(res["locked"], ["1-P1"])
        self.assertTrue((pv_root / ".paper_viewer" / "paper.locks.json").is_file())
        _, out, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {
                    "Path": str(pv_tex),
                    "Target": "Large language models have revolutionized reasoning in science.",
                    "Replacement": "HACKED",
                },
            },
            "cwd": str(pv_root),
            "workspacePaths": [str(pv_root)],
        })
        self.assertEqual(out["decision"], "deny")

        # 2. Out-of-directory build (`build/out.pdf` + `src/out.tex` via tex_path) protects `src/out.tex`
        od_root = self.root / "out_of_dir"
        (od_root / "src").mkdir(parents=True)
        (od_root / "build").mkdir(parents=True)
        src_tex = od_root / "src" / "out.tex"
        bld_pdf = od_root / "build" / "out.pdf"
        src_tex.write_text(self.tex_text, encoding="utf-8")
        bld_pdf.write_bytes(b"%PDF-1.4\n")
        res2 = paper_lock.update_locks_for_pdf(bld_pdf, action="lock", ids=["1-P1"], items=self.items, tex_path=src_tex)
        self.assertEqual(res2["locked"], ["1-P1"])
        _, out2, _ = _run_hook({
            "hookEventName": "PreToolUse",
            "toolCall": {
                "name": "edit_file",
                "args": {
                    "Path": str(src_tex),
                    "Target": "Large language models have revolutionized reasoning in science.",
                    "Replacement": "HACKED",
                },
            },
            "cwd": str(od_root),
            "workspacePaths": [str(od_root)],
        })
        self.assertEqual(out2["decision"], "deny")


class TestCliAndPowerShellLauncher(unittest.TestCase):
    def test_cli_status_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "demo.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            p = subprocess.run(
                [sys.executable, str(REPO_ROOT / "paper_lock.py"), "status", str(pdf)],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual(p.returncode, 0)
            info = json.loads(p.stdout)
            self.assertEqual(info["locked_count"], 0)
            self.assertEqual(info["mode"], "deny")

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell"), "Windows PowerShell only")
    def test_start_viewer_ps1_preflight_and_exit_codes(self) -> None:
        ps1 = REPO_ROOT / "start_viewer.ps1"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "sample.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")

            # 1. Missing PDF returns non-zero exit code and does NOT print Starting banner
            r_miss = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Pdf", str(root / "missing.pdf")],
                capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(r_miss.returncode, 0)
            self.assertNotIn("Starting Chengmen Paper Viewer", r_miss.stdout)

            # 2. Out-of-range port rejected by ValidateRange with non-zero exit code
            r_port = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Pdf", str(pdf), "-Port", "99999"],
                capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(r_port.returncode, 0)

            # 3. Occupied port detected before launching Python and exits non-zero
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            try:
                r_busy = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Pdf", str(pdf), "-Port", str(busy_port)],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertNotEqual(r_busy.returncode, 0)
                self.assertIn("already in use", r_busy.stderr)
            finally:
                sock.close()

            # 4. -PaperDir parameter set supported (non-existent dir rejected cleanly with non-zero)
            r_pdir = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-PaperDir", str(root / "no_dir")],
                capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(r_pdir.returncode, 0)
            self.assertIn("Paper directory not found", r_pdir.stderr)

            # 5. Missing -Zh file rejected cleanly with non-zero
            r_zh = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Pdf", str(pdf), "-Zh", str(root / "nope_zh.json")],
                capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(r_zh.returncode, 0)
            self.assertIn("Companion JSON file not found", r_zh.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
