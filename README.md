# Chengmen Paper Viewer (`chengmen-paper-viewer`)

> 中英双栏实时矢量论文预览与协作工作流 · Bilingual Live Vector-SVG Paper Viewer

A lightweight, zero-intrusion **live bilingual preview, navigation, and lock-guarded co-writing toolkit** for writing English LaTeX research papers (ICML / NeurIPS / ICLR / ACL / CVPR / IEEE, single-column or two-column) — built for both human co-authors and AI coding agents.

Render your compiled English `.pdf` on the left as **lossless vector SVG** (with selectable/copyable English text and clickable PDF hyperlinks) while reading, aligning, and locking synchronized **Chinese paragraph & sentence companions** on the right — without touching a single line of your submission `.tex` template or polluting your LaTeX folder.

---

## Key Features / 核心特性

1. **True Vector-SVG Rendering + Native English Text Selection (45%–500% Zoom)**
   - Exports each PDF page via PyMuPDF `get_svg_image(text_as_path=True)` so math formulas, vector plots, and tables stay razor-sharp at any magnification with zero missing-font risk.
   - Overlays an aligned transparent text layer so you can **drag-select and `Ctrl+C` copy English text** directly from the vector PDF (while auto-syncing the corresponding Chinese sentence!). Hold `Space` or toggle **`抓手`** (Hand Mode) to pan freely.
2. **Clickable PDF Internal & External Hyperlinks + Multi-Step Back History**
   - Automatically extracts PDF link annotations (`page.get_links()` + named destinations) so clicking `Appendix D`, `Section 4.5`, `Equation (2)`, `Table 1`, `Figure 3`, citations, or external URLs jumps directly to the exact destination with a target pulse ring and synced Chinese card.
   - Includes a toolbar **`↩ 返回`** (Back) button and **`Alt + ←`** shortcut with multi-level jump history to return to your exact scroll position and highlight.
3. **Bidirectional Paragraph, Figure/Table & Sentence-Level Sync**
   - **Sentence-level highlighter (Right ↔ Left)**: Extracts word-level PDF geometry and aligns Chinese companion sentences to exact English lines via monotonic dynamic programming (or optional explicit `sent_align` overrides). Hovering, clicking, or drag-selecting in Chinese highlights the corresponding English lines in multiply-blended highlighter ink (`mix-blend-mode: multiply`), and vice versa.
   - **Whole-paragraph & float regions**: Clicking a card header or any figure/table highlights the full block (including cross-column / cross-page continuations, stacked floats, and `\onecolumn` appendices).
4. **Paragraph & Section Hard-Lock Guard for AI Co-Writing (`paper_lock.py`)**
   - Lock any finalized paragraph, figure, table, or entire section with one click in the UI (`🔓 锁定` / `🔒 锁定管理`).
   - Pair with an AI agent `PreToolUse` hook (`python path/to/paper_lock.py --hook`) supporting **`Hard Lock (deny)`** or **`Ask Approval (force_ask)`** modes so AI assistants never accidentally overwrite locked LaTeX spans or tamper with `.locks.json`.
   - Optional subtle left-edge lock bars on the PDF (`PDF锁标`, off by default to keep typography inspection pure).
5. **Automatic Hot Reload (~1.5 s) & Zero LaTeX Pollution**
   - Whenever `pdflatex` / `xelatex` updates `.pdf` or you/your AI assistant edits `*_zh.json`, both panes hot-reload automatically while preserving scroll position and dynamically recomputing page numbers, bounding boxes, and hyperlinks.
   - Keep `*_zh.json` outside your submission folder via `--zh` (or inside an ignored `.paper_viewer/` directory).

---

## Repository Structure / 目录结构

| Path | Description |
|---|---|
| `paper_viewer.py` | Standalone local HTTP server (single-file Python + PyMuPDF, zero Node/npm build step) |
| `paper_lock.py` | Paragraph/section lock engine + `PreToolUse` hook guard for AI coding agents |
| `start_viewer.ps1` | Windows PowerShell launcher wrapper |
| `zh_template.json` | Starter schema for a new paper's bilingual companion JSON |
| `skill/bilingual-paper-workflow/SKILL.md` | Drop-in agent skill prompt for AI-assisted Edit–Compile–Sync paper writing |
| `requirements.txt` | Python dependencies (`pymupdf`) |

---

## Quick Start / 快速上手

### 1. Clone & Install Dependency
```bash
git clone https://github.com/JiangtaoDing/chengmen-paper-viewer.git
cd chengmen-paper-viewer
pip install -r requirements.txt
# or simply: pip install pymupdf
```

