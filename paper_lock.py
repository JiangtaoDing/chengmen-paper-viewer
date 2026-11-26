"""Paper Paragraph/Section Lock Engine & PreToolUse Hook Guard.

Shared by:
1. `paper_viewer.py` (HTTP API `/api/locks` for 1-click lock/unlock in UI)
2. AI agent `PreToolUse` hooks (`python "/abs/path/to/paper_lock.py" --hook`),
   supporting both Antigravity (`hooks.json`) and Claude Code (`settings.json`).
3. CLI inspection (`python paper_lock.py status <pdf_or_tex>`).

Lock state is persisted as `<dir>/.paper_viewer/<stem>.locks.json` (when
`.paper_viewer/` exists) or `<dir>/<stem>.locks.json` (backward-compatible).
The PreToolUse hook blocks agent edits to both locked `.tex` spans and any
`*.locks.json` file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VALID_ACTIONS = frozenset({"lock", "unlock", "toggle", "set_mode"})
VALID_MODES = frozenset({"deny", "force_ask"})
_SKIP_WALK_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", ".tox",
})
_LOCKS_SUFFIX = ".locks.json"
_LOCK_IO_MUTEX = threading.RLock()


# =====================================================================
# Path & JSON Helpers
# =====================================================================

def _is_locks_filename(name: str) -> bool:
    return name.lower().endswith(_LOCKS_SUFFIX)


def _strip_locks_stem(p: Path) -> tuple[Path, str]:
    """Return (paper_dir, stem) for a `.locks.json`, `.pdf`, or `.tex` path."""
    if _is_locks_filename(p.name):
        stem = p.name[:-len(_LOCKS_SUFFIX)]
        parent = p.parent.parent if p.parent.name == ".paper_viewer" else p.parent
        return parent, stem
    return p.parent, p.stem


def tex_path_for(
    path: Path | str,
    *,
    tex_hint: str | Path | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Resolve the `.tex` source path corresponding to a PDF, TeX, or `.locks.json` path."""
    p = Path(path).resolve()
    paper_dir, stem = _strip_locks_stem(p)

    if tex_hint is not None and str(tex_hint).strip():
        hint_p = Path(str(tex_hint).strip())
        candidates: list[Path] = []
        if hint_p.is_absolute():
            candidates.append(hint_p.resolve())
        else:
            if base_dir is not None:
                candidates.append((Path(base_dir).resolve() / hint_p).resolve())
            candidates.append((paper_dir / hint_p).resolve())
            if p.parent != paper_dir:
                candidates.append((p.parent / hint_p).resolve())
        for cand in candidates:
            if cand.is_file():
                return cand

    if p.suffix.lower() == ".tex" and not _is_locks_filename(p.name):
        return p
    return paper_dir / f"{stem}.tex"


def lock_path_for(
    path: Path | str,
    *,
    lock_dir: Path | str | None = None,
) -> Path:
    """Resolve the `<stem>.locks.json` path.

    - Recognizes existing `.locks.json` suffixes case-insensitively.
    - If `lock_dir` is provided, places `<stem>.locks.json` inside `lock_dir`.
    - Otherwise prefers `<parent>/.paper_viewer/<stem>.locks.json` when that file
      or `.paper_viewer/` directory exists, falling back to `<parent>/<stem>.locks.json`.
    """
    p = Path(path).resolve()
    if _is_locks_filename(p.name):
        if lock_dir is None:
            return p
        stem = p.name[:-len(_LOCKS_SUFFIX)]
        return Path(lock_dir).resolve() / f"{stem}{_LOCKS_SUFFIX}"

    stem = p.stem
    if lock_dir is not None:
        return Path(lock_dir).resolve() / f"{stem}{_LOCKS_SUFFIX}"

    pv_dir = p.parent / ".paper_viewer"
    pv_lock = pv_dir / f"{stem}{_LOCKS_SUFFIX}"
    if pv_lock.exists() or pv_dir.is_dir():
        return pv_lock
    return p.with_suffix(_LOCKS_SUFFIX)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _default_locks_payload(tp: Path) -> dict:
    return {
        "version": 1,
        "tex": tp.name,
        "tex_file": tp.name,
        "pdf": tp.with_suffix(".pdf").name,
        "mode": "deny",  # "deny" (hard block) or "force_ask" (strict user popup)
        "updated_at": "",
        "locks": {},
        "locked": [],
        "locked_ids": [],
    }


def _enrich_lock_dict(data: dict, lp: Path) -> dict:
    out = dict(data)
    locks = out.get("locks") if isinstance(out.get("locks"), dict) else {}
    out["locks"] = locks
    sorted_ids = sorted(locks.keys())
    out["locked"] = sorted_ids
    out["locked_ids"] = sorted_ids
    tex_name = str(out.get("tex") or out.get("tex_file") or tex_path_for(lp).name)
    out["tex"] = tex_name
    out["tex_file"] = tex_name
    out["locks_mtime"] = lp.stat().st_mtime if lp.exists() else 0.0
    return out


def load_locks(
    path: Path | str,
    *,
    strict: bool = False,
    lock_dir: Path | str | None = None,
) -> dict:
    """Load lock state from `<stem>.locks.json` (UTF-8 with optional BOM).

    When `strict=True` (used by the PreToolUse hook), corrupted or non-object
    lock files raise `ValueError` so callers can fail closed.
    """
    lp = lock_path_for(path, lock_dir=lock_dir)
    tp = tex_path_for(path)
    default = _default_locks_payload(tp)

    with _LOCK_IO_MUTEX:
        if not lp.exists():
            return default
        try:
            text = lp.read_text(encoding="utf-8-sig")
            raw = json.loads(text)
        except Exception as exc:
            if strict:
                raise ValueError(f"Corrupted lock file {lp.name}: {exc}") from exc
            return default

        if not isinstance(raw, dict):
            if strict:
                raise ValueError(f"Lock file {lp.name} must contain a JSON object")
            return default

        raw_locks = raw.get("locks")
        if raw_locks is not None and not isinstance(raw_locks, dict):
            if strict:
                raise ValueError(f"Lock file {lp.name} has invalid 'locks' field")
            raw_locks = {}

        out = dict(default)
        out.update(raw)
        if out.get("mode") not in VALID_MODES:
            out["mode"] = "deny"
        out["locks"] = dict(raw_locks) if isinstance(raw_locks, dict) else {}
        return _enrich_lock_dict(out, lp)


def save_locks(
    path: Path | str,
    data: dict,
    *,
    lock_dir: Path | str | None = None,
) -> Path:
    """Atomically write `<stem>.locks.json` using a unique temp file + fsync + replace."""
    lp = lock_path_for(path, lock_dir=lock_dir)
    tp = tex_path_for(path)

    payload: dict = {
        "version": 1,
        "tex": str(data.get("tex") or data.get("tex_file") or tp.name),
        "pdf": str(data.get("pdf") or tp.with_suffix(".pdf").name),
        "mode": data.get("mode") if data.get("mode") in VALID_MODES else "deny",
        "updated_at": _now_str(),
        "locks": data.get("locks") if isinstance(data.get("locks"), dict) else {},
    }
    if data.get("tex_path"):
        payload["tex_path"] = str(data["tex_path"])

    encoded = json.dumps(payload, ensure_ascii=False, indent=2)

    with _LOCK_IO_MUTEX:
        lp.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=lp.parent,
                prefix=f".{lp.name}.",
                suffix=".tmp",
                delete=False,
            ) as tf:
                tmp_path = Path(tf.name)
                tf.write(encoded)
                tf.flush()
                os.fsync(tf.fileno())

            last_err: OSError | None = None
            for attempt in range(10):
                try:
                    os.replace(tmp_path, lp)
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
    return lp


