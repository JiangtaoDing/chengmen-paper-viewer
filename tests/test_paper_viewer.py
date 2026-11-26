"""Automated tests for paper_viewer.py, requirements.txt, and zh_template.json."""

from __future__ import annotations

import ast
import base64
import concurrent.futures
import http.client
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pymupdf
import paper_lock
import paper_viewer


def make_test_pdf(path: Path, *, with_margin_lineno: bool = True) -> None:
    """Create a synthetic two-page academic PDF covering layout, links, digits, and rotated text."""
    doc = pymupdf.open()
    p1 = doc.new_page(width=612, height=792)

    if with_margin_lineno:
        for idx, y in enumerate(range(170, 350, 14)):
            p1.insert_text((32, y), f"{idx:03d}", fontsize=8)

    # Non-anonymous title (16pt) + smaller author/affiliation lines (10-11pt)
    p1.insert_text((120, 60), "A Novel Method for Paper Auditing", fontsize=16)
    p1.insert_text((180, 88), "Alice Smith, Bob Jones", fontsize=11)
    p1.insert_text((170, 104), "Department of Computer Science, MIT", fontsize=10)

    # Abstract & section prose starting with "References ..."
    p1.insert_text((54, 170), "Abstract", fontsize=12)
    p1.insert_text((54, 190), "This paper studies automated auditing. It finds real bugs.", fontsize=10)
    p1.insert_text((54, 230), "1 Introduction", fontsize=12)
    p1.insert_text((54, 250), "References to prior work show that many viewers have subtle bugs.", fontsize=10)
    p1.insert_text((54, 266), "We evaluate ten properties in detail. First sentence. Second sentence.", fontsize=10)

    # Left-column body digits at ICML x=54pt (must NOT be stripped as margin line numbers)
    p1.insert_text((54, 310), "1", fontsize=9)
    p1.insert_text((72, 310), "Baseline model score 80.5", fontsize=9)
    p1.insert_text((54, 324), "12", fontsize=9)
    p1.insert_text((72, 324), "Improved model score 91.2", fontsize=9)

    # Vertical 90-degree rotated figure y-axis label
    p1.insert_text((100, 500), "Accuracy (%)", fontsize=10, rotate=90)

    # Page 2
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((54, 100), "2 Method", fontsize=12)
    p2.insert_text(
        (54, 120),
        "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five.",
        fontsize=10,
    )
    p2.insert_text((72, 600), "Target section near bottom of page 2.", fontsize=10)

    # Links on page 1 (reload page 0 after new_page)
    p1 = doc[0]
    p1.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(54, 520, 150, 535), "uri": "HTTPS://EXAMPLE.COM/UPPER"})
    p1.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(160, 520, 260, 535), "uri": "https://example.com/lower"})
    p1.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(270, 520, 350, 535), "uri": "javascript:alert(1)"})
    p1.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(54, 540, 150, 555), "page": 1, "to": pymupdf.Point(72, 600)})

    doc.save(path)
    doc.close()


class _ServerTestMixin:
    """Helper mixin spinning up a local ThreadingHTTPServer against a temp directory."""

    def start_test_server(self, work_dir: Path, default_file: str, explicit_zh: Path | None = None):
        self._orig_state = (
            paper_viewer.PAPER_DIR,
            paper_viewer.DEFAULT_FILE,
            paper_viewer.EXPLICIT_ZH_PATH,
            paper_viewer.SESSION_TOKEN,
        )
        paper_viewer.PAPER_DIR = work_dir.resolve()
        paper_viewer.DEFAULT_FILE = default_file
        paper_viewer.EXPLICIT_ZH_PATH = explicit_zh.resolve() if explicit_zh else None
        paper_viewer.SESSION_TOKEN = "test-session-token-xyz-123456"
        paper_viewer._page_cache.clear()
        paper_viewer._meta_cache.clear()
        paper_viewer._text_cache.clear()
        paper_viewer._region_cache.clear()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), paper_viewer.ViewerHandler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def stop_test_server(self):
        if hasattr(self, "server") and self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=3)
        if hasattr(self, "_orig_state"):
            (
                paper_viewer.PAPER_DIR,
                paper_viewer.DEFAULT_FILE,
                paper_viewer.EXPLICIT_ZH_PATH,
                paper_viewer.SESSION_TOKEN,
            ) = self._orig_state

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if body is not None and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, resp_headers, raw

    def authed_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "X-Viewer-Token": paper_viewer.SESSION_TOKEN,
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        if extra:
            h.update(extra)
        return h


