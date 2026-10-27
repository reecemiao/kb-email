# Teams chats: feasibility and setup

**Short answer: yes, with caveats.** Teams chats can feed the same pipeline as email. Each chat-day
becomes one record (`source: teams`, thread = the chat). Labeling, rules (`source`, `participants`,
`subject` = chat topic, ...), KB selection, conversion, people inference and the wiki then work
unchanged. Getting the messages out is the hard part. It depends on your tenant's policies.

## Options

| route | what you get | needs | verdict |
|---|---|---|---|
| **Microsoft Graph, delegated `Chat.Read`** (`kbmail teams export`) | 1:1, group and meeting chats you are in; edits; file links | an Entra ID app registration (public client, device-code flow) that your tenant lets users consent to, or an admin grants | **Recommended.** Official, delta-capable, stable |
| Graph for **channel** messages | team channel posts | `ChannelMessage.Read.All`, which needs admin consent | possible only with IT; not implemented (easy to add the same way) |
| **Outlook hidden folder** `TeamsMessagesData` (`kbmail teams probe-outlook`, `export --via outlook`) | compliance copies of chat messages stored in your Exchange mailbox | classic Outlook whose profile exposes the hidden folder to COM | **Experimental.** Often not visible/synced. Try the probe |
| Manual: copy a chat, or download meeting transcripts/recaps (.docx/.vtt) | whatever you save | nothing | Always works: drop files into `raw/inbox/` → `kbmail raw import` |
| Purview eDiscovery export | everything | compliance admin | for IT, not for personal use |
| Reading the Teams client cache / UI automation | fragile partial data | n/a | **Not recommended**: undocumented, breaks often, likely against policy |

## Graph route setup

1. Ask IT, or register yourself if allowed: Entra ID → App registrations → New registration
   * Supported account types: *this organization only*
   * Authentication → *Allow public client flows* = Yes (device code)
   * API permissions → Microsoft Graph → Delegated → `Chat.Read`, `User.Read`
2. Put the IDs into `kbmail.yaml`:
   ```yaml
   teams:
     tenant: contoso.onmicrosoft.com   # or the tenant GUID
     client_id: 00000000-0000-0000-0000-000000000000
     days: 30
   ```
3. `pip install -e ".[teams]"`, then `kbmail teams export --days 30`. The first run prints a device
   code to enter at https://microsoft.com/devicelogin. The token is cached in `state/msal_cache.bin`
   (git-ignored) and refreshed silently afterwards.
4. Later: `kbmail teams export` (delta per chat via `lastModifiedDateTime`), or `kbmail sync --teams`.

Implementation notes: `GET /me/chats?$expand=members` and, per chat updated since the watermark,
`GET /chats/{id}/messages` filtered and ordered by `lastModifiedDateTime`. System events are skipped.
HTTP 429 is honoured via `Retry-After`. Attachments become `[file: name](url)` links (SharePoint/OneDrive).
Download the important ones and drop them into `raw/inbox/`. `/me/chats/getAllMessages` is avoided on
purpose: it is application-only and metered.

## Things to know

* Chat messages are short and informal. Labeling and selection work better on chat-days than on
  single messages, which is why that is the unit.
* People matching uses the member e-mail from Graph, so Teams and mail activity add up in
  `kbmail people build`.
* Triage ("needs reply") is mail-centric. Use `/kb-suggest` to cross-check whether something was
  already answered in Teams.
