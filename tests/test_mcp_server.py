"""Automated tests for Chengmen Paper Viewer MCP App server & plugin bundle."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import mcp_server  # noqa: E402
import paper_lock  # noqa: E402


def _make_sample_pdf(path: Path) -> None:
    doc = pymupdf.open()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 72), "Sample Research Paper Title", fontsize=16)
    p1.insert_text((72, 110), "1. Introduction", fontsize=12)
    p1.insert_text((72, 132), "Large language models accelerate scientific research.", fontsize=10)
    p1.insert_text((72, 170), "Second paragraph describes our method in detail.", fontsize=10)
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 72), "2. Experiments", fontsize=12)
    p2.insert_text((72, 96), "Empirical results confirm strong accuracy gains.", fontsize=10)
    doc.save(str(path))
    doc.close()


def _make_sample_paper(root: Path, *, use_paper_viewer_dir: bool = False) -> tuple[Path, Path, Path]:
    pdf = root / "demo.pdf"
    tex = root / "demo.tex"
    _make_sample_pdf(pdf)
    tex.write_text(
        "\\documentclass{article}\n"
        "\\title{Sample Research Paper Title}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\section{Introduction}\n"
        "Large language models accelerate scientific research.\n\n"
        "Second paragraph describes our method in detail.\n\n"
        "\\section{Experiments}\n"
        "Empirical results confirm strong accuracy gains.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    zh_dir = (root / ".paper_viewer") if use_paper_viewer_dir else root
    zh_dir.mkdir(parents=True, exist_ok=True)
    zh = zh_dir / "demo_zh.json"
    zh.write_text(
        json.dumps({
            "file": "demo.pdf",
            "tex": str(tex),
            "title_zh": "示例科研论文标题",
            "items": [
                {
                    "id": "Title",
                    "page": 1,
                    "section": "标题",
                    "title": "论文标题",
                    "anchor": "Sample Research Paper Title",
                    "en_preview": "Sample Research Paper Title",
                    "zh": "示例科研论文标题",
                },
                {
                    "id": "1-P1",
                    "page": 1,
                    "section": "§1 Introduction",
                    "title": "研究引言",
                    "anchor": "1. Introduction",
                    "en_preview": "Large language models accelerate scientific research.",
                    "zh": "大语言模型加速了科学研究。",
                },
                {
                    "id": "1-P2",
                    "page": 1,
                    "section": "§1 Introduction",
                    "title": "方法概述",
                    "anchor": "Second paragraph describes",
                    "en_preview": "Second paragraph describes our method in detail.",
                    "zh": "第二段详细介绍了我们的方法。",
                },
                {
                    "id": "2-P1",
                    "page": 2,
                    "section": "§2 Experiments",
                    "title": "实验结果",
                    "anchor": "2. Experiments",
                    "en_preview": "Empirical results confirm strong accuracy gains.",
                    "zh": "实证结果确认了显著的准确率提升。",
                },
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pdf, tex, zh


class TestPluginManifestsAndAssets(unittest.TestCase):
    def test_plugin_json_schema_strict(self) -> None:
        pj = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        allowed = {
            "name", "displayName", "version", "description", "logo",
            "suggestedPrompts", "author", "homepage", "repository", "disabled",
        }
        self.assertTrue(set(pj.keys()).issubset(allowed), f"Disallowed keys: {set(pj.keys()) - allowed}")
        self.assertEqual(pj["name"], "chengmen-paper-viewer")
        self.assertEqual(pj["displayName"], "Chengmen Paper Viewer")
        self.assertEqual(pj["version"], "0.3.0")
        self.assertEqual(pj["logo"], "assets/logo.svg")
        self.assertEqual(len(pj["suggestedPrompts"]), 3)
        self.assertTrue(all(isinstance(p, str) and p.strip() for p in pj["suggestedPrompts"]))
        self.assertTrue((REPO_ROOT / pj["logo"]).is_file())

    def test_mcp_config_and_hooks_and_bundle_files(self) -> None:
        mc = json.loads((REPO_ROOT / "mcp_config.json").read_text(encoding="utf-8"))
        srv = mc["mcpServers"]["chengmen_viewer"]
        self.assertEqual(srv["command"], "python")
        self.assertEqual(srv["args"], ["-B", "${PLUGIN_ROOT}/mcp_server.py", "--data-dir", "${PLUGIN_DATA}"])

        hk = json.loads((REPO_ROOT / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("chengmen-paper-lock", hk)
        pre = hk["chengmen-paper-lock"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], "edit_file|write_file|run_command")
        self.assertEqual(pre["hooks"][0]["command"], "python paper_lock.py --hook")
        self.assertEqual(pre["hooks"][0]["timeout"], 10)

        logo_txt = (REPO_ROOT / "assets" / "logo.svg").read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 128 128"', logo_txt)

        rules_txt = (REPO_ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertFalse(rules_txt.lstrip().startswith("---"), "rules/AGENTS.md must have no YAML frontmatter")
        self.assertIn("open_paper", rules_txt)

        skill_txt = (REPO_ROOT / "skills" / "chengmen-paper-viewer" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: chengmen-paper-viewer", skill_txt)
        self.assertIn("open_paper", skill_txt)

        gitignore_txt = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("mcp_app/node_modules/", gitignore_txt)


class TestMcpServerProtocolAndTools(unittest.TestCase):
    def test_jsonrpc_handshake_list_and_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            srv = mcp_server.ChengmenMcpServer(Path(td) / "data")

            init_resp = srv.handle_message({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            })
            assert init_resp is not None
            self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "chengmen-paper-viewer")
            self.assertIn("tools", init_resp["result"]["capabilities"])
            self.assertIn("resources", init_resp["result"]["capabilities"])

            # Notification MUST NOT return a response
            self.assertIsNone(srv.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

            # Ping (including id=0)
            ping_resp = srv.handle_message({"jsonrpc": "2.0", "id": 0, "method": "ping"})
            assert ping_resp is not None
            self.assertEqual(ping_resp["id"], 0)
            self.assertEqual(ping_resp["result"], {})

            # tools/list metadata verification
            tlist = srv.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert tlist is not None
            tools = {t["name"]: t for t in tlist["result"]["tools"]}
            expected_names = {
                "open_paper", "list_paper_locks", "sync_paper_companion",
                "app_select_paper", "app_get_meta", "app_get_zh",
                "app_get_page_svg", "app_get_page_png", "app_get_text_layer",
                "app_update_locks", "app_get_state", "app_save_state",
            }
            self.assertEqual(set(tools.keys()), expected_names)

            # open_paper entrypoints & resourceUri
            op_meta = tools["open_paper"]["_meta"]
            self.assertEqual(op_meta["ui"]["resourceUri"], "ui://chengmen/viewer.html")
            ep_types = [e["type"] for e in op_meta["com.google.antigravity/ui"]["entrypoints"]]
            self.assertEqual(ep_types, ["aux_pane", "global"])

            # Every app_* tool MUST have visibility=["app"]
            for name, td_def in tools.items():
                if name.startswith("app_"):
                    self.assertEqual(td_def["_meta"]["ui"]["visibility"], ["app"], f"{name} must be app-only")

            # resources/list & resources/templates/list
            rlist = srv.handle_message({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
            assert rlist is not None
            res0 = rlist["result"]["resources"][0]
            self.assertEqual(res0["uri"], "ui://chengmen/viewer.html")
            self.assertEqual(res0["mimeType"], "text/html;profile=mcp-app")
            self.assertEqual(res0["_meta"], {"ui": {"prefersBorder": False}})

            rtlist = srv.handle_message({"jsonrpc": "2.0", "id": 4, "method": "resources/templates/list"})
            assert rtlist is not None
            self.assertEqual(rtlist["result"]["resourceTemplates"], [])

            # Unknown method -> -32601; unknown tool -> -32602; unknown resource -> -32002
            err_m = srv.handle_message({"jsonrpc": "2.0", "id": 5, "method": "no/such"})
            self.assertEqual(err_m["error"]["code"], -32601)
            err_t = srv.handle_message({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}})
            self.assertEqual(err_t["error"]["code"], -32602)
            err_r = srv.handle_message({"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": "ui://nope"}})
            self.assertEqual(err_r["error"]["code"], -32002)

    def test_resources_read_and_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            srv = mcp_server.ChengmenMcpServer(Path(td) / "data")
            # Test CHENGMEN_APP_HTML fast-iteration override
            custom_html = Path(td) / "dev_viewer.html"
            custom_html.write_text("<!doctype html><html><body>DEV_OVERRIDE</body></html>", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CHENGMEN_APP_HTML": str(custom_html)}):
                rr = srv.handle_message({
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "resources/read",
                    "params": {"uri": "ui://chengmen/viewer.html"},
                })
                assert rr is not None
                item = rr["result"]["contents"][0]
                self.assertEqual(item["mimeType"], "text/html;profile=mcp-app")
                self.assertIn("DEV_OVERRIDE", item["text"])

            # Test real render_viewer_html(mode="mcp-app") path
            with mock.patch.dict(os.environ, {"CHENGMEN_APP_HTML": ""}):
                rr2 = srv.handle_message({
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "resources/read",
                    "params": {"uri": "ui://chengmen/viewer.html"},
                })
                assert rr2 is not None
                item2 = rr2["result"]["contents"][0]
                html = item2["text"]
                self.assertGreater(len(html), 1000)
                self.assertLess(len(html.encode("utf-8")), mcp_server.MAX_HTML_BYTES)
                self.assertIn("__CHENGMEN_MODE__", html)

    def test_end_to_end_tools_and_state_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "plugin_data"
            paper_dir = root / "paper_proj"
            paper_dir.mkdir()
            pdf, _tex, _zh = _make_sample_paper(paper_dir, use_paper_viewer_dir=True)
            srv = mcp_server.ChengmenMcpServer(data_dir)

            # 1. Empty open_paper({}) with no history -> has_paper=False
            r0 = srv.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "open_paper", "arguments": {}},
            })
            assert r0 is not None
            self.assertFalse(r0["result"]["isError"])
            self.assertFalse(r0["result"]["structuredContent"]["has_paper"])

            # 2. open_paper with pdf_path & jump_id
            r1 = srv.handle_message({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "open_paper", "arguments": {"pdf_path": str(pdf), "jump_id": "1-P1", "page": 1}},
            })
            assert r1 is not None
            sc1 = r1["result"]["structuredContent"]
            self.assertTrue(sc1["has_paper"])
            self.assertEqual(sc1["file"], "demo.pdf")
            self.assertEqual(sc1["jump_id"], "1-P1")
            self.assertFalse(sc1["zh_deferred"])
            self.assertTrue(sc1["zh"]["available"])
            self.assertLess(len(json.dumps(r1["result"], ensure_ascii=False).encode("utf-8")), mcp_server.MAX_OPEN_PAPER_BYTES)

            # 3. Empty open_paper({}) now restores last opened paper!
            r2 = srv.handle_message({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "open_paper", "arguments": {}},
            })
            self.assertTrue(r2["result"]["structuredContent"]["has_paper"])
            self.assertEqual(r2["result"]["structuredContent"]["file"], "demo.pdf")

            # 4. app_get_page_svg, app_get_page_png, app_get_text_layer
            r_svg = srv.handle_message({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "app_get_page_svg", "arguments": {"page_idx": 0}},
            })
            self.assertIn("<svg", r_svg["result"]["structuredContent"]["svg"])

            r_png = srv.handle_message({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "app_get_page_png", "arguments": {"page_idx": 0, "zoom": 1.5}},
            })
            png_bytes = base64.b64decode(r_png["result"]["structuredContent"]["png_b64"])
            self.assertTrue(png_bytes.startswith(b"\x89PNG"))

            r_txt = srv.handle_message({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "app_get_text_layer", "arguments": {"page_idx": 0}},
            })
            self.assertEqual(r_txt["result"]["structuredContent"]["page"], 0)
            self.assertTrue(r_txt["result"]["structuredContent"]["runs"])

            # 5. app_update_locks + list_paper_locks + sync_paper_companion
            r_lock = srv.handle_message({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "app_update_locks", "arguments": {"action": "lock", "ids": ["Title", "1-P1", "Ghost99"]}},
            })
            sc_lock = r_lock["result"]["structuredContent"]
            self.assertEqual(sorted(sc_lock["locked_ids"]), ["1-P1", "Title"])
            self.assertEqual(sc_lock["unmatched"], ["Ghost99"])
            self.assertTrue((paper_dir / ".paper_viewer" / "demo.locks.json").is_file())

            r_list = srv.handle_message({
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "list_paper_locks", "arguments": {}},
            })
            self.assertEqual(r_list["result"]["structuredContent"]["locked_count"], 2)

            r_sync = srv.handle_message({
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "sync_paper_companion", "arguments": {}},
            })
            sc_sync = r_sync["result"]["structuredContent"]
            self.assertTrue(sc_sync["available"])
            self.assertTrue(sc_sync["tex_exists"])
            self.assertEqual(sc_sync["total_items"], 4)
            self.assertGreaterEqual(sc_sync["tex_matched_count"], 3)

            # 6. app_save_state & app_get_state persistence across fresh server instance
            r_save = srv.handle_message({
                "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "app_save_state", "arguments": {"ui_state": {"zoom": 1.4, "splitDir": "row"}}},
            })
            self.assertTrue(r_save["result"]["structuredContent"]["ok"])

            srv2 = mcp_server.ChengmenMcpServer(data_dir)
            r_st = srv2.handle_message({
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "app_get_state", "arguments": {}},
            })
            self.assertEqual(r_st["result"]["structuredContent"]["ui_state"]["zoom"], 1.4)

            # 7. Path traversal rejected on `file`
            r_trav = srv.handle_message({
                "jsonrpc": "2.0", "id": 12, "method": "tools/call",
                "params": {"name": "app_select_paper", "arguments": {"file": "../escape.pdf"}},
            })
            self.assertTrue(r_trav["result"]["isError"])

    def test_explicit_zh_without_file_field_in_multi_pdf_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdir = root / "multi"
            pdir.mkdir()
            pdf1, tex1, _default_zh = _make_sample_paper(pdir, use_paper_viewer_dir=False)
            # Second PDF in same directory (triggers multi-PDF branch in resolve_zh_path_for)
            pdf2 = pdir / "appendix.pdf"
            _make_sample_pdf(pdf2)
            (pdir / "appendix_zh.json").write_text(
                json.dumps({"file": "appendix.pdf", "title_zh": "附录对照", "items": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            # External explicit zh_path that intentionally omits the optional "file" field
            ext_dir = root / "ext"
            ext_dir.mkdir()
            custom_zh = ext_dir / "custom_nofile_zh.json"
            custom_zh.write_text(
                json.dumps({
                    "tex": str(tex1),
                    "title_zh": "自定义显式外部对照",
                    "items": [
                        {
                            "id": "1-P1",
                            "page": 1,
                            "section": "§1 Introduction",
                            "title": "自定义引言",
                            "anchor": "1. Introduction",
                            "en_preview": "Large language models accelerate scientific research.",
                            "zh": "来自外部显式 zh_path 的中文对照。",
                        }
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            srv = mcp_server.ChengmenMcpServer(root / "data")

            # 1. open_paper MUST honor custom_zh instead of falling back to sibling demo_zh.json
            r_open = srv.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "open_paper", "arguments": {"pdf_path": str(pdf1), "zh_path": str(custom_zh)}},
            })
            assert r_open is not None
            sc = r_open["result"]["structuredContent"]
            self.assertEqual(Path(sc["zh_path"]).resolve(), custom_zh.resolve())
            self.assertEqual(sc["zh"]["title_zh"], "自定义显式外部对照")
            self.assertEqual([it["id"] for it in sc["zh"]["items"]], ["1-P1"])

            # 2. Follow-up app_get_zh / sync_paper_companion / app_update_locks also honor custom_zh
            r_zh = srv.handle_message({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "app_get_zh", "arguments": {}},
            })
            self.assertEqual(r_zh["result"]["structuredContent"]["title_zh"], "自定义显式外部对照")

            r_sync = srv.handle_message({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "sync_paper_companion", "arguments": {}},
            })
            self.assertEqual(Path(r_sync["result"]["structuredContent"]["zh_path"]).resolve(), custom_zh.resolve())

            r_lock = srv.handle_message({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "app_update_locks", "arguments": {"action": "lock", "ids": ["1-P1"]}},
            })
            self.assertEqual(r_lock["result"]["structuredContent"]["locked_ids"], ["1-P1"])

            # 3. Syntax-broken explicit zh_path surfaces parse error instead of silently falling back
            broken_zh = ext_dir / "broken_zh.json"
            broken_zh.write_text("{not valid json", encoding="utf-8")
            r_broken = srv.handle_message({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "open_paper", "arguments": {"pdf_path": str(pdf1), "zh_path": str(broken_zh)}},
            })
            sc_b = r_broken["result"]["structuredContent"]
            self.assertFalse(sc_b["zh"]["available"])
            self.assertTrue(sc_b["zh"].get("error"))

            # 4. Switching to appendix.pdf without zh_path loads appendix_zh.json (not hijacked)
            r_app = srv.handle_message({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "app_select_paper", "arguments": {"file": "appendix.pdf"}},
            })
            self.assertEqual(r_app["result"]["structuredContent"]["zh"]["title_zh"], "附录对照")

    def test_open_paper_size_cap_sets_zh_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf, _tex, zh = _make_sample_paper(root)
            # Inflate companion JSON to > 950 KiB so open_paper MUST defer zh
            raw = json.loads(zh.read_text(encoding="utf-8"))
            raw["items"][1]["zh"] = "大" * 400_000  # ~1.2 MB in UTF-8
            zh.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

            srv = mcp_server.ChengmenMcpServer(root / "data")
            r = srv.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "open_paper", "arguments": {"pdf_path": str(pdf)}},
            })
            assert r is not None
            self.assertFalse(r["result"]["isError"])
            self.assertTrue(r["result"]["structuredContent"]["zh_deferred"])
            total_bytes = len(json.dumps(r["result"], ensure_ascii=False).encode("utf-8"))
            self.assertLess(total_bytes, mcp_server.MAX_OPEN_PAPER_BYTES)

    def test_stdio_subprocess_clean_stdout_only_jsonrpc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td) / "pdata"
            lines = "\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "open_paper", "arguments": {}}}),
            ]) + "\n"
            p = subprocess.run(
                [sys.executable, "-B", str(REPO_ROOT / "mcp_server.py"), "--data-dir", str(data_dir)],
                input=lines,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(p.returncode, 0, f"stderr={p.stderr}")
            out_lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
            # Exactly 4 responses (notification produced NO line!)
            self.assertEqual(len(out_lines), 4, f"Expected 4 JSON-RPC lines, got: {out_lines}")
            parsed = [json.loads(ln) for ln in out_lines]
            self.assertEqual([m["id"] for m in parsed], [1, 2, 3, 4])
            self.assertFalse(parsed[3]["result"]["structuredContent"]["has_paper"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