### 2. Run the Live Viewer
```bash
# Auto-discovers <pdf_dir>/.paper_viewer/<stem>_zh.json or <stem>_zh.json
python paper_viewer.py --pdf path/to/paper/main.pdf --port 8788

# Or specify the companion JSON path explicitly (use different ports for multiple papers)
python paper_viewer.py \
  --pdf path/to/paper/main.pdf \
  --zh path/to/main_zh.json \
  --port 8788
```

Windows PowerShell shortcut:
```powershell
.\start_viewer.ps1 -Pdf "path\to\paper\main.pdf" -Zh "path\to\main_zh.json" -Port 8788
```

Open `http://127.0.0.1:8788/` in your browser (or IDE side-pane browser).

---

## AI-Assisted Co-Writing & Lock Hook / 配合 AI 协作与防误改锁

### Agent Skill
Copy `skill/bilingual-paper-workflow/SKILL.md` into your AI agent's skills directory (or paste the prompt below at the start of a writing session):

> Please read `README.md` and `zh_template.json`, generate a paragraph-by-paragraph Chinese companion JSON for `<path/to/main.tex>` and `<path/to/main.pdf>`, launch `paper_viewer.py` on port `8788`, and keep the companion JSON synchronized whenever `.tex` is edited and recompiled.

### Optional: Hard-Lock `PreToolUse` Hook (`paper_lock.py`)
Register `paper_lock.py --hook` as a `PreToolUse` hook in your agent configuration (e.g., Antigravity / Claude Code) to enforce paragraph/section locks stored in `<stem>.locks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "edit_file|write_file|run_command|Edit|Write|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"/abs/path/to/paper_lock.py\" --hook"
          }
        ]
      }
    ]
  }
}
```

---

## Companion JSON Schema (`*_zh.json`)

See [`zh_template.json`](zh_template.json) for a full starter template:

```json
{
  "file": "main.pdf",
  "tex": "path/to/paper/main.tex",
  "title_zh": "论文中文标题",
  "items": [
    {
      "id": "2.2-P1",
      "page": 2,
      "lines": "L146-161",
      "section": "§2.2 Empirical Analysis",
      "title": "本段一句话小标题",
      "kind": "prose",
      "anchor": "2.2. Empirical Analysis",
      "en_preview": "Opening words of the paragraph in the compiled PDF...",
      "zh": "这一段的流畅中文对照（可用 \\n 分段，保留关键数字与术语）。",
      "sent_align": [[0], [1]],
      "is_todo": false
    }
  ]
}
```

### Field Reference
- **`id`** *(required, unique)*: Short reference tag (`Title`, `Abstract`, `1-P1`, `Fig1`, `Tab1`, `AppA-P1`). Setting `"id": "Title"` automatically binds to the title banner on Page 1.
- **`kind`** *(optional)*: `"prose"` (default), `"figure"` (graphic above caption, e.g., `anchor: "Figure 1."`), or `"table"` (caption above table body, e.g., `anchor: "Table 1."`).
- **`anchor`** *(recommended)*: First 4–8 English words appearing in the compiled PDF block (auto-derived from `en_preview` if omitted).
- **`sent_align`** *(optional)*: Explicit mapping from each Chinese sentence index to English sentence index list(s) (e.g. `[[0], [1], [2, 3]]`); overrides automatic DP alignment when exact 1-to-1 alignment is desired.
- **`page`**: Initial hint; dynamically updated from the live PDF on every compile.
- **`is_todo`**: Marks unfinished sections with an amber badge and enables one-click `仅待写` filtering.

---

## Contributing / 欢迎共建

Contributions, bug reports, and pull requests are very welcome! Great areas to collaborate on:
- **Layout & template edge cases**: IEEE / ACM / Springer / multi-page `longtable` / wrapped figures / `tcolorbox` environments.
- **Multilingual companions**: Extensible alignment hints & UI localization for Japanese, Korean, German, French, etc.
- **SyncTeX source jump**: Clicking a PDF line to open/highlight the exact line in `.tex` inside VS Code / Cursor / Neovim.
- **Cross-platform launcher & packaging**: macOS/Linux launcher script (`start_viewer.sh`), `pyproject.toml` / `pipx` CLI entry point, and automated layout tests.

Please open an **Issue** (with a minimal PDF snippet for layout bugs) or submit a **Pull Request** directly!

## License

Released under the [MIT License](LICENSE).
