---
name: kbmail raw
description: Rules for knowledge-source files under raw/
applyTo: "raw/**"
---
Files in `raw/` are knowledge *sources*. Only the `kb-format` skill writes `raw/email/**` and `raw/teams/**`.
Preserve facts, numbers, names, dates and decisions verbatim; remove only noise (signatures, legal
disclaimers, quoted history that repeats a message already in the file, meeting join boilerplate).
Quoted mail that appears nowhere else (`--- quoted: ... ---` blocks in the converted thread) is content. Never edit `raw/notes/**` (the user's
own answers) or `raw/docs/**` and `raw/web/**` (converted documents).