def get_locks_for_pdf(
    pdf_path: Path | str,
    *,
    lock_dir: Path | str | None = None,
) -> dict:
    """Thread-safe reader for lock state associated with `pdf_path`."""
    with _LOCK_IO_MUTEX:
        lp = lock_path_for(pdf_path, lock_dir=lock_dir)
        data = load_locks(pdf_path, strict=False, lock_dir=lock_dir)
        data["lock_path"] = str(lp)
        return data


# =====================================================================
# LaTeX Comment, Normalization & Block Locator
# =====================================================================

def _strip_tex_comment_line(line: str) -> str:
    """Strip an unescaped `%` comment from a single LaTeX line.

    A `%` preceded by an even number of backslashes (0, 2, 4, ...) starts a
    comment (e.g. `foo \\% comment`); an odd number (`\\%`? No: `\\%` has 2 ->
    comment, `\\%` vs `\\%`? 1 backslash `\\%` is escaped percent `\%` -> literal).
    """
    n = len(line)
    i = 0
    while i < n:
        if line[i] == "%":
            bs = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                return line[:i]
        i += 1
    return line


def _strip_tex_comments(text: str) -> str:
    lines = _normalize_newlines(text).split("\n")
    return "\n".join(_strip_tex_comment_line(ln) for ln in lines)


def _is_escaped(text: str, pos: int) -> bool:
    bs = 0
    j = pos - 1
    while j >= 0 and text[j] == "\\":
        bs += 1
        j -= 1
    return (bs % 2) == 1


def _find_matching_delim(text: str, open_pos: int, open_ch: str, close_ch: str) -> int:
    """Return index right AFTER the matching closing delimiter, or -1 if unmatched."""
    if open_pos < 0 or open_pos >= len(text) or text[open_pos] != open_ch:
        return -1
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == open_ch and not _is_escaped(text, i):
            depth += 1
        elif ch == close_ch and not _is_escaped(text, i):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _skip_optional_bracket_arg(text: str, pos: int) -> int:
    """If `text[pos:]` starts with optional whitespace + `[...]`, advance past `]`."""
    n = len(text)
    i = pos
    while i < n and text[i].isspace():
        i += 1
    if i >= n or text[i] != "[":
        return pos
    # Track both [...] and nested {...} so `[Short \textbf{X}]` closes accurately
    b_depth = 0
    c_depth = 0
    while i < n:
        ch = text[i]
        if not _is_escaped(text, i):
            if ch == "{":
                c_depth += 1
            elif ch == "}":
                c_depth = max(0, c_depth - 1)
            elif ch == "[" and c_depth == 0:
                b_depth += 1
            elif ch == "]" and c_depth == 0:
                b_depth -= 1
                if b_depth == 0:
                    return i + 1
        i += 1
    return pos


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
    cleaned = _strip_tex_comments(tex_text)
    m_doc = re.search(r"\\begin\{document\}", cleaned)
    preamble = cleaned[:m_doc.start()] if m_doc else cleaned
    pat = re.compile(
        r"\\(?:newcommand|renewcommand|DeclareRobustCommand)\*?"
        r"\{\\([a-zA-Z]+)\}"
        r"(?:\[0\])?"
        r"\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"
    )
    for m in pat.finditer(preamble):
        name, val = m.group(1), m.group(2)
        val = re.sub(r"\\(?:textsc|textbf|emph|texttt|mathrm|text)\{([^{}]*)\}", r"\1", val)
        val = re.sub(r"\\[a-zA-Z]+\s*", "", val).strip()
        if val:
            macros[name] = val
    return macros


def _strip_latex(raw: str, macros: dict[str, str]) -> str:
    s = _strip_tex_comments(raw)
    # Expand 0-arg custom macros safely (lambda avoids re.sub backreference crashes)
    for name, val in macros.items():
        s = re.sub(
            rf"\\{re.escape(name)}(?:\{{\}}|(?![a-zA-Z]))",
            lambda _m, v=val: f" {v} ",
            s,
        )
    # Remove non-prose commands
    s = re.sub(
        r"\\(?:label|includegraphics|setlength|renewcommand|centering|footnotesize|small|scriptsize|tiny|"
        r"toprule|midrule|bottomrule|rowcolor|columncolor|cmidrule|captionsetup|vspace|hspace|vskip|hskip)"
        r"(?:\[[^\]]*\])?(?:\{[^{}]*\})*",
        " ",
        s,
    )
    s = re.sub(r"\\(?:cite|citep|citet|citealp|cref|Cref|ref|eqref|autoref)\*?(?:\[[^\]]*\])?\{[^{}]*\}", " ", s)
    # Unwrap standard LaTeX formatting / heading commands (iterate for nesting)
    for _ in range(3):
        s = re.sub(
            r"\\(?:section|subsection|subsubsection|paragraph|subparagraph|caption|title|icmltitle|"
            r"textbf|textit|emph|texttt|textsc|mathrm|mathbf|mathsf|mathtt|mathit|text|textrm|textsf|"
            r"underline|color|textcolor|begin|end)\*?(?:\[[^\]]*\])?\{([^{}]*)\}",
            r" \1 ",
            s,
        )
    s = s.replace("~", " ").replace(r"\,", " ").replace(r"\%", "%").replace(r"\_", "_").replace(r"\&", "&")
    s = re.sub(r"\\[a-zA-Z]+\*?", " ", s)
    s = re.sub(r"[{}]", " ", s)
    return _norm(s)


def _extract_caption_text(env_raw: str, macros: dict[str, str]) -> str:
    cleaned = _strip_tex_comments(env_raw)
    m = re.search(r"\\caption\*?(?![a-zA-Z])", cleaned)
    if not m:
        return _strip_latex(cleaned, macros)
    pos = _skip_optional_bracket_arg(cleaned, m.end())
    while pos < len(cleaned) and cleaned[pos].isspace():
        pos += 1
    if pos >= len(cleaned) or cleaned[pos] != "{":
        return _strip_latex(cleaned[m.start():], macros)
    end = _find_matching_delim(cleaned, pos, "{", "}")
    if end == -1:
        return _strip_latex(cleaned[pos:], macros)
    return _strip_latex(cleaned[pos:end], macros)


def _find_title_block(lines: list[str], macros: dict[str, str]) -> dict | None:
    """Locate `\\icmltitle{...}` or `\\title[...]{...}` anywhere in preamble or body."""
    full_clean = "".join(_strip_tex_comment_line(ln.rstrip("\r\n")) + "\n" for ln in lines)
    for pat in (r"\\icmltitle(?![a-zA-Z])", r"\\title(?![a-zA-Z])"):
        m = re.search(pat, full_clean)
        if not m:
            continue
        pos = _skip_optional_bracket_arg(full_clean, m.end())
        while pos < len(full_clean) and full_clean[pos].isspace():
            pos += 1
        if pos < len(full_clean) and full_clean[pos] == "{":
            end = _find_matching_delim(full_clean, pos, "{", "}")
            if end == -1:
                end = full_clean.find("\n", m.start())
                if end == -1:
                    end = len(full_clean)
        else:
            end = full_clean.find("\n", m.start())
            if end == -1:
                end = len(full_clean)
        start_line = full_clean.count("\n", 0, m.start()) + 1
        end_line = full_clean.count("\n", 0, max(m.start(), end - 1)) + 1
        raw = "".join(lines[start_line - 1:end_line])
        return {
            "start_line": start_line,
            "end_line": end_line,
            "raw": raw,
            "norm": _strip_latex(raw, macros),
        }
    return None


