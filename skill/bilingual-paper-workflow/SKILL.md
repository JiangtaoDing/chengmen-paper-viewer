---
name: bilingual-paper-workflow
description: >-
  Launch and maintain the live bilingual (English vector-SVG PDF + Chinese paragraph
  companion) paper-writing workflow using `paper_viewer.py` and `paper_lock.py`.
  Activate this skill whenever the user wants to write, revise, proofread, translate,
  or live-preview a LaTeX research paper PDF side-by-side in any project.
---

# Bilingual Live Paper-Writing Workflow

This skill pairs an English LaTeX paper PDF with a synchronized Chinese paragraph
and sentence companion via the standalone `paper_viewer.py` server, with optional
paragraph/section lock enforcement via `paper_lock.py`.

## Core Principles
1. **Serve PDFs through `paper_viewer.py`**: Renders every page as lossless vector
   SVG (`text_as_path=True`) with 45%–500% zoom, selectable/copyable English text,
   clickable PDF hyperlinks with multi-step back history (`Alt+←`), bidirectional
   paragraph/sentence sync, and 1.5 s hot reload.
2. **Keep the LaTeX submission directory clean**: Never drop viewer scripts or
   companion JSON inside the submission root unless inside an ignored `.paper_viewer/`
   folder (or pass `--zh` pointing outside the repo).
3. **Atomic Edit–Compile–Sync loop**: Whenever you edit `.tex` and recompile `.pdf`,
   always update the matching entry in `<pdf_stem>_zh.json` (`zh`, `lines`,
   `en_preview`, `anchor`, `sent_align`, `is_todo`) in the same step so both panes
   stay faithfully synchronized.
4. **Respect paragraph locks (`<stem>.locks.json`)**: Never modify locked sections
   or `.locks.json` files; ask the user to unlock a card in the UI if revisions
   are needed.

## Standard Workflow

### 1. Initialize or Update the Chinese Companion JSON
Read the target `.tex` (and compiled `.pdf` if available) and generate
`<pdf_stem>_zh.json` following `zh_template.json`:
- Required item fields: `id`, `page`, `lines`, `section`, `title`, `anchor`,
  `en_preview`, `zh`, `is_todo`.
- Optional `sent_align`: explicit per-sentence mapping (e.g., `[[0], [1], ...]`)
  for exact 1-to-1 sentence highlighting.
- Use short memorable IDs (`Title`, `Abstract`, `1-P1`, `Fig1`, `Tab1`, `AppA-P1`).
- Use `"id": "Title"` for the paper title banner on Page 1.
- Set `"kind": "figure"` for figures (caption below graphic, `anchor: "Figure 1."`)
  and `"kind": "table"` for tables (caption above table, `anchor: "Table 1."`).
- Set `"anchor"` to the first 4–8 English words rendered in the PDF block; page
  numbers and bounding boxes update automatically on every reload.

### 2. Start the Local Viewer Server
Run `paper_viewer.py` in the background (use distinct `--port` values when editing
multiple papers concurrently):

```bash
python path/to/paper_viewer.py \
  --pdf path/to/paper/main.pdf \
  --zh path/to/main_zh.json \
  --port 8788
```

Open `http://127.0.0.1:8788/` in the browser.

### 3. Edit–Compile–Sync Loop
1. Edit the `.tex` file.
2. Recompile the PDF (`pdflatex` / `xelatex`).
3. Update the corresponding card(s) in `<pdf_stem>_zh.json` — the browser hot-reloads
   automatically in ~1.5 s while keeping the reader's scroll position.
