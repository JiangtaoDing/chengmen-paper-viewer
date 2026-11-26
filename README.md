# Chengmen Paper Viewer (`chengmen-paper-viewer`)

> 中英双栏实时矢量论文预览与协作工作流 · Bilingual Live Vector-SVG Paper Viewer & Antigravity MCP App Plugin

A lightweight, zero-intrusion **live bilingual preview, navigation, and lock-guarded co-writing toolkit** for writing English LaTeX research papers (ICML / NeurIPS / ICLR / ACL / CVPR / IEEE, single-column or two-column) — available both as a **native Antigravity MCP App plugin (SEP-1865)** and as a **standalone local HTTP viewer** for any browser or AI coding agent.

Render your compiled English `.pdf` on the left as **lossless vector SVG** (with selectable/copyable English text and clickable PDF hyperlinks) while reading, aligning, and locking synchronized **Chinese paragraph & sentence companions** on the right — without touching a single line of your submission `.tex` template or polluting your LaTeX folder.

---

## Key Features / 核心特性

1. **Native Antigravity MCP App Plugin (SEP-1865)**
   - Opens directly inside Antigravity's **side pane (`+` menu)** or **left sidebar (full-page view)** — no manual background HTTP server or port management required.
   - Ships with a prebuilt `@modelcontextprotocol/ext-apps` bridge (`mcp_app/dist/mcp_bridge.js`), requiring **zero Node.js runtime** and only Python 3.9+ + `pymupdf`.
   - Adapts automatically to Antigravity light/dark theme variables (`--color-*`, `--font-sans`), persists recent papers & UI preferences to `${PLUGIN_DATA}`, and adds native IDE actions: **Attach paragraph to chat** (`updateModelContext`), **Send to Agent for review** (`sendMessage`), **Open `.tex` in side pane** (`toggle-aux-pane`), and external link opening (`openLink`).
   - Bundles the `PreToolUse` paragraph-lock hook (`hooks.json`), workspace rules (`rules/AGENTS.md`), and agent skill (`skills/chengmen-paper-viewer/SKILL.md`) in a single toggleable plugin.
2. **True Vector-SVG Rendering + Native English Text Selection (45%–500% Zoom)**
   - Exports each PDF page via PyMuPDF `get_svg_image(text_as_path=True)` so math formulas, vector plots, and tables stay razor-sharp at any magnification with zero missing-font risk.
   - Overlays an aligned transparent text layer so you can **drag-select and `Ctrl+C` copy English text** directly from the vector PDF (while auto-syncing the corresponding Chinese sentence!). Hold `Space` or toggle **`抓手`** (Hand Mode) to pan freely.
3. **Clickable PDF Internal & External Hyperlinks + Multi-Step Back History**
   - Automatically extracts PDF link annotations (`page.get_links()` + named destinations) so clicking `Appendix D`, `Section 4.5`, `Equation (2)`, `Table 1`, `Figure 3`, citations, or external URLs jumps directly to the exact destination with a target pulse ring and synced Chinese card.
   - Includes a toolbar **`↩ 返回`** (Back) button and **`Alt + ←`** shortcut with multi-level jump history to return to your exact scroll position and highlight.
4. **Bidirectional Paragraph, Figure/Table & Sentence-Level Sync**
   - **Sentence-level highlighter (Right ↔ Left)**: Extracts word-level PDF geometry and aligns Chinese companion sentences to exact English lines via monotonic dynamic programming (or optional explicit `sent_align` overrides). Hovering, clicking, or drag-selecting in Chinese highlights the corresponding English lines in multiply-blended highlighter ink (`mix-blend-mode: multiply`), and vice versa.
   - **Whole-paragraph & float regions**: Clicking a card header or any figure/table highlights the full block (including cross-column / cross-page continuations, stacked floats, and `\onecolumn` appendices).
5. **Paragraph & Section Hard-Lock Guard for AI Co-Writing (`paper_lock.py`)**
   - Lock any finalized paragraph, figure, table, or entire section with one click in the UI (`🔓 锁定` / `🔒 锁定管理`).
   - Pair with an AI agent `PreToolUse` hook (`python path/to/paper_lock.py --hook`) supporting both **Antigravity** and **Claude Code** hook protocols, with **`Hard Lock (deny)`** or **`Ask Approval (force_ask / ask)`** modes so AI assistants never accidentally overwrite locked LaTeX spans or tamper with `.locks.json`.
   - Supports storing lock files inside an ignored `.paper_viewer/<stem>.locks.json` directory (automatically used when `.paper_viewer/` exists, while remaining backward-compatible with `<stem>.locks.json`).
   - Optional subtle left-edge lock bars on the PDF (`PDF锁标`, off by default to keep typography inspection pure).
