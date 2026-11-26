---
name: chengmen-paper-viewer
description: >-
  Open, navigate, synchronize, and lock-guard English LaTeX research paper PDFs
  with Chinese paragraph/sentence companions directly inside Antigravity using
  the Chengmen Paper Viewer MCP App plugin (`open_paper`, `list_paper_locks`,
  `sync_paper_companion`, and the bundled `PreToolUse` lock guard).
---

# Chengmen Paper Viewer — Antigravity MCP App Workflow

This skill drives **Chengmen Paper Viewer** (`chengmen-paper-viewer`) as a native
Antigravity MCP App (SEP-1865) in the side pane (`aux_pane`) or full-page sidebar
(`global`), backed by lossless vector-SVG rendering, selectable English text,
clickable PDF hyperlinks, bidirectional sentence highlighting, and hard-lock
protection against accidental agent edits.

## MCP Tools Available

| Tool | Purpose |
|---|---|
| `open_paper` | Opens or updates the bilingual paper viewer UI in the side pane. All arguments are optional (`pdf_path`, `paper_dir`, `zh_path`, `jump_id`, `page`); calling with `{}` restores the last opened paper or shows the paper picker & recent list. Calling `open_paper` again with a new `jump_id` or `page` drives the open side pane directly. |
| `list_paper_locks` | Inspects the current lock mode (`deny` / `force_ask`) and all locked paragraph/section entries for a paper (`pdf_path` optional; defaults to the active paper). |
| `sync_paper_companion` | Validates companion JSON (`*_zh.json`) alignment against both the compiled `.pdf` regions and the `.tex` source spans, reporting matched counts, missing IDs, `.tex` path, and warnings. |

## Standard Co-Writing Workflow

### 1. Open or Initialize the Paper in the Side Pane
1. If `<pdf_stem>_zh.json` does not exist yet, read the `.tex` manuscript (and
   compiled `.pdf` if available) and generate `<pdf_dir>/.paper_viewer/<stem>_zh.json`
   following `zh_template.json`:
   - Top-level fields: `file` (PDF filename), optional `tex` (source `.tex` path
     when built via `-output-directory`), optional `title_zh`, and `items`.
   - Required item field: `id` (`Title`, `Abstract`, `1-P1`, `Fig1`, `Tab1`, `AppA-P1`).
   - Recommended item fields: `page`, `lines`, `section`, `title`, `anchor`,
     `en_preview`, `zh`, `is_todo`.
   - Optional fields: `kind` (`"prose"`, `"figure"`, `"table"`), `sent_align`
     (explicit mapping such as `[[0], [1, 2]]`).
2. Call `open_paper` with `pdf_path` (and optional `zh_path`, `jump_id`, `page`).
   The viewer automatically renders inside Antigravity's side pane and matches
   the IDE's light/dark theme.
3. Users can also open the viewer anytime from the side pane **`+`** menu or the
   left sidebar (**Chengmen Paper Viewer**), attach a paragraph to the next chat
   message, send a paragraph directly to the agent for revision, or open the
   linked `.tex` file in an adjacent side pane tab.

### 2. Atomic Edit–Compile–Sync Loop
1. Before editing `.tex`, call `list_paper_locks` if locks may be active. The
   bundled `PreToolUse` hook (`hooks.json` -> `paper_lock.py --hook`) automatically
   blocks edits to locked `.tex` spans and any `*.locks.json` file.
2. Edit only unlocked sections in `.tex` (if a locked paragraph needs revision,
   ask the user to click Unlock in the Chengmen Paper Viewer UI).
3. Recompile the PDF (`pdflatex` / `xelatex`).
4. Update the matching cards in `<pdf_stem>_zh.json` in the same step.
5. Call `sync_paper_companion` to verify anchor and `.tex` span alignment, or call
   `open_paper(pdf_path=..., jump_id=...)` to focus the updated paragraph in the
   viewer.

### 3. Keep the LaTeX Submission Directory Clean
- Store `*_zh.json` inside `<pdf_dir>/.paper_viewer/` (or outside via `zh_path`).
- Whenever `<pdf_dir>/.paper_viewer/` exists, `paper_lock.py` automatically stores
  `<stem>.locks.json` inside `.paper_viewer/` rather than polluting the LaTeX root.

### 4. Standalone Local HTTP Fallback (Optional)
If the user explicitly prefers an external browser window (or uses another IDE),
launch the standalone server instead:

```bash
python path/to/paper_viewer.py --pdf path/to/paper/main.pdf --zh path/to/main_zh.json --port 8788
```

Windows PowerShell:

```powershell
.\start_viewer.ps1 -Pdf "path\to\paper\main.pdf" -Zh "path\to\main_zh.json" -Port 8788
```
