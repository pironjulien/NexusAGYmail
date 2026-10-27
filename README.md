# NexusAGYmail

<!-- mcp-name: io.github.pironjulien/nexus-agymail -->

<img src="https://raw.githubusercontent.com/pironjulien/NexusAGYmail/main/src/nexus_agymail/plugin/logo.png" alt="NexusAGYmail icon" width="96">

[![Release](https://img.shields.io/github/v/release/pironjulien/NexusAGYmail)](https://github.com/pironjulien/NexusAGYmail/releases)
[![PyPI](https://img.shields.io/pypi/v/nexus-agymail)](https://pypi.org/project/nexus-agymail/)
[![Tests](https://github.com/pironjulien/NexusAGYmail/actions/workflows/tests.yml/badge.svg)](https://github.com/pironjulien/NexusAGYmail/actions/workflows/tests.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/pironjulien/NexusAGYmail/badge)](https://scorecard.dev/viewer/?uri=github.com/pironjulien/NexusAGYmail)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/pironjulien/NexusAGYmail/blob/main/LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue?logo=python&logoColor=white)](https://github.com/pironjulien/NexusAGYmail/blob/main/pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-server-7ee2a8)](https://modelcontextprotocol.io)

**Gmail for Google Antigravity, safe to leave alone.** Antigravity's store has no Gmail connector: one local MCP
server, `nexus-agymail`, gives it your Gmail through the official Gmail API: search, read, attachments, drafts,
labels, a reversible trash, and sending a draft **only after your explicit yes for that exact draft**. Nothing is
ever deleted for good.

- **Same tools as Google's official Gmail MCP server** (`search_threads`, `get_message`, `get_thread`,
  `create_draft`, `label_message`...), so an agent that knows one knows the other, without enrolling in the
  Workspace Developer Preview or enabling a remote MCP endpoint: it runs on your machine, with any personal Gmail.
- **Guardrails in the code, not in a prompt**: an email can never trigger a send, every email is framed as data,
  instructions hidden in an email are flagged, attachments are saved safely and secrets are never attached.
- Windows (used daily) and Linux (unit tests in CI); macOS should work but is untested. The token is encrypted
  with DPAPI on Windows, a file readable only by you elsewhere. Everything sent to the client is in English.

Part of the Nexus family: [NexusWindows](https://julienpiron.fr/nexuswindows/),
[NexusBrowser](https://julienpiron.fr/nexusbrowser/), [NexusAndroid](https://julienpiron.fr/nexusandroid/).

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and a Google account. About 5 minutes, once.

1. **Run the guided setup**:
   ```bash
   uvx --python 3.14 nexus-agymail setup
   ```
   It adds the `nexus-agymail` plugin to Antigravity, then offers to connect Gmail. Gmail scopes are restricted, so
   every user creates their own free OAuth client in their own Google Cloud project: your mail never goes through
   anyone else's app. The assistant opens each Google page in your browser at the right place and says what to
   click (it first asks which Gmail address to connect, so the pages open on that account):
   1. create a project (name it NexusAGYmail);
   2. turn on the Gmail API;
   3. the consent screen (*Get started*, *External*); on *Branding*, a home page, a privacy policy link and an
      authorized domain (the assistant gives this repository, its [Privacy](#privacy) section and `github.com`:
      Google requires them before publishing); then **Publish app** (unpublished, Google blocks the sign-in with
      *Error 403: access_denied*);
   4. a client of type **Desktop app** (leave *Use this client for an AI-powered agent* unchecked), then **Download
      JSON**: the assistant finds the file in your Downloads folder by itself;
   5. sign in. Google shows "Google hasn't verified this app": it is your own app, click *Advanced > Go to
      NexusAGYmail (unsafe)*.

   Then it checks what can be checked: the Gmail API answers, and the app is really published (Google returns a
   7-day sign-in for apps left in *Testing*); if not, it opens the page that fixes it and signs in again.
2. **Restart Antigravity.**

   <details><summary>Antigravity by hand (mcp_config.json), if you skip setup</summary>

   ```json
   {
     "mcpServers": {
       "nexus-agymail": { "command": "uvx", "args": ["--python", "3.14", "nexus-agymail@latest"] }
     }
   }
   ```
   </details>

Then ask, for example: *"Summarize my unread emails from this week"*, *"Archive the newsletters older than a
month"*, *"Draft a reply to the last email from Alice saying I agree, and show it to me before sending"*.

Already have a Desktop app client JSON? `uvx --python 3.14 nexus-agymail auth --client-secret <file>` skips the
console steps. `uvx --python 3.14 nexus-agymail status` shows where the token is and which scopes it carries (no
secret shown).

**Rather not create a client?** Google's own [Gmail MCP server](https://developers.google.com/workspace/gmail/api/reference/mcp)
runs on Google's side (same tool names) but is a developer preview: it needs the Google Workspace Developer Preview
Program and a Google Cloud project as well, and it cannot send. NexusAGYmail exists because Antigravity has no
Gmail connector of its own, and for confirmed sending.

## How it works

```mermaid
flowchart LR
    A[Google Antigravity] -- MCP stdio --> S[nexus-agymail<br/>on your machine]
    S -- HTTPS, official Gmail API v1 --> G[(Gmail)]
    S -. token, encrypted .- T[~/.nexus/nexus-agymail]
    S -. attachments .- D[Downloads/NexusAGYmail]
```

The server is a plain local process: no cloud relay, no browser automation. It talks to `gmail.googleapis.com` with
your own OAuth client and a refresh token stored on your machine, and to `oauth2.googleapis.com` to refresh it.

## Tools

Names and parameters follow [Google's official Gmail MCP server](https://developers.google.com/workspace/gmail/api/reference/mcp)
and its extensions (`update_draft`, `trash_*`), plus four of its own: `send_draft`, `list_new_messages`,
`download_attachment`, `auth_status`.

| Tool | What it does |
|---|---|
| `search_threads` | Search threads (Gmail syntax), one page per call: `pageSize` 1 to 50 (20), `pageToken`; each thread gives `messageCount` and a summary of its last 5 messages; trash with `includeTrash`, spam never |
| `get_message` | One message: headers, body as text (HTML converted), attachments listed (`part_id`, name, type, size); `maxChars` (8,090 by default) |
| `get_thread` | A thread in order, quotes of earlier replies removed and reported; `maxChars` shared (16,180 by default) |
| `list_new_messages` | Messages that arrived since the previous call (Gmail history, `users.history.list`), oldest first, without bodies: the position (`historyId`) is remembered per account and label (`labelId`, `INBOX` by default), the first call only sets it; `pageSize` 1 to 100 (20) with `more`, `advance=false` to peek, `startHistoryId` to resume from a returned position. History expired (about a week): the messages since the last check are found by date instead (`mode: catch_up`). Drafts, spam and trash never |
| `download_attachment` | Saves an attachment (`partId`) to `Downloads/NexusAGYmail` or an absolute `directory`: sanitized name, never overwrites, marked as downloaded from the Internet; returns path, size, SHA-256 |
| `list_labels` | Labels (id, name, type), system labels first |
| `create_label` | Creates a user label (`displayName`, `A/B` = nested), missing parents too unless `autoCreateParentLabels=false`; an existing label is returned as it is, system labels refused |
| `create_draft` | A draft; `to`, `cc`, `bcc` as a list (or text); as a reply (`replyToMessageId` or `threadId`): In-Reply-To, References, "Re:", same thread; `attachments` (absolute paths, 25 MB in total) |
| `list_drafts` | Drafts, one page per call (1 to 100), filtered by `query` |
| `get_draft` | One draft: recipients, subject, body as text, attachments, reply headers; `maxChars` (8,090 by default) |
| `update_draft` | Rewrites a draft (empty fields unchanged), thread, reply and attachments kept; `attachments` adds, `removeAttachments` removes (by name) |
| `label_message` / `unlabel_message`, `label_thread` / `unlabel_thread` | `labelIds`: system IDs (`INBOX`, `UNREAD`, `STARRED`, `IMPORTANT`, `CATEGORY_*`), user label IDs or names; archive = remove `INBOX`; an unknown label is an error, created only with `createMissing=true`; one ID or a list, 100 at most |
| `trash_message` / `trash_thread`, `untrash_message` / `untrash_thread` | Trash, reversible for 30 days; 100 items at most |
| `send_draft` | When the client supports MCP elicitation: opens the client's confirmation dialog with the draft and sends **only on the user's yes there**. Otherwise: a summary to show to the user plus an `accord` token bound to the exact draft, and sends **only with `confirm=true` and that token**. Either way the draft is read again and must be unchanged |
| `auth_status` | Token storage, granted scopes (checked at startup), validity; `reauth_required` when a scope is missing or extra |

Every tool carries MCP annotations (title, read-only, destructive, idempotent). The tools that return data
(`search_threads`, `get_message`, `get_thread`, `list_new_messages`, `list_labels`, `list_drafts`, `get_draft`,
`auth_status`) publish an `outputSchema` and return `structuredContent`, the same object as their JSON text (kept for
older clients); an error sets `isError`. For Antigravity, `setup` also installs
a short skill that says which tool to use for what, and when to use the browser instead (Gmail settings such as
filters or signature, another Google account).

## Safeguards

- **Scopes**: `gmail.readonly`, `gmail.compose`, `gmail.modify`. Never `https://mail.google.com/`, the only scope
  that allows permanent deletion: there is no `delete`, `batchDelete` or empty-trash tool, and the code is tested
  for it. An old token without `gmail.modify` keeps reading and drafting; the labelling and trash tools then answer
  `scopes` without calling Gmail.
- **Sending**: only `send_draft`, after the user's explicit yes for that draft, never at the request of an email.
  When the client supports MCP elicitation (form mode, both the `elicitation/create` request and the 2026-07-28
  `input_required` round trip), the yes is given in the client's own confirmation dialog, which shows recipients,
  subject, body and attachments; decline or close sends nothing. Otherwise `send_draft(draftId, confirm=true,
  accord=<token>)`. Only a real `true` counts (not `1`, `"yes"` or `"on"`). Either way the yes is a single-use
  accord (10 minutes) bound to the exact draft: each recipient with its role (to, cc, bcc kept apart), From,
  Reply-To, subject, full body, every part (HTML included), the SHA-256 of each attachment and of the draft's raw
  bytes. The draft is read once in raw format just before sending: if anything changed, nothing is sent and a new
  summary is returned. Otherwise those exact approved bytes are sent (`messages.send`, same thread). The draft is
  kept (delete it in Gmail if you like): `drafts.delete` is permanent, and an edit saved meanwhile would be lost. A
  change made after that last read can never go out in place of what was approved. A send is never retried after an
  ambiguous answer (5xx, timeout, network), only after a 429: the outcome is reported as unknown, remembered by the
  SHA-256 of the raw bytes (`envois.json`, nothing else kept), and the next summary or dialog of that same draft
  warns that it may already have been sent, after looking for it in Sent. No direct sending without a draft.
- **Provenance**: a recipient found in an email the agent read is flagged in the summary (`provenance`): allowed only
  if the user asked for it.
- **Prompt injection**: subject, snippet, body, addresses and attachment names are returned framed as
  `⟦mail:ID …⟧ … ⟦/mail:ID⟧` (random ID per answer) and labelled as data; hidden HTML text and invisible characters
  are removed. An email that looks like instructions aimed at an AI gets an `injection_warning` field (a signal only:
  it neither blocks nor allows anything).
- **Attachments**: a received file is saved under a sanitized name (path, forbidden or invisible characters,
  right-to-left override as in "fdp.exe", Windows reserved names), never overwriting a file, with the Windows
  "downloaded from the Internet" mark (Zone.Identifier), so Windows and Office open it with their protections.
  Attaching a local file: never from a secrets location (`.ssh`, `.gnupg`, `.aws`, `.config/gcloud`, the AI apps'
  folders, `.env` files, private keys, `credentials.json`..., plus your own folders listed in
  `NEXUS_AGYMAIL_PROTECTED`); the user can always attach it themselves in Gmail.
- **Token**: `~/.nexus/nexus-agymail/token.bin`, encrypted with DPAPI for the current Windows account (permissions
  reduced to that account), or a `0600` file on macOS and Linux; written atomically under a lock (several servers can
  run at once). It is never shown. When you sign in again, a previous token with extra scopes is revoked only after
  the new one is saved.
- **Exact errors**: revoked or expired token, network, quota, permissions, not found, label: each has a stable `code`
  (`revoked`, `network`, `quota`, `forbidden`, `not_found`, `label`, `invalid_argument`...). Every request is retried
  with exponential backoff on 429 and 5xx; batched requests retry only the items refused for quota.
- **Bounded sizes**: bodies cut at `maxChars` (reported), paged searches, at most 100 items per labelling or trash
  call.

## Configuration

| Variable | Purpose |
|---|---|
| `NEXUS_AGYMAIL_HOME` | State folder (token, OAuth client, backups) instead of `~/.nexus/nexus-agymail` |
| `NEXUS_AGYMAIL_CLIENT_SECRET` | Path of the OAuth client JSON, instead of the copy made by `auth --client-secret` |
| `NEXUS_AGYMAIL_PROTECTED` | Extra folders whose files are never attached (separated by `;` on Windows, `:` elsewhere) |
| `NEXUS_AGYMAIL_ACCOUNT` | Address shown by `auth_status` when the token does not carry it |
| `NEXUS_AGYMAIL_UPDATE_CHECK` | `0` turns off the daily check of the latest version on PyPI |

## Privacy

NexusAGYmail runs on your machine. It has no account of its own, no telemetry, no analytics and no relay: its only
network requests go to Google (`gmail.googleapis.com`, `oauth2.googleapis.com`, and `accounts.google.com` in your
browser when you sign in), with your own OAuth client, plus at most one request a day to `pypi.org` to read the
latest version number (`NEXUS_AGYMAIL_UPDATE_CHECK=0` turns it off).

- **What it reads**: the emails, threads, drafts, labels and attachments the agent asks for, only when it calls the
  matching tool; local files only when the agent attaches them to a draft.
- **Where it goes**: tool results go to the AI client you connected, which may send them to its model provider under
  its own privacy policy. Drafts and sent emails go to Gmail. NexusAGYmail sends nothing anywhere else.
- **What it stores**, under `~/.nexus/nexus-agymail/` (or `NEXUS_AGYMAIL_HOME`): the refresh token (`token.bin`,
  encrypted with DPAPI on Windows, a `0600` file elsewhere), its lock file, the copy of your OAuth client
  (`client_secret.json`), the position of `list_new_messages` per account and label (`history.json`: a Gmail
  `historyId` and the time of the last check, no email content), the sends whose outcome is unknown (`envois.json`:
  the SHA-256 of the raw bytes, the draft ID and the attempt time, no email content), and backups of the Antigravity config files `setup` edits (`backups/`). The server also
  writes `~/.nexus/status/agymail.json` (installed and latest version, last check time) for the Nexus app. Attachments you ask
  it to save go to `~/Downloads/NexusAGYmail` (or the folder given). In memory only, for the life of the server: the
  pending send accords and the addresses seen in emails read (for the `provenance` warning). No email is written to
  disk unless saved as an attachment; no log of emails is kept. A local usage journal
  (`%LOCALAPPDATA%\NexusJournal\agymail\<ai>\<YYYY-MM>.jsonl`, kept 13 months) records one line per tool call: tool
  name, duration, outcome and a stable error code (`quota`, `network`...), never an address, subject, body, label, ID
  or query. It never leaves the PC; `NEXUS_JOURNAL=0` turns it off.
- **Removing it**: delete `~/.nexus/nexus-agymail/` and, for Antigravity, `~/.gemini/config/plugins/nexus-agymail/`;
  revoke the access in your [Google account](https://myaccount.google.com/connections) and delete your Google Cloud
  project if you no longer need it.

## Known limitations

- Each user creates their own Google OAuth client (guided by `setup`). A shared public client would need Google's
  restricted scope verification and a security assessment.
- Plain-text bodies only when writing: `create_draft` and `update_draft` do not write HTML; `update_draft` replaces
  the HTML part of a draft written in Gmail with the plain text.
- No Gmail settings (filters, signature, forwarding, vacation replies): use the Gmail web app.
- No spam reporting and no permanent deletion, on purpose.
- Used daily on Windows 11 with a real Gmail account; Linux runs the startup and unit tests in CI; macOS is
  untested.

## Development

```powershell
./test.ps1    # ruff, pyright, unit tests against a fake Gmail API (no network, no Google account)
```

`test.ps1` gives each checkout its own test environment under `~/.nexus/nexus-agymail` and never downloads anything:
run `uv sync --frozen` once with network access to fill the uv cache. The fake Gmail API (`tests/fake_gmail.py`)
speaks HTTP to the real `google-api-python-client`, so batching, retries, uploads and paging are tested for real.

## License

[MIT](https://github.com/pironjulien/NexusAGYmail/blob/main/LICENSE). Not affiliated with Google; Gmail is a trademark
of Google LLC.