6. **Automatic Hot Reload (~1.5 s) & Zero LaTeX Pollution**
   - Whenever `pdflatex` / `xelatex` updates `.pdf` or you/your AI assistant edits `*_zh.json`, both panes reload automatically while preserving scroll position and dynamically recomputing page numbers, bounding boxes, and hyperlinks.
   - Keep `*_zh.json` outside your submission folder via `--zh` / `zh_path` (or inside an ignored `.paper_viewer/` or `companions/` directory).

---

## Install as an Antigravity Plugin (Recommended) / 作为 Antigravity 插件一键安装

Antigravity automatically discovers plugins placed under `~/.gemini/config/plugins/<name>/` (no extra registration step required):

```bash
# 1. Install the only Python runtime dependency
pip install "pymupdf>=1.24.0,<2.0"

# 2. Clone directly into Antigravity's plugins directory
git clone https://github.com/JiangtaoDing/chengmen-paper-viewer.git \
  ~/.gemini/config/plugins/chengmen-paper-viewer
```

On Windows PowerShell:
```powershell
pip install "pymupdf>=1.24.0,<2.0"
git clone https://github.com/JiangtaoDing/chengmen-paper-viewer.git `
  "$HOME\.gemini\config\plugins\chengmen-paper-viewer"
```

Once cloned, **Chengmen Paper Viewer** appears automatically in:
- **Side Pane (`+` menu)** and **Left Sidebar** (opens with your last paper or a paper picker when launched empty).
- **MCP Tools** (`chengmen_viewer`):
  - `open_paper` — open or jump to a paper/paragraph (`pdf_path`, `paper_dir`, `zh_path`, `jump_id`, `page`, all optional).
  - `list_paper_locks` — inspect locked sections and lock mode.
  - `sync_paper_companion` — audit PDF region & `.tex` span alignment for `*_zh.json`.
  - App-only tools (`app_select_paper`, `app_get_meta`, `app_get_zh`, `app_get_page_svg`, `app_get_page_png`, `app_get_text_layer`, `app_update_locks`, `app_get_state`, `app_save_state`) hidden from the model and called directly by the embedded UI.
- **Built-in `PreToolUse` Lock Guard** (`hooks.json`), **Workspace Rules** (`rules/AGENTS.md`), and **Agent Skill** (`skills/chengmen-paper-viewer/SKILL.md`).

> **Fast UI development**: Set `CHENGMEN_APP_HTML=/abs/path/to/viewer.html` in the server environment to iterate on UI HTML without restarting the MCP server. To rebuild `mcp_app/dist/mcp_bridge.js` after editing `mcp_app/bridge.mjs`, run `cd mcp_app && npm install && npm run build`.

---

## Repository Structure / 目录结构

| Path | Description |
|---|---|
| `plugin.json` | Antigravity plugin manifest (`chengmen-paper-viewer`, v0.3.0) |
| `mcp_config.json` | MCP server registration (`chengmen_viewer` over stdio) |
| `hooks.json` | Bundled Antigravity `PreToolUse` paragraph-lock hook |
| `mcp_server.py` | Zero-extra-dependency MCP JSON-RPC 2.0 stdio server + `ui://chengmen/viewer.html` resource |
| `mcp_app/` | Official `@modelcontextprotocol/ext-apps` bridge source & prebuilt `dist/mcp_bridge.js` |
| `paper_viewer.py` | Dual-mode vector-SVG viewer engine (native MCP App HTML + standalone local HTTP server) |
| `paper_lock.py` | Paragraph/section lock engine + `PreToolUse` hook guard (Antigravity & Claude Code) |
| `assets/logo.svg` | 128×128 high-contrast plugin icon |
| `rules/AGENTS.md` | Bundled Antigravity workspace rules |
| `skills/chengmen-paper-viewer/SKILL.md` | Bundled Antigravity plugin skill |
| `skill/bilingual-paper-workflow/SKILL.md` | Standalone agent skill prompt for non-plugin setups |
| `start_viewer.ps1` | Windows PowerShell launcher with pre-flight path/port/Python checks |
| `zh_template.json` | Starter schema for a new paper's bilingual companion JSON |
| `requirements.txt` | Python dependencies (`pymupdf>=1.24.0,<2.0`) |

