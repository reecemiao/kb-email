---
description: "Draft an email on a topic or a reply to a message, grounded in the knowledge base"
agent: agent
---
Request: ${input:request:Topic to write about, or a message/thread id to reply to (plus any instructions)}

1. If a message/thread id is given: `kbmail show <id> --thread` and identify what is being asked.
   Otherwise find related mail with `kbmail mail "<topic>"`.
2. Gather facts: `kbmail wiki search "<topic>"`, read the relevant pages and their sources. Use only facts
   you can cite; mark anything uncertain with [CHECK: ...] for the user.
3. Read `wiki/people/<recipient>.md` for each recipient (relationship, language, formality) and
   `wiki/style/writing-style.md` for the user's voice.
4. Write `drafts/YYYY-MM-DD_<slug>.md`:

   ```markdown
   ---
   to: [alice@example.com]
   cc: []
   subject: "..."
   reply_to: <message id when replying, else omit>
   reply_all: true
   sources: [wiki/..., raw/..., <message ids>]
   ---
   <email body in Markdown, in the recipient's language and the user's style>
   ```
5. Show the draft and a short list of sources and [CHECK] items. Offer `kbmail draft-outlook <file>` to
   put it into Outlook Drafts (kbmail never sends).
