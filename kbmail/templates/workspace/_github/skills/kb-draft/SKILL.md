---
name: "kb-draft"
description: "Draft an email on a topic or a reply to a message, grounded in the wiki and written in the user's style, into drafts/. Use when the user asks to write, reply to, or follow up on an email."
argument-hint: "Topic, or a message/thread id to reply to, plus any instructions"
---
The request is the text after `/kb-draft`: a topic, or a message/thread id to reply to, plus any instructions. If it is missing, ask for it.

1. If a message/thread id is given: `kbmail show <id> --thread` and identify what is being asked.
   Otherwise find related mail with `kbmail mail "<topic>"`.
2. Gather facts: `kbmail wiki search "<topic>"`, read the relevant pages and their sources. Use only facts
   you can cite; mark anything uncertain with [CHECK: ...] for the user.
3. Read `wiki/people/<recipient>.md` for each recipient (relationship, language, formality) and
   `wiki/style/writing-style.md` for the user's voice.
4. Write `drafts/YYYY-MM-DD_<slug>.md` following [the draft template](./draft-template.md): front matter
   with `to`, `cc`, `subject`, `reply_to` (when replying), `sources`; body in the recipient's language and
   the user's style.
5. Show the draft and a short list of sources and [CHECK] items. Offer `kbmail draft-outlook <file>` to
   put it into Outlook Drafts (kbmail never sends).