class TestSecurityAndHeaders(unittest.TestCase, _ServerTestMixin):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_sec_"))
        self.pdf = self.tmp / "paper.pdf"
        make_test_pdf(self.pdf)
        (self.tmp / "paper.tex").write_text(
            "\\begin{document}\n\\section{Introduction}\nHello world.\n\\end{document}\n",
            encoding="utf-8",
        )
        (self.tmp / "paper_zh.json").write_text(
            json.dumps({
                "file": "paper.pdf",
                "items": [{"id": "1-P1", "page": 1, "section": "§1", "title": "Intro", "anchor": "Introduction", "zh": "你好。"}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        self.start_test_server(self.tmp, "paper.pdf")

    def tearDown(self):
        self.stop_test_server()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_security_headers_and_csp_nonce(self):
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "SAMEORIGIN")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        csp = headers.get("content-security-policy", "")
        m = re.search(r"script-src 'nonce-([^']+)'", csp)
        self.assertIsNotNone(m, f"Missing script-src nonce in CSP: {csp}")
        html = raw.decode("utf-8")
        self.assertIn(f'<script nonce="{m.group(1)}">', html)
        self.assertIn(json.dumps(paper_viewer.SESSION_TOKEN), html)
        self.assertNotIn("__CSP_NONCE__", html)
        self.assertNotIn("__VIEWER_SESSION_TOKEN__", html)
        # Ensure HTML body contains no inline event attributes that CSP would block
        body_html = html.split("<script", 1)[0]
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", body_html, re.IGNORECASE))

    def test_invalid_host_rejected_for_get_and_post(self):
        st_get, _, _ = self.request("GET", "/api/meta", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(st_get, 403)
        body = json.dumps({"file": "paper.pdf", "action": "lock", "ids": ["1-P1"]}).encode("utf-8")
        st_post, _, _ = self.request(
            "POST",
            "/api/locks",
            body=body,
            headers=self.authed_headers({"Host": f"evil.example:{self.port}"}),
        )
        self.assertEqual(st_post, 403)

    def test_post_locks_csrf_origin_content_type_token_and_size_checks(self):
        body = json.dumps({"file": "paper.pdf", "action": "lock", "ids": ["1-P1"]}).encode("utf-8")

        # Cross-origin Origin rejected
        st, _, _ = self.request("POST", "/api/locks", body=body, headers=self.authed_headers({"Origin": "https://untrusted.example"}))
        self.assertEqual(st, 403)

        # Cross-origin Referer (when Origin absent) rejected
        hdr_ref = {"Content-Type": "application/json", "X-Viewer-Token": paper_viewer.SESSION_TOKEN, "Referer": "https://untrusted.example/x"}
        st, _, _ = self.request("POST", "/api/locks", body=body, headers=hdr_ref)
        self.assertEqual(st, 403)

        # Simple CORS Content-Type text/plain rejected with 415
        st, _, _ = self.request("POST", "/api/locks", body=body, headers=self.authed_headers({"Content-Type": "text/plain"}))
        self.assertEqual(st, 415)

        # Missing or wrong X-Viewer-Token rejected with 403
        st, _, _ = self.request("POST", "/api/locks", body=body, headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 403)
        st, _, _ = self.request("POST", "/api/locks", body=body, headers=self.authed_headers({"X-Viewer-Token": "wrong-token"}))
        self.assertEqual(st, 403)

        # Oversized Content-Length rejected with 413 without reading huge payload
        st, _, _ = self.request(
            "POST",
            "/api/locks",
            body=b"",
            headers=self.authed_headers({"Content-Length": str(paper_viewer.MAX_POST_BODY_BYTES + 1)}),
        )
        self.assertEqual(st, 413)

        # Negative / non-int Content-Length rejected with 400
        for bad_len in ("-5", "not-a-number"):
            st, _, _ = self.request("POST", "/api/locks", body=b"", headers=self.authed_headers({"Content-Length": bad_len}))
            self.assertEqual(st, 400)

    def test_path_traversal_query_param_rejected(self):
        for bad in ("../../etc/passwd", "..\\secret.pdf", "sub/paper.pdf"):
            st, _, _ = self.request("GET", f"/api/meta?file={bad}")
            self.assertEqual(st, 400)


class TestLocksApiAndConcurrency(unittest.TestCase, _ServerTestMixin):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_lock_"))
        self.pdf_a = self.tmp / "paperA.pdf"
        self.pdf_b = self.tmp / "paperB.pdf"
        make_test_pdf(self.pdf_a)
        make_test_pdf(self.pdf_b)

        # Place custom .tex ONLY in a subdirectory (no sibling paperA.tex) and reference it via zh.json "tex"
        src_dir = self.tmp / "src"
        src_dir.mkdir()
        self.custom_tex = src_dir / "custom_main.tex"
        self.custom_tex.write_text(
            "\\begin{document}\n"
            + "\n".join(f"\\section{{Sec {i}}}\nParagraph {i} body content for testing.\n" for i in range(1, 16))
            + "\n\\end{document}\n",
            encoding="utf-8",
        )

        items = [
            {"id": f"P{i}", "page": 1, "section": f"§{i}", "title": f"Sec {i}", "anchor": f"Sec {i}", "zh": f"第 {i} 段。"}
            for i in range(1, 16)
        ]
        items.append({"id": "GhostItem", "page": 2, "section": "§99", "title": "Missing", "anchor": "Nonexistent Heading XYZ", "zh": "不存在。"})
        self.zh_a = self.tmp / "paperA_zh.json"
        self.zh_a.write_text(
            json.dumps({"file": "paperA.pdf", "tex": "src/custom_main.tex", "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.start_test_server(self.tmp, "paperA.pdf", explicit_zh=self.zh_a)

    def tearDown(self):
        self.stop_test_server()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_post_locks_no_500_no_lost_updates(self):
        def lock_one(pid: str):
            body = json.dumps({"file": "paperA.pdf", "action": "lock", "ids": [pid]}).encode("utf-8")
            return self.request("POST", "/api/locks", body=body, headers=self.authed_headers())

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
            results = list(ex.map(lock_one, [f"P{i}" for i in range(1, 16)]))

        for st, _, raw in results:
            self.assertEqual(st, 200, f"Unexpected status {st}: {raw}")

        st, _, raw = self.request("GET", "/api/locks?file=paperA.pdf")
        self.assertEqual(st, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(len(data["locked_ids"]), 15)

    def test_unknown_action_and_invalid_payloads_return_400_and_preserve_locks(self):
        # Lock P2 first
        st, _, _ = self.request(
            "POST",
            "/api/locks",
            body=json.dumps({"file": "paperA.pdf", "action": "lock", "ids": ["P2"]}).encode("utf-8"),
            headers=self.authed_headers(),
        )
        self.assertEqual(st, 200)

        # Unknown action must return 400 and NEVER silently unlock P2
        st, _, _ = self.request(
            "POST",
            "/api/locks",
            body=json.dumps({"file": "paperA.pdf", "action": "unknown_noop", "ids": ["P2"]}).encode("utf-8"),
            headers=self.authed_headers(),
        )
        self.assertEqual(st, 400)

        # Verify P2 is still locked
        st, _, raw = self.request("GET", "/api/locks?file=paperA.pdf")
        self.assertIn("P2", json.loads(raw.decode("utf-8"))["locked_ids"])

        # Bare string ids, invalid mode, list root, invalid JSON -> all 400
        bad_bodies = [
            json.dumps({"file": "paperA.pdf", "action": "lock", "ids": "P2"}).encode("utf-8"),
            json.dumps({"file": "paperA.pdf", "action": "set_mode", "mode": "bogus"}).encode("utf-8"),
            b"[]",
            b"{invalid",
        ]
        for b in bad_bodies:
            st, _, _ = self.request("POST", "/api/locks", body=b, headers=self.authed_headers())
            self.assertEqual(st, 400)

    def test_explicit_zh_does_not_hijack_other_pdf_or_cross_write_locks(self):
        # paperB has no companion JSON; EXPLICIT_ZH_PATH belongs to paperA.pdf
        st, _, raw = self.request("GET", "/api/zh?file=paperB.pdf")
        self.assertEqual(st, 200)
        zh_b = json.loads(raw.decode("utf-8"))
        self.assertFalse(zh_b["available"])

        # If paperB_zh.json IS present, resolve_zh_path should pick it instead of paperA_zh.json
        (self.tmp / "paperB_zh.json").write_text(
            json.dumps({"file": "paperB.pdf", "items": [{"id": "B1", "page": 1, "section": "§1", "title": "B", "anchor": "Abstract", "zh": "乙。"}]}),
            encoding="utf-8",
        )
        st, _, raw = self.request("GET", "/api/zh?file=paperB.pdf")
        self.assertEqual(st, 200)
        zh_b2 = json.loads(raw.decode("utf-8"))
        self.assertTrue(zh_b2["available"])
        self.assertEqual([it["id"] for it in zh_b2["items"]], ["B1"])

    def test_resolve_zh_path_multi_pdf_no_default_and_omitted_file_field(self):
        # External companion JSONs outside PAPER_DIR
        ext_dir = self.tmp / "ext"
        ext_dir.mkdir()
        ext_a_with_file = ext_dir / "ext_A_zh.json"
        ext_a_with_file.write_text(
            json.dumps({"file": "paperA.pdf", "items": [{"id": "P1", "page": 1, "anchor": "Sec 1", "zh": "甲。"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        ext_a_nofile = ext_dir / "ext_A_nofile_zh.json"
        ext_a_nofile.write_text(
            json.dumps({"items": [{"id": "P1", "page": 1, "anchor": "Sec 1", "zh": "甲无file。"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        zh_b = self.tmp / "paperB_zh.json"
        zh_b.write_text(
            json.dumps({"file": "paperB.pdf", "items": [{"id": "B1", "page": 1, "anchor": "Abstract", "zh": "乙。"}]}, ensure_ascii=False),
            encoding="utf-8",
        )

        # Case (i): --paper-dir <dir> --zh ext_A_zh.json (DEFAULT_FILE == "")
        paper_viewer.DEFAULT_FILE = ""
        paper_viewer.EXPLICIT_ZH_PATH = ext_a_with_file.resolve()
        self.assertEqual(paper_viewer.resolve_zh_path(self.pdf_a), ext_a_with_file.resolve())
        self.assertEqual(paper_viewer.resolve_zh_path(self.pdf_b), zh_b.resolve())
        zh_b.unlink()
        self.assertIsNone(paper_viewer.resolve_zh_path(self.pdf_b))

        # Case (ii): --pdf paperA.pdf --zh ext_A_nofile_zh.json (omits "file" field)
        zh_b.write_text(
            json.dumps({"file": "paperB.pdf", "items": [{"id": "B1", "page": 1, "anchor": "Abstract", "zh": "乙。"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        paper_viewer.DEFAULT_FILE = "paperA.pdf"
        paper_viewer.EXPLICIT_ZH_PATH = ext_a_nofile.resolve()
        self.assertEqual(paper_viewer.resolve_zh_path(self.pdf_a), ext_a_nofile.resolve())
        self.assertEqual(paper_viewer.resolve_zh_path(self.pdf_b), zh_b.resolve())
        zh_b.unlink()
        self.assertIsNone(paper_viewer.resolve_zh_path(self.pdf_b))
        # Ensure POST /api/locks on paperB.pdf does not cross-lock paperA's P1 from ext_A_nofile_zh.json
        st, _, raw = self.request(
            "POST",
            "/api/locks",
            body=json.dumps({"file": "paperB.pdf", "action": "lock", "ids": ["P1"]}).encode("utf-8"),
            headers=self.authed_headers(),
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw.decode("utf-8"))["locked_ids"], [])

        # Case (iii): single-PDF dir with DEFAULT_FILE == "" and omitted "file" -> unambiguous, uses EXPLICIT_ZH_PATH
        single_dir = Path(tempfile.mkdtemp(prefix="pv_single_"))
        try:
            only_pdf = single_dir / "only.pdf"
            make_test_pdf(only_pdf)
            paper_viewer.PAPER_DIR = single_dir.resolve()
            paper_viewer.DEFAULT_FILE = ""
            paper_viewer.EXPLICIT_ZH_PATH = ext_a_nofile.resolve()
            self.assertEqual(paper_viewer.resolve_zh_path(only_pdf), ext_a_nofile.resolve())
        finally:
            shutil.rmtree(single_dir, ignore_errors=True)

    def test_custom_tex_hint_honored_in_zh_companion_and_locks(self):
        st, _, raw = self.request("GET", "/api/zh?file=paperA.pdf")
        self.assertEqual(st, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["tex"], "custom_main.tex")

        # Response includes unmatched & warnings arrays per shared contract, and locks P1 from src/custom_main.tex
        st, _, raw = self.request(
            "POST",
            "/api/locks",
            body=json.dumps({"file": "paperA.pdf", "action": "lock", "ids": ["P1", "GhostItem"]}).encode("utf-8"),
            headers=self.authed_headers(),
        )
        self.assertEqual(st, 200)
        ldata = json.loads(raw.decode("utf-8"))
        self.assertEqual(ldata["tex"], "custom_main.tex")
        self.assertIn("P1", ldata["locked_ids"])
        self.assertNotIn("GhostItem", ldata["locked_ids"])
        self.assertIn("GhostItem", ldata["unmatched"])
        self.assertTrue(ldata["warnings"])

    def test_set_mode_survives_broken_zh_json(self):
        self.zh_a.write_text("{broken json", encoding="utf-8")
        st, _, raw = self.request(
            "POST",
            "/api/locks",
            body=json.dumps({"file": "paperA.pdf", "action": "set_mode", "mode": "force_ask"}).encode("utf-8"),
            headers=self.authed_headers(),
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw.decode("utf-8"))["mode"], "force_ask")


class TestMalformedJsonAndBom(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_json_"))
        self.pdf = self.tmp / "paper.pdf"
        make_test_pdf(self.pdf)
        self.zh = self.tmp / "paper_zh.json"
        self._orig = (paper_viewer.PAPER_DIR, paper_viewer.DEFAULT_FILE, paper_viewer.EXPLICIT_ZH_PATH)
        paper_viewer.PAPER_DIR = self.tmp.resolve()
        paper_viewer.DEFAULT_FILE = "paper.pdf"
        paper_viewer.EXPLICIT_ZH_PATH = None
        paper_viewer._region_cache.clear()

    def tearDown(self):
        paper_viewer.PAPER_DIR, paper_viewer.DEFAULT_FILE, paper_viewer.EXPLICIT_ZH_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_utf8_bom_json_loads_successfully(self):
        payload = {"file": "paper.pdf", "title_zh": "中文标题", "items": [{"id": "Abstract", "page": 1, "anchor": "Abstract", "zh": "摘要。"}]}
        self.zh.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        res = paper_viewer.get_zh_companion("paper.pdf")
        self.assertTrue(res["available"])
        self.assertEqual(res["title_zh"], "中文标题")
        self.assertEqual(len(res["items"]), 1)

    def test_malformed_json_structures_never_crash(self):
        # Syntax error & wrong top-level types return structured available=False + error
        for raw_bytes in (b'{"file": "paper.pdf", "items": [}', b"[]", b'{"file": "paper.pdf", "items": null}'):
            self.zh.write_bytes(raw_bytes)
            paper_viewer._region_cache.clear()
            res = paper_viewer.get_zh_companion("paper.pdf")
            self.assertFalse(res["available"])
            self.assertTrue(res.get("error"))

        # Malformed items / non-string anchor / bad sent_align safely sanitized without raising
        tricky = {
            "file": "paper.pdf",
            "items": [
                {"page": 1, "zh": "missing id"},
                "not-a-dict",
                {"id": "Abstract", "page": "bad", "anchor": 123, "zh": "摘要第一句。摘要第二句。", "sent_align": [0, 1]},
            ],
        }
        self.zh.write_text(json.dumps(tricky, ensure_ascii=False), encoding="utf-8")
        paper_viewer._region_cache.clear()
        res = paper_viewer.get_zh_companion("paper.pdf")
        self.assertTrue(res["available"])
        self.assertEqual([it["id"] for it in res["items"]], ["Abstract"])
        self.assertEqual(res["items"][0]["page"], 1)
        self.assertTrue(res.get("warnings"))


class TestParsingTextLayerAndAlignment(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_parse_"))
        self.pdf = self.tmp / "syn.pdf"
        make_test_pdf(self.pdf, with_margin_lineno=True)
        self._orig = (paper_viewer.PAPER_DIR, paper_viewer.DEFAULT_FILE, paper_viewer.EXPLICIT_ZH_PATH)
        paper_viewer.PAPER_DIR = self.tmp.resolve()
        paper_viewer.DEFAULT_FILE = "syn.pdf"
        paper_viewer.EXPLICIT_ZH_PATH = None
        paper_viewer._meta_cache.clear()
        paper_viewer._text_cache.clear()
        paper_viewer._region_cache.clear()

    def tearDown(self):
        paper_viewer.PAPER_DIR, paper_viewer.DEFAULT_FILE, paper_viewer.EXPLICIT_ZH_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uri_links_case_insensitive_and_javascript_blocked(self):
        meta = paper_viewer.get_meta("syn.pdf")
        uris = [lnk["u"] for lnk in meta["links"][0] if lnk["t"] == "uri"]
        self.assertIn("HTTPS://EXAMPLE.COM/UPPER", uris)
        self.assertIn("https://example.com/lower", uris)
        self.assertFalse(any(u.lower().startswith("javascript:") for u in uris))

    def test_text_layer_preserves_body_digits_strips_margin_lineno_and_skips_rotated_text(self):
        tl = paper_viewer.get_text_layer("syn.pdf")
        runs = tl["pages"][0]["runs"]
        texts = [r[4] for r in runs]
        joined = " | ".join(texts)

        # Left-margin digits at x=54pt preserved
        self.assertTrue(any("1 Introduction" in t or t == "1" for t in texts), f"Missing section '1': {texts}")
        self.assertTrue(any("12" in t for t in texts), f"Missing table row '12': {texts}")
        # Margin line numbers (000, 001, ...) at x=32pt stripped
        self.assertNotIn("000", joined)
        self.assertNotIn("001", joined)
        # Rotated 90-deg y-axis label excluded (no huge horizontal box)
        self.assertNotIn("Accuracy (%)", joined)
        for r in runs:
            self.assertFalse(r[3] > r[2] * 1.8 and r[3] > 20, f"Degenerate tall run leaked: {r}")

    def test_non_anonymous_title_and_references_prose_not_truncated(self):
        items = [
            {"id": "Title", "page": 1, "section": "标题", "title": "Title", "anchor": "A Novel Method for Paper Auditing", "zh": "论文标题。"},
            {"id": "Abstract", "page": 1, "section": "摘要", "title": "Abstract", "anchor": "Abstract", "zh": "摘要第一句。摘要第二句。"},
            {"id": "1-P1", "page": 1, "section": "§1", "title": "Intro", "anchor": "1 Introduction", "zh": "前人工作。十个属性。第一句。第二句。"},
        ]
        _, smap = paper_viewer.build_regions(self.pdf, self.pdf.stat().st_mtime, 1.0, items)
        title_text = " ".join(s["text"] for s in smap["Title"]["en_sents"])
        self.assertIn("Novel Method", title_text)
        self.assertNotIn("Alice Smith", title_text)
        self.assertNotIn("MIT", title_text)

        intro_text = " ".join(s["text"] for s in smap["1-P1"]["en_sents"])
        self.assertIn("References to prior work", intro_text)
        self.assertIn("evaluate ten properties", intro_text)

    def test_align_zh_en_ratio_gt_3_covers_all_english_sentences(self):
        zh2 = ["第一句总结前半部分。", "第二句总结后半部分。"]
        en10 = [f"Sentence {i} describes detail {i}." for i in range(10)]
        mapping = paper_viewer._align_zh_en(zh2, en10)
        covered = sorted(set(sum(mapping, [])))
        self.assertEqual(covered, list(range(10)))

    def test_zh_en_hints_generic_and_deduplicated(self):
        self.assertEqual(len(paper_viewer.ZH_EN_HINTS), len(set(paper_viewer.ZH_EN_HINTS)))
        zh_terms = {zh for zh, _ in paper_viewer.ZH_EN_HINTS}
        for private_term in ("融合器", "直读", "盲答", "轮转"):
            self.assertNotIn(private_term, zh_terms)


class TestCacheConcurrencyAndStatusCodes(unittest.TestCase, _ServerTestMixin):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_cache_"))
        self.pdf = self.tmp / "paper.pdf"
        make_test_pdf(self.pdf)
        self.start_test_server(self.tmp, "paper.pdf")

    def tearDown(self):
        self.stop_test_server()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_mtime_eviction_and_lru_bounds(self):
        for i in range(3):
            t_val = time.time() + (i + 1) * 10
            os.utime(self.pdf, (t_val, t_val))
            paper_viewer.render_page_svg("paper.pdf", 0)
            paper_viewer.render_page_png("paper.pdf", 0)
            paper_viewer.get_text_layer("paper.pdf")
        self.assertEqual(len(paper_viewer._page_cache), 2)
        self.assertEqual(len(paper_viewer._text_cache), 1)

        lru = paper_viewer._LRUCache(max_items=2, max_bytes=10, size_fn=len)
        lru["a"] = b"123456"
        lru["b"] = b"123456"  # exceeds 10 bytes -> evicts "a"
        self.assertNotIn("a", lru)
        self.assertIn("b", lru)

    def test_concurrent_mixed_http_reads_and_status_codes(self):
        endpoints = ["/api/meta", "/api/text", "/api/zh", "/api/page/0.svg", "/api/page/0.png", "/api/search?q=auditing"]
        def hit(idx: int):
            return self.request("GET", endpoints[idx % len(endpoints)])[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            codes = list(ex.map(hit, range(24)))
        self.assertTrue(all(c == 200 for c in codes), f"Non-200 codes: {codes}")

        self.assertEqual(self.request("GET", "/api/page/abc.svg")[0], 400)
        self.assertEqual(self.request("GET", "/api/page/999.svg")[0], 404)
        self.assertEqual(self.request("GET", "/api/page/-1.svg")[0], 404)
        self.assertEqual(self.request("GET", "/api/does_not_exist")[0], 404)


class TestCliPackagingAndFrontend(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_cli_"))
        self.pdf = self.tmp / "PaperUpper.PDF"
        make_test_pdf(self.pdf)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_validation_rejects_bad_paths_and_ports(self):
        viewer_py = str(ROOT_DIR / "paper_viewer.py")
        bad_cases = [
            ["--paper-dir", str(self.tmp / "missing")],
            ["--pdf", str(self.tmp)],
            ["--pdf", str(self.pdf), "--zh", str(self.tmp / "missing_zh.json")],
            ["--paper-dir", str(self.tmp), "--port", "99999"],
            ["--paper-dir", str(self.tmp), "--port", "-1"],
            ["--paper-dir", str(self.tmp), "--default", "../escape.pdf"],
        ]
        for args in bad_cases:
            res = subprocess.run([sys.executable, "-B", viewer_py, *args], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(res.returncode, 0, f"Expected non-zero exit for args={args}")

    def test_cli_port_zero_prints_real_ephemeral_port(self):
        viewer_py = str(ROOT_DIR / "paper_viewer.py")
        proc = subprocess.Popen(
            [sys.executable, "-B", viewer_py, "--pdf", str(self.pdf), "--port", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            line = proc.stdout.readline().strip() if proc.stdout else ""
            m = re.search(r"http://127\.0\.0\.1:(\d+)", line)
            self.assertIsNotNone(m, f"Unexpected startup stdout: {line!r}")
            self.assertGreater(int(m.group(1)), 0)
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

    def test_case_insensitive_pdf_listing_and_empty_dir_error(self):
        orig = (paper_viewer.PAPER_DIR, paper_viewer.DEFAULT_FILE)
        try:
            paper_viewer.PAPER_DIR = self.tmp.resolve()
            paper_viewer.DEFAULT_FILE = ""
            self.assertIn("PaperUpper.PDF", paper_viewer.list_pdfs())

            empty = self.tmp / "empty"
            empty.mkdir()
            paper_viewer.PAPER_DIR = empty.resolve()
            with self.assertRaises(FileNotFoundError) as ctx:
                paper_viewer.resolve_pdf(None)
            self.assertIn("No PDF files found", str(ctx.exception))
        finally:
            paper_viewer.PAPER_DIR, paper_viewer.DEFAULT_FILE = orig

    def test_packaging_requirements_template_and_future_annotations(self):
        req_text = (ROOT_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pymupdf>=1.24.0,<2.0", req_text)

        tmpl = json.loads((ROOT_DIR / "zh_template.json").read_text(encoding="utf-8"))
        self.assertIsInstance(tmpl, dict)
        self.assertIn("file", tmpl)
        self.assertIn("tex", tmpl)
        self.assertIn("title_zh", tmpl)
        self.assertIsInstance(tmpl.get("items"), list)
        self.assertTrue(any("sent_align" in it for it in tmpl["items"]))

        tree = ast.parse((ROOT_DIR / "paper_viewer.py").read_text(encoding="utf-8"))
        has_future = any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        )
        self.assertTrue(has_future, "Missing `from __future__ import annotations`")

    def test_frontend_xss_and_js_syntax_contracts(self):
        html = paper_viewer.render_viewer_html(mode="http", viewer_token="tok", csp_nonce="nonce123")
        # No dynamic template interpolation inside .innerHTML = `...${...}...`
        for m in re.finditer(r"\.innerHTML\s*=\s*`([^`]*)`", html):
            tpl = m.group(1)
            # Only numeric count interpolations like `${cnt}/${ids.length}` are allowed if any
            for expr in re.findall(r"\$\{([^}]+)\}", tpl):
                self.assertIn(expr.strip(), {"cnt", "ids.length"}, f"Unsafe innerHTML interpolation: {expr!r}")

        # Required frontend fix & MCP App dual-mode markers
        for marker in (
            "X-Viewer-Token",
            "AbortController",
            "fileSwitchEpoch",
            "document.hidden",
            "zhFilterMode === 'locked'",
            "scrollRatioY",
            "itemMatchesPage",
            "window.addEventListener('blur'",
            "suppressAutoFollowBriefly",
            "lockSecList.textContent = ''",
            "__CHENGMEN_MCP_BRIDGE__",
            "app_get_page_svg",
            "app_get_page_png",
            "app_get_text_layer",
            "app_update_locks",
            "app_select_paper",
            "app_get_state",
            "app_save_state",
            "openFileInAuxPane",
            "updateModelContext",
            "sendMessage",
            "openLink",
            "URL.createObjectURL",
            "revokeObjectURL",
            "--color-background-primary",
            "--color-text-primary",
            "--color-border-primary",
            "--font-sans",
        ):
            self.assertIn(marker, html)

        # If node is installed, verify extracted JS compiles cleanly with `node --check`
        node_bin = shutil.which("node")
        if node_bin:
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
            self.assertGreaterEqual(len(scripts), 1)
            for idx, js_src in enumerate(scripts):
                js_file = self.tmp / f"viewer_check_{idx}.js"
                js_file.write_text(js_src, encoding="utf-8")
                chk = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True, text=True, timeout=10)
                self.assertEqual(chk.returncode, 0, f"JS syntax check failed (script {idx}):\n{chk.stderr}")


class TestStatelessExportsAndMcpApp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv_mcp_"))
        self.dir_a = self.tmp / "projA"
        self.dir_b = self.tmp / "projB"
        self.dir_a.mkdir()
        self.dir_b.mkdir()
        self.pdf_a = self.dir_a / "main.pdf"
        self.pdf_b = self.dir_b / "main.pdf"
        self.pdf_upper = self.dir_a / "Extra.PDF"
        make_test_pdf(self.pdf_a)
        make_test_pdf(self.pdf_b)
        make_test_pdf(self.pdf_upper)

        (self.dir_a / "main.tex").write_text(
            "\\begin{document}\n\\begin{abstract}\nThis paper studies automated auditing.\n\\end{abstract}\n\\end{document}\n",
            encoding="utf-8",
        )
        self.zh_a = self.dir_a / "main_zh.json"
        self.zh_a.write_text(
            json.dumps({
                "file": "main.pdf",
                "tex": "main.tex",
                "title_zh": "论文 A 中文标题",
                "items": [{"id": "Abstract", "page": 1, "section": "摘要", "title": "Abstract", "anchor": "Abstract", "zh": "摘要第一句。摘要第二句。"}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        # Ensure projB has a distinct companion JSON with same PDF filename "main.pdf"
        self.zh_b = self.dir_b / "main_zh.json"
        self.zh_b.write_text(
            json.dumps({
                "file": "main.pdf",
                "title_zh": "论文 B 中文标题",
                "items": [{"id": "Abstract", "page": 1, "section": "摘要", "title": "Abstract", "anchor": "Abstract", "zh": "乙论文摘要。"}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_pdfs_in_and_resolve_zh_path_for_are_stateless(self):
        orig_dir = paper_viewer.PAPER_DIR
        pdfs = paper_viewer.list_pdfs_in(self.dir_a)
        self.assertEqual([p.name for p in pdfs], ["Extra.PDF", "main.pdf"])
        self.assertEqual(paper_viewer.PAPER_DIR, orig_dir)

        self.assertEqual(paper_viewer.resolve_zh_path_for(self.pdf_a), self.zh_a.resolve())
        self.assertEqual(paper_viewer.resolve_zh_path_for(self.pdf_b), self.zh_b.resolve())
        self.assertIsNone(paper_viewer.resolve_zh_path_for(self.pdf_upper, explicit_zh=self.zh_a))

    def test_get_meta_for_and_get_zh_for_contract_and_cross_dir_cache_isolation(self):
        meta_a = paper_viewer.get_meta_for(self.pdf_a)
        for key in ("file", "pdf_path", "paper_dir", "pages", "sizes", "links", "files", "mtime", "tex_path", "tex_uri"):
            self.assertIn(key, meta_a, f"Missing key {key!r} in get_meta_for")
        self.assertEqual(meta_a["file"], "main.pdf")
        self.assertEqual(Path(meta_a["pdf_path"]), self.pdf_a.resolve())
        self.assertEqual(Path(meta_a["paper_dir"]), self.dir_a.resolve())
        self.assertEqual(meta_a["pages"], meta_a["sizes"])
        self.assertTrue(meta_a["tex_uri"].startswith("file://"))
        self.assertIn("Extra.PDF", meta_a["files"])

        meta_b = paper_viewer.get_meta_for(self.pdf_b)
        self.assertEqual(Path(meta_b["pdf_path"]), self.pdf_b.resolve())
        self.assertEqual(meta_b["tex_uri"], "")

        zh_a = paper_viewer.get_zh_for(self.pdf_a)
        for key in ("available", "items", "regions", "sentences", "locks", "mode", "tex", "tex_path", "tex_uri", "title_zh", "error", "note", "warnings"):
            self.assertIn(key, zh_a, f"Missing key {key!r} in get_zh_for")
        self.assertTrue(zh_a["available"])
        self.assertEqual(zh_a["title_zh"], "论文 A 中文标题")
        self.assertTrue(zh_a["tex_uri"].startswith("file://"))
        self.assertIn("Abstract", zh_a["sentences"])

        zh_b = paper_viewer.get_zh_for(self.pdf_b)
        self.assertTrue(zh_b["available"])
        self.assertEqual(zh_b["title_zh"], "论文 B 中文标题")

        # Missing companion JSON still satisfies contract keys
        zh_missing = paper_viewer.get_zh_for(self.pdf_upper)
        self.assertFalse(zh_missing["available"])
        for key in ("items", "regions", "sentences", "locks", "mode", "tex", "tex_path", "tex_uri", "title_zh", "error", "note", "warnings"):
            self.assertIn(key, zh_missing)

    def test_render_page_svg_png_and_text_layer_for(self):
        svg = paper_viewer.render_page_svg_for(self.pdf_a, 0)
        self.assertIsInstance(svg, str)
        self.assertIn("<svg", svg)
        with self.assertRaises(IndexError):
            paper_viewer.render_page_svg_for(self.pdf_a, 99)

        png_b64 = paper_viewer.render_page_png_b64_for(self.pdf_a, 0, zoom=1.5)
        png_bytes = base64.b64decode(png_b64)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

        tl_single = paper_viewer.get_text_layer_for(self.pdf_a, page_idx=0)
        self.assertEqual(tl_single["page"], 0)
        self.assertIn("runs", tl_single)
        self.assertIn("mtime", tl_single)
        self.assertNotIn("pages", tl_single)

        tl_all = paper_viewer.get_text_layer_for(self.pdf_a, page_idx=None)
        self.assertEqual(len(tl_all["pages"]), 2)

        with self.assertRaises(IndexError):
            paper_viewer.get_text_layer_for(self.pdf_a, page_idx=5)

    def test_render_viewer_html_mcp_app_mode_csp_and_script_escaping(self):
        bridge_path = ROOT_DIR / "mcp_app" / "dist" / "mcp_bridge.js"
        self.assertTrue(bridge_path.is_file(), "Missing prebuilt mcp_app/dist/mcp_bridge.js")
        bridge_js = bridge_path.read_text(encoding="utf-8")

        html_mcp = paper_viewer.render_viewer_html(mode="mcp-app", mcp_bridge_js=bridge_js)
        self.assertLess(len(html_mcp.encode("utf-8")), 10 * 1024 * 1024)
        self.assertIn('window.__CHENGMEN_MODE__ = "mcp-app";', html_mcp)
        self.assertNotIn("nonce=", html_mcp)
        self.assertIsNone(re.search(r"<script[^>]+src=", html_mcp, re.IGNORECASE))
        self.assertIsNone(re.search(r"<link[^>]+href=", html_mcp, re.IGNORECASE))
        self.assertNotIn("__MCP_BRIDGE_BLOCK__", html_mcp)
        self.assertNotIn("__SCRIPT_NONCE_ATTR__", html_mcp)
        self.assertNotIn("__VIEWER_MODE__", html_mcp)
        self.assertNotIn("__VIEWER_SESSION_TOKEN__", html_mcp)

        # Ensure closing </script> inside bridge JS is safely escaped
        escaped_html = paper_viewer.render_viewer_html(mode="mcp-app", mcp_bridge_js='const s = "</script><SCRIPT>";')
        self.assertIn(r"<\/script>", escaped_html)
        self.assertIn('const s = "<\\/script><SCRIPT>";', escaped_html)

        with self.assertRaises(ValueError):
            paper_viewer.render_viewer_html(mode="invalid")

        node_bin = shutil.which("node")
        if node_bin:
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_mcp, re.DOTALL)
            self.assertEqual(len(scripts), 3)
            for idx, js_src in enumerate(scripts):
                js_file = self.tmp / f"mcp_html_script_{idx}.js"
                js_file.write_text(js_src, encoding="utf-8")
                chk = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True, text=True, timeout=10)
                self.assertEqual(chk.returncode, 0, f"MCP App HTML script {idx} failed node --check:\n{chk.stderr}")

    def test_mcp_bridge_bundle_sep1865_handshake_and_concurrency_cap(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node is not installed")

        bridge_path = ROOT_DIR / "mcp_app" / "dist" / "mcp_bridge.js"
        harness = self.tmp / "bridge_harness.mjs"
        harness.write_text(
            f"""
import fs from 'node:fs';
import vm from 'node:vm';

const listeners = [];
const sent = [];
let inFlightTools = 0;
let maxObservedInFlight = 0;
const toolCallOrder = [];
const toolCallArgs = {{}};
const pendingResolvers = [];

const styleProps = new Map();
const rootAttrs = new Map();
const docEl = {{
  style: {{
    setProperty: (k, v) => styleProps.set(k, v),
    colorScheme: '',
    height: '',
  }},
  setAttribute: (k, v) => rootAttrs.set(k, v),
  getAttribute: (k) => rootAttrs.get(k) || null,
  getBoundingClientRect: () => ({{ width: 900, height: 640 }}),
  classList: {{ toggle: () => {{}} }},
}};
const document = {{
  documentElement: docEl,
  head: {{ appendChild: () => {{}} }},
  body: {{ classList: {{ add: () => {{}}, toggle: () => {{}} }} }},
  createElement: () => ({{ style: {{}}, setAttribute: () => {{}}, textContent: '' }}),
  getElementById: () => null,
}};

const windowObj = {{
  __CHENGMEN_MODE__: 'mcp-app',
  innerWidth: 900,
  innerHeight: 640,
  document,
  addEventListener: (ev, fn) => {{ if (ev === 'message') listeners.push(fn); }},
  removeEventListener: (ev, fn) => {{
    const i = listeners.indexOf(fn);
    if (i !== -1) listeners.splice(i, 1);
  }},
  parent: null,
}};

function dispatchFromHost(msg) {{
  for (const fn of [...listeners]) {{
    fn({{ data: msg, source: windowObj.parent, origin: 'null' }});
  }}
}}

windowObj.parent = {{
  postMessage: (msg) => {{
    sent.push(msg);
    if (msg.method === 'ui/initialize') {{
      queueMicrotask(() => {{
        dispatchFromHost({{
          jsonrpc: '2.0',
          id: msg.id,
          result: {{
            protocolVersion: msg.params.protocolVersion,
            hostInfo: {{ name: 'MockAntigravity', version: '1.0' }},
            hostCapabilities: {{
              serverTools: {{ listChanged: false }},
              message: {{ text: {{}} }},
              updateModelContext: {{ text: {{}} }},
              openLinks: {{}},
            }},
            hostContext: {{
              theme: 'light',
              styles: {{ variables: {{ '--color-background-primary': '#fafafa' }} }},
            }},
          }},
        }});
      }});
    }} else if (msg.method === 'ui/notifications/initialized') {{
      queueMicrotask(() => {{
        dispatchFromHost({{
          jsonrpc: '2.0',
          method: 'ui/notifications/tool-result',
          params: {{
            content: [{{ type: 'text', text: 'opened' }}],
            structuredContent: {{ has_paper: true, file: 'demo.pdf' }},
          }},
        }});
      }});
    }} else if (msg.method === 'tools/call') {{
      inFlightTools += 1;
      maxObservedInFlight = Math.max(maxObservedInFlight, inFlightTools);
      toolCallOrder.push(msg.params.name);
      toolCallArgs[msg.params.name] = msg.params.arguments;
      pendingResolvers.push(() => {{
        inFlightTools -= 1;
        dispatchFromHost({{
          jsonrpc: '2.0',
          id: msg.id,
          result: {{
            content: [{{ type: 'text', text: 'ok' }}],
            structuredContent: {{ called: msg.params.name }},
          }},
        }});
      }});
    }} else if (msg.method === 'com.google.antigravity/toggle-aux-pane') {{
      queueMicrotask(() => {{
        dispatchFromHost({{
          jsonrpc: '2.0',
          id: msg.id,
          error: {{ code: -32601, message: 'Method not found' }},
        }});
      }});
    }}
  }},
}};

const ctx = vm.createContext({{
  window: windowObj,
  self: windowObj,
  globalThis: windowObj,
  document,
  console,
  setTimeout,
  clearTimeout,
  queueMicrotask,
  URL,
  Blob,
  DOMException,
  AbortController,
  ResizeObserver: class {{ observe() {{}} disconnect() {{}} }},
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
}});
windowObj.window = windowObj;

const src = fs.readFileSync({json.dumps(str(bridge_path))}, 'utf8');
vm.runInContext(src, ctx);

const bridge = windowObj.__CHENGMEN_MCP_BRIDGE__;
if (!bridge) throw new Error('Bridge not mounted on window.__CHENGMEN_MCP_BRIDGE__');

await bridge.ready;
const caps = bridge.getCapabilities();
if (!caps.serverTools || !caps.message || !caps.updateModelContext || !caps.openLinks) {{
  throw new Error('Unexpected capabilities: ' + JSON.stringify(caps));
}}
if (rootAttrs.get('data-theme') !== 'light') {{
  throw new Error('Theme not applied: ' + rootAttrs.get('data-theme'));
}}
if (styleProps.get('--color-background-primary') !== '#fafafa') {{
  throw new Error('Host style variable not applied');
}}

// Replay buffered initial tool-result
let replayed = null;
bridge.onToolResult((r) => {{ replayed = r; }});
await new Promise((r) => setTimeout(r, 20));
if (!replayed || replayed.structuredContent?.file !== 'demo.pdf') {{
  throw new Error('Initial tool-result not received/replayed');
}}

// Fire 6 concurrent calls with priorities; cap must remain <= 3 and priority 10 must preempt queued priority 0
// Also verify undefined keys (top-level & nested) are stripped before tools/call transmission
const calls = [
  bridge.callTool('low_1', {{ keep: 'ok', drop: undefined }}, 0),
  bridge.callTool('low_2', {{ nested: {{ a: 1, b: undefined }} }}, 0),
  bridge.callTool('low_3', {{}}, 0),
  bridge.callTool('low_4', {{}}, 0),
  bridge.callTool('high_urgent', {{ pdf_path: '/a.pdf', zh_path: undefined }}, 10),
  bridge.callTool('low_5', {{}}, 0),
];
await new Promise((r) => setTimeout(r, 20));
if (maxObservedInFlight > 3) {{
  throw new Error('Concurrency cap exceeded: ' + maxObservedInFlight);
}}
while (pendingResolvers.length > 0) {{
  const fn = pendingResolvers.shift();
  fn();
  await new Promise((r) => setTimeout(r, 5));
}}
const results = await Promise.all(calls);
if (results[4]?.called !== 'high_urgent') throw new Error('Unwrap failed');
if (toolCallOrder[3] !== 'high_urgent') {{
  throw new Error('Priority scheduling failed: ' + JSON.stringify(toolCallOrder));
}}
if ('drop' in (toolCallArgs.low_1 || {{}}) || toolCallArgs.low_1?.keep !== 'ok') {{
  throw new Error('Top-level undefined key not stripped: ' + JSON.stringify(toolCallArgs.low_1));
}}
if ('b' in (toolCallArgs.low_2?.nested || {{}}) || toolCallArgs.low_2?.nested?.a !== 1) {{
  throw new Error('Nested undefined key not stripped: ' + JSON.stringify(toolCallArgs.low_2));
}}
if ('zh_path' in (toolCallArgs.high_urgent || {{}}) || toolCallArgs.high_urgent?.pdf_path !== '/a.pdf') {{
  throw new Error('zh_path undefined key leaked: ' + JSON.stringify(toolCallArgs.high_urgent));
}}

// Unsupported toggle-aux-pane (-32601) returns graceful unsupported object instead of throwing
const auxRes = await bridge.openFileInAuxPane('file:///tmp/demo.tex');
if (auxRes.ok !== false || auxRes.unsupported !== true) {{
  throw new Error('Unexpected toggle-aux-pane fallback: ' + JSON.stringify(auxRes));
}}

console.log('MCP_BRIDGE_HARNESS_OK', JSON.stringify({{ maxObservedInFlight, toolCallOrder }}));
""",
            encoding="utf-8",
        )
        res = subprocess.run([node_bin, str(harness)], capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, f"Bridge harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("MCP_BRIDGE_HARNESS_OK", res.stdout)

    def test_mcp_app_frontend_same_pdf_jump_startup_race_and_no_fetch(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node is not installed")

        bridge_path = ROOT_DIR / "mcp_app" / "dist" / "mcp_bridge.js"
        bridge_js = bridge_path.read_text(encoding="utf-8")
        html_mcp = paper_viewer.render_viewer_html(mode="mcp-app", mcp_bridge_js=bridge_js)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_mcp, re.DOTALL)
        self.assertEqual(len(scripts), 3)

        html_no_bridge = paper_viewer.render_viewer_html(mode="mcp-app", mcp_bridge_js="")
        scripts_no_bridge = re.findall(r"<script[^>]*>(.*?)</script>", html_no_bridge, re.DOTALL)
        self.assertEqual(len(scripts_no_bridge), 3)
        self.assertEqual(scripts_no_bridge[1].strip(), "")

        script_bundle_path = self.tmp / "mcp_scripts.json"
        script_bundle_path.write_text(
            json.dumps({"full": scripts, "no_bridge": scripts_no_bridge}),
            encoding="utf-8",
        )

        harness = self.tmp / "frontend_mcp_harness.mjs"
        harness.write_text(
            f"""
import fs from 'node:fs';
import vm from 'node:vm';

const bundles = JSON.parse(fs.readFileSync({json.dumps(str(script_bundle_path))}, 'utf8'));

function createMockEnv(opts = {{}}) {{
  const idMap = new Map();
  const winListeners = new Map();
  const docListeners = new Map();
  const ioInstances = [];
  let fetchCalls = 0;

  class ClassList {{
    constructor(el) {{ this.el = el; this.set = new Set(); }}
    add(...cls) {{ cls.forEach(c => c && this.set.add(c)); }}
    remove(...cls) {{ cls.forEach(c => this.set.delete(c)); }}
    toggle(c, force) {{
      if (force === undefined) {{
        if (this.set.has(c)) {{ this.set.delete(c); return false; }}
        this.set.add(c); return true;
      }}
      if (force) this.set.add(c); else this.set.delete(c);
      return Boolean(force);
    }}
    contains(c) {{ return this.set.has(c); }}
  }}

  class El {{
    constructor(tag = 'div', id = '') {{
      this.tagName = String(tag).toUpperCase();
      this.id = id;
      this.dataset = {{}};
      this.children = [];
      this.parentNode = null;
      this.classList = new ClassList(this);
      this.style = {{
        _props: new Map(),
        setProperty(k, v) {{ this._props.set(k, v); }},
        colorScheme: '',
        height: '',
        display: '',
        width: '',
        flex: '',
        left: '',
        top: '',
        userSelect: '',
      }};
      this._text = '';
      this._className = '';
      this.value = '';
      this.title = '';
      this.src = '';
      this.scrollTop = 0;
      this.scrollLeft = 0;
      this.scrollHeight = 2000;
      this.scrollWidth = 800;
      this.clientHeight = 700;
      this.clientWidth = 800;
      this.offsetWidth = 100;
      this._listeners = new Map();
    }}
    get className() {{ return this._className; }}
    set className(v) {{
      this._className = String(v || '');
      this.classList.set = new Set(this._className.split(/\\s+/).filter(Boolean));
    }}
    get textContent() {{ return this._text; }}
    set textContent(v) {{
      this._text = String(v ?? '');
      if (this._text === '') this.children = [];
    }}
    get innerHTML() {{ return this._innerHTML || ''; }}
    set innerHTML(v) {{
      this._innerHTML = String(v ?? '');
      this.children = [];
      const re = /<([a-z0-9]+)[^>]*class="([^"]+)"[^>]*>/gi;
      let m;
      while ((m = re.exec(this._innerHTML)) !== null) {{
        const child = new El(m[1]);
        child.className = m[2];
        this.appendChild(child);
      }}
    }}
    appendChild(child) {{
      if (!child) return child;
      if (child.tagName === '#FRAGMENT') {{
        for (const c of child.children) this.appendChild(c);
        child.children = [];
        return child;
      }}
      child.parentNode = this;
      this.children.push(child);
      return child;
    }}
    remove() {{
      if (!this.parentNode) return;
      const i = this.parentNode.children.indexOf(this);
      if (i !== -1) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    }}
    setAttribute(k, v) {{ this['_attr_' + k] = v; }}
    getAttribute(k) {{ return this['_attr_' + k] ?? null; }}
    addEventListener(ev, fn) {{
      if (!this._listeners.has(ev)) this._listeners.set(ev, []);
      this._listeners.get(ev).push(fn);
    }}
    removeEventListener(ev, fn) {{
      const arr = this._listeners.get(ev) || [];
      const i = arr.indexOf(fn);
      if (i !== -1) arr.splice(i, 1);
    }}
    getBoundingClientRect() {{ return {{ top: 0, left: 0, right: 800, bottom: 700, width: 800, height: 700 }}; }}
    getContext() {{ return {{ font: '', measureText: (s) => ({{ width: String(s || '').length * 7 }}) }}; }}
    focus() {{}}
    blur() {{}}
    select() {{}}
    removeAttribute(k) {{ delete this['_attr_' + k]; }}
    closest(sel) {{
      let cur = this;
      while (cur) {{
        if (cur._matches && cur._matches(sel)) return cur;
        cur = cur.parentNode;
      }}
      return null;
    }}
    scrollIntoView() {{}}
    scrollTo() {{}}
    contains(node) {{
      if (node === this) return true;
      for (const c of this.children) if (c.contains && c.contains(node)) return true;
      return false;
    }}
    _matches(sel) {{
      const pageAttr = sel.match(/^\\.page-card\\[data-page="([^"]+)"\\]$/);
      if (pageAttr) return this.classList.contains('page-card') && String(this.dataset.page) === pageAttr[1];
      const idAttr = sel.match(/^\\.zh-card\\[data-id="([^"]+)"\\]$/);
      if (idAttr) return this.classList.contains('zh-card') && String(this.dataset.id) === idAttr[1];
      if (sel.includes(' ')) {{
        const parts = sel.trim().split(/\\s+/);
        return this._matches(parts[parts.length - 1]);
      }}
      if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
      return this.tagName === sel.toUpperCase();
    }}
    querySelectorAll(sel) {{
      const out = [];
      const walk = (n) => {{
        for (const c of n.children || []) {{
          if (c._matches && c._matches(sel)) out.push(c);
          walk(c);
        }}
      }};
      walk(this);
      return out;
    }}
    querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
  }}

  const ensureId = (id) => {{
    if (!idMap.has(id)) idMap.set(id, new El('div', id));
    return idMap.get(id);
  }};
  const viewerEl = ensureId('viewer');
  const viewerInnerEl = ensureId('viewerInner');
  viewerEl.appendChild(viewerInnerEl);
  const zhPaneEl = ensureId('zhPane');
  const zhTitleEl = new El('div');
  zhTitleEl.className = 'zh-title';
  zhPaneEl.appendChild(zhTitleEl);

  const docEl = new El('html');
  const bodyEl = new El('body');
  const document = {{
    documentElement: docEl,
    head: new El('head'),
    body: bodyEl,
    hidden: false,
    title: '',
    activeElement: null,
    getElementById: (id) => ensureId(id),
    createElement: (tag) => new El(tag),
    createDocumentFragment: () => new El('#FRAGMENT'),
    createTextNode: (t) => ({{ nodeType: 3, textContent: String(t) }}),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: (ev, fn) => {{
      if (!docListeners.has(ev)) docListeners.set(ev, []);
      docListeners.get(ev).push(fn);
    }},
  }};

  const windowObj = {{
    innerWidth: 1200,
    innerHeight: 800,
    document,
    location: {{ search: '' }},
    localStorage: {{
      getItem: () => {{ throw new Error('SecurityError'); }},
      setItem: () => {{ throw new Error('SecurityError'); }},
    }},
    getSelection: () => ({{ isCollapsed: true, rangeCount: 0, toString: () => '' }}),
    addEventListener: (ev, fn) => {{
      if (!winListeners.has(ev)) winListeners.set(ev, []);
      winListeners.get(ev).push(fn);
    }},
    removeEventListener: (ev, fn) => {{
      const arr = winListeners.get(ev) || [];
      const i = arr.indexOf(fn);
      if (i !== -1) arr.splice(i, 1);
    }},
    parent: null,
  }};

  const dispatchFromHost = (msg) => {{
    for (const fn of [...(winListeners.get('message') || [])]) {{
      fn({{ data: msg, source: windowObj.parent, origin: 'null' }});
    }}
  }};

  windowObj.parent = {{
    postMessage: (msg) => {{
      if (opts.onPostMessage) opts.onPostMessage(msg, dispatchFromHost);
    }},
  }};

  class MockIntersectionObserver {{
    constructor(cb) {{
      this.cb = cb;
      this.observed = [];
      this.disconnected = false;
      ioInstances.push(this);
    }}
    observe(el) {{ this.observed.push(el); }}
    disconnect() {{ this.disconnected = true; }}
    trigger(el) {{
      if (this.disconnected) return;
      this.cb([{{ isIntersecting: true, target: el }}]);
    }}
  }}

  const timers = new Set();
  const safeSetTimeout = (fn, ms) => {{
    const id = setTimeout(() => {{ timers.delete(id); fn(); }}, ms);
    timers.add(id);
    return id;
  }};
  const safeClearTimeout = (id) => {{ timers.delete(id); clearTimeout(id); }};
  const cleanup = () => {{ for (const id of timers) clearTimeout(id); timers.clear(); }};

  const blobCounter = {{ n: 0 }};
  const MockURL = {{
    createObjectURL: () => `blob:mock-${{++blobCounter.n}}`,
    revokeObjectURL: () => {{}},
  }};

  const ctx = vm.createContext({{
    window: windowObj,
    self: windowObj,
    globalThis: windowObj,
    document,
    location: windowObj.location,
    navigator: {{}},
    CSS: {{ escape: (s) => String(s) }},
    console,
    setTimeout: safeSetTimeout,
    clearTimeout: safeClearTimeout,
    queueMicrotask,
    URL: MockURL,
    URLSearchParams,
    Blob,
    DOMException,
    AbortController,
    IntersectionObserver: MockIntersectionObserver,
    ResizeObserver: class {{ observe() {{}} disconnect() {{}} }},
    requestAnimationFrame: (cb) => safeSetTimeout(cb, 0),
    fetch: async () => {{
      fetchCalls += 1;
      throw new Error('CSP_FETCH_BLOCKED');
    }},
  }});
  windowObj.window = windowObj;

  return {{
    ctx,
    idMap,
    ioInstances,
    dispatchFromHost,
    cleanup,
    getFetchCalls: () => fetchCalls,
  }};
}}

// --- Scenario A: delayed open_paper tool-input/result + same-PDF jump to page 4 + observer scroll to page 3 ---
{{
  const toolCalls = [];
  const metaPayload = {{
    file: 'paper.pdf',
    pdf_path: '/proj/paper.pdf',
    paper_dir: '/proj',
    mtime: 1000,
    mtime_str: '12:00:00',
    page_count: 4,
    pages: [{{ w: 612, h: 792 }}, {{ w: 612, h: 792 }}, {{ w: 612, h: 792 }}, {{ w: 612, h: 792 }}],
    sizes: [{{ w: 612, h: 792 }}, {{ w: 612, h: 792 }}, {{ w: 612, h: 792 }}, {{ w: 612, h: 792 }}],
    links: [[], [], [], []],
    files: ['paper.pdf'],
    tex_path: '',
    tex_uri: '',
  }};
  const zhPayload = {{
    available: true,
    file: 'paper.pdf',
    mtime: 1000,
    mtime_str: '12:00:00',
    pdf_mtime: 1000,
    locks_mtime: 0,
    title_zh: '测试论文',
    items: [
      {{ id: 'Sec1', page: 1, section: '§1', title: 'Intro', zh: '引言。', en_preview: 'Intro' }},
      {{ id: 'Sec3', page: 4, section: '§3', title: 'Method', zh: '方法。', en_preview: 'Method' }},
    ],
    regions: [
      {{ id: 'Sec3', page: 4, rect: [0.1, 0.2, 0.9, 0.4] }},
    ],
    sentences: {{}},
    locked_ids: [],
    lock_mode: 'deny',
  }};

  const env = createMockEnv({{
    onPostMessage: (msg, reply) => {{
      if (msg.method === 'ui/initialize') {{
        queueMicrotask(() => reply({{
          jsonrpc: '2.0',
          id: msg.id,
          result: {{
            protocolVersion: msg.params.protocolVersion,
            hostInfo: {{ name: 'MockHost', version: '1.0' }},
            hostCapabilities: {{ serverTools: {{}}, message: {{ text: {{}} }}, updateModelContext: {{ text: {{}} }} }},
            hostContext: {{ theme: 'dark' }},
          }},
        }}));
      }} else if (msg.method === 'ui/notifications/initialized') {{
        // Simulate host sending tool-input at 30ms and tool-result at 60ms (after ui/initialize)
        setTimeout(() => reply({{
          jsonrpc: '2.0',
          method: 'ui/notifications/tool-input',
          params: {{ arguments: {{ pdf_path: '/proj/paper.pdf' }} }},
        }}), 30);
        setTimeout(() => reply({{
          jsonrpc: '2.0',
          method: 'ui/notifications/tool-result',
          params: {{
            content: [{{ type: 'text', text: 'opened' }}],
            structuredContent: {{
              has_paper: true,
              file: 'paper.pdf',
              pdf_path: '/proj/paper.pdf',
              paper_dir: '/proj',
              zh_path: '/proj/paper_zh.json',
              meta: metaPayload,
              zh: zhPayload,
            }},
          }},
        }}), 60);
      }} else if (msg.method === 'tools/call') {{
        toolCalls.push({{ name: msg.params.name, args: msg.params.arguments }});
        let sc = {{ ok: true }};
        if (msg.params.name === 'app_get_page_svg') {{
          sc = {{ page_idx: msg.params.arguments.page_idx, svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>' }};
        }} else if (msg.params.name === 'app_get_text_layer') {{
          sc = {{ page: msg.params.arguments.page_idx, w: 612, h: 792, runs: [] }};
        }}
        queueMicrotask(() => reply({{
          jsonrpc: '2.0',
          id: msg.id,
          result: {{ content: [{{ type: 'text', text: 'ok' }}], structuredContent: sc }},
        }}));
      }}
    }},
  }});

  try {{
    for (const s of bundles.full) vm.runInContext(s, env.ctx);
    await new Promise(r => setTimeout(r, 450));

    if (toolCalls.some(c => c.name === 'app_get_state')) {{
      throw new Error('Startup race: app_get_state called despite delayed open_paper tool-input/result');
    }}
    const initialSvgPages = toolCalls.filter(c => c.name === 'app_get_page_svg').map(c => c.args.page_idx);
    if (!initialSvgPages.includes(0) || !initialSvgPages.includes(1)) {{
      throw new Error('Initial pages 0..1 did not eagerly load: ' + JSON.stringify(initialSvgPages));
    }}
    if (initialSvgPages.includes(2) || initialSvgPages.includes(3)) {{
      throw new Error('Pages 2..3 should still be unloaded before jump/scroll: ' + JSON.stringify(initialSvgPages));
    }}

    // Second open_paper on the SAME PDF with jump_id='Sec3', page=4
    env.dispatchFromHost({{
      jsonrpc: '2.0',
      method: 'ui/notifications/tool-result',
      params: {{
        content: [{{ type: 'text', text: 'jumped' }}],
        structuredContent: {{
          has_paper: true,
          file: 'paper.pdf',
          pdf_path: '/proj/paper.pdf',
          paper_dir: '/proj',
          zh_path: '/proj/paper_zh.json',
          jump_id: 'Sec3',
          page: 4,
          meta: metaPayload,
          zh: zhPayload,
        }},
      }},
    }});
    await new Promise(r => setTimeout(r, 60));

    const card4 = env.idMap.get('viewer').querySelector('.page-card[data-page="4"]');
    if (!card4 || card4.dataset.mcpLoaded !== '1') {{
      throw new Error('Same-PDF jump to page 4 failed to load page 4 assets');
    }}

    // Now simulate scrolling to page 3 via the existing IntersectionObserver (must NOT be killed by epoch/abort!)
    const card3 = env.idMap.get('viewer').querySelector('.page-card[data-page="3"]');
    const activeIo = env.ioInstances[env.ioInstances.length - 1];
    if (!activeIo || activeIo.disconnected) {{
      throw new Error('IntersectionObserver missing or unexpectedly disconnected');
    }}
    activeIo.trigger(card3);
    await new Promise(r => setTimeout(r, 60));

    if (!card3 || card3.dataset.mcpLoaded !== '1') {{
      throw new Error('Same-PDF observer scroll to page 3 left page 3 blank (epoch/signal invalidated)');
    }}
    if (env.getFetchCalls() !== 0) {{
      throw new Error('Unexpected fetch() call in MCP App mode: ' + env.getFetchCalls());
    }}
  }} finally {{
    env.cleanup();
  }}
}}

// --- Scenario B: standalone startup with deleted last_pdf falls back silently without error toast ---
{{
  const toolCalls = [];
  const env = createMockEnv({{
    onPostMessage: (msg, reply) => {{
      if (msg.method === 'ui/initialize') {{
        queueMicrotask(() => reply({{
          jsonrpc: '2.0',
          id: msg.id,
          result: {{
            protocolVersion: msg.params.protocolVersion,
            hostInfo: {{ name: 'MockHost', version: '1.0' }},
            hostCapabilities: {{ serverTools: {{}} }},
            hostContext: {{ theme: 'dark' }},
          }},
        }}));
      }} else if (msg.method === 'tools/call') {{
        toolCalls.push(msg.params.name);
        if (msg.params.name === 'app_get_state') {{
          queueMicrotask(() => reply({{
            jsonrpc: '2.0',
            id: msg.id,
            result: {{
              content: [{{ type: 'text', text: 'state' }}],
              structuredContent: {{ last_pdf: '/deleted/missing.pdf', recent_papers: [] }},
            }},
          }}));
        }} else if (msg.params.name === 'app_select_paper') {{
          queueMicrotask(() => reply({{
            jsonrpc: '2.0',
            id: msg.id,
            result: {{
              isError: true,
              content: [{{ type: 'text', text: 'PDF not found: /deleted/missing.pdf' }}],
            }},
          }}));
        }}
      }}
    }},
  }});

  try {{
    for (const s of bundles.full) vm.runInContext(s, env.ctx);
    await new Promise(r => setTimeout(r, 460));

    if (!toolCalls.includes('app_get_state') || !toolCalls.includes('app_select_paper')) {{
      throw new Error('Expected delayed fallback recovery calls, got: ' + JSON.stringify(toolCalls));
    }}
    const guideEl = env.idMap.get('emptyGuide');
    if (!guideEl.classList.contains('open')) {{
      throw new Error('Expected emptyGuide to open after deleted last_pdf recovery failed');
    }}
    const toastTxt = env.idMap.get('toast').textContent || '';
    if (toastTxt.includes('打开论文失败')) {{
      throw new Error('Startup recovery popped error toast for deleted last_pdf: ' + toastTxt);
    }}
    if (env.getFetchCalls() !== 0) {{
      throw new Error('Unexpected fetch() call during startup recovery: ' + env.getFetchCalls());
    }}
  }} finally {{
    env.cleanup();
  }}
}}

// --- Scenario C: mcp-app mode without MCP_BRIDGE must NEVER fall back to fetch('/api/...') ---
{{
  const env = createMockEnv();
  try {{
    for (const s of bundles.no_bridge) vm.runInContext(s, env.ctx);
    await new Promise(r => setTimeout(r, 50));
    if (env.getFetchCalls() !== 0) {{
      throw new Error('mcp-app mode fell back to fetch() without bridge: ' + env.getFetchCalls());
    }}
    const guideEl = env.idMap.get('emptyGuide');
    if (!guideEl.classList.contains('open')) {{
      throw new Error('Expected emptyGuide to open when MCP_BRIDGE is missing');
    }}
  }} finally {{
    env.cleanup();
  }}
}}

console.log('FRONTEND_MCP_HARNESS_OK');
""",
            encoding="utf-8",
        )
        res = subprocess.run([node_bin, str(harness)], capture_output=True, text=True, timeout=25)
        self.assertEqual(res.returncode, 0, f"Frontend MCP harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("FRONTEND_MCP_HARNESS_OK", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