_FLOAT_BEGIN_RE = re.compile(
    r"\\begin\{(figure\*?|table\*?|wrapfigure|wraptable|longtable|sidewaysfigure|sidewaystable|SCfigure|SCtable)\}"
)
_SECTION_CMD_RE = re.compile(r"\\section(\*?)(?=\s*[\[{])")
_SUBSECTION_CMD_RE = re.compile(r"\\subsection(\*?)(?=\s*[\[{])")
_APPENDIX_CMD_RE = re.compile(r"\\appendix(?![a-zA-Z])")
_TOP_HEADING_START_RE = re.compile(
    r"^\\(?:section|subsection|appendix(?![a-zA-Z])|bibliography|begin\{abstract\})"
)


def _appendix_letter(idx: int, upper: bool = True) -> str:
    if 1 <= idx <= 26:
        return chr((64 if upper else 96) + idx)
    return f"A{idx}" if upper else f"a{idx}"


def _parse_tex_structure(tex_text: str) -> dict:
    """Parse LaTeX file into title block, floats (figures/tables), and prose blocks."""
    tex_norm = _normalize_newlines(tex_text)
    macros = _extract_zero_arg_macros(tex_norm)
    lines = tex_norm.splitlines(keepends=True)
    n_lines = len(lines)
    clean_lines = [_strip_tex_comment_line(ln.rstrip("\n")) for ln in lines]

    title_block = _find_title_block(lines, macros)

    doc_start = 0
    for i, cln in enumerate(clean_lines):
        if re.search(r"\\begin\{document\}", cln):
            doc_start = i + 1
            break

    floats: list[dict] = []
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
        stripped = clean_lines[i].strip()
        if not stripped:
            i += 1
            continue
        if re.search(r"\\end\{document\}", stripped):
            break
        if _APPENDIX_CMD_RE.search(stripped):
            in_appendix = True
            app_sec_idx = 0
            fig_app_n = 0
            tab_app_n = 0

        m_sec = _SECTION_CMD_RE.search(stripped)
        if m_sec and not m_sec.group(1):
            if in_appendix:
                app_sec_idx += 1
                fig_app_n = 0
                tab_app_n = 0
            else:
                sec_num += 1
                subsec_num = 0

        m_env = _FLOAT_BEGIN_RE.search(stripped)
        if m_env:
            env_name = m_env.group(1)
            end_tag = rf"\end{{{env_name}}}"
            start_i = i
            end_i = i
            while end_i < n_lines and end_tag not in clean_lines[end_i]:
                end_i += 1
            if end_i >= n_lines:
                end_i = n_lines - 1
            for k in range(start_i, end_i + 1):
                float_line_mask[k] = True
            raw = "".join(lines[start_i:end_i + 1])
            kind = "figure" if "figure" in env_name.lower() else "table"
            if kind == "figure":
                if in_appendix and app_sec_idx > 0:
                    fig_app_n += 1
                    lbl = f"{_appendix_letter(app_sec_idx, True)}.{fig_app_n}"
                else:
                    fig_main_n += 1
                    lbl = str(fig_main_n)
            else:
                if in_appendix and app_sec_idx > 0:
                    tab_app_n += 1
                    lbl = f"{_appendix_letter(app_sec_idx, True)}.{tab_app_n}"
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

    # Pass 2: parse non-float prose blocks with synchronized section numbering
    blocks: list[dict] = []
    in_appendix = False
    sec_num = 0
    subsec_num = 0
    app_sec_idx = 0
    cur_start: int | None = None
    cur_lines: list[str] = []
    cur_prefix: str = ""

    def flush_block() -> None:
        nonlocal cur_start, cur_lines, cur_prefix
        if cur_start is None or not cur_lines:
            cur_start = None
            cur_lines = []
            cur_prefix = ""
            return
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
            first_clean = _strip_tex_comment_line(cur_lines[s_off].rstrip("\n")).strip()
            blocks.append({
                "start_line": sl,
                "end_line": el,
                "raw": raw,
                "norm": norm,
                "is_top_heading": bool(_TOP_HEADING_START_RE.match(first_clean)),
            })
        cur_start = None
        cur_lines = []
        cur_prefix = ""

    boundary_re = re.compile(
        r"^\\(?:section|subsection|subsubsection|paragraph|begin\{abstract\}|end\{abstract\}|icmltitle|title(?![a-zA-Z])|bibliography|end\{document\})"
    )

    for i in range(doc_start, n_lines):
        ln = lines[i]
        raw_stripped = ln.strip()
        clean_stripped = clean_lines[i].strip()

        if float_line_mask[i]:
            flush_block()
            continue
        if re.search(r"\\end\{document\}", clean_stripped):
            flush_block()
            break
        if clean_stripped.startswith((r"\bibliography", r"\bibliographystyle", r"\newpage", r"\clearpage", r"\onecolumn", r"\twocolumn")):
            flush_block()
            continue
        if not raw_stripped:
            flush_block()
            continue
        if raw_stripped.startswith("%") or not clean_stripped:
            if cur_start is not None:
                cur_lines.append(ln)
            continue
        if _APPENDIX_CMD_RE.search(clean_stripped):
            flush_block()
            in_appendix = True
            app_sec_idx = 0
            # Only skip the line if nothing else of substance sits on it
            remainder = _APPENDIX_CMD_RE.sub("", clean_stripped).strip()
            if not remainder:
                continue

        if boundary_re.match(clean_stripped):
            flush_block()
            m_sec = _SECTION_CMD_RE.match(clean_stripped)
            m_sub = _SUBSECTION_CMD_RE.match(clean_stripped)
            if m_sec and not m_sec.group(1):
                if in_appendix:
                    app_sec_idx += 1
                    cur_prefix = _appendix_letter(app_sec_idx, False)
                else:
                    sec_num += 1
                    subsec_num = 0
                    cur_prefix = str(sec_num)
            elif m_sub and not m_sub.group(1):
                subsec_num += 1
                if in_appendix and app_sec_idx > 0:
                    cur_prefix = f"{_appendix_letter(app_sec_idx, False)} {subsec_num}"
                else:
                    cur_prefix = f"{sec_num} {subsec_num}"
            cur_start = i
            cur_lines = [ln]
            continue

        if cur_start is None:
            cur_start = i
            cur_lines = [ln]
        else:
            cur_lines.append(ln)

    flush_block()
    return {
        "lines": lines,
        "title_block": title_block,
        "floats": floats,
        "blocks": blocks,
        "macros": macros,
        "float_line_mask": float_line_mask,
    }


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
    title_block = parsed["title_block"]
    floats = parsed["floats"]
    blocks = parsed["blocks"]
    float_line_mask = parsed["float_line_mask"]

    spans: dict[str, dict] = {}
    used_floats: set[int] = set()

    # 1. Match figures and tables first
    for it in items:
        cid = it.get("id")
        if not cid:
            continue
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
            snip = fl["raw"].strip("\r\n")
            spans[cid] = {
                "start_line": fl["start_line"],
                "end_line": fl["end_line"],
                "tex_snippet": snip,
                "tex_chunks": [snip] if snip else [],
                "tex_chunk_lines": [[fl["start_line"], fl["end_line"]]],
            }

    # 2. Match prose items in order (handling Title in preamble or body)
    prose_items = [it for it in items if it.get("id") and it.get("kind") not in ("figure", "table")]
    matched_starts: list[tuple[str, int]] = []
    used_blocks: set[int] = set()

    for it in prose_items:
        cid = it["id"]
        anc_raw = (it.get("anchor") or "").strip()
        anc_norm = _norm(anc_raw)
        prev_norm = _norm(it.get("en_preview") or "")

        if cid == "Title":
            # Prefer dedicated title block (supports preamble \title{} and body \icmltitle{} / \title{})
            if title_block is not None:
                snip = title_block["raw"].strip("\r\n")
                spans[cid] = {
                    "start_line": title_block["start_line"],
                    "end_line": title_block["end_line"],
                    "tex_snippet": snip,
                    "tex_chunks": [snip] if snip else [],
                    "tex_chunk_lines": [[title_block["start_line"], title_block["end_line"]]],
                }
                # Mark overlapping body block (if any) as used so other items don't claim it
                for bi, b in enumerate(blocks):
                    if b["start_line"] <= title_block["end_line"] and title_block["start_line"] <= b["end_line"]:
                        used_blocks.add(bi)
                continue

        best_bi = None
        best_sc = -1.0

        if cid == "Abstract" or anc_norm == "abstract":
            for bi, b in enumerate(blocks):
                if bi in used_blocks:
                    continue
                if r"\begin{abstract}" in _strip_tex_comments(b["raw"]):
                    best_bi, best_sc = bi, 200.0
                    break
        else:
            for bi, b in enumerate(blocks):
                if bi in used_blocks:
                    continue
                b_clean = _strip_tex_comments(b["raw"])
                if r"\icmltitle" in b_clean or r"\begin{abstract}" in b_clean:
                    continue
                sc = 0.0
                if anc_norm:
                    sc = max(sc, _word_overlap_score(anc_norm, b["norm"]))
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

    matched_starts.sort(key=lambda x: x[1])
    for idx, (cid, bi) in enumerate(matched_starts):
        next_bi = matched_starts[idx + 1][1] if idx + 1 < len(matched_starts) else len(blocks)
        if cid == "Title":
            end_bi = bi
        elif cid == "Abstract":
            end_bi = bi
            while end_bi + 1 < next_bi and r"\end{abstract}" not in _strip_tex_comments(blocks[end_bi]["raw"]):
                end_bi += 1
        else:
            # Do not let a prose span swallow unlisted subsequent top-level sections/subsections
            limit_bi = next_bi
            for k in range(bi + 1, next_bi):
                if blocks[k].get("is_top_heading"):
                    limit_bi = k
                    break
            end_bi = max(bi, limit_bi - 1)

        sl = blocks[bi]["start_line"]
        el = blocks[end_bi]["end_line"]

        # Split around any float environments embedded between prose lines
        chunks: list[str] = []
        chunk_lines: list[list[int]] = []
        cur_c: list[str] = []
        cur_c_start: int | None = None
        for l_idx in range(sl - 1, el):
            if float_line_mask[l_idx]:
                if cur_c and cur_c_start is not None:
                    txt_c = "".join(cur_c).strip("\r\n")
                    if txt_c.strip():
                        chunks.append(txt_c)
                        chunk_lines.append([cur_c_start + 1, l_idx])
                    cur_c = []
                    cur_c_start = None
            else:
                if cur_c_start is None:
                    cur_c_start = l_idx
                cur_c.append(lines[l_idx])
        if cur_c and cur_c_start is not None:
            txt_c = "".join(cur_c).strip("\r\n")
            if txt_c.strip():
                chunks.append(txt_c)
                chunk_lines.append([cur_c_start + 1, el])

        spans[cid] = {
            "start_line": sl,
            "end_line": el,
            "tex_snippet": chunks[0] if len(chunks) == 1 else "\n".join(chunks),
            "tex_chunks": chunks,
            "tex_chunk_lines": chunk_lines,
        }

    return spans


