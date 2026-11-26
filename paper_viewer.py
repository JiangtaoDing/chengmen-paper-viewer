"""
Bilingual Live Vector-SVG PDF Paper Viewer (中英双栏实时矢量论文预览器)
Reusable across any LaTeX paper project (ICML / NeurIPS / ICLR / ACL / IEEE / single- or two-column).

Usage:
    python paper_viewer.py --pdf "path/to/paper/main.pdf"
    python paper_viewer.py --pdf "path/to/main.pdf" --zh "path/to/main_zh.json" --port 8788
    python paper_viewer.py --paper-dir "path/to/paper_dir" --default "main.pdf" --port 8788
"""

import argparse
import http.server
import json
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paper_lock

# Runtime configuration populated in main()
PAPER_DIR: Path = Path.cwd()
DEFAULT_FILE: str = ""
EXPLICIT_ZH_PATH: Path | None = None
PORT: int = 8788
RENDER_SCALE: float = 2.8

_cache_lock = threading.Lock()
_page_cache: dict[tuple[str, float, int, str], bytes] = {}
_meta_cache: dict[tuple[str, float], dict] = {}
_text_cache: dict[tuple[str, float], dict] = {}
_region_cache: dict[tuple[str, float, float], tuple[list[dict], dict[str, dict]]] = {}

ABBREVS = {
    "al.", "e.g.", "i.e.", "cf.", "vs.", "etc.", "fig.", "figs.", "tab.", "tabs.",
    "sec.", "secs.", "eq.", "eqs.", "app.", "ref.", "refs.", "corr.", "distr.",
    "mism.", "orig.", "sh.", "fwd.", "pp.", "tok.", "ms.", "no.", "vol.", "proc.",
}

ZH_EN_HINTS = [
    ("置零", "zero"), ("全零", "zero"), ("错配", "swap"), ("错配", "mism"),
    ("交换", "swap"), ("匹配", "match"), ("循环", "circular"), ("轮转", "rotation"),
    ("位置偏置", "position"), ("偏置", "bias"), ("自校正", "self-correction"),
    ("预填充", "prefill"), ("门控", "gate"), ("对照", "control"), ("干扰", "distractor"),
    ("支持", "support"), ("金标准", "gold"), ("翻转", "flip"), ("反事实", "counterfactual"),
    ("对称", "symmetric"), ("不对称", "asymmetric"), ("透镜", "lens"), ("岭", "ridge"),
    ("单向量", "single-vector"), ("单向量", "single vector"), ("单个向量", "single vector"),
    ("词表", "vocabulary"), ("解码", "decod"), ("白盒", "white-box"),
    ("离线", "offline"), ("在线", "online"), ("停止", "stops"), ("置信区间", "ci"),
    ("自助法", "bootstrap"), ("平衡准确率", "balanced"), ("盲答", "blind"),
    ("背景", "background"), ("私有", "private"), ("逐题", "item-level"),
    ("融合器", "fuser"), ("任务能力", "competence"), ("判断", "judgment"),
    ("预实验", "pilot"), ("直读", "direct-reading"),
]

_ZH_SENT_RE = re.compile(r'[^\n。！？!?]+(?:[。！？!?]+[」”’）\)\]]*)?|\n+')


def split_zh_tokens(text: str) -> tuple[list[dict], list[str]]:
    if not text:
        return [], []
    tokens: list[dict] = []
    cleans: list[str] = []
    for raw_tok in _ZH_SENT_RE.findall(text):
        cleaned = raw_tok.strip()
        if cleaned:
            s_idx = len(cleans)
            cleans.append(cleaned)
            tokens.append({"t": raw_tok, "s": s_idx})
        else:
            tokens.append({"t": raw_tok, "s": None})
    return tokens, cleans


def _is_heading_words(ws: list[tuple]) -> bool:
    if not ws:
        return False
    line_set = {(w[0], w[6], w[7]) for w in ws}
    txt = " ".join(w[5] for w in ws).strip()
    if len(line_set) <= 2 and len(txt) < 96:
        if re.match(r'^(\d+(\.\d+)*\.?|[A-Z]\.)\s+[A-Z]', txt) or txt in ("Abstract", "AI Disclosure Statement"):
            return True
    return False


def _split_en_blocks_to_sentences(
    item_blocks: list[tuple[int, int]],
    words_by_page_bno: dict[tuple[int, int], list[tuple]],
    page_dims: list[tuple[float, float]],
    keep_first_heading: bool = False,
) -> list[dict]:
    blocks: list[list[tuple]] = []
    for p_idx, bno in item_blocks:
        wlist = words_by_page_bno.get((p_idx, bno))
        if wlist:
            blocks.append(wlist)
    if not blocks:
        return []

    has_body = any(not _is_heading_words(ws) for ws in blocks)
    raw_sents: list[dict] = []
    cur_words: list[tuple] = []

    def flush():
        nonlocal cur_words
        if not cur_words:
            return
        txt = re.sub(r'(\w)-\s+(\w)', r'\1\2', " ".join(w[5] for w in cur_words))
        line_groups: dict[tuple[int, int, int], list[float]] = {}
        line_order: list[tuple[int, int, int]] = []
        for (p_idx, wx0, wy0, wx1, wy1, _wstr, bno, lno, _wno) in cur_words:
            lk = (p_idx, bno, lno)
            if lk not in line_groups:
                line_groups[lk] = [wx0, wy0, wx1, wy1]
                line_order.append(lk)
            else:
                g = line_groups[lk]
                g[0] = min(g[0], wx0)
                g[1] = min(g[1], wy0)
                g[2] = max(g[2], wx1)
                g[3] = max(g[3], wy1)
        rects = []
        for lk in line_order:
            p_idx = lk[0]
            pw, ph = page_dims[p_idx]
            x0, y0, x1, y1 = line_groups[lk]
            rects.append({
                "page": p_idx + 1,
                "rect": [
                    round(max(0.0, (x0 - 1.5) / pw), 4),
                    round(max(0.0, (y0 - 1.2) / ph), 4),
                    round(min(1.0, (x1 + 1.5) / pw), 4),
                    round(min(1.0, (y1 + 1.2) / ph), 4),
                ],
            })
        raw_sents.append({"text": txt, "rects": rects})
        cur_words = []

    for bi, ws in enumerate(blocks):
        if has_body and _is_heading_words(ws):
            if keep_first_heading and bi == 0:
                cur_words.extend(ws)
                flush()
            continue
        for idx, w in enumerate(ws):
            cur_words.append(w)
            wstr = w[5]
            w_clean = wstr.rstrip('”"\')]}')
            if not w_clean or w_clean in (".", "..", "...") or w_clean[-1] not in ".?!":
                continue
            if w_clean.lower() in ABBREVS:
                continue
            if re.match(r'^(\d+(\.\d+)*|[a-zA-Z])\.$', w_clean):
                if len(cur_words) <= 2:
                    continue
                prev_w = cur_words[-2][5].lower().lstrip('([{"“')
                if prev_w in ("figure", "fig.", "table", "tab.", "section", "sec.", "appendix", "equation", "eq."):
                    continue
            # Merge run-in bold paragraph title (e.g. "Circular evaluation.", "Models.") with its first sentence
            if idx < 5 and len(cur_words) <= 5 and w_clean.endswith(".") and idx + 1 < len(ws):
                continue
            nxt = ws[idx + 1][5] if idx + 1 < len(ws) else None
            if nxt is not None:
                nxt_c = nxt.lstrip('(“"[\'')
                if nxt_c and (nxt_c[0].islower() or nxt_c.startswith(".")):
                    continue
            flush()
    flush()
    return raw_sents


def _extract_anchors(s: str) -> tuple[set[str], set[str]]:
    s_low = s.lower().replace(",", "")
    nums = set(re.findall(r'\d+(?:\.\d+)?', s_low))
    words = set(re.findall(r'[a-z][a-z0-9\-]{1,}', s_low))
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "into", "over", "only", "than", "when", "where", "which", "have", "has",
        "had", "its", "our", "can", "all", "both", "each", "more", "most", "such",
        "very", "also", "under", "after", "before", "between", "across", "using",
        "used", "uses",
    }
    return nums, (words - stop)


def _sent_sim(zh_clean: str, en_text: str, zk_pos: float, em_pos: float) -> float:
    pos_score = max(0.0, 1.0 - 1.6 * abs(zk_pos - em_pos))
    z_nums, z_words = _extract_anchors(zh_clean)
    e_nums, e_words = _extract_anchors(en_text)
    num_hit = len(z_nums & e_nums)
    word_hit = len(z_words & e_words)
    en_low = en_text.lower()
    hint_hit = sum(1 for zh_kw, en_kw in ZH_EN_HINTS if zh_kw in zh_clean and en_kw in en_low)
    q_zh = ("？" in zh_clean or "?" in zh_clean)
    q_en = ("?" in en_text)
    q_bonus = 0.9 if (q_zh and q_en) else (-0.35 if q_zh != q_en else 0.0)
    len_ratio = min(len(zh_clean) * 2.5, max(1, len(en_text))) / max(len(zh_clean) * 2.5, max(1, len(en_text)), 1.0)
    return (
        pos_score
        + 0.85 * min(3, num_hit)
        + 0.40 * min(3, word_hit)
        + 0.35 * min(3, hint_hit)
        + 0.30 * len_ratio
        + q_bonus
    )


