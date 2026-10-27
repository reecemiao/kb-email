---
description: "Describe pending images (only when Copilot vision is enabled)"
agent: agent
---
Check `kbmail.yaml`: if `copilot.vision` is not `true`, stop and tell the user to describe the images
themselves in `review/images/descriptions.yaml` (preview: `review/images/REVIEW.md`), then run
`kbmail images import`.

Otherwise, open `review/images/descriptions.yaml`. For each entry with an empty `description` and
`type: image`, look at the image file in `review/images/` and write a description useful for a knowledge
base: what kind of image (screenshot, chart, architecture diagram, photo, table), the main content
(boxes and arrows, axis names and trends, key numbers and labels, error messages verbatim), and what it
shows in the context given. If the image is decorative or irrelevant set `skip: true`. If you cannot
read it reliably, leave it empty and add a question with `kbmail questions add`.
Run `kbmail images import` at the end.