def section_group_key(item: dict) -> str:
    """Group items into generic section buckets for 1-click whole-section locking."""
    cid = str(item.get("id") or "").strip()
    sec = str(item.get("section") or "")
    kind = str(item.get("kind") or "").lower()
    sec_clean = re.sub(r"^[🔒🔓\s]+", "", sec).strip()

    if cid in ("Title", "Abstract"):
        return "标题与摘要"

    # Explicit numbered section in `section` (e.g., "§2.2 Method", "Section 3")
    m_sec = re.search(r"(?:§\s*|\b(?:section|sec\.?)\s*)(\d+)", sec_clean, re.I)
    if m_sec:
        return f"§{m_sec.group(1)}"

    # Explicit appendix in `section` or `App...` item id
    m_app_sec = re.search(r"(?:附录|\bappendix)\s*([A-Z])\b", sec_clean, re.I)
    if m_app_sec:
        return f"附录 {m_app_sec.group(1).upper()}"
    if "附录" in sec_clean or re.search(r"\bappendix\b", sec_clean, re.I):
        return "附录"
    m_app_id = re.match(r"^App([A-Z])(?![a-z])", cid)
    if m_app_id:
        return f"附录 {m_app_id.group(1)}"
    if re.match(r"^App(?:$|[^a-z])", cid):
        return "附录"

    # Explicit disclosure / impact / ethics / acknowledgements (word-boundary only!)
    if (
        "声明" in sec_clean
        or "影响" in sec_clean
        or re.search(r"\b(impact|ethics|disclosure|broader|acknowledg\w*)\b", sec_clean, re.I)
        or re.search(r"(?:^|[^A-Za-z])(Impact|Ethics|Disclosure|Ack|Acknowledgements)(?=$|[^A-Za-z])", cid, re.I)
    ):
        return "声明与影响"

    # Numeric prefix in item ID (e.g., "2.1-P1" -> "§2")
    m_id = re.match(r"^(\d+)(?:\.|-|_)", cid)
    if m_id:
        return f"§{m_id.group(1)}"

    # Standalone figures/tables without an explicit §N section tag
    if kind in ("figure", "table") or re.match(r"^(?:图|表)\s*\d", sec_clean):
        return "图表"

    return sec_clean.split("（")[0].strip() or "其他"


def _unpack_update_args(
    args: tuple,
    action: str | None,
    ids: list[str] | None,
    mode: str | None,
    items: list[dict] | None,
) -> tuple[str | None, list[str] | None, str | None, list[dict] | None]:
    """Support both new `(pdf_path, action, ids, mode, items)` and legacy `(pdf_path, items, action, ids, mode)`."""
    if not args:
        return action, ids, mode, items
    if isinstance(args[0], list):
        # Legacy positional order: (items, [action], [ids], [mode])
        if items is not None:
            raise TypeError("Multiple values provided for 'items'")
        items = args[0]
        if len(args) >= 2:
            if action is not None:
                raise TypeError("Multiple values provided for 'action'")
            action = args[1]
        if len(args) >= 3:
            if ids is not None:
                raise TypeError("Multiple values provided for 'ids'")
            ids = args[2]
        if len(args) >= 4:
            if mode is not None:
                raise TypeError("Multiple values provided for 'mode'")
            mode = args[3]
        if len(args) > 4:
            raise TypeError(f"Too many positional arguments ({len(args) + 1})")
    else:
        # New contract order: (action, [ids], [mode], [items])
        if action is not None:
            raise TypeError("Multiple values provided for 'action'")
        action = args[0]
        if len(args) >= 2:
            if ids is not None:
                raise TypeError("Multiple values provided for 'ids'")
            ids = args[1]
        if len(args) >= 3:
            if mode is not None:
                raise TypeError("Multiple values provided for 'mode'")
            mode = args[2]
        if len(args) >= 4:
            if items is not None:
                raise TypeError("Multiple values provided for 'items'")
            items = args[3]
        if len(args) > 4:
            raise TypeError(f"Too many positional arguments ({len(args) + 1})")
    return action, ids, mode, items