def _align_zh_en(zh_cleans: list[str], en_texts: list[str]) -> list[list[int]]:
    K, M = len(zh_cleans), len(en_texts)
    if K == 0 or M == 0:
        return [[] for _ in range(K)]
    if K == 1:
        return [list(range(M))]
    if M == 1:
        return [[0] for _ in range(K)]

    NEG = -1e9
    dp = [[NEG] * (M + 1) for _ in range(K + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (M + 1) for _ in range(K + 1)]
    dp[0][0] = 0.0

    for i in range(K + 1):
        for j in range(M + 1):
            if dp[i][j] <= NEG / 2:
                continue
            for di, dj in ((1, 1), (1, 2), (1, 3), (2, 1), (3, 1)):
                ni, nj = i + di, j + dj
                if ni > K or nj > M:
                    continue
                zc = " ".join(zh_cleans[i:ni])
                et = " ".join(en_texts[j:nj])
                sim = _sent_sim(zc, et, ((i + ni) / 2.0) / K, ((j + nj) / 2.0) / M)
                sc = sim * ((di + dj) / 2.0) - 1.15 * ((di - 1) + (dj - 1))
                if dp[i][j] + sc > dp[ni][nj]:
                    dp[ni][nj] = dp[i][j] + sc
                    back[ni][nj] = (i, j)

    if back[K][M] is None:
        return [[min(M - 1, round(z * (M - 1) / max(1, K - 1)))] for z in range(K)]

    mapping: list[list[int]] = [[] for _ in range(K)]
    ci, cj = K, M
    while ci > 0 or cj > 0:
        step = back[ci][cj]
        if step is None:
            break
        pi, pj = step
        es = list(range(pj, cj))
        for z_idx in range(pi, ci):
            mapping[z_idx].extend(es)
        ci, cj = pi, pj

    for z_idx in range(K):
        if not mapping[z_idx]:
            mapping[z_idx] = [min(range(M), key=lambda m: abs((z_idx + 0.5) / K - (m + 0.5) / M))]
    return mapping


def _norm(text: str) -> str:
    s = text.lower()
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = s.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"-\s+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _item_anchor_phrase(item: dict) -> str:
    anc = (item.get("anchor") or "").strip()
    if anc:
        return _norm(anc)
    prev = (item.get("en_preview") or "").strip()
    prev = re.sub(r"\\section\{([^}]*)\}", r"\1", prev)
    prev = re.sub(r"\[TODO[^\]]*\]", "", prev)
    words = _norm(prev).split()
    return " ".join(words[:7])


def _merge_rects_by_column(rects: list[tuple[float, float, float, float]], page_w: float) -> list[list[float]]:
    if not rects:
        return []
    mid = page_w / 2.0
    buckets: dict[str, list[tuple[float, float, float, float]]] = {"full": [], "left": [], "right": []}
    for x0, y0, x1, y1 in rects:
        if x0 < mid - 60 and x1 > mid + 60:
            buckets["full"].append((x0, y0, x1, y1))
        elif (x0 + x1) / 2.0 < mid:
            buckets["left"].append((x0, y0, x1, y1))
        else:
            buckets["right"].append((x0, y0, x1, y1))

    merged: list[list[float]] = []
    for grp in buckets.values():
        if not grp:
            continue
        grp.sort(key=lambda r: r[1])
        cur = list(grp[0])
        for r in grp[1:]:
            if r[1] <= cur[3] + 28:
                cur[0] = min(cur[0], r[0])
                cur[1] = min(cur[1], r[1])
                cur[2] = max(cur[2], r[2])
                cur[3] = max(cur[3], r[3])
            else:
                merged.append(cur)
                cur = list(r)
        merged.append(cur)
    return merged


def _is_margin_line_number(x0: float, x1: float, txt: str, pw: float) -> bool:
    if x1 < pw * 0.105 and x1 - x0 < 36:
        stripped = re.sub(r"\s+", "", txt)
        if stripped.isdigit():
            return True
    return False


def build_regions(pdf_path: Path, pdf_mtime: float, zh_mtime: float, items: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    key = (pdf_path.name, pdf_mtime, zh_mtime)
    with _cache_lock:
        cached = _region_cache.get(key)
        if cached is not None:
            return cached

    doc = pymupdf.open(pdf_path)
    try:
        float_items = [it for it in items if it.get("kind") in ("figure", "table")]
        prose_items = [it for it in items if it.get("kind") not in ("figure", "table")]

        prose_anchors = [(it["id"], _item_anchor_phrase(it)) for it in prose_items]
        float_anchors = [(it["id"], it.get("kind"), _item_anchor_phrase(it)) for it in float_items]

        page_dims: list[tuple[float, float]] = []
        raw_blocks_per_page: list[list[tuple[float, float, float, float, str, str, int, float]]] = []
        words_by_page_bno: dict[tuple[int, int], list[tuple]] = {}
        body_fs_samples: list[float] = []

        for p_idx, page in enumerate(doc):
            pw, ph = float(page.rect.width), float(page.rect.height)
            page_dims.append((pw, ph))
            dict_blks = page.get_text("dict").get("blocks", [])
            fs_by_bbox: dict[tuple[float, float, float, float], float] = {}
            for db in dict_blks:
                if db.get("type") != 0:
                    continue
                w_sum, c_sum = 0.0, 0
                for ln in db.get("lines", []):
                    for sp in ln.get("spans", []):
                        st = sp.get("text", "").strip()
                        if st:
                            n_ch = len(st)
                            w_sum += float(sp.get("size", 0.0)) * n_ch
                            c_sum += n_ch
                if c_sum > 0:
                    fs_val = w_sum / c_sum
                    bx0, by0, bx1, by1 = db["bbox"]
                    fs_by_bbox[(round(bx0, 1), round(by0, 1), round(bx1, 1), round(by1, 1))] = fs_val
                    if c_sum >= 120 and by0 >= ph * 0.08 and by1 <= ph * 0.90 and not _is_margin_line_number(bx0, bx1, "0", pw):
                        body_fs_samples.append(fs_val)

            blks = []
            for b in page.get_text("blocks"):
                x0, y0, x1, y1, txt, bno, btype = b
                if btype != 0:
                    continue
                if _is_margin_line_number(x0, x1, txt, pw):
                    continue
                if y1 < ph * 0.072:  # running header
                    continue
                if y0 > ph * 0.915:  # page number footer
                    continue
                ntxt = _norm(txt)
                if not ntxt:
                    continue
                bfs = fs_by_bbox.get((round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)), 0.0)
                blks.append((x0, y0, x1, y1, txt, ntxt, int(bno), bfs))
            raw_blocks_per_page.append(blks)

            for w in page.get_text("words"):
                wx0, wy0, wx1, wy1, wstr, bno, lno, wno = w
                if _is_margin_line_number(wx0, wx1, wstr, pw):
                    continue
                words_by_page_bno.setdefault((p_idx, int(bno)), []).append(
                    (p_idx, float(wx0), float(wy0), float(wx1), float(wy1), str(wstr), int(bno), int(lno), int(wno))
                )

        if body_fs_samples:
            body_fs_samples.sort()
            body_fs = body_fs_samples[len(body_fs_samples) // 2]
        else:
            body_fs = 10.0

        for k_pb in words_by_page_bno:
            words_by_page_bno[k_pb].sort(key=lambda x: (x[7], x[8]))

        float_boxes_per_page: list[list[tuple[str, tuple[float, float, float, float]]]] = [[] for _ in range(len(doc))]
        item_blocks_by_id: dict[str, list[tuple[int, int]]] = {}
        matched_float_ids: set[str] = set()

        for p_idx, blks in enumerate(raw_blocks_per_page):
            pw, ph = page_dims[p_idx]
            mid = pw / 2.0
            for fid, fkind, fanc in float_anchors:
                if fid in matched_float_ids or not fanc:
                    continue
                cap_blk = None
                for b in blks:
                    if b[5].startswith(fanc) or (fanc in b[5] and b[5].startswith(("figure ", "fig. ", "table ", "tab. "))):
                        cap_blk = b
                        break
                if cap_blk is None:
                    continue
                matched_float_ids.add(fid)
                cx0, cy0, cx1, cy1 = cap_blk[0], cap_blk[1], cap_blk[2], cap_blk[3]
                is_wide = (cx0 < mid - 40 and cx1 > mid + 40)
                cap_b_list = [(p_idx, cap_blk[6])]

                if fkind == "figure":
                    cap_end_y = cy1
                    for b in blks:
                        if b is cap_blk:
                            continue
                        if b[1] >= cy0 - 2 and b[1] <= cap_end_y + 14 and b[0] >= cx0 - 15 and b[2] <= cx1 + 20:
                            if any(anc and b[5].startswith(anc) for _, anc in prose_anchors):
                                break
                            cap_end_y = max(cap_end_y, b[3])
                            cap_b_list.append((p_idx, b[6]))
                    bx0 = pw * 0.08 if is_wide else max(pw * 0.08, cx0 - 6.0)
                    bx1 = pw * 0.92 if is_wide else min(pw * 0.92, cx1 + 6.0)
                    prose_above = [
                        b[3] for b in blks
                        if b[3] < cy0 - 24
                        and b[0] < bx1 - 10 and b[2] > bx0 + 10
                        and (
                            (
                                (b[2] - b[0]) > pw * 0.56
                                and len(b[4]) >= 60
                                and b[0] < bx0 + 25
                                and b[4].strip().endswith((".", ":", "?", "!", ")", "]"))
                            )
                            or any(anc and b[5].startswith(anc) for _, anc in prose_anchors)
                        )
                    ]
                    if prose_above:
                        top_y = max(ph * 0.075, max(prose_above) + 4.0)
                    elif cy0 < ph * 0.46:
                        top_y = ph * 0.075
                    else:
                        top_y = max(ph * 0.075, cy0 - 110.0)
                        for b in blks:
                            if b[3] <= cy0 + 2 and b[1] >= cy0 - 135 and "[todo" in b[5]:
                                top_y = min(top_y, b[1] - 18.0)
                    prev_floats_above = [
                        fb[3] for _, fb in float_boxes_per_page[p_idx]
                        if fb[3] < cy0 - 12 and fb[0] < bx1 - 10 and fb[2] > bx0 + 10
                    ]
                    if prev_floats_above:
                        top_y = max(top_y, max(prev_floats_above) + 4.0)
                    float_boxes_per_page[p_idx].append((fid, (bx0, top_y, bx1, cap_end_y + 4.0)))
                else:
                    col_left = pw * 0.08 if is_wide else (pw * 0.08 if (cx0 + cx1) / 2.0 < mid else mid - 4.0)
                    col_right = pw * 0.92 if is_wide else (mid - 6.0 if (cx0 + cx1) / 2.0 < mid else pw * 0.92)
                    below = [
                        b for b in blks
                        if b[1] >= cy0 - 2
                        and col_left <= (b[0] + b[2]) / 2.0 <= col_right
                    ]
                    below.sort(key=lambda b: b[1])
                    end_y = cy1
                    cap_end_y = cy1
                    still_in_caption = True
                    seen_table_body = False
                    col_w = max(1.0, col_right - col_left)
                    for b in below:
                        if b is cap_blk:
                            continue
                        if b[5].startswith(("references", "figure ", "fig. ", "table ", "tab. ")):
                            break
                        if any(anc and b[5].startswith(anc) for _, anc in prose_anchors):
                            break
                        if b[1] > end_y + 40:
                            break
                        bfs = b[7]
                        bw = b[2] - b[0]
                        btxt_s = b[4].strip()
                        # Stop before body-text prose paragraphs (e.g. cross-column continuations below a top-of-column table)
                        if (not still_in_caption or b[1] > cap_end_y + 12) and bfs >= body_fs - 0.35 and (bw >= col_w * 0.55 or len(btxt_s) >= 45):
                            break
                        # Also stop if a large float-separation gap follows a table body and block is not a footnote
                        if seen_table_body and b[1] > end_y + 16 and not btxt_s.startswith(("*", "∗", "†", "‡", "§", "◊", "Note", "note")):
                            if bfs >= body_fs - 0.45:
                                break
                        if still_in_caption and b[1] <= cap_end_y + 8 and (b[2] - b[0]) >= (cx1 - cx0) * 0.55:
                            cap_b_list.append((p_idx, b[6]))
                            cap_end_y = max(cap_end_y, b[3])
                        else:
                            still_in_caption = False
                            seen_table_body = True
                        end_y = max(end_y, b[3])
                    float_boxes_per_page[p_idx].append((fid, (col_left, max(ph * 0.073, cy0 - 4.0), col_right, end_y + 6.0)))
                item_blocks_by_id[fid] = cap_b_list

        prose_stream: list[tuple[int, tuple[float, float, float, float, str, str, int, float]]] = []
        for p_idx, blks in enumerate(raw_blocks_per_page):
            pw, ph = page_dims[p_idx]
            mid = pw / 2.0
            fboxes = float_boxes_per_page[p_idx]

            def in_float(b):
                cx = (b[0] + b[2]) / 2.0
                cy = (b[1] + b[3]) / 2.0
                for _, (fx0, fy0, fx1, fy1) in fboxes:
                    if fx0 <= cx <= fx1 and fy0 <= cy <= fy1:
                        return True
                return False

            pblks = [b for b in blks if not in_float(b)]
            wide_blks = [b for b in pblks if (b[2] - b[0]) > pw * 0.56 and b[0] < mid - 40 and b[2] > mid + 40]
            right_prose_blks = [b for b in pblks if b[0] > mid - 8 and (b[2] - b[0]) > pw * 0.24]
            wide_chars = sum(len(b[4]) for b in wide_blks)
            tot_chars = sum(len(b[4]) for b in pblks)
            is_onecol = (
                len(pblks) > 0
                and len(wide_blks) >= 2
                and len(right_prose_blks) < 2
                and (wide_chars >= tot_chars * 0.35 or len(wide_blks) >= max(2, len(pblks) // 2))
            )

            if is_onecol:
                pblks.sort(key=lambda b: (b[1], b[0]))
            else:
                full_top = [b for b in pblks if b[0] < mid - 60 and b[2] > mid + 60 and b[1] < ph * 0.28]
                rest = [b for b in pblks if b not in full_top]
                left_col = [b for b in rest if (b[0] + b[2]) / 2.0 < mid]
                right_col = [b for b in rest if (b[0] + b[2]) / 2.0 >= mid]
                full_top.sort(key=lambda b: b[1])
                left_col.sort(key=lambda b: b[1])
                right_col.sort(key=lambda b: b[1])
                pblks = full_top + left_col + right_col

            for b in pblks:
                prose_stream.append((p_idx, b))

        item_page_rects: dict[tuple[str, int], list[tuple[float, float, float, float]]] = {}
        current_id: str | None = None
        used_prose_ids: set[str] = set()
        has_title_item = any(pid == "Title" for pid, _ in prose_anchors)

        for p_idx, b in prose_stream:
            ntxt = b[5]
            if ntxt == "references" or ntxt.startswith("references "):
                current_id = None
                continue

            if has_title_item and p_idx == 0 and b[1] < page_dims[0][1] * 0.195 and "anonymous" not in ntxt and ntxt != "abstract":
                current_id = "Title"
                used_prose_ids.add("Title")
                item_page_rects.setdefault(("Title", p_idx), []).append((b[0], b[1], b[2], b[3]))
                item_blocks_by_id.setdefault("Title", []).append((p_idx, b[6]))
                continue

            matched_id = None
            for pid, panc in prose_anchors:
                if not panc or pid in used_prose_ids:
                    continue
                if ntxt.startswith(panc) or (len(panc) >= 12 and panc in ntxt):
                    matched_id = pid
                    break

            if matched_id is not None:
                current_id = matched_id
                used_prose_ids.add(matched_id)

            if current_id is not None:
                if current_id == "Title" and ("anonymous" in ntxt or b[1] >= page_dims[0][1] * 0.195):
                    continue
                item_page_rects.setdefault((current_id, p_idx), []).append((b[0], b[1], b[2], b[3]))
                item_blocks_by_id.setdefault(current_id, []).append((p_idx, b[6]))

        regions: list[dict] = []
        for p_idx, fboxes in enumerate(float_boxes_per_page):
            pw, ph = page_dims[p_idx]
            for fid, (x0, y0, x1, y1) in fboxes:
                regions.append({
                    "id": fid,
                    "page": p_idx + 1,
                    "col": 0,
                    "rect": [
                        round(max(0.0, x0 / pw), 4),
                        round(max(0.0, y0 / ph), 4),
                        round(min(1.0, x1 / pw), 4),
                        round(min(1.0, y1 / ph), 4),
                    ],
                })

        for (pid, p_idx), rlist in item_page_rects.items():
            pw, ph = page_dims[p_idx]
            merged = _merge_rects_by_column(rlist, pw)
            for x0, y0, x1, y1 in merged:
                pad_x, pad_y = 4.0, 2.5
                col_rank = 0 if (x0 < pw * 0.35 and x1 > pw * 0.65) else (1 if (x0 + x1) / 2.0 < pw / 2.0 else 2)
                regions.append({
                    "id": pid,
                    "page": p_idx + 1,
                    "col": col_rank,
                    "rect": [
                        round(max(0.0, (x0 - pad_x) / pw), 4),
                        round(max(0.0, (y0 - pad_y) / ph), 4),
                        round(min(1.0, (x1 + pad_x) / pw), 4),
                        round(min(1.0, (y1 + pad_y) / ph), 4),
                    ],
                })

        regions.sort(key=lambda r: (r["page"], r["col"], r["rect"][1], r["rect"][0]))
        for r in regions:
            r.pop("col", None)

        # Build bidirectional sentence-level alignment map per item
        sentence_map: dict[str, dict] = {}
        for it in items:
            cid = it["id"]
            zh_text = it.get("zh") or ""
            iblocks = item_blocks_by_id.get(cid, [])
            if not zh_text or not iblocks:
                continue
            zh_tokens, zh_cleans = split_zh_tokens(zh_text)
            if not zh_cleans:
                continue
            keep_hd = ("标题" in zh_text[:20])
            en_sents = _split_en_blocks_to_sentences(iblocks, words_by_page_bno, page_dims, keep_first_heading=keep_hd)
            if not en_sents:
                continue
            manual_align = it.get("sent_align")
            if isinstance(manual_align, list) and len(manual_align) == len(zh_cleans):
                zh_to_en = [[int(m) for m in grp if 0 <= int(m) < len(en_sents)] for grp in manual_align]
            else:
                zh_to_en = _align_zh_en(zh_cleans, [e["text"] for e in en_sents])

            en_to_zh: list[list[int]] = [[] for _ in range(len(en_sents))]
            for z_i, e_list in enumerate(zh_to_en):
                for e_i in e_list:
                    if 0 <= e_i < len(en_sents) and z_i not in en_to_zh[e_i]:
                        en_to_zh[e_i].append(z_i)
            for e_i in range(len(en_sents)):
                if not en_to_zh[e_i]:
                    nearest_z = min(
                        range(len(zh_cleans)),
                        key=lambda z_i: abs((z_i + 0.5) / len(zh_cleans) - (e_i + 0.5) / len(en_sents)),
                    )
                    en_to_zh[e_i] = [nearest_z]
                en_sents[e_i]["zh"] = en_to_zh[e_i]

            sentence_map[cid] = {
                "zh_tokens": zh_tokens,
                "zh_to_en": zh_to_en,
                "en_sents": en_sents,
            }
    finally:
        doc.close()

    result = (regions, sentence_map)
    with _cache_lock:
        for k in [k for k in _region_cache if k[0] == pdf_path.name and k != key]:
            del _region_cache[k]
        _region_cache[key] = result
    return result


def list_pdfs() -> list[str]:
    pdfs = sorted(
        [p.name for p in PAPER_DIR.glob("*.pdf") if p.is_file()],
        key=lambda name: (0 if name == DEFAULT_FILE else 1, name.lower()),
    )
    return pdfs


def resolve_pdf(name: str | None) -> Path:
    pdfs = list_pdfs()
    chosen = name if name in pdfs else (DEFAULT_FILE if DEFAULT_FILE in pdfs else (pdfs[0] if pdfs else DEFAULT_FILE))
    path = (PAPER_DIR / chosen).resolve()
    if PAPER_DIR.resolve() not in path.parents:
        raise FileNotFoundError("Invalid path")
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {chosen}")
    return path


def resolve_zh_path(pdf_path: Path) -> Path | None:
    if EXPLICIT_ZH_PATH is not None and EXPLICIT_ZH_PATH.exists():
        return EXPLICIT_ZH_PATH
    stem = pdf_path.stem
    candidates = [
        pdf_path.parent / ".paper_viewer" / f"{stem}_zh.json",
        pdf_path.parent / f"{stem}_zh.json",
        Path(__file__).resolve().parent / "companions" / f"{stem}_zh.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def get_meta(filename: str | None) -> dict:
    path = resolve_pdf(filename)
    mtime = path.stat().st_mtime
    key = (path.name, mtime)
    with _cache_lock:
        if key in _meta_cache:
            data = dict(_meta_cache[key])
            data["files"] = list_pdfs()
            return data

    doc = pymupdf.open(path)
    try:
        pages = [{"w": round(p.rect.width, 1), "h": round(p.rect.height, 1)} for p in doc]
        toc = [{"level": lvl, "title": title, "page": page} for lvl, title, page in doc.get_toc()]
        try:
            named_dests = doc.resolve_names() or {}
        except Exception:
            named_dests = {}
        links_per_page: list[list[dict]] = []
        n_pages = len(doc)
        for p in doc:
            pw, ph = float(p.rect.width) or 1.0, float(p.rect.height) or 1.0
            plinks: list[dict] = []
            for lnk in p.get_links():
                fr = lnk.get("from")
                if not fr:
                    continue
                rect_n = [
                    round(max(0.0, fr.x0 / pw), 4),
                    round(max(0.0, fr.y0 / ph), 4),
                    round(min(1.0, fr.x1 / pw), 4),
                    round(min(1.0, fr.y1 / ph), 4),
                ]
                kind = lnk.get("kind")
                if kind == pymupdf.LINK_URI and lnk.get("uri"):
                    uri = str(lnk["uri"]).strip()
                    if uri.startswith(("http://", "https://", "mailto:")):
                        plinks.append({"r": rect_n, "t": "uri", "u": uri})
                elif kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
                    dst_p = lnk.get("page")
                    to_pt = lnk.get("to")
                    ndest = str(lnk.get("nameddest") or "")
                    is_pdf_bottom_up = (kind == pymupdf.LINK_NAMED)
                    if (dst_p is None or dst_p < 0) and ndest and ndest in named_dests:
                        resolved = named_dests[ndest]
                        dst_p = resolved.get("page")
                        to_pt = resolved.get("to")
                        is_pdf_bottom_up = True
                    if isinstance(dst_p, int) and 0 <= dst_p < n_pages:
                        dpw = float(doc[dst_p].rect.width) or 1.0
                        dph = float(doc[dst_p].rect.height) or 1.0
                        tx, ty = 0.0, 0.0
                        if to_pt is not None:
                            try:
                                raw_x = float(to_pt.x) if hasattr(to_pt, "x") else float(to_pt[0])
                                raw_y = float(to_pt.y) if hasattr(to_pt, "y") else float(to_pt[1])
                                tx = max(0.0, min(1.0, raw_x / dpw))
                                y_top = (dph - raw_y) if is_pdf_bottom_up else raw_y
                                ty = max(0.0, min(1.0, y_top / dph))
                            except Exception:
                                tx, ty = 0.0, 0.0
                        entry = {"r": rect_n, "t": "goto", "p": dst_p + 1, "x": round(tx, 4), "y": round(ty, 4)}
                        if ndest:
                            entry["d"] = ndest
                        plinks.append(entry)
            links_per_page.append(plinks)
        info = {
            "file": path.name,
            "files": list_pdfs(),
            "page_count": n_pages,
            "mtime": mtime,
            "mtime_str": time.strftime("%H:%M:%S", time.localtime(mtime)),
            "pages": pages,
            "toc": toc,
            "links": links_per_page,
        }
    finally:
        doc.close()

    with _cache_lock:
        for k in [k for k in _meta_cache if k[0] == path.name and k[1] != mtime]:
            del _meta_cache[k]
        for k in [k for k in _page_cache if k[0] == path.name and k[1] != mtime]:
            del _page_cache[k]
        for k in [k for k in _text_cache if k[0] == path.name and k[1] != mtime]:
            del _text_cache[k]
        _meta_cache[key] = info
    return info


def get_text_layer(filename: str | None) -> dict:
    path = resolve_pdf(filename)
    mtime = path.stat().st_mtime
    key = (path.name, mtime)
    with _cache_lock:
        cached = _text_cache.get(key)
        if cached is not None:
            return cached

    doc = pymupdf.open(path)
    pages_out: list[dict] = []
    try:
        for page in doc:
            pw, ph = float(page.rect.width), float(page.rect.height)
            mid = pw / 2.0
            words = page.get_text("words")
            by_line: dict[tuple[int, int], list[tuple]] = {}
            for wx0, wy0, wx1, wy1, wstr, bno, lno, wno in words:
                if _is_margin_line_number(wx0, wx1, str(wstr), pw):
                    continue
                by_line.setdefault((int(bno), int(lno)), []).append(
                    (float(wx0), float(wy0), float(wx1), float(wy1), str(wstr), int(wno))
                )

            raw_runs: list[tuple[float, float, float, float, str, int, int]] = []
            for (bno, lno), wl in by_line.items():
                wl.sort(key=lambda w: w[5])
                cur = [wl[0]]
                for w in wl[1:]:
                    if w[0] - cur[-1][2] > 14.0:
                        rx0 = min(x[0] for x in cur)
                        ry0 = min(x[1] for x in cur)
                        rx1 = max(x[2] for x in cur)
                        ry1 = max(x[3] for x in cur)
                        raw_runs.append((rx0, ry0, rx1, ry1, " ".join(x[4] for x in cur), bno, lno))
                        cur = [w]
                    else:
                        cur.append(w)
                rx0 = min(x[0] for x in cur)
                ry0 = min(x[1] for x in cur)
                rx1 = max(x[2] for x in cur)
                ry1 = max(x[3] for x in cur)
                raw_runs.append((rx0, ry0, rx1, ry1, " ".join(x[4] for x in cur), bno, lno))

            wide_cnt = sum(1 for r in raw_runs if (r[2] - r[0]) > pw * 0.56 and r[0] < mid - 40 and r[2] > mid + 40)
            right_prose_cnt = sum(1 for r in raw_runs if r[0] > mid - 8 and (r[2] - r[0]) > pw * 0.24)
            is_onecol = len(raw_runs) > 0 and wide_cnt >= 3 and right_prose_cnt < 3
            if is_onecol:
                raw_runs.sort(key=lambda r: (round(r[1], 1), r[0]))
            else:
                top_full = [r for r in raw_runs if r[0] < mid - 55 and r[2] > mid + 55 and r[1] < ph * 0.28]
                top_set = set(range(len(raw_runs)))
                top_ids = {i for i, r in enumerate(raw_runs) if r[0] < mid - 55 and r[2] > mid + 55 and r[1] < ph * 0.28}
                rest = [raw_runs[i] for i in top_set - top_ids]
                left_c = [r for r in rest if (r[0] + r[2]) / 2.0 < mid]
                right_c = [r for r in rest if (r[0] + r[2]) / 2.0 >= mid]
                top_full.sort(key=lambda r: (round(r[1], 1), r[0]))
                left_c.sort(key=lambda r: (round(r[1], 1), r[0]))
                right_c.sort(key=lambda r: (round(r[1], 1), r[0]))
                raw_runs = top_full + left_c + right_c

            compact_runs: list[list] = []
            for idx, r in enumerate(raw_runs):
                rx0, ry0, rx1, ry1, rtxt, bno, _lno = r
                sep = " "
                if idx + 1 < len(raw_runs):
                    nxt = raw_runs[idx + 1]
                    if nxt[5] != bno or abs(nxt[1] - ry0) > 22.0:
                        sep = "\n"
                compact_runs.append([
                    round(rx0, 2),
                    round(ry0, 2),
                    round(max(1.0, rx1 - rx0), 2),
                    round(max(4.0, ry1 - ry0), 2),
                    rtxt,
                    sep,
                ])
            pages_out.append({"w": round(pw, 2), "h": round(ph, 2), "runs": compact_runs})
    finally:
        doc.close()

    res = {"file": path.name, "mtime": mtime, "pages": pages_out}
    with _cache_lock:
        _text_cache[key] = res
    return res


def get_zh_companion(filename: str | None) -> dict:
    pdf_path = resolve_pdf(filename)
    pdf_mtime = pdf_path.stat().st_mtime
    resolved_name = pdf_path.name
    zh_path = resolve_zh_path(pdf_path)
    if zh_path is None or not zh_path.exists():
        return {
            "available": False,
            "mtime": 0,
            "pdf_mtime": pdf_mtime,
            "mtime_str": "--:--:--",
            "note": f"未找到 {pdf_path.stem}_zh.json（可用 --zh 指定路径）",
            "items": [],
            "regions": [],
        }
    mtime = zh_path.stat().st_mtime
    try:
        raw = json.loads(zh_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "mtime": mtime, "pdf_mtime": pdf_mtime, "mtime_str": "--:--:--", "error": str(exc), "items": [], "regions": []}

    target_file = raw.get("file", resolved_name)
    if target_file and resolved_name != target_file:
        return {
            "available": False,
            "mtime": mtime,
            "pdf_mtime": pdf_mtime,
            "mtime_str": time.strftime("%H:%M:%S", time.localtime(mtime)),
            "note": f"当前中文对照对应 {target_file}",
            "items": [],
            "regions": [],
        }

    items = [dict(it) for it in raw.get("items", [])]
    regions, sentence_map = build_regions(pdf_path, pdf_mtime, mtime, items)
    lp = paper_lock.lock_path_for(pdf_path)
    locks_mtime = lp.stat().st_mtime if lp.exists() else 0.0
    ldata = paper_lock.load_locks(pdf_path)
    locked_set = set((ldata.get("locks") or {}).keys())
    primary_page: dict[str, int] = {}
    for r in regions:
        primary_page.setdefault(r["id"], r["page"])
    for it in items:
        if it["id"] in primary_page:
            it["page"] = primary_page[it["id"]]
        if it["id"] in sentence_map:
            it["sent_map"] = sentence_map[it["id"]]
        it["section_group"] = paper_lock.section_group_key(it)
        it["locked"] = it["id"] in locked_set
    return {
        "available": True,
        "file": resolved_name,
        "title_zh": raw.get("title_zh", ""),
        "mtime": mtime,
        "pdf_mtime": pdf_mtime,
        "locks_mtime": locks_mtime,
        "lock_mode": ldata.get("mode", "deny"),
        "locked_ids": sorted(locked_set),
        "mtime_str": time.strftime("%H:%M:%S", time.localtime(mtime)),
        "items": items,
        "regions": regions,
    }


def render_page_svg(filename: str | None, page_idx: int) -> tuple[bytes, float]:
    path = resolve_pdf(filename)
    mtime = path.stat().st_mtime
    key = (path.name, mtime, page_idx, "svg")
    with _cache_lock:
        cached = _page_cache.get(key)
        if cached is not None:
            return cached, mtime

    doc = pymupdf.open(path)
    try:
        if page_idx < 0 or page_idx >= len(doc):
            raise IndexError("Page out of range")
        page = doc[page_idx]
        svg_str = page.get_svg_image(matrix=pymupdf.Identity, text_as_path=True)
        svg_bytes = svg_str.encode("utf-8")
    finally:
        doc.close()

    with _cache_lock:
        _page_cache[key] = svg_bytes
    return svg_bytes, mtime


def render_page_png(filename: str | None, page_idx: int) -> tuple[bytes, float]:
    path = resolve_pdf(filename)
    mtime = path.stat().st_mtime
    key = (path.name, mtime, page_idx, "png")
    with _cache_lock:
        cached = _page_cache.get(key)
        if cached is not None:
            return cached, mtime

    doc = pymupdf.open(path)
    try:
        if page_idx < 0 or page_idx >= len(doc):
            raise IndexError("Page out of range")
        page = doc[page_idx]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE), alpha=False)
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()

    with _cache_lock:
        _page_cache[key] = png_bytes
    return png_bytes, mtime


def search_pdf(filename: str | None, query: str) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    path = resolve_pdf(filename)
    doc = pymupdf.open(path)
    matches = []
    try:
        q_lower = q.lower()
        for idx, page in enumerate(doc):
            text = page.get_text("text")
            pos = text.lower().find(q_lower)
            if pos != -1:
                count = text.lower().count(q_lower)
                start = max(0, pos - 40)
                end = min(len(text), pos + len(q) + 60)
                snippet = " ".join(text[start:end].split())
                matches.append({"page": idx + 1, "count": count, "snippet": snippet})
    finally:
        doc.close()
    return matches


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Paper Bilingual Vector Preview</title>
  <style>
    :root {
      --bg: #1a1c20;
      --toolbar: #25282e;
      --panel: #202228;
      --card: #282b33;
      --card-hover: #2f333d;
      --card-active: #2a354a;
      --border: #3a3e47;
      --text: #eaecef;
      --zh-text: #eef1f6;
      --muted: #98a0ad;
      --accent: #6aa1ff;
      --accent-soft: rgba(106, 161, 255, 0.16);
      --todo: #f6b73c;
      --todo-soft: rgba(246, 183, 60, 0.16);
      --btn-bg: #32363f;
      --btn-hover: #404550;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, sans-serif;
      font-size: 13px;
      overflow: hidden;
    }
    .app {
      display: flex;
      flex-direction: column;
      height: 100vh;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      background: var(--toolbar);
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      z-index: 20;
    }
    .group {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .sep {
      width: 1px;
      height: 18px;
      background: var(--border);
      margin: 0 2px;
    }
    select, input, button {
      background: var(--btn-bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 5px;
      height: 26px;
      font-size: 12px;
      padding: 0 7px;
      outline: none;
    }
    button {
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 26px;
      white-space: nowrap;
    }
    button:hover { background: var(--btn-hover); }
    button.active {
      border-color: var(--accent);
      color: #a9caff;
      background: var(--accent-soft);
    }
    #fileSelect { max-width: 155px; }
    #pageInput { width: 40px; text-align: center; }
    #searchInput { width: 112px; }
    .muted { color: var(--muted); font-size: 12px; }
    .spacer { flex: 1; }
    .live-badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      color: var(--muted);
      padding: 0 4px;
    }
    .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #34a853;
      display: inline-block;
    }
    .dot.updating { background: #fbbc04; }

    .body-wrap {
      display: flex;
      flex: 1;
      overflow: hidden;
      position: relative;
      min-height: 0;
    }
    .search-drawer {
      display: none;
      width: 220px;
      background: #22252b;
      border-right: 1px solid var(--border);
      overflow-y: auto;
      padding: 8px;
      flex-shrink: 0;
    }
    .search-drawer.open { display: block; }
    .search-item {
      padding: 6px 8px;
      border-radius: 5px;
      cursor: pointer;
      margin-bottom: 4px;
      background: #2b2f37;
      border: 1px solid transparent;
    }
    .search-item:hover { border-color: var(--accent); }
    .search-item .pg { font-weight: 600; color: var(--accent); font-size: 11px; }
    .search-item .snip { color: var(--muted); font-size: 11px; margin-top: 2px; line-height: 1.35; }

    .split-container {
      display: flex;
      flex: 1;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
    }
    .split-container.dir-row { flex-direction: row; }
    .split-container.dir-col { flex-direction: column; }

    .viewer {
      flex: 1 1 58%;
      min-width: 240px;
      min-height: 180px;
      overflow: auto;
      scroll-behavior: auto;
      cursor: grab;
    }
    .viewer-inner {
      min-width: 100%;
      width: max-content;
      padding: 14px 16px 36px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 14px;
    }
    .viewer.panning {
      cursor: grabbing !important;
      user-select: none;
    }
    .viewer.panning .page-card {
      cursor: grabbing !important;
    }
    .page-card {
      position: relative;
      background: #ffffff;
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.45);
      border-radius: 2px;
      overflow: hidden;
      flex-shrink: 0;
      cursor: grab;
    }
    .page-card.has-hover { cursor: pointer; }
    .page-card.has-link-hover,
    .page-card.has-link-hover .pdf-tl-span { cursor: pointer !important; }
    #backJumpBtn:disabled { opacity: 0.36; cursor: default; }
    #backJumpBtn:disabled:hover { background: var(--btn-bg); }
    .page-card img {
      display: block;
      width: 100%;
      height: auto;
      user-select: none;
      pointer-events: none;
    }
    .page-overlay {
      position: absolute;
      inset: 0;
      pointer-events: none;
      z-index: 4;
    }
    .page-text-layer {
      position: absolute;
      inset: 0;
      overflow: hidden;
      z-index: 5;
      line-height: 1.0;
      font-size: 0;
      user-select: text;
      -webkit-user-select: text;
    }
    .viewer.panning .page-text-layer,
    .viewer.force-pan .page-text-layer {
      pointer-events: none;
      user-select: none;
    }
    .pdf-tl-span {
      position: absolute;
      color: transparent;
      white-space: pre;
      transform-origin: 0% 0%;
      line-height: 1.0;
      font-family: "Times New Roman", Times, Georgia, serif;
      cursor: text;
      user-select: text;
      -webkit-user-select: text;
    }
    .pdf-tl-span::selection {
      background: rgba(59, 130, 246, 0.28);
      color: transparent;
    }
    .pdf-flash-box {
      position: absolute;
      background: rgba(59, 130, 246, 0.14);
      border: 2px solid rgba(59, 130, 246, 0.92);
      border-radius: 4px;
      pointer-events: none;
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.18);
      animation: pdfFlashFade 1.15s ease-out forwards;
    }
    .pdf-sent-hover-box {
      position: absolute;
      background: rgba(253, 224, 71, 0.30);
      border-bottom: 1.5px dashed rgba(217, 119, 6, 0.72);
      border-radius: 2px;
      pointer-events: none;
      mix-blend-mode: multiply;
    }
    .pdf-sent-hl-box {
      position: absolute;
      background: rgba(253, 224, 71, 0.56);
      border-bottom: 2px solid rgba(217, 119, 6, 0.95);
      border-radius: 2px;
      pointer-events: none;
      mix-blend-mode: multiply;
      animation: sentPulse 0.38s ease-out;
    }
    .pdf-link-hover-box {
      position: absolute;
      background: rgba(59, 130, 246, 0.16);
      border-bottom: 2px solid rgba(37, 99, 235, 0.95);
      border-radius: 2px;
      pointer-events: none;
      mix-blend-mode: multiply;
    }
    .pdf-link-target-ring {
      position: absolute;
      width: 28px;
      height: 28px;
      margin-left: -14px;
      margin-top: -14px;
      border-radius: 50%;
      border: 2.5px solid rgba(37, 99, 235, 0.92);
      background: rgba(59, 130, 246, 0.18);
      pointer-events: none;
      box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.22);
      animation: linkTargetPulse 1.25s ease-out forwards;
    }
    @keyframes linkTargetPulse {
      0% { transform: scale(0.55); opacity: 1; }
      45% { transform: scale(1.25); opacity: 0.92; }
      100% { transform: scale(1.65); opacity: 0; }
    }
    @keyframes pdfFlashFade {
      0% { opacity: 1; background: rgba(59, 130, 246, 0.24); box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.28); }
      45% { opacity: 0.92; background: rgba(59, 130, 246, 0.10); box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.14); }
      100% { opacity: 0; background: rgba(59, 130, 246, 0); box-shadow: none; }
    }
    @keyframes sentPulse {
      0% { background: rgba(251, 191, 36, 0.82); }
      100% { background: rgba(253, 224, 71, 0.56); }
    }
    .hover-pill {
      position: fixed;
      background: rgba(27, 30, 38, 0.94);
      border: 1px solid var(--accent);
      color: #dbe8ff;
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 5px;
      pointer-events: none;
      z-index: 60;
      display: none;
      max-width: 280px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      box-shadow: 0 3px 10px rgba(0, 0, 0, 0.4);
    }
    .page-badge {
      position: absolute;
      right: 8px;
      bottom: 6px;
      background: rgba(24, 25, 28, 0.75);
      color: #e8eaed;
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 4px;
      pointer-events: none;
      z-index: 6;
    }

    .splitter {
      background: var(--border);
      flex-shrink: 0;
      position: relative;
      transition: background 0.15s;
      z-index: 10;
    }
    .splitter:hover, .splitter.dragging { background: var(--accent); }
    .split-container.dir-row > .splitter {
      width: 5px;
      cursor: col-resize;
    }
    .split-container.dir-col > .splitter {
      height: 5px;
      cursor: row-resize;
    }
    .splitter.hidden, .zh-pane.hidden { display: none; }

    .zh-pane {
      flex: 0 0 42%;
      min-width: 260px;
      min-height: 180px;
      background: var(--panel);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .zh-header {
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      background: #23262d;
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex-shrink: 0;
    }
    .zh-header-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
    }
    .zh-title {
      font-weight: 600;
      font-size: 13px;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .zh-title .pill {
      font-size: 10px;
      font-weight: 500;
      padding: 1px 6px;
      border-radius: 99px;
      background: var(--accent-soft);
      color: #a9caff;
    }
    .zh-controls {
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
    }
    .zh-controls button {
      height: 23px;
      font-size: 11px;
      padding: 0 7px;
    }
    .zh-filter-input {
      flex: 1;
      min-width: 90px;
      height: 23px;
      font-size: 11px;
      padding: 0 7px;
      background: #1b1d22;
    }
    .zh-list {
      flex: 1;
      overflow-y: auto;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 9px;
    }
    .zh-card {
      background: var(--card);
      border: 1px solid #353944;
      border-left: 3px solid transparent;
      border-radius: 7px;
      padding: 9px 11px;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s, box-shadow 0.2s;
    }
    .zh-card:hover {
      background: var(--card-hover);
      border-color: #4b5263;
    }
    .zh-card.is-current-page {
      border-left-color: var(--accent);
      background: #272d3a;
    }
    .zh-card.is-selected {
      border-color: var(--accent);
      border-left-color: var(--accent);
      background: var(--card-active);
      box-shadow: 0 0 0 1px rgba(106, 161, 255, 0.35);
    }
    .zh-card.is-todo {
      border-left-color: var(--todo);
    }
    .zh-card.is-locked {
      border-color: #36506e;
      border-left-color: #38bdf8;
      background: #232b36;
    }
    .zh-card.is-locked.is-selected {
      border-color: #38bdf8;
      background: #263549;
    }
    .badge-lock {
      background: rgba(56, 189, 248, 0.16);
      color: #7dd3fc;
      border: 1px solid rgba(56, 189, 248, 0.35);
    }
    .lock-card-btn {
      height: 20px;
      font-size: 10.5px;
      padding: 0 6px;
      background: transparent;
      border: 1px solid #444956;
      color: #a9b3c4;
      margin-left: 4px;
    }
    .lock-card-btn:hover {
      border-color: #38bdf8;
      color: #e0f2fe;
      background: rgba(56, 189, 248, 0.12);
    }
    .lock-card-btn.is-locked {
      background: rgba(56, 189, 248, 0.18);
      border-color: rgba(56, 189, 248, 0.65);
      color: #7dd3fc;
      font-weight: 600;
    }
    .lock-menu-wrap {
      position: relative;
      display: inline-flex;
    }
    .lock-popover {
      display: none;
      position: absolute;
      right: 0;
      top: 28px;
      width: 268px;
      background: #22262e;
      border: 1px solid #424a59;
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.55);
      padding: 9px;
      z-index: 80;
    }
    .lock-popover.open { display: block; }
    .lock-pop-title {
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 6px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .lock-mode-row {
      display: flex;
      gap: 4px;
      margin-bottom: 8px;
      padding-bottom: 8px;
      border-bottom: 1px solid #333845;
    }
    .lock-mode-row button {
      flex: 1;
      height: 24px;
      font-size: 11px;
      padding: 0 6px;
    }
    .lock-mode-row button.active {
      border-color: #38bdf8;
      color: #7dd3fc;
      background: rgba(56, 189, 248, 0.16);
    }
    .lock-sec-list {
      max-height: 270px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .lock-sec-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      padding: 4px 6px;
      border-radius: 5px;
      background: #2a2f39;
      font-size: 11.5px;
    }
    .lock-sec-row .sec-name {
      font-weight: 600;
      color: #dce4f2;
    }
    .lock-sec-row .sec-cnt {
      font-size: 10.5px;
      color: var(--muted);
      margin-left: 4px;
    }
    .lock-sec-row button {
      height: 21px;
      font-size: 10.5px;
      padding: 0 7px;
    }
    .lock-sec-row button.all-locked {
      border-color: rgba(56, 189, 248, 0.6);
      color: #7dd3fc;
      background: rgba(56, 189, 248, 0.16);
    }
    .pdf-lock-rail {
      position: absolute;
      border-left: 2.5px solid rgba(56, 189, 248, 0.72);
      pointer-events: none;
    }
    .zh-card.flash-ring {
      animation: cardFlash 0.75s ease-out;
    }
    @keyframes cardFlash {
      0% { box-shadow: 0 0 0 3px rgba(106, 161, 255, 0.85); background: #334566; }
      100% { box-shadow: 0 0 0 1px rgba(106, 161, 255, 0.35); background: var(--card-active); }
    }
    .zh-card-meta {
      display: flex;
      align-items: center;
      gap: 5px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }
    .badge {
      font-size: 10.5px;
      padding: 1px 6px;
      border-radius: 4px;
      font-weight: 600;
      line-height: 1.4;
    }
    .badge-id {
      background: var(--accent-soft);
      color: #a9caff;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }
    .badge-page {
      background: #333845;
      color: #cfd6e3;
    }
    .badge-lines {
      background: #2c303a;
      color: var(--muted);
      font-weight: 400;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }
    .badge-todo {
      background: var(--todo-soft);
      color: var(--todo);
    }
    .copy-id-btn {
      margin-left: auto;
      height: 20px;
      font-size: 10.5px;
      padding: 0 6px;
      opacity: 0.72;
      background: transparent;
      border-color: #444956;
    }
    .zh-card:hover .copy-id-btn { opacity: 1; }
    .zh-card-heading {
      font-size: 12px;
      font-weight: 600;
      color: #d6e0f0;
      margin-bottom: 4px;
    }
    .zh-card-heading .sec {
      color: var(--muted);
      font-weight: 500;
    }
    .zh-en-preview {
      font-size: 11px;
      color: #8f98a8;
      line-height: 15.5px;
      height: 31px;
      margin-bottom: 6px;
      padding: 0 7px;
      border-left: 2px solid #3f4554;
      overflow: hidden;
      text-overflow: ellipsis;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }
    .zh-en-preview.is-sent-preview {
      border-left-color: #f59e0b;
      color: #dbe6f8;
      background: rgba(245, 158, 11, 0.08);
      border-radius: 0 4px 4px 0;
    }
    .zh-body {
      font-size: 13.5px;
      line-height: 1.68;
      color: var(--zh-text);
      white-space: pre-line;
      word-break: break-word;
    }
    .zh-sent {
      border-radius: 3px;
      cursor: pointer;
      transition: background 0.1s;
    }
    .zh-sent:hover, .zh-sent.is-hover {
      background: rgba(250, 204, 21, 0.16);
      color: #fff9db;
    }
    .zh-sent.is-active {
      background: rgba(250, 204, 21, 0.30);
      color: #fffdf0;
      box-shadow: inset 0 -1.5px 0 rgba(250, 204, 21, 0.92);
    }
    .zh-empty {
      color: var(--muted);
      text-align: center;
      padding: 28px 12px;
      font-size: 12.5px;
      line-height: 1.6;
    }
    .zh-tip {
      font-size: 11px;
      color: var(--muted);
      padding: 5px 10px;
      border-top: 1px solid var(--border);
      background: #22252c;
      flex-shrink: 0;
    }

    .toast {
      position: fixed;
      right: 16px;
      bottom: 16px;
      background: rgba(37, 40, 46, 0.96);
      border: 1px solid var(--accent);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      opacity: 0;
      transition: opacity 0.25s;
      pointer-events: none;
      z-index: 50;
    }
    .toast.show { opacity: 1; }
  </style>
</head>
<body>
  <div class="app">
    <div class="toolbar">
      <div class="group">
        <select id="fileSelect" title="选择 PDF"></select>
      </div>
      <div class="sep"></div>
      <div class="group">
        <button id="prevBtn" title="上一页">◀</button>
        <input id="pageInput" type="number" min="1" value="1" />
        <span class="muted">/ <span id="pageCount">-</span></span>
        <button id="nextBtn" title="下一页">▶</button>
        <button id="backJumpBtn" disabled title="返回跳转前的位置 (Alt+←)">↩ 返回</button>
      </div>
      <div class="sep"></div>
      <div class="group">
        <button id="zoomOutBtn" title="缩小">−</button>
        <span id="zoomLabel" class="muted" style="min-width:34px;text-align:center;">适应</span>
        <button id="zoomInBtn" title="放大">+</button>
        <button id="fitBtn" class="active" title="适应宽度">适应</button>
        <button id="panModeBtn" title="切换抓手平移模式（默认可直接划选复制英文；按住空格或拖空白处也可平移）">抓手</button>
      </div>
      <div class="sep"></div>
      <div class="group">
        <button id="zhToggleBtn" class="active" title="显示或隐藏中文对照面板">中文对照</button>
        <button id="splitDirBtn" title="切换左右分栏 / 上下分栏">左右分栏</button>
        <button id="pdfLockMarkBtn" title="默认关闭以防干扰 PDF 排版判断；开启后仅在锁定段左侧显示细竖条">PDF锁标</button>
      </div>
      <div class="sep"></div>
      <div class="group">
        <input id="searchInput" type="text" placeholder="搜英文 PDF..." />
        <button id="searchBtn">搜</button>
      </div>
      <div class="spacer"></div>
      <div class="group">
        <span class="live-badge" title="每 1.5 秒自动检测 PDF 与中文对照更新">
          <span id="liveDot" class="dot"></span>
          <span id="updatedAt">--:--:--</span>
        </span>
        <button id="reloadBtn" title="手动刷新">↻</button>
      </div>
    </div>

    <div class="body-wrap">
      <aside id="searchDrawer" class="search-drawer"></aside>
      <div id="splitContainer" class="split-container dir-row">
        <main id="viewer" class="viewer"><div id="viewerInner" class="viewer-inner"></div></main>
        <div id="splitter" class="splitter" title="拖动调整分栏比例"></div>
        <section id="zhPane" class="zh-pane">
          <div class="zh-header">
            <div class="zh-header-top">
              <div class="zh-title">
                <span>中文对照</span>
                <span id="zhCountPill" class="pill">0 段</span>
              </div>
              <div style="display:flex;align-items:center;gap:6px;">
                <div class="lock-menu-wrap">
                  <button id="lockMenuBtn" style="height:22px;font-size:11px;padding:0 7px;" title="按整节一键锁定/解锁，或切换拦截模式">🔒 锁定管理 (0) ▾</button>
                  <div id="lockPopover" class="lock-popover">
                    <div class="lock-pop-title">
                      <span>Agent 拦截模式</span>
                      <span id="lockModeStatus" style="color:#7dd3fc;">硬锁禁改</span>
                    </div>
                    <div class="lock-mode-row">
                      <button id="lockModeDenyBtn" class="active" title="严格禁止 Agent 改动任何已锁定段落，必须先在此解锁">🔒 硬锁禁改</button>
                      <button id="lockModeAskBtn" title="Agent 碰触锁定段时强制弹窗向你申请批准">✋ 弹窗确认</button>
                    </div>
                    <div class="lock-pop-title"><span>按整节一键锁定 / 解锁</span></div>
                    <div id="lockSecList" class="lock-sec-list"></div>
                  </div>
                </div>
                <span id="zhUpdatedAt" class="muted" style="font-size:11px;">同步 --:--:--</span>
              </div>
            </div>
            <div class="zh-controls">
              <button id="filterAllBtn" class="active" data-filter="all">全部</button>
              <button id="filterPageBtn" data-filter="page">仅当前页 (P1)</button>
              <button id="filterLockedBtn" data-filter="locked">仅已锁 (0)</button>
              <button id="filterTodoBtn" data-filter="todo">仅待写</button>
              <button id="autoSyncBtn" class="active" title="滚动 PDF 时自动高亮并滚动到对应中文段落">跟随</button>
              <input id="zhFilterInput" class="zh-filter-input" type="text" placeholder="筛编号/中文..." />
            </div>
          </div>
          <div id="zhList" class="zh-list"></div>
          <div class="zh-tip">支持逐句联动：在右侧划词选中或单击任一中文句子，左侧同步荧光高亮对应英文原句（反之亦然）</div>
        </section>
      </div>
    </div>
  </div>
  <div id="hoverPill" class="hover-pill"></div>
  <div id="toast" class="toast"></div>

  <script>
    let currentFile = new URLSearchParams(location.search).get('file') || '';
    let currentMeta = null;
    let currentZh = null;
    let currentPage = 1;
    let selectedZhId = null;
    let flashingItemId = null;
    let flashTimer = null;
    let activeSent = null;     // { itemId, zhIndices: [], enIndices: [] }
    let hoverSent = null;      // { itemId, zhIndices: [], enIndices: [] }
    let hoveredItemId = null;
    let hoveredPage = null;
    let isMouseDownInZh = false;
    let justDraggedZhSelection = false;
    let zoomMode = 'fit';
    let zoomScale = 1.0;

    let regionsByPage = {};
    let regionsById = {};
    let itemById = {};
    let enSentRectsByPage = {}; // page -> [{ itemId, enIdx, zhIndices, rect }]
    let pdfLinksByPage = {};    // page -> [{ r:[x0,y0,x1,y1], t:'goto'|'uri', p?, x?, y?, d?, u? }]
    let hoveredPdfLink = null;  // { page, link }
    let jumpHistory = [];       // [{ scrollTop, scrollLeft, page, zhScroll, selectedZhId, activeSent }]

    let zhVisible = localStorage.getItem('paper_zh_visible') !== '0';
    let splitDir = localStorage.getItem('paper_split_dir') || (window.innerWidth < 760 ? 'col' : 'row');
    let zhFilterMode = localStorage.getItem('paper_zh_filter') || 'all';
    let autoFollow = localStorage.getItem('paper_zh_follow') !== '0';
    let showPdfLockMarks = localStorage.getItem('paper_pdf_lock_marks') === '1';
    let suppressNextAutoFollow = false;
    let lockedSet = new Set();
    let lockMode = 'deny';

    const fileSelect = document.getElementById('fileSelect');
    const viewer = document.getElementById('viewer');
    const viewerInner = document.getElementById('viewerInner');
    const pageInput = document.getElementById('pageInput');
    const pageCountEl = document.getElementById('pageCount');
    const backJumpBtn = document.getElementById('backJumpBtn');
    const zoomLabel = document.getElementById('zoomLabel');
    const fitBtn = document.getElementById('fitBtn');
    const updatedAt = document.getElementById('updatedAt');
    const liveDot = document.getElementById('liveDot');
    const searchInput = document.getElementById('searchInput');
    const searchDrawer = document.getElementById('searchDrawer');
    const toast = document.getElementById('toast');
    const hoverPill = document.getElementById('hoverPill');

    const splitContainer = document.getElementById('splitContainer');
    const splitter = document.getElementById('splitter');
    const zhPane = document.getElementById('zhPane');
    const zhToggleBtn = document.getElementById('zhToggleBtn');
    const splitDirBtn = document.getElementById('splitDirBtn');
    const pdfLockMarkBtn = document.getElementById('pdfLockMarkBtn');
    const zhList = document.getElementById('zhList');
    const zhCountPill = document.getElementById('zhCountPill');
    const zhUpdatedAt = document.getElementById('zhUpdatedAt');
    const filterAllBtn = document.getElementById('filterAllBtn');
    const filterPageBtn = document.getElementById('filterPageBtn');
    const filterLockedBtn = document.getElementById('filterLockedBtn');
    const filterTodoBtn = document.getElementById('filterTodoBtn');
    const autoSyncBtn = document.getElementById('autoSyncBtn');
    const zhFilterInput = document.getElementById('zhFilterInput');
    const lockMenuBtn = document.getElementById('lockMenuBtn');
    const lockPopover = document.getElementById('lockPopover');
    const lockModeStatus = document.getElementById('lockModeStatus');
    const lockModeDenyBtn = document.getElementById('lockModeDenyBtn');
    const lockModeAskBtn = document.getElementById('lockModeAskBtn');
    const lockSecList = document.getElementById('lockSecList');

    let toastTimer = null;
    function showToast(msg) {
      toast.textContent = msg;
      toast.classList.add('show');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove('show'), 1900);
    }

    function indexZhData(zh) {
      regionsByPage = {};
      regionsById = {};
      itemById = {};
      enSentRectsByPage = {};
      lockedSet = new Set(Array.isArray(zh?.locked_ids) ? zh.locked_ids : []);
      lockMode = zh?.lock_mode === 'force_ask' ? 'force_ask' : 'deny';
      if (!zh || !zh.available) return;
      (zh.items || []).forEach(it => {
        itemById[it.id] = it;
        const sm = it.sent_map;
        if (sm && Array.isArray(sm.en_sents)) {
          sm.en_sents.forEach((es, eIdx) => {
            (es.rects || []).forEach(rp => {
              if (!enSentRectsByPage[rp.page]) enSentRectsByPage[rp.page] = [];
              enSentRectsByPage[rp.page].push({
                itemId: it.id,
                enIdx: eIdx,
                zhIndices: es.zh || [],
                rect: rp.rect,
              });
            });
          });
        }
      });
      (zh.regions || []).forEach(r => {
        if (!regionsByPage[r.page]) regionsByPage[r.page] = [];
        regionsByPage[r.page].push(r);
        if (!regionsById[r.id]) regionsById[r.id] = [];
        regionsById[r.id].push(r);
      });
    }

    function hitTestSentence(page, nx, ny) {
      const list = enSentRectsByPage[page] || [];
      if (!list.length) return null;
      let best = null;
      let bestDist = 0.006;
      for (const s of list) {
        const [x0, y0, x1, y1] = s.rect;
        if (nx >= x0 - 0.004 && nx <= x1 + 0.004 && ny >= y0 - 0.002 && ny <= y1 + 0.002) {
          return s;
        }
        if (nx >= x0 - 0.01 && nx <= x1 + 0.01) {
          const dy = ny < y0 ? (y0 - ny) : (ny > y1 ? (ny - y1) : 0);
          if (dy < bestDist) {
            bestDist = dy;
            best = s;
          }
        }
      }
      return best;
    }

    function getEnRectsFor(itemId, enIndices) {
      const it = itemById[itemId];
      if (!it || !it.sent_map || !Array.isArray(it.sent_map.en_sents)) return [];
      const out = [];
      enIndices.forEach(eIdx => {
        const es = it.sent_map.en_sents[eIdx];
        if (es && Array.isArray(es.rects)) {
          es.rects.forEach(rp => out.push(rp));
        }
      });
      return out;
    }

    function hitTestRegion(page, nx, ny) {
      const list = regionsByPage[page] || [];
      if (!list.length) return null;
      let exact = null;
      let exactArea = Infinity;
      for (const r of list) {
        const [x0, y0, x1, y1] = r.rect;
        if (nx >= x0 && nx <= x1 && ny >= y0 && ny <= y1) {
          const area = (x1 - x0) * (y1 - y0);
          if (area < exactArea) {
            exact = r;
            exactArea = area;
          }
        }
      }
      if (exact) return exact;

      let nearest = null;
      let bestDist = 0.045;
      for (const r of list) {
        const [x0, y0, x1, y1] = r.rect;
        const dx = nx < x0 ? (x0 - nx) : (nx > x1 ? (nx - x1) : 0);
        const dy = ny < y0 ? (y0 - ny) : (ny > y1 ? (ny - y1) : 0);
        const dist = Math.hypot(dx * 1.4, dy);
        if (dist < bestDist) {
          bestDist = dist;
          nearest = r;
        }
      }
      return nearest;
    }

    function indexMetaLinks(meta) {
      pdfLinksByPage = {};
      if (!meta || !Array.isArray(meta.links)) return;
      meta.links.forEach((arr, idx) => {
        if (Array.isArray(arr) && arr.length) {
          pdfLinksByPage[idx + 1] = arr;
        }
      });
    }

    function hitTestPdfLink(page, nx, ny) {
      const list = pdfLinksByPage[page];
      if (!list || !list.length) return null;
      const padX = 0.0025;
      const padY = 0.0018;
      let best = null;
      let bestArea = Infinity;
      for (const lnk of list) {
        const [x0, y0, x1, y1] = lnk.r;
        if (nx >= x0 - padX && nx <= x1 + padX && ny >= y0 - padY && ny <= y1 + padY) {
          const area = (x1 - x0) * (y1 - y0);
          if (area < bestArea) {
            best = lnk;
            bestArea = area;
          }
        }
      }
      return best;
    }

    function formatPdfLinkLabel(lnk) {
      if (!lnk) return '';
      if (lnk.t === 'uri') {
        return `🔗 打开链接 ${lnk.u}`;
      }
      const d = lnk.d || '';
      const pg = `P${lnk.p}`;
      if (d.startsWith('appendix.')) return `🔗 跳至 Appendix ${d.slice(9)} (${pg})`;
      if (d.startsWith('section.')) return `🔗 跳至 Section ${d.slice(8)} (${pg})`;
      if (d.startsWith('subsection.')) return `🔗 跳至 Section ${d.slice(11)} (${pg})`;
      if (d.startsWith('subsubsection.')) return `🔗 跳至 Section ${d.slice(14)} (${pg})`;
      if (d.startsWith('equation.')) return `🔗 跳至 Equation (${d.slice(9)}) (${pg})`;
      if (d.startsWith('proposition.')) return `🔗 跳至 Proposition ${d.slice(12)} (${pg})`;
      if (d.startsWith('theorem.')) return `🔗 跳至 Theorem ${d.slice(8)} (${pg})`;
      if (d.startsWith('lemma.')) return `🔗 跳至 Lemma ${d.slice(6)} (${pg})`;
      if (d.startsWith('table.')) return `🔗 跳至 Table (${pg})`;
      if (d.startsWith('figure.')) return `🔗 跳至 Figure (${pg})`;
      if (d.startsWith('cite.')) return `🔗 跳至参考文献 ${d.slice(5)} (${pg})`;
      return `🔗 跳至第 ${lnk.p} 页`;
    }

    function updateBackJumpBtn() {
      if (!backJumpBtn) return;
      if (jumpHistory.length > 0) {
        const last = jumpHistory[jumpHistory.length - 1];
        backJumpBtn.disabled = false;
        backJumpBtn.classList.add('active');
        backJumpBtn.textContent = `↩ 返回 P${last.page}`;
        backJumpBtn.title = `返回第 ${last.page} 页原位置 (Alt+←，共 ${jumpHistory.length} 步历史)`;
      } else {
        backJumpBtn.disabled = true;
        backJumpBtn.classList.remove('active');
        backJumpBtn.textContent = '↩ 返回';
        backJumpBtn.title = '返回跳转前的位置 (Alt+←)';
      }
    }

    function pushJumpHistory() {
      jumpHistory.push({
        scrollTop: viewer.scrollTop,
        scrollLeft: viewer.scrollLeft,
        page: currentPage,
        zhScroll: zhList ? zhList.scrollTop : 0,
        selectedZhId,
        activeSent: activeSent ? {
          itemId: activeSent.itemId,
          zhIndices: [...activeSent.zhIndices],
          enIndices: [...activeSent.enIndices],
        } : null,
      });
      if (jumpHistory.length > 30) jumpHistory.shift();
      updateBackJumpBtn();
    }

    function flashLinkTarget(pageNum, nx, ny) {
      const pageCard = viewer.querySelector(`.page-card[data-page="${pageNum}"]`);
      if (!pageCard) return;
      const ov = pageCard.querySelector('.page-overlay');
      if (!ov) return;
      const { flashLayer } = ensureOverlayLayers(ov);
      const ring = document.createElement('div');
      ring.className = 'pdf-link-target-ring';
      ring.style.left = (Math.max(0.03, Math.min(0.97, nx || 0.12)) * 100).toFixed(3) + '%';
      ring.style.top = (Math.max(0.03, Math.min(0.97, ny || 0.08)) * 100).toFixed(3) + '%';
      flashLayer.appendChild(ring);
      setTimeout(() => { ring.remove(); }, 1350);
    }

    function jumpToPdfLink(lnk) {
      if (!lnk) return;
      if (lnk.t === 'uri' && lnk.u) {
        window.open(lnk.u, '_blank', 'noopener,noreferrer');
        return;
      }
      if (lnk.t !== 'goto' || !lnk.p) return;
      pushJumpHistory();
      const targetPage = Math.max(1, Math.min(currentMeta ? currentMeta.page_count : lnk.p, Number(lnk.p)));
      const pageCard = viewer.querySelector(`.page-card[data-page="${targetPage}"]`);
      suppressNextAutoFollow = true;
      currentPage = targetPage;
      pageInput.value = targetPage;
      if (pageCard) {
        const cardRect = pageCard.getBoundingClientRect();
        const vRect = viewer.getBoundingClientRect();
        const cardTopInViewer = cardRect.top - vRect.top + viewer.scrollTop;
        const cardLeftInViewer = cardRect.left - vRect.left + viewer.scrollLeft;
        const destY = cardTopInViewer + (lnk.y || 0) * pageCard.clientHeight - Math.max(36, viewer.clientHeight * 0.12);
        const destX = viewer.scrollWidth > viewer.clientWidth + 4
          ? Math.max(0, cardLeftInViewer + (lnk.x || 0) * pageCard.clientWidth - viewer.clientWidth * 0.18)
          : 0;
        viewer.scrollTo({ left: destX, top: Math.max(0, destY), behavior: 'smooth' });
        flashLinkTarget(targetPage, lnk.x, lnk.y);
      } else {
        jumpToPage(targetPage);
      }
      highlightZhCards(true);
      showToast(formatPdfLinkLabel(lnk) + '（点「↩ 返回」或按 Alt+← 可回到原处）');
      setTimeout(() => { suppressNextAutoFollow = false; }, 750);
    }

    function goBackJump() {
      if (!jumpHistory.length) return;
      const prev = jumpHistory.pop();
      updateBackJumpBtn();
      suppressNextAutoFollow = true;
      currentPage = prev.page;
      pageInput.value = prev.page;
      selectedZhId = prev.selectedZhId;
      activeSent = prev.activeSent;
      viewer.scrollTo({ left: prev.scrollLeft, top: prev.scrollTop, behavior: 'smooth' });
      renderPdfOverlays();
      highlightZhCards(false);
      updateZhSentenceClasses();
      if (zhList && typeof prev.zhScroll === 'number') {
        zhList.scrollTo({ top: prev.zhScroll, behavior: 'smooth' });
      }
      showToast(`已返回第 ${prev.page} 页原位置`);
      setTimeout(() => { suppressNextAutoFollow = false; }, 750);
    }

    function renderPdfLinkHoverOverlay() {
      viewer.querySelectorAll('.page-card').forEach(card => {
        const pg = Number(card.dataset.page);
        const ov = card.querySelector('.page-overlay');
        if (!ov) return;
        const { linkLayer } = ensureOverlayLayers(ov);
        linkLayer.innerHTML = '';
        if (hoveredPdfLink && hoveredPdfLink.page === pg && hoveredPdfLink.link) {
          const [x0, y0, x1, y1] = hoveredPdfLink.link.r;
          const box = document.createElement('div');
          box.className = 'pdf-link-hover-box';
          box.style.left = (Math.max(0, x0 - 0.0015) * 100).toFixed(3) + '%';
          box.style.top = (Math.max(0, y0 - 0.001) * 100).toFixed(3) + '%';
          box.style.width = (Math.min(1, x1 - x0 + 0.003) * 100).toFixed(3) + '%';
          box.style.height = (Math.min(1, y1 - y0 + 0.002) * 100).toFixed(3) + '%';
          linkLayer.appendChild(box);
        }
      });
    }

    function ensureOverlayLayers(ov) {
      let flashLayer = ov.querySelector('.ov-flash');
      if (!flashLayer) {
        const lockLayer = document.createElement('div');
        lockLayer.className = 'ov-lock';
        flashLayer = document.createElement('div');
        flashLayer.className = 'ov-flash';
        const hoverLayer = document.createElement('div');
        hoverLayer.className = 'ov-hover';
        const activeLayer = document.createElement('div');
        activeLayer.className = 'ov-active';
        const linkLayer = document.createElement('div');
        linkLayer.className = 'ov-link';
        ov.appendChild(lockLayer);
        ov.appendChild(flashLayer);
        ov.appendChild(hoverLayer);
        ov.appendChild(activeLayer);
        ov.appendChild(linkLayer);
      }
      return {
        lockLayer: ov.querySelector('.ov-lock'),
        flashLayer: ov.querySelector('.ov-flash'),
        hoverLayer: ov.querySelector('.ov-hover'),
        activeLayer: ov.querySelector('.ov-active'),
        linkLayer: ov.querySelector('.ov-link'),
      };
    }

    function renderPdfLockOverlays() {
      viewer.querySelectorAll('.page-card').forEach(card => {
        const pg = Number(card.dataset.page);
        const ov = card.querySelector('.page-overlay');
        if (!ov) return;
        const { lockLayer } = ensureOverlayLayers(ov);
        lockLayer.innerHTML = '';
        if (!showPdfLockMarks || lockedSet.size === 0) return;
        const regs = regionsByPage[pg] || [];
        regs.forEach(r => {
          if (!lockedSet.has(r.id)) return;
          const rail = document.createElement('div');
          rail.className = 'pdf-lock-rail';
          rail.style.left = Math.max(0, (r.rect[0] * 100 - 0.6)) + '%';
          rail.style.top = (r.rect[1] * 100) + '%';
          rail.style.height = ((r.rect[3] - r.rect[1]) * 100) + '%';
          lockLayer.appendChild(rail);
        });
      });
    }

    function flashPdfBlock(itemId) {
      if (flashTimer) clearTimeout(flashTimer);
      flashingItemId = itemId;
      viewer.querySelectorAll('.page-card').forEach(card => {
        const pg = Number(card.dataset.page);
        const ov = card.querySelector('.page-overlay');
        if (!ov) return;
        const { flashLayer } = ensureOverlayLayers(ov);
        flashLayer.innerHTML = '';
        if (itemId && regionsById[itemId]) {
          regionsById[itemId].forEach(r => {
            if (r.page !== pg) return;
            const box = document.createElement('div');
            box.className = 'pdf-flash-box';
            box.style.left = (r.rect[0] * 100) + '%';
            box.style.top = (r.rect[1] * 100) + '%';
            box.style.width = ((r.rect[2] - r.rect[0]) * 100) + '%';
            box.style.height = ((r.rect[3] - r.rect[1]) * 100) + '%';
            flashLayer.appendChild(box);
          });
        }
      });
      flashTimer = setTimeout(() => {
        flashingItemId = null;
        viewer.querySelectorAll('.page-overlay .ov-flash').forEach(el => { el.innerHTML = ''; });
      }, 1150);
    }

    function renderPdfHoverOverlays() {
      const hoverRects = (hoverSent && hoverSent.enIndices.length) ? getEnRectsFor(hoverSent.itemId, hoverSent.enIndices) : [];
      viewer.querySelectorAll('.page-card').forEach(card => {
        const pg = Number(card.dataset.page);
        const ov = card.querySelector('.page-overlay');
        if (!ov) return;
        const { hoverLayer } = ensureOverlayLayers(ov);
        hoverLayer.innerHTML = '';
        hoverRects.forEach(rp => {
          if (rp.page !== pg) return;
          const sbox = document.createElement('div');
          sbox.className = 'pdf-sent-hover-box';
          sbox.style.left = (rp.rect[0] * 100) + '%';
          sbox.style.top = (rp.rect[1] * 100) + '%';
          sbox.style.width = ((rp.rect[2] - rp.rect[0]) * 100) + '%';
          sbox.style.height = ((rp.rect[3] - rp.rect[1]) * 100) + '%';
          hoverLayer.appendChild(sbox);
        });
      });
    }

    function renderPdfActiveOverlays() {
      const hasActiveSent = !!(activeSent && activeSent.itemId === selectedZhId && activeSent.enIndices.length);
      const activeRects = hasActiveSent ? getEnRectsFor(activeSent.itemId, activeSent.enIndices) : [];
      viewer.querySelectorAll('.page-card').forEach(card => {
        const pg = Number(card.dataset.page);
        const ov = card.querySelector('.page-overlay');
        if (!ov) return;
        const { activeLayer } = ensureOverlayLayers(ov);
        activeLayer.innerHTML = '';
        activeRects.forEach(rp => {
          if (rp.page !== pg) return;
          const sbox = document.createElement('div');
          sbox.className = 'pdf-sent-hl-box';
          sbox.style.left = (rp.rect[0] * 100) + '%';
          sbox.style.top = (rp.rect[1] * 100) + '%';
          sbox.style.width = ((rp.rect[2] - rp.rect[0]) * 100) + '%';
          sbox.style.height = ((rp.rect[3] - rp.rect[1]) * 100) + '%';
          activeLayer.appendChild(sbox);
        });
      });
    }

    function renderPdfOverlays() {
      renderPdfLockOverlays();
      renderPdfHoverOverlays();
      renderPdfActiveOverlays();
    }

    async function postLockAction(action, ids = [], mode = null) {
      try {
        const payload = { file: currentFile, action, ids };
        if (mode) payload.mode = mode;
        const res = await fetch('/api/locks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
          showToast('锁定更新失败: ' + (data.error || res.status));
          return;
        }
        lockedSet = new Set(data.locked_ids || []);
        lockMode = data.mode === 'force_ask' ? 'force_ask' : 'deny';
        if (currentZh) {
          currentZh.locked_ids = data.locked_ids || [];
          currentZh.lock_mode = lockMode;
          currentZh.locks_mtime = data.locks_mtime || Date.now() / 1000;
          (currentZh.items || []).forEach(it => { it.locked = lockedSet.has(it.id); });
        }
        renderLockControls();
        renderZhPane(true);
        renderPdfLockOverlays();
        if (action === 'set_mode') {
          showToast(lockMode === 'deny' ? '已切换为：硬锁禁改模式（Agent 绝对无法修改锁定段）' : '已切换为：强询问模式（Agent 碰触锁定段时强制弹窗确认）');
        } else {
          const n = lockedSet.size;
          showToast(`已更新锁定状态（当前共锁定 ${n} 处）`);
        }
      } catch (e) {
        console.error(e);
        showToast('锁定请求异常');
      }
    }

    function renderLockControls() {
      const nLocked = lockedSet.size;
      if (lockMenuBtn) {
        lockMenuBtn.textContent = `🔒 锁定管理 (${nLocked}) ▾`;
        lockMenuBtn.classList.toggle('active', nLocked > 0);
      }
      if (filterLockedBtn) {
        filterLockedBtn.textContent = `仅已锁 (${nLocked})`;
      }
      if (lockModeStatus) {
        lockModeStatus.textContent = lockMode === 'deny' ? '硬锁禁改' : '弹窗确认';
      }
      if (lockModeDenyBtn) lockModeDenyBtn.classList.toggle('active', lockMode === 'deny');
      if (lockModeAskBtn) lockModeAskBtn.classList.toggle('active', lockMode === 'force_ask');
      if (pdfLockMarkBtn) pdfLockMarkBtn.classList.toggle('active', showPdfLockMarks);

      if (!lockSecList || !currentZh || !currentZh.available) return;
      const groups = [];
      const gMap = {};
      (currentZh.items || []).forEach(it => {
        const g = it.section_group || '其他';
        if (!gMap[g]) {
          gMap[g] = [];
          groups.push(g);
        }
        gMap[g].push(it.id);
      });
      lockSecList.innerHTML = '';
      groups.forEach(g => {
        const ids = gMap[g];
        const cnt = ids.filter(id => lockedSet.has(id)).length;
        const allLocked = cnt === ids.length && ids.length > 0;
        const row = document.createElement('div');
        row.className = 'lock-sec-row';
        const left = document.createElement('div');
        left.innerHTML = `<span class="sec-name"></span><span class="sec-cnt">${cnt}/${ids.length}</span>`;
        left.querySelector('.sec-name').textContent = g;
        const btn = document.createElement('button');
        btn.className = allLocked ? 'all-locked' : '';
        btn.textContent = allLocked ? '🔓 解锁整节' : '🔒 锁整节';
        btn.onclick = (e) => {
          e.stopPropagation();
          postLockAction(allLocked ? 'unlock' : 'lock', ids);
        };
        row.appendChild(left);
        row.appendChild(btn);
        lockSecList.appendChild(row);
      });
    }

    function updateZhSentenceClasses() {
      zhList.querySelectorAll('.zh-card').forEach(card => {
        const cid = card.dataset.id;
        const it = itemById[cid];
        const prevEl = card.querySelector('.zh-en-preview');
        const isActCard = activeSent && activeSent.itemId === cid && activeSent.enIndices.length > 0;
        const isHovCard = hoverSent && hoverSent.itemId === cid && hoverSent.enIndices.length > 0;

        if (prevEl && it) {
          if (isActCard && it.sent_map && Array.isArray(it.sent_map.en_sents)) {
            const enTxt = activeSent.enIndices
              .map(i => it.sent_map.en_sents[i]?.text || '')
              .filter(Boolean)
              .join(' ');
            if (enTxt && prevEl.textContent !== enTxt) {
              prevEl.textContent = enTxt;
            }
            prevEl.classList.add('is-sent-preview');
          } else {
            const defTxt = it.en_preview || '';
            if (prevEl.textContent !== defTxt) {
              prevEl.textContent = defTxt;
            }
            prevEl.classList.remove('is-sent-preview');
          }
        }

        card.querySelectorAll('.zh-sent').forEach(sp => {
          const sIdx = Number(sp.dataset.sidx);
          const isAct = isActCard && activeSent.zhIndices.includes(sIdx);
          const isHov = !isAct && isHovCard && hoverSent.zhIndices.includes(sIdx);
          sp.classList.toggle('is-active', !!isAct);
          sp.classList.toggle('is-hover', !!isHov);
        });
      });
    }

    function jumpToPdfRects(rects, fallbackItemId, fallbackPage) {
      if (!rects || !rects.length) {
        jumpToPdfItem(fallbackItemId, fallbackPage);
        return;
      }
      const primary = rects[0];
      const pageCard = viewer.querySelector(`.page-card[data-page="${primary.page}"]`);
      if (!pageCard) {
        jumpToPage(primary.page);
        renderPdfOverlays();
        return;
      }
      currentPage = primary.page;
      pageInput.value = primary.page;
      const cardRect = pageCard.getBoundingClientRect();
      const vRect = viewer.getBoundingClientRect();
      const cardTopInViewer = cardRect.top - vRect.top + viewer.scrollTop;
      const cardLeftInViewer = cardRect.left - vRect.left + viewer.scrollLeft;

      const sentTopPx = cardTopInViewer + primary.rect[1] * pageCard.clientHeight;
      const sentBotPx = cardTopInViewer + primary.rect[3] * pageCard.clientHeight;
      const visTop = viewer.scrollTop + 48;
      const visBot = viewer.scrollTop + viewer.clientHeight - 48;

      let targetY = viewer.scrollTop;
      if (sentTopPx < visTop || sentBotPx > visBot) {
        targetY = Math.max(0, sentTopPx - viewer.clientHeight * 0.30);
      }

      const midX = cardLeftInViewer + ((primary.rect[0] + primary.rect[2]) / 2) * pageCard.clientWidth;
      const targetX = viewer.scrollWidth > viewer.clientWidth + 4 ? Math.max(0, midX - viewer.clientWidth / 2) : 0;
      viewer.scrollTo({ left: targetX, top: targetY, behavior: 'smooth' });
      renderPdfOverlays();
    }

    function jumpToZhItem(itemId, flash = true) {
      const item = itemById[itemId];
      if (!item) return;
      selectedZhId = itemId;

      if (!zhVisible) {
        zhVisible = true;
        applyLayoutState();
      }

      let needRerender = false;
      if (zhFilterInput.value.trim()) {
        zhFilterInput.value = '';
        needRerender = true;
      }
      if (zhFilterMode === 'todo' && !item.is_todo) {
        zhFilterMode = 'all';
        localStorage.setItem('paper_zh_filter', 'all');
        needRerender = true;
      } else if (zhFilterMode === 'page' && Number(item.page) !== currentPage) {
        zhFilterMode = 'all';
        localStorage.setItem('paper_zh_filter', 'all');
        needRerender = true;
      }

      if (needRerender) {
        renderZhPane(false);
      } else {
        highlightZhCards(false);
      }

      renderPdfOverlays();

      requestAnimationFrame(() => {
        const card = zhList.querySelector(`.zh-card[data-id="${CSS.escape(itemId)}"]`);
        if (card) {
          card.scrollIntoView({ behavior: 'smooth', block: 'center' });
          if (flash) {
            card.classList.remove('flash-ring');
            void card.offsetWidth;
            card.classList.add('flash-ring');
          }
        }
      });
    }

    function jumpToPdfItem(itemId, fallbackPage) {
      const regs = regionsById[itemId];
      if (!regs || !regs.length) {
        jumpToPage(fallbackPage);
        renderPdfOverlays();
        return;
      }
      const primary = regs[0];
      const pageCard = viewer.querySelector(`.page-card[data-page="${primary.page}"]`);
      if (!pageCard) {
        jumpToPage(primary.page);
        renderPdfOverlays();
        return;
      }
      currentPage = primary.page;
      pageInput.value = primary.page;
      const cardRect = pageCard.getBoundingClientRect();
      const vRect = viewer.getBoundingClientRect();
      const cardTopInViewer = cardRect.top - vRect.top + viewer.scrollTop;
      const cardLeftInViewer = cardRect.left - vRect.left + viewer.scrollLeft;
      const targetY = cardTopInViewer + primary.rect[1] * pageCard.clientHeight - Math.max(40, viewer.clientHeight * 0.18);
      const midX = cardLeftInViewer + ((primary.rect[0] + primary.rect[2]) / 2) * pageCard.clientWidth;
      const targetX = viewer.scrollWidth > viewer.clientWidth + 4 ? Math.max(0, midX - viewer.clientWidth / 2) : 0;
      viewer.scrollTo({ left: targetX, top: Math.max(0, targetY), behavior: 'smooth' });
      renderPdfOverlays();
    }

    function applyLayoutState() {
      zhPane.classList.toggle('hidden', !zhVisible);
      splitter.classList.toggle('hidden', !zhVisible);
      zhToggleBtn.classList.toggle('active', zhVisible);

      splitContainer.classList.toggle('dir-row', splitDir === 'row');
      splitContainer.classList.toggle('dir-col', splitDir === 'col');
      splitDirBtn.textContent = splitDir === 'row' ? '左右分栏' : '上下分栏';
      splitDirBtn.style.display = zhVisible ? 'inline-flex' : 'none';

      localStorage.setItem('paper_zh_visible', zhVisible ? '1' : '0');
      localStorage.setItem('paper_split_dir', splitDir);
      requestAnimationFrame(() => { if (zoomMode === 'fit') applyZoom(); });
    }

    function computePageWidth() {
      const avail = Math.max(240, viewer.clientWidth - 36);
      if (zoomMode === 'fit') return Math.min(avail, 960);
      return Math.round(760 * zoomScale);
    }

    function applyZoom() {
      const w = computePageWidth();
      document.querySelectorAll('.page-card').forEach(card => {
        card.style.width = w + 'px';
        const pw = Number(card.dataset.pw) || 612;
        card.style.setProperty('--page-scale', (w / pw).toFixed(5));
      });
      zoomLabel.textContent = zoomMode === 'fit' ? '适应' : Math.round(zoomScale * 100) + '%';
      fitBtn.classList.toggle('active', zoomMode === 'fit');
    }

    function setZoomAround(newScale, clientX, clientY) {
      const clamped = Math.max(0.45, Math.min(5.0, newScale));
      const rect = viewer.getBoundingClientRect();
      const relX = (clientX !== undefined ? clientX - rect.left : viewer.clientWidth / 2);
      const relY = (clientY !== undefined ? clientY - rect.top : viewer.clientHeight / 2);
      const oldW = Math.max(1, viewer.scrollWidth);
      const oldH = Math.max(1, viewer.scrollHeight);
      const ratioX = (viewer.scrollLeft + relX) / oldW;
      const ratioY = (viewer.scrollTop + relY) / oldH;

      zoomScale = clamped;
      zoomMode = 'manual';
      applyZoom();

      viewer.scrollLeft = Math.max(0, ratioX * viewer.scrollWidth - relX);
      viewer.scrollTop = Math.max(0, ratioY * viewer.scrollHeight - relY);
    }

    let panState = null;
    let suppressNextPdfClick = false;

    function attachPageInteraction(card, pageNum) {
      card.addEventListener('mousemove', (e) => {
        if (panState && panState.moved) return;
        const rect = card.getBoundingClientRect();
        const nx = (e.clientX - rect.left) / rect.width;
        const ny = (e.clientY - rect.top) / rect.height;

        const linkHit = (!panMode && !spaceHeld) ? hitTestPdfLink(pageNum, nx, ny) : null;
        if (linkHit) {
          card.classList.add('has-link-hover');
          card.classList.add('has-hover');
          const changedLink = !hoveredPdfLink || hoveredPdfLink.page !== pageNum || hoveredPdfLink.link !== linkHit;
          if (changedLink) {
            hoveredPdfLink = { page: pageNum, link: linkHit };
            renderPdfLinkHoverOverlay();
          }
          hoverPill.textContent = formatPdfLinkLabel(linkHit);
          hoverPill.style.display = 'block';
          const px = Math.min(window.innerWidth - 290, e.clientX + 14);
          const py = Math.min(window.innerHeight - 32, e.clientY + 14);
          hoverPill.style.left = px + 'px';
          hoverPill.style.top = py + 'px';
          return;
        }
        if (hoveredPdfLink !== null) {
          card.classList.remove('has-link-hover');
          hoveredPdfLink = null;
          renderPdfLinkHoverOverlay();
        } else {
          card.classList.remove('has-link-hover');
        }

        const sentHit = hitTestSentence(pageNum, nx, ny);
        const hit = sentHit ? { id: sentHit.itemId } : hitTestRegion(pageNum, nx, ny);
        if (hit && itemById[hit.id]) {
          const it = itemById[hit.id];
          card.classList.add('has-hover');
          let needOverlay = false;
          if (hoveredItemId !== hit.id || hoveredPage !== pageNum) {
            hoveredItemId = hit.id;
            hoveredPage = pageNum;
          }
          if (sentHit) {
            if (!hoverSent || hoverSent.itemId !== sentHit.itemId || hoverSent.enIndices[0] !== sentHit.enIdx) {
              hoverSent = { itemId: sentHit.itemId, zhIndices: sentHit.zhIndices || [], enIndices: [sentHit.enIdx] };
              needOverlay = true;
              updateZhSentenceClasses();
            }
            hoverPill.textContent = `${it.id} · 第 ${sentHit.enIdx + 1} 句`;
          } else {
            if (hoverSent !== null) {
              hoverSent = null;
              needOverlay = true;
              updateZhSentenceClasses();
            }
            hoverPill.textContent = `${it.id} · ${it.title}`;
          }
          if (needOverlay) renderPdfHoverOverlays();
          hoverPill.style.display = 'block';
          const px = Math.min(window.innerWidth - 290, e.clientX + 14);
          const py = Math.min(window.innerHeight - 32, e.clientY + 14);
          hoverPill.style.left = px + 'px';
          hoverPill.style.top = py + 'px';
        } else {
          card.classList.remove('has-hover');
          hoverPill.style.display = 'none';
          hoveredItemId = null;
          hoveredPage = null;
          if (hoverSent !== null) {
            hoverSent = null;
            renderPdfHoverOverlays();
            updateZhSentenceClasses();
          }
        }
      });

      card.addEventListener('mouseleave', () => {
        card.classList.remove('has-hover');
        card.classList.remove('has-link-hover');
        hoverPill.style.display = 'none';
        hoveredItemId = null;
        hoveredPage = null;
        if (hoveredPdfLink !== null) {
          hoveredPdfLink = null;
          renderPdfLinkHoverOverlay();
        }
        if (hoverSent !== null) {
          hoverSent = null;
          renderPdfHoverOverlays();
          updateZhSentenceClasses();
        }
      });

      card.addEventListener('click', (e) => {
        if (suppressNextPdfClick) return;
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed && sel.toString().trim().length > 0) return;
        const rect = card.getBoundingClientRect();
        const nx = (e.clientX - rect.left) / rect.width;
        const ny = (e.clientY - rect.top) / rect.height;

        if (!panMode && !spaceHeld) {
          const linkHit = hitTestPdfLink(pageNum, nx, ny);
          if (linkHit) {
            e.preventDefault();
            e.stopPropagation();
            hoveredPdfLink = null;
            renderPdfLinkHoverOverlay();
            hoverPill.style.display = 'none';
            jumpToPdfLink(linkHit);
            return;
          }
        }

        const sentHit = hitTestSentence(pageNum, nx, ny);
        const hit = sentHit ? { id: sentHit.itemId } : hitTestRegion(pageNum, nx, ny);
        if (hit && itemById[hit.id]) {
          currentPage = pageNum;
          pageInput.value = pageNum;
          if (sentHit) {
            activeSent = {
              itemId: sentHit.itemId,
              zhIndices: sentHit.zhIndices || [],
              enIndices: [sentHit.enIdx],
            };
          } else {
            activeSent = null;
            flashPdfBlock(hit.id);
          }
          jumpToZhItem(hit.id, true);
          requestAnimationFrame(() => {
            updateZhSentenceClasses();
            if (sentHit && sentHit.zhIndices && sentHit.zhIndices.length) {
              const targetSpan = zhList.querySelector(
                `.zh-card[data-id="${CSS.escape(hit.id)}"] .zh-sent[data-sidx="${sentHit.zhIndices[0]}"]`
              );
              if (targetSpan) targetSpan.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
          });
        }
      });
    }

    const _measureCanvas = document.createElement('canvas');
    const _measureCtx = _measureCanvas.getContext('2d');

    function populateTextLayers(textData) {
      if (!textData || !Array.isArray(textData.pages)) return;
      viewer.querySelectorAll('.page-card').forEach(card => {
        const pIdx = Number(card.dataset.page) - 1;
        const tl = card.querySelector('.page-text-layer');
        const pInfo = textData.pages[pIdx];
        if (!tl || !pInfo) return;
        tl.innerHTML = '';
        const pw = pInfo.w || 612;
        const ph = pInfo.h || 792;
        const frag = document.createDocumentFragment();
        (pInfo.runs || []).forEach(run => {
          const [x0, y0, rw, rh, txt, sep] = run;
          if (!txt) return;
          const fsPt = Math.max(4.5, rh * 0.86);
          _measureCtx.font = `${fsPt.toFixed(2)}px "Times New Roman", Times, Georgia, serif`;
          const natW = Math.max(1.0, _measureCtx.measureText(txt).width);
          const sx = Math.max(0.55, Math.min(1.65, rw / natW));

          const sp = document.createElement('span');
          sp.className = 'pdf-tl-span';
          sp.dataset.nx = ((x0 + rw * 0.5) / pw).toFixed(4);
          sp.dataset.ny = ((y0 + rh * 0.5) / ph).toFixed(4);
          sp.style.left = ((x0 / pw) * 100).toFixed(3) + '%';
          sp.style.top = ((y0 / ph) * 100).toFixed(3) + '%';
          sp.style.fontSize = `calc(var(--page-scale) * ${fsPt.toFixed(2)}px)`;
          sp.style.transform = `scaleX(${sx.toFixed(4)})`;
          sp.textContent = txt;
          frag.appendChild(sp);
          if (sep) frag.appendChild(document.createTextNode(sep));
        });
        tl.appendChild(frag);
      });
    }

    async function loadTextLayer() {
      try {
        const url = `/api/text${currentFile ? '?file=' + encodeURIComponent(currentFile) : ''}`;
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        populateTextLayers(data);
      } catch (e) {
        console.error(e);
      }
    }

    function buildPages(meta, preserveScroll = false) {
      const prevScrollTop = viewer.scrollTop;
      const prevScrollLeft = viewer.scrollLeft;
      const prevScrollRatio = viewer.scrollHeight > 0 ? prevScrollTop / viewer.scrollHeight : 0;
      const w = computePageWidth();

      viewerInner.innerHTML = '';
      for (let i = 0; i < meta.page_count; i++) {
        const p = meta.pages[i] || { w: 612, h: 792 };
        const card = document.createElement('div');
        card.className = 'page-card';
        card.dataset.page = String(i + 1);
        card.dataset.pw = String(p.w);
        card.style.width = w + 'px';
        card.style.setProperty('--page-scale', (w / p.w).toFixed(5));
        card.style.aspectRatio = `${p.w} / ${p.h}`;

        const img = document.createElement('img');
        img.loading = i < 3 ? 'eager' : 'lazy';
        img.draggable = false;
        img.alt = `Page ${i + 1}`;
        img.src = `/api/page/${i}.svg?file=${encodeURIComponent(meta.file)}&v=${encodeURIComponent(meta.mtime)}`;

        const overlay = document.createElement('div');
        overlay.className = 'page-overlay';

        const textLayer = document.createElement('div');
        textLayer.className = 'page-text-layer';

        const badge = document.createElement('div');
        badge.className = 'page-badge';
        badge.textContent = `${i + 1} / ${meta.page_count}`;

        card.appendChild(img);
        card.appendChild(overlay);
        card.appendChild(textLayer);
        card.appendChild(badge);
        attachPageInteraction(card, i + 1);
        viewerInner.appendChild(card);
      }

      renderPdfOverlays();
      loadTextLayer();

      if (preserveScroll) {
        requestAnimationFrame(() => {
          viewer.scrollTop = Math.round(prevScrollRatio * viewer.scrollHeight);
          viewer.scrollLeft = prevScrollLeft;
        });
      }
    }

    function syncFileDropdown(meta) {
      fileSelect.innerHTML = '';
      meta.files.forEach(f => {
        const opt = document.createElement('option');
        opt.value = f;
        opt.textContent = f;
        if (f === meta.file) opt.selected = true;
        fileSelect.appendChild(opt);
      });
      currentFile = meta.file;
      document.title = `${meta.file} — 中英对照矢量预览`;
    }

    function renderZhPane(preserveScroll = true) {
      const prevScroll = zhList.scrollTop;
      filterAllBtn.classList.toggle('active', zhFilterMode === 'all');
      filterPageBtn.classList.toggle('active', zhFilterMode === 'page');
      if (filterLockedBtn) filterLockedBtn.classList.toggle('active', zhFilterMode === 'locked');
      filterTodoBtn.classList.toggle('active', zhFilterMode === 'todo');
      filterPageBtn.textContent = `仅当前页 (P${currentPage})`;
      autoSyncBtn.classList.toggle('active', autoFollow);
      renderLockControls();

      if (!currentZh || !currentZh.available) {
        zhCountPill.textContent = '无';
        zhUpdatedAt.textContent = '未挂载对照';
        zhList.innerHTML = `<div class="zh-empty">${currentZh?.note || '当前 PDF 暂无中文对照 JSON。'}</div>`;
        return;
      }

      zhUpdatedAt.textContent = `同步 ${currentZh.mtime_str}`;
      const q = zhFilterInput.value.trim().toLowerCase();
      const allItems = currentZh.items || [];
      const filtered = allItems.filter(item => {
        if (zhFilterMode === 'page' && Number(item.page) !== currentPage) return false;
        if (zhFilterMode === 'locked' && !lockedSet.has(item.id)) return false;
        if (zhFilterMode === 'todo' && !item.is_todo) return false;
        if (q) {
          const hay = `${item.id} ${item.section} ${item.title} ${item.en_preview} ${item.zh} p${item.page}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });

      zhCountPill.textContent = `${filtered.length}/${allItems.length} 段`;
      if (!filtered.length) {
        zhList.innerHTML = `<div class="zh-empty">当前筛选条件下无匹配段落。</div>`;
        return;
      }

      const frag = document.createDocumentFragment();
      filtered.forEach(item => {
        const isLocked = lockedSet.has(item.id);
        const card = document.createElement('article');
        card.className = 'zh-card';
        card.dataset.id = item.id;
        card.dataset.page = String(item.page);
        if (Number(item.page) === currentPage) card.classList.add('is-current-page');
        if (item.id === selectedZhId) card.classList.add('is-selected');
        if (item.is_todo) card.classList.add('is-todo');
        if (isLocked) card.classList.add('is-locked');

        const metaRow = document.createElement('div');
        metaRow.className = 'zh-card-meta';

        const idBadge = document.createElement('span');
        idBadge.className = 'badge badge-id';
        idBadge.textContent = item.id;
        metaRow.appendChild(idBadge);

        const pgBadge = document.createElement('span');
        pgBadge.className = 'badge badge-page';
        pgBadge.textContent = `P${item.page}`;
        metaRow.appendChild(pgBadge);

        if (item.lines) {
          const lnBadge = document.createElement('span');
          lnBadge.className = 'badge badge-lines';
          lnBadge.textContent = item.lines;
          metaRow.appendChild(lnBadge);
        }

        if (item.is_todo) {
          const todoBadge = document.createElement('span');
          todoBadge.className = 'badge badge-todo';
          todoBadge.textContent = 'TODO';
          metaRow.appendChild(todoBadge);
        }

        const copyBtn = document.createElement('button');
        copyBtn.className = 'copy-id-btn';
        copyBtn.textContent = '复制编号';
        copyBtn.title = `复制 ${item.id} (${item.lines || 'P' + item.page})`;
        copyBtn.onclick = (e) => {
          e.stopPropagation();
          const tag = `${item.id} (${item.lines || 'P' + item.page})`;
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(tag).then(() => showToast(`已复制 ${tag}`)).catch(() => showToast(tag));
          } else {
            showToast(tag);
          }
        };
        metaRow.appendChild(copyBtn);

        const lockBtn = document.createElement('button');
        lockBtn.className = 'lock-card-btn' + (isLocked ? ' is-locked' : '');
        lockBtn.textContent = isLocked ? '🔒 已锁定' : '🔓 锁定';
        lockBtn.title = isLocked ? '点击解锁：解锁后 Agent 才允许修改该段对应的 .tex 源码' : '点击锁定：锁定后 Agent 将被 Hook 拦截，无法擅自修改该段';
        lockBtn.onclick = (e) => {
          e.stopPropagation();
          postLockAction('toggle', [item.id]);
        };
        metaRow.appendChild(lockBtn);

        const heading = document.createElement('div');
        heading.className = 'zh-card-heading';
        heading.innerHTML = `<span class="sec"></span> · <span class="ttl"></span>`;
        heading.querySelector('.sec').textContent = item.section || '';
        heading.querySelector('.ttl').textContent = item.title || '';

        const enPrev = document.createElement('div');
        enPrev.className = 'zh-en-preview';
        enPrev.textContent = item.en_preview || '';

        const body = document.createElement('div');
        body.className = 'zh-body';
        const sm = item.sent_map;
        if (sm && Array.isArray(sm.zh_tokens) && Array.isArray(sm.en_sents) && sm.en_sents.length > 0) {
          sm.zh_tokens.forEach(tok => {
            if (tok.s === null || tok.s === undefined) {
              body.appendChild(document.createTextNode(tok.t));
            } else {
              const sp = document.createElement('span');
              sp.className = 'zh-sent';
              sp.dataset.itemId = item.id;
              sp.dataset.sidx = String(tok.s);
              sp.textContent = tok.t;
              const mappedEn = (sm.zh_to_en && sm.zh_to_en[tok.s]) ? sm.zh_to_en[tok.s] : [];

              sp.addEventListener('mouseenter', () => {
                if (isMouseDownInZh) return;
                hoverSent = { itemId: item.id, zhIndices: [tok.s], enIndices: mappedEn };
                renderPdfHoverOverlays();
              });
              sp.addEventListener('mouseleave', () => {
                if (isMouseDownInZh) return;
                if (hoverSent && hoverSent.itemId === item.id && hoverSent.zhIndices[0] === tok.s) {
                  hoverSent = null;
                  renderPdfHoverOverlays();
                }
              });
              sp.addEventListener('click', (e) => {
                const sel = window.getSelection();
                if (justDraggedZhSelection || (sel && !sel.isCollapsed && sel.toString().trim().length > 0)) {
                  return;
                }
                e.stopPropagation();
                selectedZhId = item.id;
                suppressNextAutoFollow = true;
                const isSameSingle = activeSent
                  && activeSent.itemId === item.id
                  && activeSent.zhIndices.length === 1
                  && activeSent.zhIndices[0] === tok.s;
                if (isSameSingle) {
                  activeSent = null;
                  renderPdfOverlays();
                } else {
                  activeSent = { itemId: item.id, zhIndices: [tok.s], enIndices: mappedEn };
                  const rects = getEnRectsFor(item.id, mappedEn);
                  jumpToPdfRects(rects, item.id, item.page);
                }
                highlightZhCards(false);
                updateZhSentenceClasses();
                setTimeout(() => { suppressNextAutoFollow = false; }, 700);
              });
              body.appendChild(sp);
            }
          });
        } else {
          body.textContent = item.zh || '';
        }

        card.appendChild(metaRow);
        card.appendChild(heading);
        if (item.en_preview || (sm && sm.en_sents && sm.en_sents.length)) card.appendChild(enPrev);
        card.appendChild(body);

        card.onclick = () => {
          const sel = window.getSelection();
          if (justDraggedZhSelection || (sel && !sel.isCollapsed && sel.toString().trim().length > 0)) {
            return;
          }
          selectedZhId = item.id;
          activeSent = null;
          suppressNextAutoFollow = true;
          jumpToPdfItem(item.id, item.page);
          flashPdfBlock(item.id);
          highlightZhCards(false);
          updateZhSentenceClasses();
          setTimeout(() => { suppressNextAutoFollow = false; }, 700);
        };

        frag.appendChild(card);
      });

      zhList.innerHTML = '';
      zhList.appendChild(frag);
      updateZhSentenceClasses();
      if (preserveScroll) zhList.scrollTop = prevScroll;
    }

    function applyZhTextSelection(commitDomAndScroll = false) {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
      const rawTxt = sel.toString().trim();
      if (!rawTxt) return false;
      const range = sel.getRangeAt(0);
      if (!zhList.contains(range.commonAncestorContainer)) return false;

      const hitSpans = [];
      zhList.querySelectorAll('.zh-sent').forEach(sp => {
        try {
          if (range.intersectsNode(sp)) hitSpans.push(sp);
        } catch (_) {}
      });
      if (!hitSpans.length) return false;

      const targetItemId = hitSpans[0].dataset.itemId;
      const it = itemById[targetItemId];
      if (!it || !it.sent_map) return false;

      const zhSet = new Set();
      const enSet = new Set();
      hitSpans.forEach(sp => {
        if (sp.dataset.itemId !== targetItemId) return;
        const zIdx = Number(sp.dataset.sidx);
        zhSet.add(zIdx);
        const mapped = (it.sent_map.zh_to_en && it.sent_map.zh_to_en[zIdx]) || [];
        mapped.forEach(eIdx => enSet.add(eIdx));
      });
      const zhIndices = Array.from(zhSet).sort((a, b) => a - b);
      const enIndices = Array.from(enSet).sort((a, b) => a - b);
      if (!enIndices.length) return false;

      selectedZhId = targetItemId;
      activeSent = { itemId: targetItemId, zhIndices, enIndices };
      hoverSent = null;
      renderPdfHoverOverlays();
      renderPdfActiveOverlays();

      if (commitDomAndScroll) {
        suppressNextAutoFollow = true;
        highlightZhCards(false);
        updateZhSentenceClasses();
        const rects = getEnRectsFor(targetItemId, enIndices);
        jumpToPdfRects(rects, targetItemId, it.page);
        setTimeout(() => { suppressNextAutoFollow = false; }, 700);
      }
      return true;
    }

    zhList.addEventListener('mousedown', (e) => {
      if (e.button === 0) isMouseDownInZh = true;
    });
    window.addEventListener('mouseup', () => {
      if (!isMouseDownInZh) return;
      isMouseDownInZh = false;
      if (applyZhTextSelection(true)) {
        justDraggedZhSelection = true;
        setTimeout(() => { justDraggedZhSelection = false; }, 180);
      }
    });
    document.addEventListener('selectionchange', () => {
      if (isMouseDownInZh) {
        applyZhTextSelection(false);
      }
    });

    function highlightZhCards(scrollToActive = false) {
      filterPageBtn.textContent = `仅当前页 (P${currentPage})`;
      if (zhFilterMode === 'page') {
        renderZhPane(false);
        return;
      }
      let firstForPage = null;
      zhList.querySelectorAll('.zh-card').forEach(card => {
        const isPg = Number(card.dataset.page) === currentPage;
        card.classList.toggle('is-current-page', isPg);
        card.classList.toggle('is-selected', card.dataset.id === selectedZhId);
        if (isPg && !firstForPage) firstForPage = card;
      });
      if (scrollToActive && autoFollow && !suppressNextAutoFollow && firstForPage) {
        const listRect = zhList.getBoundingClientRect();
        const cardRect = firstForPage.getBoundingClientRect();
        if (cardRect.top < listRect.top || cardRect.top > listRect.bottom - 80) {
          firstForPage.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }
    }

    async function loadMeta(preserveScroll = false, isAuto = false) {
      try {
        const url = `/api/meta${currentFile ? '?file=' + encodeURIComponent(currentFile) : ''}`;
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return;
        const meta = await res.json();
        const changed = !currentMeta || currentMeta.file !== meta.file || currentMeta.mtime !== meta.mtime;
        if (changed) {
          liveDot.classList.add('updating');
          currentMeta = meta;
          indexMetaLinks(meta);
          syncFileDropdown(meta);
          pageCountEl.textContent = meta.page_count;
          pageInput.max = meta.page_count;
          updatedAt.textContent = meta.mtime_str;
          buildPages(meta, preserveScroll);
          if (isAuto) showToast(`已自动刷新 ${meta.file} (${meta.mtime_str})`);
          setTimeout(() => liveDot.classList.remove('updating'), 400);
        }
      } catch (e) {
        console.error(e);
      }
    }

    async function loadZh(isAuto = false) {
      try {
        const url = `/api/zh${currentFile ? '?file=' + encodeURIComponent(currentFile) : ''}`;
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return;
        const zh = await res.json();
        const changed = !currentZh
          || currentZh.mtime !== zh.mtime
          || currentZh.pdf_mtime !== zh.pdf_mtime
          || currentZh.locks_mtime !== zh.locks_mtime
          || currentZh.available !== zh.available
          || currentZh.file !== zh.file;
        if (changed) {
          const textUpdated = currentZh && currentZh.mtime !== zh.mtime;
          currentZh = zh;
          indexZhData(zh);
          renderZhPane(true);
          renderPdfOverlays();
          if (isAuto && textUpdated && zh.available) showToast(`已同步更新中文对照 (${zh.mtime_str})`);
        }
      } catch (e) {
        console.error(e);
      }
    }

    function jumpToPage(n) {
      if (!currentMeta) return;
      const target = Math.max(1, Math.min(currentMeta.page_count, Number(n) || 1));
      currentPage = target;
      const el = viewer.querySelector(`.page-card[data-page="${target}"]`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        pageInput.value = target;
      }
      highlightZhCards(false);
    }

    function updateCurrentPageOnScroll() {
      const cards = viewer.querySelectorAll('.page-card');
      const vTop = viewer.getBoundingClientRect().top + 90;
      let best = 1;
      for (const c of cards) {
        if (c.getBoundingClientRect().top <= vTop) best = Number(c.dataset.page);
        else break;
      }
      if (document.activeElement !== pageInput) {
        pageInput.value = best;
      }
      if (best !== currentPage) {
        currentPage = best;
        highlightZhCards(true);
      }
    }

    async function runSearch() {
      const q = searchInput.value.trim();
      if (!q) {
        searchDrawer.classList.remove('open');
        searchDrawer.innerHTML = '';
        return;
      }
      const res = await fetch(`/api/search?file=${encodeURIComponent(currentFile)}&q=${encodeURIComponent(q)}`);
      const data = await res.json();
      searchDrawer.classList.add('open');
      if (!data.matches.length) {
        searchDrawer.innerHTML = `<div class="muted" style="padding:8px;">未找到 "${q}"</div>`;
        return;
      }
      searchDrawer.innerHTML = `<div class="muted" style="margin-bottom:6px;">共 ${data.matches.length} 页匹配</div>`;
      data.matches.forEach(m => {
        const div = document.createElement('div');
        div.className = 'search-item';
        div.innerHTML = `<div class="pg">第 ${m.page} 页 (${m.count} 处)</div><div class="snip"></div>`;
        div.querySelector('.snip').textContent = m.snippet;
        div.onclick = () => jumpToPage(m.page);
        searchDrawer.appendChild(div);
      });
      jumpToPage(data.matches[0].page);
    }

    let isDragging = false;
    splitter.addEventListener('mousedown', (e) => {
      e.preventDefault();
      isDragging = true;
      splitter.classList.add('dragging');
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      const rect = splitContainer.getBoundingClientRect();
      if (splitDir === 'row') {
        const rightPx = Math.max(250, Math.min(rect.width - 260, rect.right - e.clientX));
        const pct = (rightPx / rect.width) * 100;
        zhPane.style.flex = `0 0 ${pct.toFixed(1)}%`;
        viewer.style.flex = `1 1 ${(100 - pct).toFixed(1)}%`;
      } else {
        const bottomPx = Math.max(170, Math.min(rect.height - 200, rect.bottom - e.clientY));
        const pct = (bottomPx / rect.height) * 100;
        zhPane.style.flex = `0 0 ${pct.toFixed(1)}%`;
        viewer.style.flex = `1 1 ${(100 - pct).toFixed(1)}%`;
      }
      if (zoomMode === 'fit') applyZoom();
    });
    window.addEventListener('mouseup', () => {
      if (!isDragging) return;
      isDragging = false;
      splitter.classList.remove('dragging');
      document.body.style.userSelect = '';
    });

    fileSelect.addEventListener('change', () => {
      currentFile = fileSelect.value;
      currentMeta = null;
      currentZh = null;
      selectedZhId = null;
      loadMeta(false, false);
      loadZh(false);
    });
    document.getElementById('prevBtn').onclick = () => jumpToPage(Number(pageInput.value) - 1);
    document.getElementById('nextBtn').onclick = () => jumpToPage(Number(pageInput.value) + 1);
    if (backJumpBtn) backJumpBtn.onclick = goBackJump;
    pageInput.addEventListener('change', () => jumpToPage(pageInput.value));
    document.getElementById('zoomInBtn').onclick = () => {
      const base = zoomMode === 'fit' ? (computePageWidth() / 760) : zoomScale;
      setZoomAround(base + 0.2);
    };
    document.getElementById('zoomOutBtn').onclick = () => {
      const base = zoomMode === 'fit' ? (computePageWidth() / 760) : zoomScale;
      setZoomAround(base - 0.2);
    };
    fitBtn.onclick = () => {
      zoomMode = 'fit';
      applyZoom();
      viewer.scrollLeft = 0;
    };

    viewer.addEventListener('wheel', (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const base = zoomMode === 'fit' ? (computePageWidth() / 760) : zoomScale;
      const factor = e.deltaY < 0 ? 1.12 : (1 / 1.12);
      setZoomAround(base * factor, e.clientX, e.clientY);
    }, { passive: false });

    let panMode = false;
    let spaceHeld = false;
    const panModeBtn = document.getElementById('panModeBtn');
    function updatePanCursorMode() {
      const active = panMode || spaceHeld;
      viewer.classList.toggle('force-pan', active);
      if (panModeBtn) panModeBtn.classList.toggle('active', panMode);
    }
    if (panModeBtn) {
      panModeBtn.onclick = () => {
        panMode = !panMode;
        updatePanCursorMode();
        showToast(panMode ? '已切换为抓手平移模式' : '已切换为英文划词复制模式');
      };
    }
    window.addEventListener('keydown', (e) => {
      if (e.code === 'Space' && document.activeElement?.tagName !== 'INPUT') {
        spaceHeld = true;
        updatePanCursorMode();
      }
      if (e.altKey && e.key === 'ArrowLeft') {
        e.preventDefault();
        goBackJump();
      }
    });
    window.addEventListener('keyup', (e) => {
      if (e.code === 'Space') {
        spaceHeld = false;
        updatePanCursorMode();
      }
    });

    viewer.addEventListener('mousedown', (e) => {
      const isMiddle = e.button === 1;
      if (e.button !== 0 && !isMiddle) return;
      // Let native browser text selection work when dragging on PDF text (unless in panMode / Space / middle click)
      if (!isMiddle && !panMode && !spaceHeld && e.target && e.target.classList && e.target.classList.contains('pdf-tl-span')) {
        return;
      }
      if (isMiddle) e.preventDefault();
      panState = {
        startX: e.clientX,
        startY: e.clientY,
        scrollLeft: viewer.scrollLeft,
        scrollTop: viewer.scrollTop,
        moved: false,
      };
    });
    window.addEventListener('mousemove', (e) => {
      if (!panState) return;
      const dx = e.clientX - panState.startX;
      const dy = e.clientY - panState.startY;
      if (!panState.moved && Math.hypot(dx, dy) > 4) {
        panState.moved = true;
        viewer.classList.add('panning');
        hoverPill.style.display = 'none';
      }
      if (panState.moved) {
        e.preventDefault();
        viewer.scrollLeft = panState.scrollLeft - dx;
        viewer.scrollTop = panState.scrollTop - dy;
      }
    });
    window.addEventListener('mouseup', () => {
      if (panState) {
        if (panState.moved) {
          suppressNextPdfClick = true;
          setTimeout(() => { suppressNextPdfClick = false; }, 120);
        }
        viewer.classList.remove('panning');
        panState = null;
        return;
      }
      // If user drag-selected English text on the PDF, keep selection intact for Ctrl+C AND sync matching Chinese sentence!
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.rangeCount > 0 && sel.toString().trim().length > 0) {
        const range = sel.getRangeAt(0);
        if (viewer.contains(range.commonAncestorContainer)) {
          suppressNextPdfClick = true;
          setTimeout(() => { suppressNextPdfClick = false; }, 180);
          let hitCard = null;
          const hitSpans = [];
          viewer.querySelectorAll('.pdf-tl-span').forEach(sp => {
            try {
              if (range.intersectsNode(sp)) {
                if (!hitCard) hitCard = sp.closest('.page-card');
                hitSpans.push(sp);
              }
            } catch (_) {}
          });
          if (hitCard && hitSpans.length) {
            const pg = Number(hitCard.dataset.page);
            const zhSet = new Set();
            const enSet = new Set();
            let targetItem = null;
            hitSpans.forEach(sp => {
              const sh = hitTestSentence(pg, Number(sp.dataset.nx), Number(sp.dataset.ny));
              if (sh) {
                if (!targetItem) targetItem = sh.itemId;
                if (sh.itemId === targetItem) {
                  enSet.add(sh.enIdx);
                  (sh.zhIndices || []).forEach(z => zhSet.add(z));
                }
              }
            });
            if (targetItem && enSet.size > 0) {
              selectedZhId = targetItem;
              activeSent = {
                itemId: targetItem,
                zhIndices: Array.from(zhSet).sort((a, b) => a - b),
                enIndices: Array.from(enSet).sort((a, b) => a - b),
              };
              renderPdfActiveOverlays();
              highlightZhCards(false);
              updateZhSentenceClasses();
              const targetSpan = zhList.querySelector(
                `.zh-card[data-id="${CSS.escape(targetItem)}"] .zh-sent[data-sidx="${activeSent.zhIndices[0]}"]`
              );
              if (targetSpan) targetSpan.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
          }
        }
      }
    });

    zhToggleBtn.onclick = () => {
      zhVisible = !zhVisible;
      applyLayoutState();
    };
    splitDirBtn.onclick = () => {
      splitDir = splitDir === 'row' ? 'col' : 'row';
      zhPane.style.flex = '0 0 42%';
      viewer.style.flex = '1 1 58%';
      applyLayoutState();
    };
    [filterAllBtn, filterPageBtn, filterLockedBtn, filterTodoBtn].forEach(btn => {
      if (!btn) return;
      btn.onclick = () => {
        zhFilterMode = btn.dataset.filter;
        localStorage.setItem('paper_zh_filter', zhFilterMode);
        renderZhPane(false);
      };
    });
    if (pdfLockMarkBtn) {
      pdfLockMarkBtn.classList.toggle('active', showPdfLockMarks);
      pdfLockMarkBtn.onclick = () => {
        showPdfLockMarks = !showPdfLockMarks;
        localStorage.setItem('paper_pdf_lock_marks', showPdfLockMarks ? '1' : '0');
        pdfLockMarkBtn.classList.toggle('active', showPdfLockMarks);
        renderPdfLockOverlays();
        showToast(showPdfLockMarks ? '已开启左侧 PDF 细锁边条' : '已隐藏左侧 PDF 锁标（保持纯净 PDF 排版视图）');
      };
    }
    if (lockMenuBtn && lockPopover) {
      lockMenuBtn.onclick = (e) => {
        e.stopPropagation();
        lockPopover.classList.toggle('open');
        if (lockPopover.classList.contains('open')) renderLockControls();
      };
      lockPopover.onclick = (e) => e.stopPropagation();
      document.addEventListener('click', () => lockPopover.classList.remove('open'));
    }
    if (lockModeDenyBtn) {
      lockModeDenyBtn.onclick = () => postLockAction('set_mode', [], 'deny');
    }
    if (lockModeAskBtn) {
      lockModeAskBtn.onclick = () => postLockAction('set_mode', [], 'force_ask');
    }
    autoSyncBtn.onclick = () => {
      autoFollow = !autoFollow;
      localStorage.setItem('paper_zh_follow', autoFollow ? '1' : '0');
      autoSyncBtn.classList.toggle('active', autoFollow);
      showToast(autoFollow ? '已开启滚动跟随' : '已暂停滚动跟随');
    };
    zhFilterInput.addEventListener('input', () => renderZhPane(false));
    document.getElementById('reloadBtn').onclick = () => {
      currentMeta = null;
      currentZh = null;
      loadMeta(true, false);
      loadZh(false);
      showToast('已刷新 PDF 与中文对照');
    };
    document.getElementById('searchBtn').onclick = runSearch;
    searchInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') runSearch();
      if (e.key === 'Escape') { searchInput.value = ''; runSearch(); }
    });
    viewer.addEventListener('scroll', updateCurrentPageOnScroll, { passive: true });
    window.addEventListener('resize', () => { if (zoomMode === 'fit') applyZoom(); });

    applyLayoutState();
    loadMeta(false, false);
    loadZh(false);
    setInterval(() => {
      loadMeta(true, true);
      loadZh(true);
    }, 1500);
  </script>
</body>
</html>
"""


class ViewerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        file_param = qs.get("file", [None])[0]

        try:
            if parsed.path in ("/", "/index.html"):
                self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/meta":
                data = get_meta(file_param)
                self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif parsed.path == "/api/text":
                data = get_text_layer(file_param)
                self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif parsed.path == "/api/zh":
                data = get_zh_companion(file_param)
                self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif parsed.path == "/api/locks":
                pdf_path = resolve_pdf(file_param)
                ldata = paper_lock.load_locks(pdf_path)
                lp = paper_lock.lock_path_for(pdf_path)
                out = {
                    "file": pdf_path.name,
                    "tex": ldata.get("tex", pdf_path.with_suffix(".tex").name),
                    "mode": ldata.get("mode", "deny"),
                    "locked_ids": sorted((ldata.get("locks") or {}).keys()),
                    "locks": ldata.get("locks") or {},
                    "locks_mtime": lp.stat().st_mtime if lp.exists() else 0.0,
                }
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif parsed.path == "/api/search":
                q = qs.get("q", [""])[0]
                matches = search_pdf(file_param, q)
                self._send(200, json.dumps({"matches": matches}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif parsed.path.startswith("/api/page/") and parsed.path.endswith(".svg"):
                idx_str = parsed.path[len("/api/page/"):-len(".svg")]
                svg_bytes, _ = render_page_svg(file_param, int(idx_str))
                self._send(200, svg_bytes, "image/svg+xml; charset=utf-8")
            elif parsed.path.startswith("/api/page/") and parsed.path.endswith(".png"):
                idx_str = parsed.path[len("/api/page/"):-len(".png")]
                png_bytes, _ = render_page_png(file_param, int(idx_str))
                self._send(200, png_bytes, "image/png")
            else:
                self._send(404, b"Not Found", "text/plain")
        except Exception as e:
            self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path != "/api/locks":
                self._send(404, b"Not Found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
            file_param = payload.get("file")
            action = str(payload.get("action") or "toggle")
            ids = payload.get("ids") or []
            mode = payload.get("mode")

            pdf_path = resolve_pdf(file_param)
            zh_path = resolve_zh_path(pdf_path)
            items = []
            if zh_path and zh_path.exists():
                items = json.loads(zh_path.read_text(encoding="utf-8")).get("items", [])

            ldata = paper_lock.update_locks_for_pdf(pdf_path, items, action=action, ids=ids, mode=mode)
            lp = paper_lock.lock_path_for(pdf_path)
            out = {
                "ok": True,
                "file": pdf_path.name,
                "tex": ldata.get("tex", pdf_path.with_suffix(".tex").name),
                "mode": ldata.get("mode", "deny"),
                "locked_ids": sorted((ldata.get("locks") or {}).keys()),
                "locks_mtime": lp.stat().st_mtime if lp.exists() else 0.0,
            }
            self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Bilingual Vector-SVG PDF Paper Viewer")
    parser.add_argument("--pdf", type=str, default=None, help="Path to target PDF file (e.g. paper/main.pdf)")
    parser.add_argument("--paper-dir", type=str, default=None, help="Directory containing PDF files (if --pdf is not given)")
    parser.add_argument("--default", type=str, default=None, help="Default PDF filename inside --paper-dir")
    parser.add_argument("--zh", type=str, default=None, help="Optional path to Chinese companion JSON")
    parser.add_argument("--port", type=int, default=8788, help="Local HTTP port (default: 8788)")
    return parser.parse_args()


def main():
    global PAPER_DIR, DEFAULT_FILE, EXPLICIT_ZH_PATH, PORT
    args = parse_args()
    PORT = args.port

    if args.pdf:
        pdf_path = Path(args.pdf).resolve()
        if not pdf_path.exists():
            raise SystemExit(f"PDF not found: {pdf_path}")
        PAPER_DIR = pdf_path.parent
        DEFAULT_FILE = pdf_path.name
    elif args.paper_dir:
        PAPER_DIR = Path(args.paper_dir).resolve()
        DEFAULT_FILE = args.default or ""
    else:
        raise SystemExit("Please pass --pdf <path/to/main.pdf> or --paper-dir <path/to/dir>")

    if args.zh:
        EXPLICIT_ZH_PATH = Path(args.zh).resolve()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), ViewerHandler)
    print(f"[paper_viewer] Serving {PAPER_DIR / DEFAULT_FILE} at http://127.0.0.1:{PORT}", flush=True)
    if EXPLICIT_ZH_PATH:
        print(f"[paper_viewer] Companion JSON: {EXPLICIT_ZH_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
