---
name: kbmail wiki
description: Rules for knowledge-base pages under wiki/
applyTo: "wiki/**"
---
When creating or editing files under `wiki/`, follow `wiki/SCHEMA.md` exactly: required front matter,
claims with `(src: ...; conf: ...; seen: ...)`, typed `relations`, `[[wikilinks]]` relative to `wiki/`,
supersession instead of silent overwrite, an entry in `wiki/index.md`, and one line appended to
`wiki/log.md` for every operation.