def update_locks_for_pdf(
    pdf_path: Path | str,
    *args,
    action: str | None = None,
    ids: list[str] | None = None,
    mode: str | None = None,
    items: list[dict] | None = None,
    tex_path: Path | str | None = None,
    lock_dir: Path | str | None = None,
) -> dict:
    """Apply lock/unlock/toggle/set_mode action and atomically persist `<stem>.locks.json`.

    Supports both keyword and positional calls:
    - Contract: `update_locks_for_pdf(pdf_path, action, ids=None, mode=None, items=None, *, tex_path=None, lock_dir=None)`
    - Legacy:   `update_locks_for_pdf(pdf_path, items, action, ids=None, mode=None)`
    """
    action, ids, mode, items = _unpack_update_args(args, action, ids, mode, items)

    if not isinstance(action, str) or action not in VALID_ACTIONS:
        raise ValueError(f"Invalid lock action: {action!r}. Must be one of {sorted(VALID_ACTIONS)}.")
    if mode is not None and (not isinstance(mode, str) or mode not in VALID_MODES):
        raise ValueError(f"Invalid lock mode: {mode!r}. Must be one of {sorted(VALID_MODES)}.")
    if action == "set_mode" and mode not in VALID_MODES:
        raise ValueError(f"'set_mode' requires mode in {sorted(VALID_MODES)}, got {mode!r}.")

    if ids is not None:
        if isinstance(ids, (str, bytes)) or not isinstance(ids, Iterable):
            raise ValueError("'ids' must be a list of string item IDs.")
        ids_list = list(ids)
        if any(not isinstance(x, str) or not x for x in ids_list):
            raise ValueError("All elements of 'ids' must be non-empty strings.")
    else:
        ids_list = []

    pdf_p = Path(pdf_path).resolve()
    tp = tex_path_for(pdf_p, tex_hint=tex_path)
    items_list = [it for it in (items or []) if isinstance(it, dict) and isinstance(it.get("id"), str)]
    item_by_id = {it["id"]: it for it in items_list}

    unmatched: list[str] = []
    warnings: list[str] = []

    with _LOCK_IO_MUTEX:
        data = load_locks(pdf_p, strict=False, lock_dir=lock_dir)
        data["tex"] = tp.name
        data["tex_file"] = tp.name
        data["pdf"] = pdf_p.name
        if tp.exists() and tp.parent != pdf_p.parent:
            data["tex_path"] = str(tp.resolve())

        if action == "set_mode":
            data["mode"] = mode
            lp = save_locks(pdf_p, data, lock_dir=lock_dir)
            res = _enrich_lock_dict(data, lp)
            res["unmatched"] = []
            res["warnings"] = []
            return res

        locks = dict(data.get("locks") or {})
        need_spans = any(
            (cid not in locks) if action == "toggle" else (action == "lock")
            for cid in ids_list
        )
        spans: dict[str, dict] = {}
        tex_exists = tp.exists()
        if need_spans and tex_exists and items_list:
            tex_text = _normalize_newlines(tp.read_text(encoding="utf-8-sig", errors="replace"))
            spans = locate_tex_spans(tex_text, items_list)

        for cid in ids_list:
            is_locked = cid in locks
            do_lock = (not is_locked) if action == "toggle" else (action == "lock")
            if not do_lock:
                locks.pop(cid, None)
                continue

            if not tex_exists:
                unmatched.append(cid)
                warnings.append(f"LaTeX source not found ({tp.name}); skipped locking '{cid}'.")
                continue

            if cid not in item_by_id:
                unmatched.append(cid)
                warnings.append(f"Item '{cid}' not found in companion items; skipped locking.")
                continue

            sp = spans.get(cid)
            snippet = (sp.get("tex_snippet") or "") if sp else ""
            chunks = [c for c in ((sp.get("tex_chunks") if sp else None) or ([snippet] if snippet else [])) if c]
            if not sp or not chunks:
                unmatched.append(cid)
                warnings.append(f"Could not locate '{cid}' in {tp.name}; lock was not created to avoid a phantom lock.")
                continue

            it = item_by_id[cid]
            chunk_lines = sp.get("tex_chunk_lines") or [[sp["start_line"], sp["end_line"]]]
            locks[cid] = {
                "id": cid,
                "section": re.sub(r"^[🔒🔓\s]+", "", str(it.get("section") or ""))
                .replace("（已锁定）", "")
                .replace("·已锁定", "")
                .strip(),
                "title": str(it.get("title") or ""),
                "kind": str(it.get("kind") or "prose"),
                "anchor": str(it.get("anchor") or ""),
                "tex_start_line": sp["start_line"],
                "tex_end_line": sp["end_line"],
                "tex_chunk_lines": chunk_lines,
                "tex_snippet": snippet,
                "tex_chunks": chunks,
                "tex_sha256": _sha256(snippet),
                "locked_at": _now_str(),
            }

        data["locks"] = locks
        lp = save_locks(pdf_p, data, lock_dir=lock_dir)
        res = _enrich_lock_dict(data, lp)
        res["unmatched"] = unmatched
        res["warnings"] = warnings
        return res


# =====================================================================
# PreToolUse Hook Guard (Antigravity + Claude Code)
# =====================================================================

_GENERIC_TEX_LINE_RE = re.compile(
    r"^\\(?:begin|end)\{(?:figure\*?|table\*?|tabular\*?|center|itemize|enumerate|align\*?|equation\*?|minipage|subfigure)\}(?:\[[^\]]*\])?$|"
    r"^\\(?:centering|small|footnotesize|scriptsize|tiny|toprule|midrule|bottomrule|hline|medskip|bigskip|smallskip)$"
)


def _strip_inactive_latex(tex: str) -> str:
    """Return active (compilable) LaTeX text with comments, \\iffalse...\\fi,
    comment environments, and post-\\end{document} content removed.
    """
    cleaned = _strip_tex_comments(_normalize_newlines(tex))
    m_end = re.search(r"\\end\{document\}", cleaned)
    if m_end:
        cleaned = cleaned[:m_end.start()]

    # Remove \begin{comment}...\end{comment} (unclosed runs to end)
    cleaned = re.sub(r"\\begin\{comment\}.*?(?:\\end\{comment\}|$)", "", cleaned, flags=re.S)

    # Remove \iffalse ... \fi blocks (handling nested \if...\fi; unclosed strips to end)
    out: list[str] = []
    token_re = re.compile(r"\\(?:iffalse|if[a-zA-Z]+|fi)(?![a-zA-Z])")
    pos = 0
    skip_depth = 0
    for m in token_re.finditer(cleaned):
        tok = m.group(0)
        if skip_depth == 0:
            if tok == r"\iffalse":
                out.append(cleaned[pos:m.start()])
                skip_depth = 1
                pos = m.end()
        else:
            if tok == r"\fi":
                skip_depth -= 1
                if skip_depth == 0:
                    pos = m.end()
            elif tok.startswith(r"\if"):
                skip_depth += 1
    if skip_depth == 0:
        out.append(cleaned[pos:])
    return "".join(out)


def _line_offset(lines: list[str], line_no_1based: int) -> int:
    idx = max(0, min(len(lines), line_no_1based - 1))
    return sum(len(x) for x in lines[:idx])


