"""Chengmen Paper Viewer — Antigravity MCP App Server (SEP-1865).

Zero extra Python dependencies (Python 3.9+ stdlib + PyMuPDF via `paper_viewer.py`
and `paper_lock.py`). Implements MCP JSON-RPC 2.0 over line-delimited UTF-8 stdio.

All protocol messages go to stdout; all diagnostics go strictly to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import paper_lock  # noqa: E402
import paper_viewer  # noqa: E402

SERVER_NAME = "chengmen-paper-viewer"
SERVER_VERSION = "0.3.0"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

APP_RESOURCE_URI = "ui://chengmen/viewer.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"
BRIDGE_JS_PATH = PLUGIN_ROOT / "mcp_app" / "dist" / "mcp_bridge.js"

MAX_HTML_BYTES = 10 * 1024 * 1024  # 10 MiB MCP App HTML resource cap
MAX_OPEN_PAPER_BYTES = 900 * 1024  # < 900 KiB safety budget under 1 MiB trajectory cap
MAX_RECENT_PAPERS = 20
MAX_UI_STATE_BYTES = 64 * 1024


def _log(msg: str) -> None:
    sys.stderr.write(f"[chengmen-mcp] {msg}\n")
    sys.stderr.flush()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# =====================================================================
# Persistent State Store (${PLUGIN_DATA}/state.json)
# =====================================================================

class StateStore:
    """Thread-safe, atomically persisted plugin state store."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.state_path = self.data_dir / "state.json"
        self._lock = threading.RLock()

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "version": 1,
            "last_pdf": "",
            "last_paper_dir": "",
            "last_zh": "",
            "recent_papers": [],
            "ui_state": {},
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            base = self._default_state()
            if not self.state_path.exists():
                return base
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    return base
                out = dict(base)
                for k in ("last_pdf", "last_paper_dir", "last_zh"):
                    if isinstance(raw.get(k), str):
                        out[k] = raw[k]
                if isinstance(raw.get("recent_papers"), list):
                    out["recent_papers"] = [
                        r for r in raw["recent_papers"] if isinstance(r, dict) and isinstance(r.get("pdf_path"), str)
                    ][:MAX_RECENT_PAPERS]
                if isinstance(raw.get("ui_state"), dict):
                    out["ui_state"] = dict(raw["ui_state"])
                return out
            except Exception as exc:
                _log(f"Failed to read state.json ({exc}); falling back to defaults.")
                return base

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.data_dir,
                prefix=".state.",
                suffix=".tmp",
                delete=False,
            ) as tf:
                tmp_path = Path(tf.name)
                tf.write(payload)
                tf.flush()
                os.fsync(tf.fileno())
            last_err: OSError | None = None
            for attempt in range(8):
                try:
                    os.replace(tmp_path, self.state_path)
                    tmp_path = None
                    last_err = None
                    break
                except PermissionError as exc:
                    last_err = exc
                    time.sleep(0.01 * (attempt + 1))
            if last_err is not None:
                raise last_err
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def record_open(
        self,
        pdf_path: Path,
        paper_dir: Path,
        zh_path: Path | None,
        title_zh: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            st = self.load()
            pdf_s = str(pdf_path.resolve())
            pdir_s = str(paper_dir.resolve())
            zh_s = str(zh_path.resolve()) if zh_path is not None else ""
            st["last_pdf"] = pdf_s
            st["last_paper_dir"] = pdir_s
            st["last_zh"] = zh_s

            entry = {
                "pdf_path": pdf_s,
                "file": pdf_path.name,
                "paper_dir": pdir_s,
                "zh_path": zh_s,
                "title_zh": title_zh,
                "opened_at": _now_str(),
            }
            recent = [r for r in st["recent_papers"] if r.get("pdf_path") != pdf_s]
            recent.insert(0, entry)
            st["recent_papers"] = recent[:MAX_RECENT_PAPERS]
            self._write_unlocked(st)
            return st

    def save_partial(
        self,
        *,
        ui_state: dict[str, Any] | None = None,
        last_pdf: str | None = None,
        last_paper_dir: str | None = None,
        last_zh: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            st = self.load()
            if ui_state is not None:
                if not isinstance(ui_state, dict):
                    raise ValueError("'ui_state' must be a JSON object.")
                merged = dict(st.get("ui_state") or {})
                merged.update(ui_state)
                if len(json.dumps(merged, ensure_ascii=False).encode("utf-8")) > MAX_UI_STATE_BYTES:
                    raise ValueError(f"'ui_state' exceeds maximum size of {MAX_UI_STATE_BYTES} bytes.")
                st["ui_state"] = merged
            if last_pdf is not None:
                if not isinstance(last_pdf, str):
                    raise ValueError("'last_pdf' must be a string.")
                st["last_pdf"] = last_pdf
            if last_paper_dir is not None:
                if not isinstance(last_paper_dir, str):
                    raise ValueError("'last_paper_dir' must be a string.")
                st["last_paper_dir"] = last_paper_dir
            if last_zh is not None:
                if not isinstance(last_zh, str):
                    raise ValueError("'last_zh' must be a string.")
                st["last_zh"] = last_zh
            self._write_unlocked(st)
            return st


# =====================================================================
# MCP Tool & Resource Schemas (SEP-1865)
# =====================================================================

_APP_ONLY_META = {"ui": {"visibility": ["app"]}}

_OPEN_PAPER_META = {
    "ui": {"resourceUri": APP_RESOURCE_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Chengmen Paper Viewer"},
            {"type": "global", "title": "Chengmen Paper Viewer"},
        ]
    },
}

UI_RESOURCE_DESCRIPTOR = {
    "uri": APP_RESOURCE_URI,
    "name": "Chengmen Paper Viewer",
    "description": "Self-contained bilingual vector-SVG LaTeX paper viewer UI (MCP App).",
    "mimeType": APP_MIME_TYPE,
    "_meta": {"ui": {"prefersBorder": False}},
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "open_paper",
        "title": "Chengmen Paper Viewer",
        "description": (
            "Open or navigate a compiled English LaTeX PDF alongside its synchronized Chinese "
            "companion in the Chengmen Paper Viewer UI. All parameters are optional; calling "
            "with `{}` restores the most recently opened paper or opens the paper picker."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string", "description": "Optional path to a .pdf file (or filename within paper_dir)."},
                "paper_dir": {"type": "string", "description": "Optional directory containing PDF files."},
                "file": {"type": "string", "description": "Optional PDF filename inside paper_dir."},
                "zh_path": {"type": "string", "description": "Optional explicit path to *_zh.json companion file."},
                "jump_id": {"type": "string", "description": "Optional companion item ID to focus (e.g. '2.2-P1', 'Fig1')."},
                "page": {"type": "integer", "minimum": 1, "description": "Optional 1-based page number to jump to."},
            },
            "additionalProperties": False,
        },
        "_meta": _OPEN_PAPER_META,
    },
    {
        "name": "list_paper_locks",
        "title": "List Paper Locks",
        "description": (
            "Return the paragraph/section lock mode and all locked entries from `<stem>.locks.json` "
            "(or `.paper_viewer/<stem>.locks.json`) for the given or currently active paper."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string", "description": "Optional path to the paper .pdf or .tex file."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_paper_companion",
        "title": "Sync Paper Companion",
        "description": (
            "Re-validate Chinese companion JSON (`*_zh.json`) alignment against both the compiled PDF "
            "regions and the LaTeX source spans, reporting matched counts, missing IDs, `.tex` path, and warnings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string", "description": "Optional path to the target .pdf file."},
                "zh_path": {"type": "string", "description": "Optional explicit path to *_zh.json."},
            },
            "additionalProperties": False,
        },
    },
    # ---- App-only tools (hidden from the LLM; callable only by the MCP App UI) ----
    {
        "name": "app_select_paper",
        "title": "Select Paper (App)",
        "description": "Select or switch the active PDF/directory from inside the Chengmen Paper Viewer UI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string"},
                "paper_dir": {"type": "string"},
                "file": {"type": "string"},
                "zh_path": {"type": "string"},
                "jump_id": {"type": "string"},
                "page": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_get_meta",
        "title": "Get PDF Metadata (App)",
        "description": "Fetch PDF page geometry, hyperlinks, file list, mtime, and resolved .tex metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string"},
                "paper_dir": {"type": "string"},
                "file": {"type": "string"},
                "zh_path": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_get_zh",
        "title": "Get Chinese Companion (App)",
        "description": "Fetch synchronized Chinese companion cards, regions, sentence mappings, and lock state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string"},
                "file": {"type": "string"},
                "zh_path": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_get_page_svg",
        "title": "Render Page SVG (App)",
        "description": "Render a single 0-based PDF page as lossless vector SVG text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_idx": {"type": "integer", "minimum": 0},
                "pdf_path": {"type": "string"},
                "file": {"type": "string"},
            },
            "required": ["page_idx"],
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_get_page_png",
        "title": "Render Page PNG (App)",
        "description": "Render a single 0-based PDF page as Base64-encoded PNG.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_idx": {"type": "integer", "minimum": 0},
                "zoom": {"type": "number", "minimum": 0.25, "maximum": 6.0},
                "pdf_path": {"type": "string"},
                "file": {"type": "string"},
            },
            "required": ["page_idx"],
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_get_text_layer",
        "title": "Get PDF Text Layer (App)",
        "description": "Fetch selectable English text runs for one 0-based page or all pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_idx": {"type": ["integer", "null"], "minimum": 0},
                "pdf_path": {"type": "string"},
                "file": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_update_locks",
        "title": "Update Paragraph Locks (App)",
        "description": "Lock, unlock, toggle, or change the enforcement mode for paragraphs/sections.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["lock", "unlock", "toggle", "set_mode"]},
                "ids": {"type": "array", "items": {"type": "string"}},
                "mode": {"type": "string", "enum": ["deny", "force_ask"]},
                "pdf_path": {"type": "string"},
                "file": {"type": "string"},
                "zh_path": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_get_state",
        "title": "Get Persisted Viewer State (App)",
        "description": "Read persisted viewer preferences and recently opened papers from plugin data storage.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
    {
        "name": "app_save_state",
        "title": "Save Persisted Viewer State (App)",
        "description": "Persist viewer UI preferences (zoom, split layout, filter/lock-bar toggles) to plugin data storage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ui_state": {"type": "object"},
                "last_pdf": {"type": "string"},
                "last_paper_dir": {"type": "string"},
                "last_zh": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": _APP_ONLY_META,
    },
]

