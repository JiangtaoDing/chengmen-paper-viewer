"""Paper Paragraph/Section Lock Engine & Antigravity PreToolUse Hook Guard.

Shared by:
1. `paper_viewer.py` (HTTP API `/api/locks` for 1-click lock/unlock in UI)
2. AI agent global `PreToolUse` hook (`python "/abs/path/to/paper_lock.py" --hook`)

Lock state is persisted alongside the manuscript as `<stem>.locks.json` (e.g.
`paper/main.locks.json`). Only the viewer UI (or explicit user CLI action)
modifies `<stem>.locks.json`; the PreToolUse hook hard-blocks agent edits to both
locked `.tex` spans and any `*.locks.json` file.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# =====================================================================
# Path & JSON Helpers
# =====================================================================

def tex_path_for(path: Path) -> Path:
    p = Path(path).resolve()
    if p.suffix.lower() == ".tex":
        return p
    return p.with_suffix(".tex")


def lock_path_for(path: Path) -> Path:
    p = Path(path).resolve()
    if p.name.endswith(".locks.json"):
        return p
    return p.with_suffix(".locks.json")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def load_locks(path: Path) -> dict:
    lp = lock_path_for(path)
    tp = tex_path_for(path)
    default = {
        "version": 1,
        "tex": tp.name,
        "pdf": tp.with_suffix(".pdf").name,
        "mode": "deny",  # "deny" (hard block) or "force_ask" (strict user popup)
        "updated_at": "",
        "locks": {},
    }
    if not lp.exists():
        return default
    try:
        raw = json.loads(lp.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default
        out = dict(default)
        out.update(raw)
        if out.get("mode") not in ("deny", "force_ask"):
            out["mode"] = "deny"
        if not isinstance(out.get("locks"), dict):
            out["locks"] = {}
        return out
    except Exception:
        return default


def save_locks(path: Path, data: dict) -> Path:
    lp = lock_path_for(path)
    data = dict(data)
    data["version"] = 1
    data["updated_at"] = _now_str()
    if data.get("mode") not in ("deny", "force_ask"):
        data["mode"] = "deny"
    tmp = lp.with_suffix(".locks.json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(lp)
    return lp


# =====================================================================
# LaTeX Normalization & Block Locator
# =====================================================================

def _norm(text: str) -> str:
    s = text.lower()
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"-\s+", "", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _extract_zero_arg_macros(tex_text: str) -> dict[str, str]:
    macros: dict[str, str] = {}
    preamble = tex_text.split(r"\begin{document}", 1)[0]
    for m in re.finditer(r"\\newcommand\{\\([a-zA-Z]+)\}\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", preamble):
        name, val = m.group(1), m.group(2)
        # strip simple formatting wrappers inside macro value
        val = re.sub(r"\\(?:textsc|textbf|emph|texttt|mathrm|text)\{([^{}]*)\}", r"\1", val)
        val = re.sub(r"\\[a-zA-Z]+\s*", "", val).strip()
        if val:
            macros[name] = val
    return macros


def _strip_latex(raw: str, macros: dict[str, str]) -> str:
    s = raw
    # Remove comments (unescaped %)
    s = re.sub(r"(?m)(?<!\\)%.*$", "", s)
    # Expand 0-arg custom macros
    for name, val in macros.items():
        s = re.sub(rf"\\{re.escape(name)}(?:\{{\}}|\b)", f" {val} ", s)
    # Remove label/ref/cite/includegraphics
    s = re.sub(r"\\(?:label|includegraphics|setlength|renewcommand|centering|footnotesize|small|scriptsize|tiny|toprule|midrule|bottomrule|rowcolor|columncolor|cmidrule)(?:\[[^\]]*\])?(?:\{[^{}]*\})*", " ", s)
    s = re.sub(r"\\(?:cite|citep|citet|citealp|cref|Cref|ref|eqref)\*?(?:\[[^\]]*\])?\{[^{}]*\}", " ", s)
    # Unwrap common formatting commands (iterate for nesting)
    for _ in range(3):
        s = re.sub(
            r"\\(?:section|subsection|subsubsection|paragraph|caption|textbf|textit|emph|texttt|textsc|mathrm|text|icmltitle|NUM|refv|subv|best|second|dwin|dpos|dsecpos|dmid|dmidw|dsecmid|dzero|dzerow|dseczero|color|begin|end)\*?(?:\[[^\]]*\])?\{([^{}]*)\}",
            r" \1 ",
            s,
        )
    s = s.replace("~", " ").replace(r"\,", " ").replace(r"\%", "%").replace(r"\_", "_").replace(r"\&", "&")
    s = re.sub(r"\\[a-zA-Z]+\*?", " ", s)
    s = re.sub(r"[{}]", " ", s)
    return _norm(s)


def _extract_caption_text(env_raw: str, macros: dict[str, str]) -> str:
    idx = env_raw.find(r"\caption")
    if idx < 0:
        return _strip_latex(env_raw, macros)
    brace = env_raw.find("{", idx)
    if brace < 0:
        return _strip_latex(env_raw[idx:], macros)
    depth = 0
    end = brace
    for i in range(brace, len(env_raw)):
        ch = env_raw[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return _strip_latex(env_raw[brace:end], macros)


def _parse_tex_structure(tex_text: str) -> dict:
    """Parse LaTeX file into floats (figures/tables) and non-float prose blocks."""
    macros = _extract_zero_arg_macros(tex_text)
    lines = tex_text.splitlines(keepends=True)
    n_lines = len(lines)

    doc_start = 0
    for i, ln in enumerate(lines):
        if r"\begin{document}" in ln:
            doc_start = i + 1
            break

    floats: list[dict] = []  # {kind, label_num, start_line, end_line, raw, norm_cap}
    float_line_mask = [False] * n_lines

    in_appendix = False
    sec_num = 0
    subsec_num = 0
    app_sec_idx = 0
    fig_main_n = 0
    tab_main_n = 0
    fig_app_n = 0
    tab_app_n = 0

    i = doc_start
    while i < n_lines:
        ln = lines[i]
        stripped = ln.strip()
        if stripped.startswith("%"):
            i += 1
            continue
        if r"\appendix" in stripped:
            in_appendix = True
            app_sec_idx = 0
            fig_app_n = 0
            tab_app_n = 0

        m_sec = re.match(r"\\section(\*?)\{(.+)\}", stripped)
        if m_sec and not m_sec.group(1):
            if in_appendix:
                app_sec_idx += 1
                fig_app_n = 0
                tab_app_n = 0
            else:
                sec_num += 1
                subsec_num = 0

        m_env = re.match(r"\\begin\{(figure\*?|table\*?)\}", stripped)
        if m_env:
            env_name = m_env.group(1)
            end_tag = rf"\end{{{env_name}}}"
            start_i = i
            end_i = i
            while end_i < n_lines and end_tag not in lines[end_i]:
                end_i += 1
            if end_i >= n_lines:
                end_i = n_lines - 1
            for k in range(start_i, end_i + 1):
                float_line_mask[k] = True
            raw = "".join(lines[start_i:end_i + 1])
            kind = "figure" if env_name.startswith("figure") else "table"
            if kind == "figure":
                if in_appendix and app_sec_idx > 0:
                    fig_app_n += 1
                    lbl = f"{chr(64 + app_sec_idx)}.{fig_app_n}"
                else:
                    fig_main_n += 1
                    lbl = str(fig_main_n)
            else:
                if in_appendix and app_sec_idx > 0:
                    tab_app_n += 1
                    lbl = f"{chr(64 + app_sec_idx)}.{tab_app_n}"
                else:
                    tab_main_n += 1
                    lbl = str(tab_main_n)
            floats.append({
                "kind": kind,
                "label_num": lbl,
                "start_line": start_i + 1,
                "end_line": end_i + 1,
                "raw": raw,
                "norm_cap": _extract_caption_text(raw, macros),
            })
            i = end_i + 1
            continue
        i += 1

    # Now parse non-float prose blocks with section numbering prefixes
    blocks: list[dict] = []
    in_appendix = False
    sec_num = 0
    subsec_num = 0
    app_sec_idx = 0
    cur_start: int | None = None
    cur_lines: list[str] = []
    cur_prefix: str = ""

    def flush_block(end_idx_0: int):
        nonlocal cur_start, cur_lines, cur_prefix
        if cur_start is None or not cur_lines:
            cur_start = None
            cur_lines = []
            cur_prefix = ""
            return
        # Trim leading/trailing pure comment or empty lines
        s_off = 0
        while s_off < len(cur_lines) and (not cur_lines[s_off].strip() or cur_lines[s_off].strip().startswith("%")):
            s_off += 1
        e_off = len(cur_lines) - 1
        while e_off >= s_off and (not cur_lines[e_off].strip() or cur_lines[e_off].strip().startswith("%")):
            e_off -= 1
        if s_off <= e_off:
            sl = cur_start + s_off + 1
            el = cur_start + e_off + 1
            raw = "".join(cur_lines[s_off:e_off + 1])
            norm = _strip_latex(raw, macros)
            if cur_prefix:
                norm = f"{cur_prefix} {norm}".strip()
            blocks.append({
                "start_line": sl,
                "end_line": el,
                "raw": raw,
                "norm": norm,
            })
        cur_start = None
        cur_lines = []
        cur_prefix = ""

    boundary_re = re.compile(
        r"^\\(?:section|subsection|subsubsection|paragraph|begin\{abstract\}|end\{abstract\}|icmltitle|bibliography|end\{document\})"
    )

    for i in range(doc_start, n_lines):
        ln = lines[i]
        stripped = ln.strip()
        if float_line_mask[i]:
            flush_block(i - 1)
            continue
        if stripped.startswith(r"\end{document}"):
            flush_block(i - 1)
            break
        if stripped.startswith((r"\bibliography", r"\bibliographystyle", r"\newpage", r"\clearpage", r"\onecolumn", r"\twocolumn")):
            flush_block(i - 1)
            continue
        if not stripped:
            flush_block(i - 1)
            continue
        if stripped.startswith("%"):
            if cur_start is not None:
                cur_lines.append(ln)
            continue
        if r"\appendix" in stripped:
            flush_block(i - 1)
            in_appendix = True
            app_sec_idx = 0
            continue

        # Start new block on section / subsection / paragraph / abstract / title
        if boundary_re.match(stripped):
            flush_block(i - 1)
            m_sec = re.match(r"\\section(\*?)\{", stripped)
            m_sub = re.match(r"\\subsection(\*?)\{", stripped)
            if m_sec and not m_sec.group(1):
                if in_appendix:
                    app_sec_idx += 1
                    cur_prefix = chr(96 + app_sec_idx)
                else:
                    sec_num += 1
                    subsec_num = 0
                    cur_prefix = str(sec_num)
            elif m_sub and not m_sub.group(1):
                subsec_num += 1
                if in_appendix and app_sec_idx > 0:
                    cur_prefix = f"{chr(96 + app_sec_idx)} {subsec_num}"
                else:
                    cur_prefix = f"{sec_num} {subsec_num}"
            cur_start = i
            cur_lines = [ln]
            # Keep \section/\subsection glued to its immediately following text if no blank line
            continue

        if cur_start is None:
            cur_start = i
            cur_lines = [ln]
        else:
            cur_lines.append(ln)

    flush_block(n_lines - 1)
    return {"lines": lines, "floats": floats, "blocks": blocks, "macros": macros, "float_line_mask": float_line_mask}


def _word_overlap_score(query_norm: str, cand_norm: str) -> float:
    if not query_norm or not cand_norm:
        return 0.0
    if cand_norm.startswith(query_norm):
        return 100.0 + len(query_norm)
    if query_norm in cand_norm:
        pos = cand_norm.find(query_norm)
        return 60.0 + len(query_norm) - min(30.0, pos * 0.15)
    q_words = [w for w in query_norm.split() if len(w) >= 2]
    if not q_words:
        return 0.0
    c_set = set(cand_norm.split())
    hits = sum(1 for w in q_words if w in c_set)
    return 40.0 * (hits / len(q_words))


def locate_tex_spans(tex_text: str, items: list[dict]) -> dict[str, dict]:
    """Map every companion-JSON item id to its exact 1-based [start_line, end_line] in `.tex`."""
    parsed = _parse_tex_structure(tex_text)
    lines = parsed["lines"]
    floats = parsed["floats"]
    blocks = parsed["blocks"]
    float_line_mask = parsed["float_line_mask"]

    spans: dict[str, dict] = {}
    used_floats: set[int] = set()

    # 1. Match figures and tables first
    for it in items:
        cid = it["id"]
        kind = it.get("kind")
        if kind not in ("figure", "table"):
            continue
        anc = (it.get("anchor") or "").strip()
        prev = _norm(it.get("en_preview") or "")
        m_num = re.search(r"(?:Figure|Fig\.|Table|Tab\.)\s+([A-Z]\.\d+|\d+)", anc, re.I)
        want_lbl = m_num.group(1) if m_num else None

        best_fi = None
        best_sc = -1.0
        for fi, fl in enumerate(floats):
            if fi in used_floats or fl["kind"] != kind:
                continue
            sc = 0.0
            if want_lbl and fl["label_num"].lower() == want_lbl.lower():
                sc += 120.0
            if prev:
                sc += _word_overlap_score(prev[:120], fl["norm_cap"])
            if sc > best_sc:
                best_sc = sc
                best_fi = fi
        if best_fi is not None and best_sc > 10.0:
            used_floats.add(best_fi)
            fl = floats[best_fi]
            spans[cid] = {
                "start_line": fl["start_line"],
                "end_line": fl["end_line"],
                "tex_snippet": fl["raw"].strip("\r\n"),
            }

    # 2. Match prose items in order
    prose_items = [it for it in items if it.get("kind") not in ("figure", "table")]
    matched_starts: list[tuple[str, int]] = []  # (id, block_idx)
    used_blocks: set[int] = set()

    for it in prose_items:
        cid = it["id"]
        anc_raw = (it.get("anchor") or "").strip()
        anc_norm = _norm(anc_raw)
        prev_norm = _norm(it.get("en_preview") or "")

        best_bi = None
        best_sc = -1.0

        if cid == "Title":
            for bi, b in enumerate(blocks):
                if r"\icmltitle" in b["raw"]:
                    best_bi, best_sc = bi, 200.0
                    break
        elif cid == "Abstract" or anc_norm == "abstract":
            for bi, b in enumerate(blocks):
                if r"\begin{abstract}" in b["raw"]:
                    best_bi, best_sc = bi, 200.0
                    break
        else:
            for bi, b in enumerate(blocks):
                if bi in used_blocks:
                    continue
                if r"\icmltitle" in b["raw"] or r"\begin{abstract}" in b["raw"]:
                    continue
                sc = 0.0
                if anc_norm:
                    sc = max(sc, _word_overlap_score(anc_norm, b["norm"]))
                    # Also try stripping leading section number from anchor (e.g. "2.2. why..." -> "why...")
                    anc_no_num = re.sub(r"^(?:[a-z]|\d+(?:\s+\d+)*)\s+", "", anc_norm)
                    if anc_no_num and anc_no_num != anc_norm:
                        sc = max(sc, _word_overlap_score(anc_no_num, b["norm"]) * 0.96)
                if prev_norm:
                    sc += 0.35 * _word_overlap_score(prev_norm[:120], b["norm"])
                if sc > best_sc:
                    best_sc = sc
                    best_bi = bi

        if best_bi is not None and best_sc >= 22.0:
            used_blocks.add(best_bi)
            matched_starts.append((cid, best_bi))

    # Sort matched prose blocks by document position and compute ranges up to next prose start
    matched_starts.sort(key=lambda x: x[1])
    for idx, (cid, bi) in enumerate(matched_starts):
        next_bi = matched_starts[idx + 1][1] if idx + 1 < len(matched_starts) else len(blocks)
        if cid == "Title":
            end_bi = bi
        elif cid == "Abstract":
            # Include all blocks until \end{abstract}
            end_bi = bi
            while end_bi + 1 < next_bi and r"\end{abstract}" not in blocks[end_bi]["raw"]:
                end_bi += 1
        else:
            end_bi = max(bi, next_bi - 1)

        sl = blocks[bi]["start_line"]
        el = blocks[end_bi]["end_line"]

        # Split around any float environments embedded between prose lines so locking prose
        # never accidentally locks an adjacent float (and vice versa).
        chunks: list[str] = []
        cur_c: list[str] = []
        for l_idx in range(sl - 1, el):
            if float_line_mask[l_idx]:
                if cur_c:
                    txt_c = "".join(cur_c).strip("\r\n")
                    if txt_c.strip():
                        chunks.append(txt_c)
                    cur_c = []
            else:
                cur_c.append(lines[l_idx])
        if cur_c:
            txt_c = "".join(cur_c).strip("\r\n")
            if txt_c.strip():
                chunks.append(txt_c)

        spans[cid] = {
            "start_line": sl,
            "end_line": el,
            "tex_snippet": chunks[0] if len(chunks) == 1 else "\n".join(chunks),
            "tex_chunks": chunks,
        }

    return spans


def section_group_key(item: dict) -> str:
    """Group items into clean section buckets for 1-click whole-section locking."""
    cid = str(item.get("id") or "")
    sec = str(item.get("section") or "")
    sec_clean = re.sub(r"^[🔒\s]+", "", sec)
    if cid in ("Title", "Abstract"):
        return "标题与摘要"
    m = re.search(r"§\s*(\d+)", sec_clean)
    if m:
        return f"§{m.group(1)}"
    if cid.startswith("Fig1") or cid == "TabPF":
        return "§2"
    if cid in ("Fig2", "FigDepthProbe"):
        return "§3"
    if cid in ("Fig3", "Fig4", "FigSpanU") or cid.startswith(("Tab2", "Tab3", "Tab4", "Tab5", "Tab6", "Tab7", "Tab8", "Tab9", "Tab10", "Tab11", "Tab12", "Tab13", "TabMain", "TabMC", "TabCF", "TabAbl", "TabOBQA", "TabXRecv", "TabSpan", "TabMulti", "TabFail", "TabGate", "TabCost")):
        return "§4"
    if "附录" in sec_clean or cid.startswith(("App", "TabA", "TabC", "FigA", "FigC")):
        m_a = re.search(r"附录\s*([A-Z])", sec_clean)
        return f"附录 {m_a.group(1)}" if m_a else "附录"
    if "AI" in cid or "Impact" in cid or "声明" in sec_clean:
        return "声明与影响"
    # Fallback: digit prefix in id like 2.1-P1 -> §2
    m_id = re.match(r"^(\d+)\.", cid)
    if m_id:
        return f"§{m_id.group(1)}"
    return sec_clean.split("（")[0].strip() or "其他"


def update_locks_for_pdf(
    pdf_path: Path,
    items: list[dict],
    action: str,
    ids: list[str] | None = None,
    mode: str | None = None,
) -> dict:
    """Apply lock/unlock/toggle/set_mode action and persist `<stem>.locks.json`."""
    tp = tex_path_for(pdf_path)
    data = load_locks(pdf_path)
    data["tex"] = tp.name
    data["pdf"] = pdf_path.name

    if action == "set_mode" and mode in ("deny", "force_ask"):
        data["mode"] = mode
        save_locks(pdf_path, data)
        return data

    item_by_id = {it["id"]: it for it in items}
    tex_text = tp.read_text(encoding="utf-8") if tp.exists() else ""
    spans = locate_tex_spans(tex_text, items) if tex_text else {}

    target_ids = [cid for cid in (ids or []) if cid in item_by_id]
    locks = data["locks"]

    for cid in target_ids:
        is_locked = cid in locks
        do_lock = (not is_locked) if action == "toggle" else (action == "lock")
        if do_lock:
            it = item_by_id[cid]
            sp = spans.get(cid)
            snippet = sp["tex_snippet"] if sp else ""
            chunks = sp.get("tex_chunks") if sp else ([snippet] if snippet else [])
            locks[cid] = {
                "id": cid,
                "section": re.sub(r"^[🔒\s]+", "", str(it.get("section") or "")).replace("（已锁定）", "").replace("·已锁定", "").strip(),
                "title": str(it.get("title") or ""),
                "kind": str(it.get("kind") or "prose"),
                "anchor": str(it.get("anchor") or ""),
                "tex_start_line": sp["start_line"] if sp else None,
                "tex_end_line": sp["end_line"] if sp else None,
                "tex_snippet": snippet,
                "tex_chunks": chunks,
                "tex_sha256": _sha256(snippet) if snippet else "",
                "locked_at": _now_str(),
            }
        else:
            locks.pop(cid, None)

    save_locks(pdf_path, data)
    return data


# =====================================================================
# Antigravity PreToolUse Hook Guard
# =====================================================================

def _find_lock_char_spans(src: str, lock_entry: dict) -> list[tuple[int, int]]:
    """Locate a locked entry's character span(s) in current `.tex` text."""
    chunks = lock_entry.get("tex_chunks") or []
    if not chunks and lock_entry.get("tex_snippet"):
        chunks = [lock_entry["tex_snippet"]]

    found: list[tuple[int, int]] = []
    for ch in chunks:
        if not ch:
            continue
        pos = src.find(ch)
        if pos != -1:
            found.append((pos, pos + len(ch)))
            continue
        slines = [ln.strip() for ln in ch.splitlines() if ln.strip() and not ln.strip().startswith("%")]
        if slines:
            p0 = src.find(slines[0])
            if p0 != -1:
                p1 = src.find(slines[-1], p0)
                if p1 != -1:
                    found.append((p0, p1 + len(slines[-1])))
    if found:
        return found

    # Fallback to stored 1-based line numbers
    sl = lock_entry.get("tex_start_line")
    el = lock_entry.get("tex_end_line")
    if isinstance(sl, int) and isinstance(el, int) and sl >= 1 and el >= sl:
        lines = src.splitlines(keepends=True)
        c0 = sum(len(x) for x in lines[:sl - 1])
        c1 = sum(len(x) for x in lines[:min(el, len(lines))])
        if c1 > c0:
            return [(c0, c1)]
    return []