---

## Standalone Local HTTP Quick Start / 独立本地模式快速上手

### 1. Clone & Install Dependency
```bash
git clone https://github.com/JiangtaoDing/chengmen-paper-viewer.git
cd chengmen-paper-viewer
pip install -r requirements.txt
# or simply: pip install "pymupdf>=1.24.0,<2.0"
```

### 2. Run the Live Viewer
```bash
# Auto-discovers companion JSON in order:
#   1. <pdf_dir>/.paper_viewer/<stem>_zh.json
#   2. <pdf_dir>/<stem>_zh.json
#   3. <repo_dir>/companions/<stem>_zh.json
python paper_viewer.py --pdf path/to/paper/main.pdf --port 8788

# Specify the companion JSON path explicitly (use different ports for multiple papers)
python paper_viewer.py \
  --pdf path/to/paper/main.pdf \
  --zh path/to/main_zh.json \
  --port 8788

# Or serve a whole directory of PDFs with an optional default selection
python paper_viewer.py \
  --paper-dir path/to/paper \
  --default main.pdf \
  --port 8788
```

Windows PowerShell shortcuts:
```powershell
.\start_viewer.ps1 -Pdf "path\to\paper\main.pdf" -Zh "path\to\main_zh.json" -Port 8788
.\start_viewer.ps1 -PaperDir "path\to\paper" -Default "main.pdf" -Port 8788
```

Open `http://127.0.0.1:8788/` in your browser (or IDE side-pane browser).

---

## Standalone AI Co-Writing & Lock Hook / 独立配置 AI 协作与防误改锁

If you are **not** using the Antigravity plugin bundle above, you can still register `paper_lock.py --hook` manually in either **Antigravity** (`hooks.json`) or **Claude Code** (`settings.json`):

#### Antigravity (`.agents/hooks.json` or global `hooks.json`)
```json
{
  "chengmen-paper-lock": {
    "PreToolUse": [
      {
        "matcher": "edit_file|write_file|run_command",
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

#### Claude Code (`.claude/settings.json` or `~/.claude/settings.json`)
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
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

### Inspect Lock State via CLI
```bash
python paper_lock.py status path/to/paper/main.pdf
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
- **Top-level `"file"`**: Target PDF filename (used to guard against loading/locking the wrong PDF when switching files).
- **Top-level `"tex"`** *(optional)*: Path to the source `.tex` file (relative to the companion JSON or PDF directory, or absolute). Honored by `paper_lock.py` when `.pdf` and `.tex` live in different directories (e.g., `-output-directory=build`); falls back to `<pdf_stem>.tex` alongside the PDF if omitted or not found.
- **`id`** *(required, unique)*: Short reference tag (`Title`, `Abstract`, `1-P1`, `Fig1`, `Tab1`, `AppA-P1`). Setting `"id": "Title"` automatically binds to the title banner on Page 1 and locates `\title{...}` / `\icmltitle{...}` in `.tex`.
- **`zh`** *(recommended)*: Chinese paragraph/caption companion text (supports `\n` line breaks).
- **`kind`** *(optional)*: `"prose"` (default), `"figure"` (graphic above caption, e.g., `anchor: "Figure 1."`), or `"table"` (caption above table body, e.g., `anchor: "Table 1."`).
- **`anchor`** *(recommended)*: First 4–8 English words appearing in the compiled PDF block (auto-derived from `en_preview` if omitted).
- **`section`**, **`title`**, **`lines`**, **`en_preview`** *(recommended)*: Display metadata and fallback matching hints for UI cards and whole-section lock grouping.
- **`sent_align`** *(optional)*: Explicit mapping from each Chinese sentence index to English sentence index list(s) (e.g. `[[0], [1], [2, 3]]`); overrides automatic DP alignment when exact 1-to-1 alignment is desired.
- **`page`** *(optional)*: Initial hint; dynamically updated from the live PDF on every compile.
- **`is_todo`** *(optional)*: Marks unfinished sections with an amber badge and enables one-click `仅待写` filtering.

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
