---
name: kbmail raw
description: Rules for knowledge-source files under raw/
applyTo: "raw/**"
---
Files in `raw/` are knowledge *sources*. `raw/email/**` and `raw/teams/**` are built by `kbmail kb assemble`
from `work/convert/<thread>/summary.md` (written by the `kb-format` skill) and the mail itself: change a
summary there and assemble again instead of editing these files. Everything below the
`<!-- kbmail: ... -->` line is regenerated from the mail. Never edit `raw/notes/**` (the user's own
answers) or `raw/docs/**` and `raw/web/**` (converted documents).
