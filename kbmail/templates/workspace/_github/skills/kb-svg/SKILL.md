---
name: "kb-svg"
description: "Finish SVG diagrams that kbmail could not convert to Mermaid automatically, working from their text digests (no image vision needed). Use when `kbmail status` lists SVGs for /kb-svg."
---
kbmail already converted the easy SVGs itself (draw.io exports and simple box-and-arrow diagrams →
`review/images/<id>.mmd`), skipped icons, and extracted raster images that SVGs wrap. What remains
are the hard cases. For each one there is a **text digest** `review/images/<id>.svg.md`, which
unpacks the SVG: texts in reading order, shapes with coordinates and the text inside them,
connectors and the shapes they touch, embedded images, and a heuristic draft Mermaid.

**Work from the digest, not the `.svg`.** Raw SVGs are often huge, minified, or full of base64
fonts and images. With vision disabled they may also be treated as images, so you can't use them.
Open the raw SVG as text only for a detail the digest lacks, and read just the part you need.

1. Run `kbmail images svg`. It analyses new SVGs and refreshes the digests and
   `review/images/descriptions.yaml`.
2. For each entry in `review/images/descriptions.yaml` with `type: svg` and no description, read its
   `digest` file. `why_not_automatic` says what stopped the automatic conversion.
3. Decide what is most useful for an LLM reading the knowledge base later:
   - **Diagram** (flow, architecture, sequence, state, org chart, timeline): write Mermaid to
     `review/images/<id>.mmd` (plain Mermaid, no code fences). Start from the draft in the digest and
     fix it with the shape and connector lists: attach dangling connectors to the nearest box in
     their direction, and turn free-standing texts near a line into edge labels. Also put a
     one-sentence description in the YAML entry.
   - **Chart**: write a precise description in the YAML: chart type, axis titles and units, each
     series and its values read from the shape coordinates (bar heights relative to the axis line),
     and the trend. Use a Mermaid `xychart-beta` or `pie` only when the values are certain.
   - **Table or text layout**: transcribe it as a Markdown table or list in the description.
   - **Decoration** (logo, background, icon set): set `skip: true`.
   - **Not understandable from text** (a drawing, a map, a photo-like illustration): leave the
     description empty and add a question:
     `kbmail questions add "What does diagram <id> show?" --context "<context from the YAML>" --step kb-svg`.
4. Keep labels verbatim, in their original language.
5. Run `kbmail images import`, then `kbmail kb queue`.

To check an automatic conversion, compare `review/images/<id>.mmd` with its digest
(`kbmail svg2mermaid review/images/<id>.svg --digest`). Fix the `.mmd` if needed before
`/kb-format` uses it.