_KNOWN_TOOL_NAMES = frozenset(t["name"] for t in TOOL_DEFINITIONS)


# =====================================================================
# MCP Server Core
# =====================================================================

class ChengmenMcpServer:
    """Stateless-per-call MCP JSON-RPC 2.0 server backed by paper_viewer & paper_lock."""

    def __init__(self, data_dir: Path) -> None:
        self.store = StateStore(data_dir)

    # ---- Path & companion resolution helpers ----

    def _safe_file_in_dir(self, paper_dir: Path, filename: str) -> Path:
        if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
            raise ValueError(f"Invalid PDF filename: {filename!r}")
        pdir = paper_dir.resolve()
        if not pdir.is_dir():
            raise FileNotFoundError(f"Paper directory not found: {pdir}")
        pdfs = paper_viewer.list_pdfs_in(pdir)
        by_name = {p.name: p for p in pdfs}
        if filename in by_name:
            return by_name[filename]
        # Case-insensitive fallback on Windows / mixed case
        low = filename.lower()
        for p in pdfs:
            if p.name.lower() == low:
                return p
        raise FileNotFoundError(f"PDF '{filename}' not found in {pdir}")

    def _resolve_target(
        self,
        args: dict[str, Any],
        *,
        allow_empty: bool = False,
    ) -> tuple[Path | None, Path | None, Path | None]:
        """Resolve `(pdf_path, paper_dir, explicit_zh)` from tool args + persisted state."""
        state = self.store.load()
        raw_pdf = str(args.get("pdf_path") or "").strip()
        raw_dir = str(args.get("paper_dir") or "").strip()
        raw_file = str(args.get("file") or "").strip()
        raw_zh = str(args.get("zh_path") or "").strip()

        paper_dir: Path | None = None
        if raw_dir:
            pdir = Path(raw_dir).expanduser().resolve()
            if not pdir.is_dir():
                raise FileNotFoundError(f"Paper directory not found: {pdir}")
            paper_dir = pdir

        pdf_path: Path | None = None
        if raw_pdf:
            cand = Path(raw_pdf).expanduser()
            if not cand.is_absolute():
                if paper_dir is not None:
                    cand_res = (paper_dir / cand).resolve()
                    if paper_dir != cand_res.parent and paper_dir not in cand_res.parents:
                        raise ValueError("pdf_path escapes the specified paper_dir.")
                    cand = cand_res
                elif state.get("last_paper_dir") and Path(state["last_paper_dir"]).is_dir():
                    cand = (Path(state["last_paper_dir"]) / cand).resolve()
                else:
                    cand = cand.resolve()
            else:
                cand = cand.resolve()

            # Allow passing a directory via pdf_path gracefully (e.g., from quick picker)
            if cand.is_dir():
                paper_dir = cand
                pdfs = paper_viewer.list_pdfs_in(paper_dir)
                if not pdfs:
                    raise FileNotFoundError(f"No PDF files found in directory: {paper_dir}")
                pdf_path = pdfs[0]
            else:
                if not cand.is_file():
                    raise FileNotFoundError(f"PDF file not found: {cand}")
                if cand.suffix.lower() != ".pdf":
                    raise ValueError(f"Target file is not a PDF: {cand.name}")
                pdf_path = cand
                if paper_dir is None:
                    paper_dir = pdf_path.parent
        elif raw_file:
            if paper_dir is None:
                if state.get("last_paper_dir") and Path(state["last_paper_dir"]).is_dir():
                    paper_dir = Path(state["last_paper_dir"]).resolve()
                elif state.get("last_pdf") and Path(state["last_pdf"]).parent.is_dir():
                    paper_dir = Path(state["last_pdf"]).resolve().parent
                else:
                    raise FileNotFoundError("No active paper directory; specify 'pdf_path' or 'paper_dir'.")
            pdf_path = self._safe_file_in_dir(paper_dir, raw_file)
        elif paper_dir is not None:
            pdfs = paper_viewer.list_pdfs_in(paper_dir)
            if not pdfs:
                raise FileNotFoundError(f"No PDF files found in directory: {paper_dir}")
            last_pdf_s = state.get("last_pdf") or ""
            matched_last = next((p for p in pdfs if str(p.resolve()) == last_pdf_s), None)
            pdf_path = matched_last or pdfs[0]
        else:
            # Empty args `{}`: try persisted last_pdf or last_paper_dir
            last_pdf_s = state.get("last_pdf") or ""
            if last_pdf_s:
                lp = Path(last_pdf_s)
                if lp.is_file():
                    pdf_path = lp.resolve()
                    last_dir_s = state.get("last_paper_dir") or ""
                    if last_dir_s and Path(last_dir_s).is_dir() and (
                        Path(last_dir_s).resolve() == pdf_path.parent
                        or Path(last_dir_s).resolve() in pdf_path.parents
                    ):
                        paper_dir = Path(last_dir_s).resolve()
                    else:
                        paper_dir = pdf_path.parent
            if pdf_path is None and state.get("last_paper_dir"):
                ld = Path(state["last_paper_dir"])
                if ld.is_dir():
                    pdfs = paper_viewer.list_pdfs_in(ld)
                    if pdfs:
                        paper_dir = ld.resolve()
                        pdf_path = pdfs[0]

        if pdf_path is None:
            if allow_empty:
                return None, None, None
            raise FileNotFoundError("No active paper found. Pass 'pdf_path' or 'paper_dir' first.")

        if paper_dir is None:
            paper_dir = pdf_path.parent

        explicit_zh: Path | None = None
        if raw_zh:
            zh_cand = Path(raw_zh).expanduser()
            if not zh_cand.is_absolute():
                zh_cand = (pdf_path.parent / zh_cand).resolve()
            else:
                zh_cand = zh_cand.resolve()
            explicit_zh = zh_cand
        elif state.get("last_zh") and state.get("last_pdf") == str(pdf_path.resolve()):
            zh_prev = Path(state["last_zh"])
            if zh_prev.is_file():
                explicit_zh = zh_prev.resolve()

        return pdf_path, paper_dir, explicit_zh

    @staticmethod
    def _tool_ok(text: str, structured: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": False,
        }

    @staticmethod
    def _tool_err(message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": f"Error: {message}"}],
            "structuredContent": {"error": message},
            "isError": True,
        }

    @staticmethod
    def _read_ui_html() -> str:
        override = os.environ.get("CHENGMEN_APP_HTML", "").strip()
        if override:
            ov_path = Path(override).expanduser()
            if ov_path.is_file():
                html = ov_path.read_text(encoding="utf-8-sig")
                if not html.strip():
                    raise RuntimeError(f"CHENGMEN_APP_HTML file is empty: {ov_path}")
                if len(html.encode("utf-8")) >= MAX_HTML_BYTES:
                    raise RuntimeError(f"CHENGMEN_APP_HTML exceeds {MAX_HTML_BYTES} bytes limit.")
                return html

        bridge_js = ""
        if BRIDGE_JS_PATH.is_file():
            bridge_js = BRIDGE_JS_PATH.read_text(encoding="utf-8")
        else:
            _log(f"Warning: {BRIDGE_JS_PATH} not found; rendering MCP App HTML without bundled bridge.")

        html = paper_viewer.render_viewer_html(mode="mcp-app", mcp_bridge_js=bridge_js)
        encoded_len = len(html.encode("utf-8"))
        if encoded_len == 0 or encoded_len >= MAX_HTML_BYTES:
            raise RuntimeError(f"Generated MCP App HTML size invalid: {encoded_len} bytes.")
        return html

    # ---- Tool implementations ----

    def _build_open_result(self, args: dict[str, Any], *, allow_empty: bool) -> dict[str, Any]:
        pdf_path, paper_dir, explicit_zh = self._resolve_target(args, allow_empty=allow_empty)
        if pdf_path is None or paper_dir is None:
            st = self.store.load()
            structured = {
                "has_paper": False,
                "recent_papers": st["recent_papers"],
                "ui_state": st["ui_state"],
                "note": "No paper is currently open. Select a recent paper or enter a local PDF path or folder.",
            }
            return self._tool_ok(
                "Chengmen Paper Viewer opened with no active paper. Choose a recent paper or pass pdf_path/paper_dir.",
                structured,
            )

        zh_default = pdf_path.name if explicit_zh is not None else None
        meta = paper_viewer.get_meta_for(
            pdf_path, paper_dir=paper_dir, explicit_zh=explicit_zh, default_file=zh_default
        )
        zh = paper_viewer.get_zh_for(pdf_path, explicit_zh=explicit_zh, default_file=zh_default)

        resolved_zh = paper_viewer.resolve_zh_path_for(
            pdf_path, explicit_zh=explicit_zh, default_file=zh_default
        )
        st = self.store.record_open(
            pdf_path=pdf_path,
            paper_dir=paper_dir,
            zh_path=explicit_zh,
            title_zh=str(zh.get("title_zh") or ""),
        )

        jump_id = str(args.get("jump_id") or "").strip() or None
        page_arg = args.get("page")
        jump_page: int | None = None
        if page_arg is not None:
            if isinstance(page_arg, bool) or not isinstance(page_arg, int) or page_arg < 1:
                raise ValueError("'page' must be a positive 1-based integer.")
            jump_page = page_arg

        structured: dict[str, Any] = {
            "has_paper": True,
            "pdf_path": str(pdf_path.resolve()),
            "paper_dir": str(paper_dir.resolve()),
            "zh_path": str(resolved_zh.resolve()) if resolved_zh is not None else "",
            "file": pdf_path.name,
            "meta": meta,
            "zh": zh,
            "zh_deferred": False,
            "meta_deferred": False,
            "jump_id": jump_id,
            "page": jump_page,
            "recent_papers": st["recent_papers"],
            "ui_state": st["ui_state"],
        }

        page_cnt = meta.get("page_count") or len(meta.get("pages") or [])
        locked_cnt = len(zh.get("locked_ids") or zh.get("locks") or [])
        item_cnt = len(zh.get("items") or [])
        summary_parts = [
            f"Opened {pdf_path.name} ({page_cnt} pages)",
            f"companion: {'loaded (' + str(item_cnt) + ' items)' if zh.get('available') else 'unavailable'}",
            f"locks: {locked_cnt} ({zh.get('lock_mode') or zh.get('mode') or 'deny'})",
        ]
        if jump_id:
            summary_parts.append(f"jump_id={jump_id}")
        if jump_page is not None:
            summary_parts.append(f"page={jump_page}")
        summary = " | ".join(summary_parts)

        # Enforce strict < 900 KiB budget so trajectory CallToolResult never hits the 1 MiB host cap
        result = self._tool_ok(summary, structured)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OPEN_PAPER_BYTES:
            structured["zh_deferred"] = True
            structured["zh"] = {
                "available": bool(zh.get("available")),
                "file": zh.get("file", pdf_path.name),
                "title_zh": zh.get("title_zh", ""),
                "item_count": item_cnt,
                "lock_mode": zh.get("lock_mode") or zh.get("mode") or "deny",
                "locked_ids": zh.get("locked_ids") or [],
                "tex": zh.get("tex", ""),
                "tex_path": zh.get("tex_path", ""),
                "tex_uri": zh.get("tex_uri", ""),
                "mtime": zh.get("mtime", 0),
                "pdf_mtime": zh.get("pdf_mtime", 0),
                "locks_mtime": zh.get("locks_mtime", 0),
            }
            result = self._tool_ok(summary, structured)

        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OPEN_PAPER_BYTES:
            structured["meta_deferred"] = True
            structured["meta"] = {
                "file": meta.get("file", pdf_path.name),
                "pdf_path": str(pdf_path.resolve()),
                "paper_dir": str(paper_dir.resolve()),
                "files": meta.get("files") or [pdf_path.name],
                "page_count": page_cnt,
                "pages": meta.get("pages") or [],
                "mtime": meta.get("mtime", 0),
                "mtime_str": meta.get("mtime_str", ""),
                "tex_path": meta.get("tex_path", ""),
                "tex_uri": meta.get("tex_uri", ""),
            }
            result = self._tool_ok(summary, structured)

        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OPEN_PAPER_BYTES:
            structured["recent_papers"] = st["recent_papers"][:5]
            result = self._tool_ok(summary, structured)

        return result

    def _tool_open_paper(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._build_open_result(args, allow_empty=True)

    def _tool_app_select_paper(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._build_open_result(args, allow_empty=False)

    def _tool_list_paper_locks(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_pdf = str(args.get("pdf_path") or "").strip()
        if raw_pdf and Path(raw_pdf).expanduser().suffix.lower() == ".tex":
            target = Path(raw_pdf).expanduser().resolve()
            if not target.exists():
                raise FileNotFoundError(f"TeX file not found: {target}")
            pdf_path = target.with_suffix(".pdf")
        else:
            pdf_path, _pdir, _zh = self._resolve_target(args, allow_empty=False)
            assert pdf_path is not None

        ldata = paper_lock.get_locks_for_pdf(pdf_path)
        locks_map = ldata.get("locks") or {}
        lock_summaries = [
            {
                "id": cid,
                "section": entry.get("section", ""),
                "title": entry.get("title", ""),
                "kind": entry.get("kind", "prose"),
                "tex_start_line": entry.get("tex_start_line"),
                "tex_end_line": entry.get("tex_end_line"),
                "locked_at": entry.get("locked_at", ""),
            }
            for cid, entry in sorted(locks_map.items())
            if isinstance(entry, dict)
        ]
        structured = {
            "pdf_path": str(pdf_path.resolve()),
            "file": pdf_path.name,
            "tex": ldata.get("tex", ""),
            "tex_path": ldata.get("tex_path") or str(paper_lock.tex_path_for(pdf_path)),
            "lock_path": ldata.get("lock_path", str(paper_lock.lock_path_for(pdf_path))),
            "mode": ldata.get("mode", "deny"),
            "locked_count": len(lock_summaries),
            "locked_ids": ldata.get("locked_ids") or sorted(locks_map.keys()),
            "locks": lock_summaries,
        }
        if lock_summaries:
            ids_str = ", ".join(s["id"] for s in lock_summaries[:12])
            if len(lock_summaries) > 12:
                ids_str += f" (+{len(lock_summaries) - 12} more)"
            text = (
                f"{pdf_path.name}: {len(lock_summaries)} locked region(s) in '{structured['mode']}' mode "
                f"({ids_str}). Lock file: {structured['lock_path']}"
            )
        else:
            text = f"{pdf_path.name}: no locked regions (mode='{structured['mode']}')."
        return self._tool_ok(text, structured)

    def _tool_sync_paper_companion(self, args: dict[str, Any]) -> dict[str, Any]:
        pdf_path, _pdir, explicit_zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None

        zh_default = pdf_path.name if explicit_zh is not None else None
        zh_file = paper_viewer.resolve_zh_path_for(pdf_path, explicit_zh=explicit_zh, default_file=zh_default)
        zh_data = paper_viewer.get_zh_for(pdf_path, explicit_zh=explicit_zh, default_file=zh_default)
        items = zh_data.get("items") if isinstance(zh_data.get("items"), list) else []
        regions = zh_data.get("regions") if isinstance(zh_data.get("regions"), list) else []
        item_ids = [it["id"] for it in items if isinstance(it, dict) and isinstance(it.get("id"), str)]
        region_ids = {r.get("id") for r in regions if isinstance(r, dict) and r.get("id")}
        pdf_missing = [cid for cid in item_ids if cid not in region_ids]

        tex_hint = zh_data.get("tex_path") or zh_data.get("tex")
        base_dir = zh_file.parent if zh_file is not None else pdf_path.parent
        tp = paper_lock.tex_path_for(pdf_path, tex_hint=tex_hint, base_dir=base_dir)
        tex_exists = tp.is_file()

        tex_matched_count = 0
        tex_missing: list[str] = []
        warnings: list[str] = list(zh_data.get("warnings") or [])
        if tex_exists and items:
            tex_text = tp.read_text(encoding="utf-8-sig", errors="replace")
            spans = paper_lock.locate_tex_spans(tex_text, items)
            tex_matched_count = sum(1 for cid in item_ids if cid in spans)
            tex_missing = [cid for cid in item_ids if cid not in spans]
        elif not tex_exists:
            warnings.append(f"LaTeX source not found at {tp}")

        ldata = paper_lock.get_locks_for_pdf(pdf_path)
        structured = {
            "pdf_path": str(pdf_path.resolve()),
            "file": pdf_path.name,
            "zh_path": str(zh_file.resolve()) if zh_file is not None else "",
            "available": bool(zh_data.get("available")),
            "tex_path": str(tp.resolve()),
            "tex_exists": tex_exists,
            "total_items": len(item_ids),
            "pdf_matched_count": len(item_ids) - len(pdf_missing),
            "pdf_missing_ids": pdf_missing,
            "tex_matched_count": tex_matched_count,
            "tex_missing_ids": tex_missing,
            "locked_count": len(ldata.get("locked_ids") or []),
            "lock_mode": ldata.get("mode", "deny"),
            "warnings": warnings,
            "error": zh_data.get("error") or "",
        }
        if not structured["available"]:
            msg = f"Companion JSON unavailable for {pdf_path.name}: {zh_data.get('error') or zh_data.get('note') or 'not found'}"
        else:
            msg = (
                f"Synced {pdf_path.name}: {structured['pdf_matched_count']}/{structured['total_items']} PDF anchors matched, "
                f"{structured['tex_matched_count']}/{structured['total_items']} LaTeX spans matched "
                f"(tex={'found' if tex_exists else 'missing'}, locks={structured['locked_count']})."
            )
        return self._tool_ok(msg, structured)

    def _tool_app_get_meta(self, args: dict[str, Any]) -> dict[str, Any]:
        pdf_path, paper_dir, explicit_zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None and paper_dir is not None
        zh_default = pdf_path.name if explicit_zh is not None else None
        meta = paper_viewer.get_meta_for(
            pdf_path, paper_dir=paper_dir, explicit_zh=explicit_zh, default_file=zh_default
        )
        return self._tool_ok(f"Meta for {pdf_path.name} ({meta.get('page_count', 0)} pages)", meta)

    def _tool_app_get_zh(self, args: dict[str, Any]) -> dict[str, Any]:
        pdf_path, _pdir, explicit_zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None
        zh_default = pdf_path.name if explicit_zh is not None else None
        zh = paper_viewer.get_zh_for(pdf_path, explicit_zh=explicit_zh, default_file=zh_default)
        return self._tool_ok(
            f"Companion for {pdf_path.name} (available={bool(zh.get('available'))}, items={len(zh.get('items') or [])})",
            zh,
        )

    def _tool_app_get_page_svg(self, args: dict[str, Any]) -> dict[str, Any]:
        page_idx = args.get("page_idx")
        if isinstance(page_idx, bool) or not isinstance(page_idx, int) or page_idx < 0:
            raise ValueError("'page_idx' must be a non-negative integer.")
        pdf_path, _pdir, _zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None
        svg = paper_viewer.render_page_svg_for(pdf_path, page_idx)
        mtime = pdf_path.stat().st_mtime
        structured = {
            "page_idx": page_idx,
            "svg": svg,
            "file": pdf_path.name,
            "mtime": mtime,
        }
        return self._tool_ok(f"Rendered SVG for {pdf_path.name} page {page_idx + 1} ({len(svg)} chars)", structured)

    def _tool_app_get_page_png(self, args: dict[str, Any]) -> dict[str, Any]:
        page_idx = args.get("page_idx")
        if isinstance(page_idx, bool) or not isinstance(page_idx, int) or page_idx < 0:
            raise ValueError("'page_idx' must be a non-negative integer.")
        zoom_raw = args.get("zoom", 2.0)
        if isinstance(zoom_raw, bool) or not isinstance(zoom_raw, (int, float)):
            raise ValueError("'zoom' must be a number between 0.25 and 6.0.")
        zoom = float(zoom_raw)
        if not (0.25 <= zoom <= 6.0):
            raise ValueError("'zoom' must be between 0.25 and 6.0.")
        pdf_path, _pdir, _zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None
        png_b64 = paper_viewer.render_page_png_b64_for(pdf_path, page_idx, zoom=zoom)
        mtime = pdf_path.stat().st_mtime
        structured = {
            "page_idx": page_idx,
            "png_b64": png_b64,
            "mime_type": "image/png",
            "file": pdf_path.name,
            "mtime": mtime,
        }
        return self._tool_ok(f"Rendered PNG for {pdf_path.name} page {page_idx + 1}", structured)

    def _tool_app_get_text_layer(self, args: dict[str, Any]) -> dict[str, Any]:
        page_idx = args.get("page_idx", None)
        if page_idx is not None and (isinstance(page_idx, bool) or not isinstance(page_idx, int) or page_idx < 0):
            raise ValueError("'page_idx' must be a non-negative integer or null.")
        pdf_path, _pdir, _zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None
        layer = paper_viewer.get_text_layer_for(pdf_path, page_idx=page_idx)
        return self._tool_ok(
            f"Text layer for {pdf_path.name} (page_idx={page_idx})",
            layer,
        )

    def _tool_app_update_locks(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        ids = args.get("ids")
        mode = args.get("mode")
        pdf_path, _pdir, explicit_zh = self._resolve_target(args, allow_empty=False)
        assert pdf_path is not None

        zh_default = pdf_path.name if explicit_zh is not None else None
        zh_file = paper_viewer.resolve_zh_path_for(pdf_path, explicit_zh=explicit_zh, default_file=zh_default)
        items: list[dict[str, Any]] = []
        tex_hint: str | None = None
        base_dir = pdf_path.parent
        if zh_file is not None and zh_file.is_file():
            base_dir = zh_file.parent
            try:
                raw_zh = json.loads(zh_file.read_text(encoding="utf-8-sig"))
                if isinstance(raw_zh, dict):
                    target_file = raw_zh.get("file")
                    if not target_file or target_file == pdf_path.name:
                        raw_items = raw_zh.get("items")
                        if isinstance(raw_items, list):
                            items = [it for it in raw_items if isinstance(it, dict) and isinstance(it.get("id"), str)]
                        if isinstance(raw_zh.get("tex"), str) and raw_zh["tex"].strip():
                            tex_hint = raw_zh["tex"].strip()
            except Exception as exc:
                _log(f"Warning: could not parse {zh_file} during app_update_locks: {exc}")

        tp = paper_lock.tex_path_for(pdf_path, tex_hint=tex_hint, base_dir=base_dir)
        ldata = paper_lock.update_locks_for_pdf(
            pdf_path,
            action=action,
            ids=ids,
            mode=mode,
            items=items,
            tex_path=tp,
        )
        structured = {
            "ok": True,
            "file": pdf_path.name,
            "pdf_path": str(pdf_path.resolve()),
            "tex": ldata.get("tex", tp.name),
            "mode": ldata.get("mode", "deny"),
            "locked_ids": ldata.get("locked_ids") or [],
            "locks": ldata.get("locks") or {},
            "locks_mtime": ldata.get("locks_mtime", 0.0),
            "unmatched": ldata.get("unmatched") or [],
            "warnings": ldata.get("warnings") or [],
        }
        return self._tool_ok(
            f"Updated locks ({action}) for {pdf_path.name}: {len(structured['locked_ids'])} locked",
            structured,
        )

    def _tool_app_get_state(self, _args: dict[str, Any]) -> dict[str, Any]:
        st = self.store.load()
        return self._tool_ok("Loaded persisted viewer state.", st)

    def _tool_app_save_state(self, args: dict[str, Any]) -> dict[str, Any]:
        st = self.store.save_partial(
            ui_state=args.get("ui_state"),
            last_pdf=args.get("last_pdf"),
            last_paper_dir=args.get("last_paper_dir"),
            last_zh=args.get("last_zh"),
        )
        return self._tool_ok("Saved viewer state.", {"ok": True, "state": st})

    # ---- JSON-RPC 2.0 Dispatcher ----

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(msg, dict):
            return self._jsonrpc_error(None, -32600, "Invalid Request: message must be an object.")

        method = msg.get("method")
        has_id = "id" in msg
        req_id = msg.get("id")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(method, str):
            if not has_id:
                return None
            return self._jsonrpc_error(req_id, -32600, "Invalid Request: missing string 'method'.")

        # Notifications (no "id") MUST NOT receive a response
        if not has_id:
            return None

        if not isinstance(params, dict):
            return self._jsonrpc_error(req_id, -32602, "Invalid params: expected JSON object.")

        try:
            if method == "initialize":
                proto = params.get("protocolVersion")
                if not isinstance(proto, str) or not proto.strip():
                    proto = DEFAULT_PROTOCOL_VERSION
                return self._jsonrpc_ok(req_id, {
                    "protocolVersion": proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                })

            if method == "ping":
                return self._jsonrpc_ok(req_id, {})

            if method == "tools/list":
                return self._jsonrpc_ok(req_id, {"tools": TOOL_DEFINITIONS})

            if method == "resources/list":
                return self._jsonrpc_ok(req_id, {"resources": [UI_RESOURCE_DESCRIPTOR]})

            if method == "resources/templates/list":
                return self._jsonrpc_ok(req_id, {"resourceTemplates": []})

            if method == "resources/read":
                uri = params.get("uri")
                if uri != APP_RESOURCE_URI:
                    return self._jsonrpc_error(req_id, -32002, f"Resource not found: {uri!r}")
                html = self._read_ui_html()
                return self._jsonrpc_ok(req_id, {
                    "contents": [
                        {
                            "uri": APP_RESOURCE_URI,
                            "mimeType": APP_MIME_TYPE,
                            "text": html,
                            "_meta": {"ui": {"prefersBorder": False}},
                        }
                    ]
                })

            if method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments")
                if tool_args is None:
                    tool_args = {}
                if not isinstance(tool_name, str) or tool_name not in _KNOWN_TOOL_NAMES:
                    return self._jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name!r}")
                if not isinstance(tool_args, dict):
                    return self._jsonrpc_error(req_id, -32602, "Tool 'arguments' must be a JSON object.")

                dispatch = {
                    "open_paper": self._tool_open_paper,
                    "list_paper_locks": self._tool_list_paper_locks,
                    "sync_paper_companion": self._tool_sync_paper_companion,
                    "app_select_paper": self._tool_app_select_paper,
                    "app_get_meta": self._tool_app_get_meta,
                    "app_get_zh": self._tool_app_get_zh,
                    "app_get_page_svg": self._tool_app_get_page_svg,
                    "app_get_page_png": self._tool_app_get_page_png,
                    "app_get_text_layer": self._tool_app_get_text_layer,
                    "app_update_locks": self._tool_app_update_locks,
                    "app_get_state": self._tool_app_get_state,
                    "app_save_state": self._tool_app_save_state,
                }
                try:
                    res = dispatch[tool_name](tool_args)
                except (FileNotFoundError, ValueError, IndexError, KeyError, RuntimeError) as exc:
                    res = self._tool_err(str(exc))
                except Exception as exc:
                    _log(f"Tool '{tool_name}' unexpected error: {type(exc).__name__}: {exc}")
                    res = self._tool_err(f"{type(exc).__name__}: {exc}")
                return self._jsonrpc_ok(req_id, res)

            return self._jsonrpc_error(req_id, -32601, f"Method not found: {method!r}")
        except Exception as exc:
            _log(f"RPC '{method}' internal error: {type(exc).__name__}: {exc}")
            return self._jsonrpc_error(req_id, -32603, f"Internal error: {exc}")

    @staticmethod
    def _jsonrpc_ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve_stdio(self) -> int:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                resp = self._jsonrpc_error(None, -32700, f"Parse error: {exc}")
                sys.stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
                continue

            resp = self.handle_message(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chengmen Paper Viewer MCP App stdio server (SEP-1865)")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Persistent plugin data directory (defaults to $PLUGIN_DATA or ~/.chengmen_paper_viewer).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(argv)
    if ns.data_dir and ns.data_dir.strip():
        data_dir = Path(ns.data_dir).expanduser()
    elif os.environ.get("PLUGIN_DATA", "").strip():
        data_dir = Path(os.environ["PLUGIN_DATA"]).expanduser()
    else:
        data_dir = Path.home() / ".chengmen_paper_viewer"
    server = ChengmenMcpServer(data_dir=data_dir)
    return server.serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
