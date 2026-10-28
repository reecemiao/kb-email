# Teams chats

Teams chats feed the same pipeline as email. Each chat-day becomes one record (`source: teams`,
thread = the chat), so labeling, rules, KB selection, people inference and the wiki work unchanged.
Getting the messages out is the hard part, and it depends on what your tenant allows.

## Which route? (no access to the Entra ID portal is fine)

| route | you need | gets you | effort |
|---|---|---|---|
| **A. Graph PowerShell script** (`teams.via: powershell`, default) | the `Microsoft.Graph.Authentication` module (per-user install, no admin), and a tenant that lets Microsoft's own "Microsoft Graph Command Line Tools" app read your chats | all your 1:1, group and meeting chats, with delta runs | low once working |
| **B. Outlook hidden folder** (`teams.via: outlook`) | classic Outlook whose profile exposes `TeamsMessagesData` | chat copies stored in your mailbox | low, works only on some profiles |
| **C. Share to Outlook** | nothing | the threads you choose, as normal mail | per thread |
| **D. Pasted text** (`kbmail teams import-text`) | nothing | any chat you copy from the Teams window | per chat |
| **E. Meeting transcripts** (`.vtt` / `.docx` into `raw/inbox/`) | transcripts enabled for the meeting | what was said, by whom | per meeting |
| F. Own app registration (`teams.via: graph`) | an Entra app that IT registers for you | same as A | needs IT |

Start with the A test. If it fails, use B–E. These can be combined: for example A for chats, and E
for meetings.

## A. Graph PowerShell script

`scripts/teams-export.ps1` (installed by `kbmail init`) signs in with **Microsoft's own** Graph
PowerShell app, so you don't register anything. It asks only for delegated `Chat.Read` +
`User.Read`, so it can only read chats you are a member of. It saves the raw Graph responses as
JSON under `data/teams-graph/` (git-ignored), and `kbmail teams import` turns them into records.

```powershell
Install-Module Microsoft.Graph.Authentication -Scope CurrentUser   # once; no admin rights needed
cd C:\kb
.\scripts\teams-export.ps1 -Test        # signs in; prints "OK: ... Chat.Read works" if your tenant allows it
kbmail teams export                     # runs the script (last 30 days first time, then deltas) + imports
```

- **Delta:** the script remembers its last run in `data/teams-graph/since.txt` (1 hour overlap;
  kbmail de-duplicates by message id). `-Days N` overrides it. `kbmail sync --teams` (or
  `sync.teams: true`) includes this route.
- **Sign-in:** a browser window, or `-DeviceCode` to sign in with a code. Graph PowerShell normally
  remembers the sign-in for later runs. If it asks again, sign in again.
- **Run it yourself:** `.\scripts\teams-export.ps1`, then `kbmail teams import`. This is useful if
  the script needs a different PowerShell, or if you schedule it.

If `-Test` fails:

| message | meaning | what to do |
|---|---|---|
| "Need admin approval" / `AADSTS65001` / consent required | your tenant blocks users from granting Chat.Read to that app | ask IT to grant consent for "Microsoft Graph Command Line Tools" (Chat.Read, User.Read), a one-click admin action, or use B–E |
| `Install-Module` blocked (no PowerShell Gallery) | package source blocked | ask IT for the module, or use B–E |
| "running scripts is disabled" | execution policy | `kbmail teams export` already passes `-ExecutionPolicy Bypass` for this process. If a group policy enforces signing, use B–E |

**Graph Explorer** (https://developer.microsoft.com/graph/graph-explorer) is the same idea in a
browser, if your tenant allows it. Run `GET https://graph.microsoft.com/v1.0/me/chats/{chat-id}/messages`,
save the response as a `.json` file, and run `kbmail teams import <file>`. The importer accepts any
saved Graph chat or chatMessage JSON.

Don't paste another app's client ID into `teams.client_id` to get around the missing
registration. That impersonates the other app and usually breaks your company's IT policy. The
script uses Microsoft's own tool as intended.

## B. Outlook hidden folder (experimental)

Exchange keeps a compliance copy of Teams chats in a hidden mailbox folder (`TeamsMessagesData`).
Some classic Outlook profiles expose it to COM, many don't:

```powershell
kbmail teams probe-outlook                       # tells you whether yours does
kbmail teams export --via outlook --days 30      # if it does
```

## C. Share to Outlook

In Teams, use **… → Share to Outlook** on an important message or thread and send it to
yourself. The next `kbmail export` picks it up like any mail, so labels, rules and the KB pipeline
apply. A rule on the subject or sender pattern of these mails can route them to the right area.
Teams' "missed activity" notification emails also arrive in Outlook. They are only snippets, but
they show which chats are worth exporting with D.

## D. Pasted chat text

Open the chat, select the messages (or Ctrl+A in the message pane), copy, and paste into a text
file named after the chat. Then:

```powershell
kbmail teams import-text "C:\kb\raw\inbox\Payment cut-over.txt"      # title = file name, or --title
```

The parser understands the common copy formats: `[10/27/2026 9:30 AM] Name: text`,
`Name` / `time` / `text` on separate lines, and Japanese dates and 午前/午後. Text it can't split
becomes one message. Speakers are matched to email addresses already seen in your mail, so people
inference links them up. Re-importing the same paste (or an overlapping one) doesn't create
duplicates.

## E. Meeting transcripts

In the meeting's **Recap** (or the chat's Files/Recordings), download the transcript as `.vtt` or
`.docx` and drop it into `raw/inbox/`. Then run `kbmail raw import`. A `.vtt` becomes
`raw/docs/<name>.md` with speakers and timestamps, and the wiki ingests it like any other source
(`/kb-wiki-ingest`).

## F. Own app registration (if IT does it for you)

Ask IT for a public-client app with delegated `Chat.Read` + `User.Read` and "Allow public client
flows". Put its ids in `kbmail.yaml` (`teams.via: graph`, `teams.client_id`, `teams.tenant`) and
install the Teams extra (`uv tool install -e ".[teams]"`). Then run `kbmail teams export`. The
first run shows a device code. The token is cached in `state/msal_cache.bin` (git-ignored).

## Not supported, on purpose

- **Reading the Teams client's local cache** (IndexedDB/LevelDB under `%LOCALAPPDATA%`), and UI
  automation: undocumented, breaks with every update, and very likely against policy.
- **Channel messages via Graph:** they need `ChannelMessage.Read.All`, which only an admin can
  grant. Use C or D for important channel threads.
- `/me/chats/getAllMessages`: application-only and metered.

## Things to know

- Chat messages are short and informal. Labeling and selection work better on chat-days than on
  single messages, which is why that is the unit.
- Triage ("needs reply") is mail-centric. `/kb-suggest` cross-checks whether something was already
  answered in Teams when Teams records exist.