def _find_all_positions(haystack: str, needle: str) -> list[int]:
    if not needle:
        return []
    positions: list[int] = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _find_lock_char_spans(src: str, lock_entry: dict) -> list[tuple[int, int]]:
    """Locate a locked entry's character span(s) in current normalized `.tex` text."""
    src_n = _normalize_newlines(src)
    lines = src_n.splitlines(keepends=True)
    rec_sl = lock_entry.get("tex_start_line")
    target_off = _line_offset(lines, rec_sl) if isinstance(rec_sl, int) and rec_sl >= 1 else 0

    raw_chunks = lock_entry.get("tex_chunks") or []
    if not raw_chunks and lock_entry.get("tex_snippet"):
        raw_chunks = [lock_entry["tex_snippet"]]
    chunks = [_normalize_newlines(c) for c in raw_chunks if c]

    found: list[tuple[int, int]] = []
    for ch in chunks:
        hits = _find_all_positions(src_n, ch)
        if hits:
            best_pos = min(hits, key=lambda p: abs(p - target_off))
            found.append((best_pos, best_pos + len(ch)))
            continue

        # Fallback: pick distinctive non-generic anchor lines closest to recorded line offset
        slines = [ln.strip() for ln in ch.splitlines() if ln.strip() and not ln.strip().startswith("%")]
        distinct = [ln for ln in slines if not _GENERIC_TEX_LINE_RE.match(ln)]
        if distinct:
            # Prefer caption/label or longest distinctive line as anchor
            anchor_line = max(
                distinct,
                key=lambda ln: (1 if (r"\caption" in ln or r"\label" in ln) else 0, len(ln)),
            )
            a_hits = _find_all_positions(src_n, anchor_line)
            if a_hits:
                ap = min(a_hits, key=lambda p: abs(p - target_off))
                # Expand outward to first/last line only within a bounded window around anchor
                p0 = ap
                if slines[0] != anchor_line:
                    prev_pos = src_n.rfind(slines[0], max(0, ap - len(ch) - 200), ap + 1)
                    if prev_pos != -1:
                        p0 = prev_pos
                p1_end = ap + len(anchor_line)
                if slines[-1] != anchor_line:
                    next_pos = src_n.find(slines[-1], ap, min(len(src_n), ap + len(ch) + 200))
                    if next_pos != -1:
                        p1_end = next_pos + len(slines[-1])
                found.append((p0, p1_end))

    if found:
        return found

    # Fallback to stored per-chunk line ranges first (never spans across embedded floats!)
    chunk_lines = lock_entry.get("tex_chunk_lines")
    if isinstance(chunk_lines, list) and chunk_lines:
        spans_from_chunks: list[tuple[int, int]] = []
        for pair in chunk_lines:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                sl, el = pair
                if isinstance(sl, int) and isinstance(el, int) and sl >= 1 and el >= sl:
                    c0 = _line_offset(lines, sl)
                    c1 = _line_offset(lines, min(el, len(lines)) + 1)
                    if c1 > c0:
                        spans_from_chunks.append((c0, c1))
        if spans_from_chunks:
            return spans_from_chunks

    sl = lock_entry.get("tex_start_line")
    el = lock_entry.get("tex_end_line")
    if isinstance(sl, int) and isinstance(el, int) and sl >= 1 and el >= sl:
        c0 = _line_offset(lines, sl)
        c1 = _line_offset(lines, min(el, len(lines)) + 1)
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
            f"请确认是否允许修改，或在论文预览器 UI 中解锁后再改。"
        )
    return (
        f"[PAPER-LOCK 硬锁拦截] 禁止修改 {tex_name} 中已锁定的区域：{labels}。"
        f"请先提示用户在论文预览器 UI 中点击「解锁」后再修改。"
    )


def _resolve_tool_path(fpath_str: str, data: dict) -> Path:
    p = Path(fpath_str)
    if p.is_absolute():
        return p.resolve()
    cwd = data.get("cwd")
    if cwd:
        return (Path(str(cwd)) / p).resolve()
    for wp in data.get("workspacePaths") or []:
        if wp:
            cand = (Path(str(wp)) / p).resolve()
            if cand.exists() or cand.parent.exists():
                return cand
    return p.resolve()


def _discover_workspace_locks(data: dict, extra_dirs: Iterable[Path] = ()) -> list[Path]:
    roots: list[Path] = []
    for wp in data.get("workspacePaths") or []:
        if wp:
            roots.append(Path(str(wp)))
    if data.get("cwd"):
        roots.append(Path(str(data["cwd"])))
    for d in extra_dirs:
        roots.append(d)

    seen_roots: set[Path] = set()
    found: list[Path] = []
    seen_files: set[Path] = set()

    for root in roots:
        try:
            rr = root.resolve()
        except OSError:
            continue
        if rr in seen_roots or not rr.is_dir():
            continue
        seen_roots.add(rr)
        base_depth = len(rr.parts)
        for dirpath, dirnames, filenames in os.walk(rr):
            dp = Path(dirpath)
            depth = len(dp.parts) - base_depth
            # Prune heavy/irrelevant dirs while keeping `.paper_viewer`
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_WALK_DIRS and (depth < 5 or d == ".paper_viewer")
            ]
            for fn in filenames:
                if _is_locks_filename(fn):
                    fp = (dp / fn).resolve()
                    if fp not in seen_files:
                        seen_files.add(fp)
                        found.append(fp)
    return found


