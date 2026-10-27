---
description: "Convert SVG diagrams to Mermaid (or describe them) - works without vision"
agent: agent
---
SVG files are XML text, so you can read them even when image vision is disabled.

For each entry in `review/images/descriptions.yaml` with `type: svg` and no description:
1. Read `review/images/<id>.svg` as text. Look at `<text>` elements, shapes, groups and connectors
   (`<line>`, `<path>`, markers/arrowheads), and their coordinates to infer structure.
2. Decide what is best for an LLM to understand later:
   - flow, architecture, sequence, state, org chart, timeline, simple chart → write a Mermaid diagram to
     `review/images/<id>.mmd` (plain Mermaid, no code fences) and put a one-sentence description in the
     YAML entry
   - icons/logos/decoration → `skip: true`
   - complex charts or drawings → a precise text description in the YAML (values, labels, trends)
3. Keep labels verbatim (original language).
Run `kbmail images import` at the end, then `kbmail kb queue`.