def _format_block_reason(hits: list[dict], mode: str, tex_name: str) -> str:
    labels = ", ".join(f"{h['id']} ({h.get('title') or h.get('section') or 'locked'})" for h in hits[:5])
    if len(hits) > 5:
        labels += f" 等共 {len(hits)} 处"
    if mode == "force_ask":
        return (
            f"[PAPER-LOCK 确认] 该修改涉及 {tex_name} 中已锁定的区域：{labels}。"
            f"请确认是否允许修改，或在论文预览器中解锁后再改。"
        )
    return (
        f"[PAPER-LOCK 硬锁拦截] 禁止修改 {tex_name} 中已锁定的区域：{labels}。"
        f"请先提示用户在论文预览器 (http://127.0.0.1:8788/) 中点击「解锁」后再修改。"
    )


def inspect_pre_tool_use(data: dict) -> dict:
    tc = data.get("toolCall") or {}
    tname = str(tc.get("name") or "").lower()
    targs = tc.get("args") or {}

    # 1. Direct file edit / write tools
    if tname in ("edit_file", "write_file"):
        fpath_str = str(targs.get("Path") or targs.get("path") or "")
        if not fpath_str:
            return {"decision": "allow"}
        fpath = Path(fpath_str)

        # Never let the agent edit/overwrite a `.locks.json` file directly!
        if fpath.name.lower().endswith(".locks.json"):
            return {
                "decision": "deny",
                "reason": (
                    f"[PAPER-LOCK 保护] 锁状态文件 {fpath.name} 仅允许用户在论文预览器 UI 中操作，"
                    f"Agent 禁止直接修改或解锁。"
                ),
            }

        if fpath.suffix.lower() != ".tex":
            return {"decision": "allow"}

        lp = lock_path_for(fpath)
        if not lp.exists():
            return {"decision": "allow"}

        ldata = load_locks(fpath)
        locks = ldata.get("locks") or {}
        if not locks:
            return {"decision": "allow"}

        mode = ldata.get("mode", "deny")
        decision = "force_ask" if mode == "force_ask" else "deny"

        if tname == "edit_file":
            target = str(targs.get("Target") or targs.get("target") or "")
            replacement = str(targs.get("Replacement") or targs.get("replacement") or "")
            if not target or not fpath.exists():
                return {"decision": "allow"}
            src = fpath.read_text(encoding="utf-8", errors="replace")

            # Find every occurrence of Target in src
            t_spans: list[tuple[int, int]] = []
            start = 0
            while True:
                idx = src.find(target, start)
                if idx == -1:
                    break
                t_spans.append((idx, idx + len(target)))
                start = idx + len(target)

            hits: list[dict] = []
            for entry in locks.values():
                l_spans = _find_lock_char_spans(src, entry)
                touched = False
                for tc0, tc1 in t_spans:
                    for lc0, lc1 in l_spans:
                        if tc0 < lc1 and lc0 < tc1:
                            # Allow only if the locked snippet is completely unchanged (strictly preserved)
                            snip = entry.get("tex_snippet") or ""
                            if snip and snip in target and snip in replacement:
                                continue
                            touched = True
                            break
                    if touched:
                        break
                if touched:
                    hits.append(entry)

            if hits:
                return {"decision": decision, "reason": _format_block_reason(hits, mode, fpath.name)}
            return {"decision": "allow"}

        if tname == "write_file":
            if targs.get("Append") or targs.get("append"):
                return {"decision": "allow"}
            content = str(targs.get("Content") or targs.get("content") or "")
            hits = []
            for entry in locks.values():
                chunks = entry.get("tex_chunks") or ([entry["tex_snippet"]] if entry.get("tex_snippet") else [])
                if any(ch and ch not in content for ch in chunks):
                    hits.append(entry)
            if hits:
                return {"decision": decision, "reason": _format_block_reason(hits, mode, fpath.name)}
            return {"decision": "allow"}

    # 2. Guard against shell commands overwriting `.locks.json` or locked `.tex` files
    if tname == "run_command":
        cmd = str(targs.get("CommandLine") or targs.get("commandLine") or "")
        if not cmd:
            return {"decision": "allow"}
        cmd_low = cmd.lower()

        write_cues = (
            "write_text", "write_bytes", "set-content", "add-content", "out-file",
            "remove-item", "del ", "rm ", "move-item", "copy-item", "replace(",
            "sed -i", "open(", ".locks.json >", "> ", ">> ",
        )
        # Protect any `.locks.json` from shell mutation
        if ".locks.json" in cmd_low and any(c in cmd_low for c in write_cues):
            return {
                "decision": "deny",
                "reason": "[PAPER-LOCK 保护] 禁止通过命令行篡改 .locks.json 锁文件，请由用户在预览器 UI 中操作。",
            }

        # Check workspace for any active `*.locks.json`
        ws_roots: list[Path] = []
        for wp in data.get("workspacePaths") or []:
            if wp:
                ws_roots.append(Path(wp))
        if data.get("cwd"):
            ws_roots.append(Path(data["cwd"]))

        seen_locks: set[Path] = set()
        for root in ws_roots:
            if not root.exists():
                continue
            for cand in list(root.glob("*.locks.json")) + list(root.glob("paper/*.locks.json")):
                rc = cand.resolve()
                if rc in seen_locks:
                    continue
                seen_locks.add(rc)
                ldata = load_locks(rc)
                if not ldata.get("locks"):
                    continue
                tex_name = str(ldata.get("tex") or rc.with_suffix(".tex").name).lower()
                if tex_name not in cmd_low:
                    continue
                # Safe LaTeX compile/read invocations are always allowed
                safe_only = (
                    ("pdflatex" in cmd_low or "xelatex" in cmd_low or "lualatex" in cmd_low or "latexmk" in cmd_low or "select-string" in cmd_low)
                    and not any(c in cmd_low for c in ("write_text", "set-content", "out-file", "sed -i", "remove-item", "del ", "rm "))
                )
                if safe_only:
                    continue
                if any(c in cmd_low for c in write_cues):
                    mode = ldata.get("mode", "deny")
                    decision = "force_ask" if mode == "force_ask" else "deny"
                    return {
                        "decision": decision,
                        "reason": (
                            f"[PAPER-LOCK] 检测到命令试图写入已含锁定区域的 {ldata.get('tex')} "
                            f"（共 {len(ldata['locks'])} 处已锁）。请改用 edit_file 编辑未锁定区域，或在预览器中先解锁。"
                        ),
                    }

    return {"decision": "allow"}


def main() -> None:
    if "--hook" in sys.argv:
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw.strip() else {}
            ev = str(data.get("hookEventName") or "")
            if ev == "PreToolUse":
                res = inspect_pre_tool_use(data)
            else:
                res = {}
            sys.stdout.write(json.dumps(res, ensure_ascii=False))
            sys.stdout.flush()
        except Exception:
            sys.stdout.write(json.dumps({"decision": "allow"}))
            sys.stdout.flush()
        return

    print("paper_lock.py: use via paper_viewer.py or --hook (Antigravity PreToolUse).")


if __name__ == "__main__":
    main()
