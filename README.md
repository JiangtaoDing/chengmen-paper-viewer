# Bilingual Live Paper Viewer (中英双栏实时矢量论文预览器)

A lightweight, zero-intrusion **live bilingual preview & editing companion** for writing English LaTeX research papers (ICML / NeurIPS / ICLR / ACL / CVPR / IEEE, single-column or two-column).

Render your compiled English `.pdf` on the left as **lossless vector SVG** (45%–500% zoom, drag-to-pan) while reading and revising synchronized **Chinese paragraph & sentence companions** on the right — without modifying a single line of your submission `.tex` template or leaving temporary files inside your LaTeX directory.

---

## Key Features / 核心特性

1. **True Vector-SVG Rendering (45%–500% Lossless Zoom)**
   - Renders every PDF page via PyMuPDF `get_svg_image(text_as_path=True)` so math symbols, vector figures, and tables remain razor-sharp at any zoom level with zero missing-font issues.
   - Supports `Ctrl + Scroll` / trackpad pinch zoom centered on cursor and click-drag panning on the PDF pane.
2. **Bidirectional Paragraph, Figure/Table, and Sentence-Level Sync**
   - **Sentence-level highlighter (Right ↔ Left)**: Uses word-level PDF geometry and monotonic dynamic programming to align Chinese companion sentences with their exact English lines in the PDF. Hovering, clicking, or selecting text in Chinese highlights the corresponding English sentence lines with multiply-blended highlighter ink (`mix-blend-mode: multiply`), and vice versa.
   - **Whole-paragraph & float regions**: Clicking a card header or any figure/table highlights the full block (including cross-column and cross-page continuations, stacked `[h]` figures, and `\onecolumn` appendices).
3. **Automatic Hot Reload (~1.5 s)**
   - Whenever `pdflatex` / `xelatex` updates `.pdf` or you/your AI assistant edits `*_zh.json`, the browser automatically refreshes while preserving scroll position and dynamically recomputing all page numbers and bounding boxes.
4. **Revision-Focused Navigation & Filtering**
   - Filter cards by **All (`全部`)**, **Current Page (`仅当前页`)**, **Pending (`仅待写 / TODO`)**, or **Follow (`跟随`)**, plus instant keyword search.
   - One-click **Copy ID (`复制编号`)** (e.g., `2.2-P1 (L146-161)`) to reference exact paragraphs when collaborating with co-authors or AI coding assistants.
5. **Zero Pollution of Your LaTeX Directory**
   - Store the companion JSON outside your submission folder via `--zh` (or in `.paper_viewer/<stem>_zh.json`), keeping your submission zip spotless.

---

## Repository Structure / 目录结构

| Path | Description |
|---|---|
| `paper_viewer.py` | Standalone local HTTP server (single-file Python + PyMuPDF, zero Node/npm build step) |
| `start_viewer.ps1` | Windows PowerShell launcher wrapper |
| `zh_template.json` | Starter schema for a new paper's bilingual companion JSON |
| `skill/bilingual-paper-workflow/SKILL.md` | Drop-in agent skill prompt for AI-assisted Edit–Compile–Sync paper writing |
| `requirements.txt` | Python dependencies (`pymupdf`) |

---

## Quick Start / 快速上手

### 1. Install Dependency
```bash
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

## AI-Assisted Edit–Compile–Sync Workflow / 配合 AI 写论文

Copy `skill/bilingual-paper-workflow/SKILL.md` into your AI agent's skills directory (or paste the prompt below at the start of a writing session):

> Please read `README.md` and `zh_template.json`, generate a paragraph-by-paragraph Chinese companion JSON for `<path/to/main.tex>` and `<path/to/main.pdf>`, launch `paper_viewer.py` on port `8788`, and keep the companion JSON synchronized whenever `.tex` is edited and recompiled.

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
      "is_todo": false
    }
  ]
}
```

### Field Reference
- **`id`** *(required, unique)*: Short reference tag (`Title`, `Abstract`, `1-P1`, `Fig1`, `Tab1`, `AppA-P1`). Setting `"id": "Title"` automatically binds to the title banner on Page 1.
- **`kind`** *(optional)*: `"prose"` (default), `"figure"` (graphic above caption, e.g., `anchor: "Figure 1."`), or `"table"` (caption above table body, e.g., `anchor: "Table 1."`).
- **`anchor`** *(recommended)*: First 4–8 English words appearing in the compiled PDF block (auto-derived from `en_preview` if omitted).
- **`page`**: Initial hint; dynamically updated from the live PDF on every compile.
- **`is_todo`**: Marks unfinished sections with an amber badge and enables one-click `仅待写` filtering.

---

## Contributing / 欢迎共建

Contributions, bug reports, and PRs are very welcome! Areas that are especially helpful:
- Support for additional layout edge cases (IEEE / ACM / Springer / multi-page longtables / wrapped figures).
- Better cross-lingual sentence alignment (e.g., Japanese / Korean / German companions or pluggable glossary hints).
- Optional dark-mode theme and split-ratio persistence.
- Automated layout regression tests across public LaTeX conference templates.

Please open an **Issue** with a minimal PDF snippet or submit a **Pull Request** directly.

## License

Released under the [MIT License](LICENSE).