def _find_lock_files_for_tex(tex_path: Path, data: dict) -> list[Path]:
    """Find all `.locks.json` files governing `tex_path` (direct, `.paper_viewer/`, or workspace `tex_path`)."""
    tp = tex_path.resolve()
    direct: list[Path] = []
    for cand in (
        tp.parent / ".paper_viewer" / f"{tp.stem}{_LOCKS_SUFFIX}",
        tp.with_suffix(_LOCKS_SUFFIX),
    ):
        if cand.exists():
            direct.append(cand.resolve())

    seen = set(direct)
    # Also match workspace lock files that explicitly point to this .tex (e.g. build/main.locks.json -> src/main.tex)
    for lp in _discover_workspace_locks(data, extra_dirs=(tp.parent,)):
        if lp in seen:
            continue
        try:
            raw = json.loads(lp.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        rec_tp = raw.get("tex_path")
        if rec_tp:
            try:
                if Path(str(rec_tp)).resolve() == tp:
                    direct.append(lp)
                    seen.add(lp)
                    continue
            except OSError:
                pass
        if tex_path_for(lp, tex_hint=raw.get("tex")) == tp:
            direct.append(lp)
            seen.add(lp)
    return direct


def _check_post_edit_locks(src_n: str, after_n: str, locks: dict, t_spans: list[tuple[int, int]] | None) -> list[dict]:
    """Verify every locked entry remains intact and active after an edit/write."""
    active_src = _strip_inactive_latex(src_n)
    active_after = _strip_inactive_latex(after_n)
    hits: list[dict] = []

    for entry in locks.values():
        raw_chunks = entry.get("tex_chunks") or ([entry["tex_snippet"]] if entry.get("tex_snippet") else [])
        chunks = [_normalize_newlines(c) for c in raw_chunks if c]

        if chunks:
            violated = False
            all_present_before = True
            for ch in chunks:
                before_cnt = src_n.count(ch)
                if before_cnt == 0:
                    all_present_before = False
                    break
                if after_n.count(ch) < before_cnt:
                    violated = True
                    break
                act_ch = _strip_inactive_latex(ch).strip()
                if act_ch and active_after.count(act_ch) < active_src.count(act_ch):
                    violated = True
                    break
            if violated:
                hits.append(entry)
                continue
            if all_present_before:
                # All chunks verbatim & active count preserved even after edit!
                continue

        # Fallback (for legacy/drifted locks whose snippet wasn't verbatim in `src_n`):
        l_spans = _find_lock_char_spans(src_n, entry)
        if not l_spans:
            continue
        if t_spans is None:
            # Full-file write: require each spanned slice to remain active in `after_n`
            for lc0, lc1 in l_spans:
                seg = src_n[lc0:lc1]
                act_seg = _strip_inactive_latex(seg).strip()
                if seg not in after_n or (act_seg and act_seg not in active_after):
                    hits.append(entry)
                    break
        else:
            touched = False
            for tc0, tc1 in t_spans:
                for lc0, lc1 in l_spans:
                    if tc0 < lc1 and lc0 < tc1:
                        seg = src_n[lc0:lc1]
                        act_seg = _strip_inactive_latex(seg).strip()
                        if seg not in after_n or (act_seg and act_seg not in active_after):
                            touched = True
                            break
                if touched:
                    break
            if touched:
                hits.append(entry)
    return hits


def _split_shell_segments(cmd: str) -> list[str]:
    """Split a shell command on unquoted `&&`, `||`, `;`, `|`, or newlines."""
    segs: list[str] = []
    buf: list[str] = []
    in_sq = False
    in_dq = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
        elif ch == '"' and not in_sq:
            # Handle escaped quotes inside double quotes
            if in_dq and i > 0 and cmd[i - 1] == "\\":
                buf.append(ch)
            else:
                in_dq = not in_dq
                buf.append(ch)
            i += 1
        elif not in_sq and not in_dq:
            if cmd[i:i + 2] in ("&&", "||", "|&"):
                seg = "".join(buf).strip()
                if seg:
                    segs.append(seg)
                buf = []
                i += 2
            elif cmd[i:i + 2] == ">|":
                # POSIX/Bash noclobber overwrite redirection, not a pipe
                buf.append(">|")
                i += 2
            elif ch in (";", "|", "\n", "\r"):
                seg = "".join(buf).strip()
                if seg:
                    segs.append(seg)
                buf = []
                i += 1
            else:
                buf.append(ch)
                i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        segs.append(tail)
    return segs


_LOCK_FILE_REF_RE = re.compile(r"\.locks(?:\.json|\.\*|\*)|\*\s*\.locks|\.paper_viewer", re.I)

_DESTRUCTIVE_CMD_RE = re.compile(
    r"(?i)"
    r"\b(?:set-content|add-content|clear-content|out-file|remove-item|move-item|copy-item|rename-item|new-item|"
    r"sc|ac|clc|ri|mi|ci|rni|ni|del|erase|rd|rmdir|ren|move|copy|"
    r"rm|mv|cp|truncate|tee|dd|unlink|shred|touch)\b"
    r"|\bsed\b[^\n]*\s-i"
    r"|\bperl\b[^\n]*\s-[a-z]*i"
    r"|\bruby\b[^\n]*\s-[a-z]*i"
    r"|\bgit\s+(?:checkout|restore|reset|clean|rm|mv)\b"
    r"|\b(?:write_text|write_bytes|unlink|rmdir|rename|replace|remove|rmtree|copy2?|copyfile|writeFileSync|unlinkSync|rmSync|renameSync)\s*\("
    r"|\b(?:os\.(?:remove|unlink|rename|replace)|shutil\.(?:move|copy|copy2|copyfile|rmtree))\b"
    r"|\bopen\s*\([^)]*,\s*(?:mode\s*=\s*)?['\"][rbtuU]*[wax+][rbtuUwax+]*['\"]"
    r"|\.write\s*\("
)

_REDIRECT_TARGET_RE = re.compile(
    r"(?:(?<!\w)[0-9*]|&)?>(?:>|\||&(?!\d+(?:\s|$)|-))?\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s'\";|&]+))"
)
_SAFE_READER_START_RE = re.compile(
    r"^\s*(?:pdflatex|xelatex|lualatex|latexmk|bibtex|biber|select-string|grep|rg|findstr|cat|type|get-content|head|tail|wc)\b",
    re.I,
)


def _segment_redirects_to(seg_low: str, target_pattern: str | re.Pattern) -> bool:
    for m in _REDIRECT_TARGET_RE.finditer(seg_low):
        rhs = m.group(1) or m.group(2) or m.group(3) or ""
        if isinstance(target_pattern, str):
            if target_pattern in rhs:
                return True
        else:
            if target_pattern.search(rhs):
                return True
    return False


def _segment_mutates_target(seg: str, target_pattern: str | re.Pattern) -> bool:
    seg_low = seg.lower()
    if isinstance(target_pattern, str):
        has_ref = target_pattern in seg_low
    else:
        has_ref = bool(target_pattern.search(seg_low))
    if not has_ref:
        return False

    # 1. Explicit output redirection targeting the protected file
    if _segment_redirects_to(seg_low, target_pattern):
        return True

    # 2. Pure safe reader/compiler (e.g. `pdflatex main.tex > build.log`, `grep foo main.tex > hits.txt`)
    if _SAFE_READER_START_RE.match(seg_low) and not _DESTRUCTIVE_CMD_RE.search(seg):
        return False

    # 3. Destructive / mutating command or write-mode file API
    if _DESTRUCTIVE_CMD_RE.search(seg):
        return True

    return False


def _extract_protocol_and_call(data: dict) -> tuple[str, str, str, dict]:
    """Detect hook protocol (`antigravity` or `claude`) and return `(proto, event, tool_name, args)`."""
    if "tool_name" in data or "hook_event_name" in data:
        ev = str(data.get("hook_event_name") or "PreToolUse")
        tname = str(data.get("tool_name") or "").lower()
        targs = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        return "claude", ev, tname, targs
    ev = str(data.get("hookEventName") or "")
    tc = data.get("toolCall") if isinstance(data.get("toolCall"), dict) else {}
    tname = str(tc.get("name") or "").lower()
    targs = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    return "antigravity", ev, tname, targs


def _format_hook_output(protocol: str, result: dict) -> dict:
    if protocol != "claude":
        return result
    decision = str(result.get("decision") or "allow")
    reason = str(result.get("reason") or "")
    if decision == "allow":
        return {}
    perm = "ask" if decision == "force_ask" else "deny"
    out: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": perm,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    return out


def _inspect_tex_edit_or_write(
    tname: str,
    targs: dict,
    data: dict,
) -> dict:
    fpath_str = str(
        targs.get("Path")
        or targs.get("path")
        or targs.get("file_path")
        or targs.get("TargetFile")
        or ""
    )
    if not fpath_str:
        return {"decision": "allow"}
    fpath = _resolve_tool_path(fpath_str, data)

    # Never let the agent edit/overwrite a `.locks.json` file directly!
    if _is_locks_filename(fpath.name):
        return {
            "decision": "deny",
            "reason": (
                f"[PAPER-LOCK 保护] 锁状态文件 {fpath.name} 仅允许用户在论文预览器 UI 中操作，"
                f"Agent 禁止直接修改或解锁。"
            ),
        }

    if fpath.suffix.lower() != ".tex":
        return {"decision": "allow"}

    lock_files = _find_lock_files_for_tex(fpath, data)
    if not lock_files:
        return {"decision": "allow"}

    # Merge active locks across matching lock files (fail-closed if any is corrupted!)
    merged_locks: dict[str, dict] = {}
    effective_mode = "force_ask"
    for lp in lock_files:
        try:
            ldata = load_locks(lp, strict=True)
        except ValueError as exc:
            sys.stderr.write(f"[paper_lock] {exc}\n")
            return {
                "decision": "deny",
                "reason": (
                    f"[PAPER-LOCK 保护] 锁状态文件 {lp.name} 解析失败（可能已损坏），已默认拦截修改。"
                    f"请检查或修复 {lp.name} 后重试。"
                ),
            }
        if ldata.get("locks"):
            merged_locks.update(ldata["locks"])
            if ldata.get("mode", "deny") == "deny":
                effective_mode = "deny"

    if not merged_locks:
        return {"decision": "allow"}

    decision = "force_ask" if effective_mode == "force_ask" else "deny"

    # Edit tools (Antigravity edit_file, Claude Edit / MultiEdit)
    if tname in ("edit_file", "edit", "multiedit", "multi_edit", "replace_file_content", "multi_replace_file_content"):
        if not fpath.exists():
            return {"decision": "allow"}
        src_n = _normalize_newlines(fpath.read_text(encoding="utf-8-sig", errors="replace"))

        edit_pairs: list[tuple[str, str, bool]] = []
        if isinstance(targs.get("edits"), list):
            for ed in targs["edits"]:
                if isinstance(ed, dict):
                    old_s = _normalize_newlines(str(ed.get("old_string") or ed.get("Target") or ed.get("target") or ""))
                    new_s = _normalize_newlines(str(ed.get("new_string") or ed.get("Replacement") or ed.get("replacement") or ""))
                    # Claude MultiEdit defaults to replacing a single match unless replace_all=True
                    replace_all = bool(ed.get("replace_all", False))
                    if old_s:
                        edit_pairs.append((old_s, new_s, replace_all))
        elif isinstance(targs.get("ReplacementChunks"), list):
            for ed in targs["ReplacementChunks"]:
                if isinstance(ed, dict):
                    old_s = _normalize_newlines(str(ed.get("TargetContent") or ""))
                    new_s = _normalize_newlines(str(ed.get("ReplacementContent") or ""))
                    if old_s:
                        edit_pairs.append((old_s, new_s, True))
        else:
            old_s = _normalize_newlines(str(
                targs.get("Target")
                or targs.get("target")
                or targs.get("old_string")
                or targs.get("TargetContent")
                or ""
            ))
            new_s = _normalize_newlines(str(
                targs.get("Replacement")
                or targs.get("replacement")
                or targs.get("new_string")
                or targs.get("ReplacementContent")
                or ""
            ))
            # Antigravity `edit_file` replaces all occurrences; Claude `Edit` replaces 1 unless replace_all=True
            default_all = tname != "edit"
            replace_all = bool(targs.get("replace_all", default_all))
            if old_s:
                edit_pairs.append((old_s, new_s, replace_all))

        if not edit_pairs:
            return {"decision": "allow"}

        after_n = src_n
        all_t_spans: list[tuple[int, int]] = []
        for old_s, new_s, replace_all in edit_pairs:
            orig_positions = _find_all_positions(src_n, old_s)
            if orig_positions:
                if replace_all:
                    for idx in orig_positions:
                        all_t_spans.append((idx, idx + len(old_s)))
                else:
                    idx = orig_positions[0]
                    all_t_spans.append((idx, idx + len(old_s)))
            # Apply sequentially to `after_n` so chained MultiEdit steps cannot bypass locks
            cur_positions = _find_all_positions(after_n, old_s)
            if cur_positions:
                if replace_all:
                    after_n = after_n.replace(old_s, new_s)
                else:
                    after_n = after_n.replace(old_s, new_s, 1)

        hits = _check_post_edit_locks(
            src_n,
            after_n,
            merged_locks,
            t_spans=None if len(edit_pairs) > 1 else all_t_spans,
        )
        if hits:
            return {"decision": decision, "reason": _format_block_reason(hits, effective_mode, fpath.name)}
        return {"decision": "allow"}

    # Write tools (Antigravity write_file, Claude Write)
    if tname in ("write_file", "write", "write_to_file"):
        if targs.get("Append") or targs.get("append"):
            return {"decision": "allow"}
        content_n = _normalize_newlines(str(
            targs.get("Content")
            or targs.get("content")
            or targs.get("CodeContent")
            or ""
        ))
        src_n = _normalize_newlines(fpath.read_text(encoding="utf-8-sig", errors="replace")) if fpath.exists() else ""
        hits = _check_post_edit_locks(src_n, content_n, merged_locks, t_spans=None)
        if hits:
            return {"decision": decision, "reason": _format_block_reason(hits, effective_mode, fpath.name)}
        return {"decision": "allow"}

    return {"decision": "allow"}


def inspect_pre_tool_use(data: dict) -> dict:
    """Inspect a PreToolUse hook payload (Antigravity or Claude Code) and return an internal decision dict."""
    _proto, _ev, tname, targs = _extract_protocol_and_call(data)

    # 1. Direct file edit / write tools
    if tname in (
        "edit_file", "edit", "multiedit", "multi_edit",
        "replace_file_content", "multi_replace_file_content",
        "write_file", "write", "write_to_file",
    ):
        return _inspect_tex_edit_or_write(tname, targs, data)

    # 2. Shell command tools (Antigravity run_command, Claude Bash)
    if tname in ("run_command", "bash", "shell", "execute_command"):
        cmd = str(
            targs.get("CommandLine")
            or targs.get("commandLine")
            or targs.get("command")
            or targs.get("cmd")
            or ""
        )
        if not cmd:
            return {"decision": "allow"}

        segments = _split_shell_segments(cmd)

        # Protect any `.locks.json` / `.locks.*` / `.paper_viewer` from shell mutation
        for seg in segments:
            if _segment_mutates_target(seg, _LOCK_FILE_REF_RE):
                return {
                    "decision": "deny",
                    "reason": "[PAPER-LOCK 保护] 禁止通过命令行篡改或删除 .locks.json 锁文件，请由用户在论文预览器 UI 中操作。",
                }

        # Discover all workspace `.locks.json` files (including `.paper_viewer/` & subdirs)
        for lp in _discover_workspace_locks(data):
            try:
                ldata = load_locks(lp, strict=True)
            except ValueError as exc:
                sys.stderr.write(f"[paper_lock] {exc}\n")
                fallback_tex = tex_path_for(lp).name.lower()
                if any(_segment_mutates_target(seg, fallback_tex) for seg in segments):
                    return {
                        "decision": "deny",
                        "reason": (
                            f"[PAPER-LOCK 保护] 锁状态文件 {lp.name} 解析失败（可能已损坏），"
                            f"已禁止命令行修改 {fallback_tex}。"
                        ),
                    }
                continue

            if not ldata.get("locks"):
                continue

            tex_name = str(ldata.get("tex") or tex_path_for(lp).name)
            tex_low = tex_name.lower()
            for seg in segments:
                if _segment_mutates_target(seg, tex_low):
                    mode = ldata.get("mode", "deny")
                    decision = "force_ask" if mode == "force_ask" else "deny"
                    return {
                        "decision": decision,
                        "reason": (
                            f"[PAPER-LOCK] 检测到命令试图写入已含锁定区域的 {tex_name}"
                            f"（共 {len(ldata['locks'])} 处已锁）。请改用 edit_file/Edit 编辑未锁定区域，或在论文预览器 UI 中先解锁。"
                        ),
                    }

    return {"decision": "allow"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--hook" in args:
        protocol = "antigravity"
        try:
            raw = sys.stdin.read()
            if not raw.strip():
                sys.stdout.write(json.dumps({"decision": "allow"}))
                sys.stdout.flush()
                return 0
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Hook stdin payload must be a JSON object")
            protocol, ev, _tname, _targs = _extract_protocol_and_call(data)
            if ev == "PreToolUse":
                internal = inspect_pre_tool_use(data)
                out = _format_hook_output(protocol, internal)
            else:
                out = {}
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
            return 0
        except Exception as exc:
            sys.stderr.write(f"[paper_lock] Hook error: {exc}\n")
            fail_closed = {
                "decision": "deny",
                "reason": f"[PAPER-LOCK 错误] 锁校验脚本处理异常（{type(exc).__name__}），已默认拦截以保护已锁区域。",
            }
            sys.stdout.write(json.dumps(_format_hook_output(protocol, fail_closed), ensure_ascii=False))
            sys.stdout.flush()
            return 0

    parser = argparse.ArgumentParser(
        prog="paper_lock.py",
        description="Chengmen Paper Viewer lock engine and PreToolUse hook guard.",
    )
    parser.add_argument("--hook", action="store_true", help="Run as a PreToolUse hook (reads JSON from stdin).")
    sub = parser.add_subparsers(dest="cmd")

    p_status = sub.add_parser("status", help="Show lock status for a PDF, TeX, or .locks.json file.")
    p_status.add_argument("target", help="Path to main.pdf, main.tex, or main.locks.json")
    p_status.add_argument("--lock-dir", default=None, help="Optional directory containing .locks.json")

    ns = parser.parse_args(args)
    if ns.cmd == "status":
        lp = lock_path_for(ns.target, lock_dir=ns.lock_dir)
        info = get_locks_for_pdf(ns.target, lock_dir=ns.lock_dir)
        print(json.dumps({
            "lock_file": str(lp),
            "exists": lp.exists(),
            "tex": info.get("tex"),
            "mode": info.get("mode"),
            "locked_count": len(info.get("locked") or []),
            "locked": info.get("locked") or [],
            "updated_at": info.get("updated_at") or "",
        }, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
