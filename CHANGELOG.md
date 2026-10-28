# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.3] - 2026-10-28

### Fixed
- Address lists with chained `:` groups (`a:a:a:…`, found by the nightly fuzzing): Python's address parser calls
  itself once per group and ran out of stack. A list to send is refused ("nested address groups"), the sender of a
  received message is read as unreadable instead of crashing the read, and draft recipients are kept raw for the send
  approval.

### Added
- Signed releases: the files PyPI serves (wheel, sdist) are attached to each release with their Sigstore attestation
  and SHA256SUMS.txt (`release-assets.yml`; verify with `gh attestation verify <file> --repo pironjulien/NexusAGYmail`).

## [0.12.2] - 2026-10-27

### Changed
- CodeQL findings addressed: one import per module, reasons written in silent `except` blocks, unused variables
  removed; the others dismissed on GitHub with their reason.
- Shared guard policy (`garde_fous.py`): lowercase Cyrillic and Greek look-alikes (в к м н т β η) are detected again.

### Fixed
- Recipient lists always read back the same and show what was checked (found by the new fuzzing): an address
  whose local part contains a complete encoded word (`=?utf-8?q?…?=`) is refused, a display name is read once like
  a mail reader does then written back (a name that literally looks like an encoded word is itself encoded), a blank
  display name is dropped and tabs in a name become a space.

## [0.12.1] - 2026-10-27

### Added
- CodeQL code scanning of the server and the workflows, on every push and weekly.
- Coverage-guided fuzzing (Atheris, `fuzz/`, `.github/workflows/fuzz.yml`) of recipient lists (no line break of any
  kind, so no hidden header) and attachment file names (always safe to create on disk).

### Changed
- `SECURITY.md`: disclosure timeline (first answer within 3 days, fix and advisory usually within 30 days).
- Shared guard policy (`garde_fous.py`) brought up to date: zero-width and other format characters, and common
  Cyrillic or Greek look-alikes, no longer hide a word that needs confirmation.

### Fixed
- Recipient addresses with a leading, trailing or doubled dot in the local part (`a..b@example.com`) are refused
  (RFC 5322 dot-atom); attachment names are limited in UTF-16 units like Windows, so a name full of emoji can always
  be saved (both found by the new fuzzing).

## [0.12.0] - 2026-10-27

### Added
- The server announces how long its tool list stays fresh (one minute, private cache: MCP protocol 2026-07-28).

### Changed
- Minimum dependency versions raised to the ones the lock file already uses; the MCP SDK is capped below 3.
- Python 3.15 is now tested (and listed) alongside 3.14.

## [0.11.5] - 2026-10-27

### Security
- Sending: one record PER ATTEMPT (unique ID) in `envois.json`, under the SHA-256 of the raw bytes. Two simultaneous
  attempts on the same bytes (two valid accords) no longer clear each other: A's clear refusal only removes A's
  record, and B's unknown outcome stays reported (`previous_send`). A confirmed send only removes its own record: an
  older attempt with an unknown outcome stays reported. The previous format (one record per hash) is read as one
  unknown attempt.

### Known limitation
- Matching by Message-ID: if a client reuses the same Message-ID for another message (a copied draft, a resend),
  `found_in_sent` may point to that message. Not fixed (noted only).

## [0.11.4] - 2026-10-27

### Security
- Looking in Sent after an unknown outcome: a mere resemblance (same subject, To, Cc and text) now only gives
  `similar_in_sent` ("similar message, possible duplicate", `similarId`), never "this exact draft was already
  sent": attachments and Bcc are not compared there. `found_in_sent` is stated only when proven by the Message-ID of
  the draft's raw bytes (`rfc822msgid:` search). No Message-ID is added to the approved bytes.
- Sending: the "attempt in progress" record (`envois.json`) is written BEFORE `messages.send` and survives a server
  crash; if it cannot be written, nothing is sent. A local storage failure after sending no longer hides what Gmail
  did: sent (with `state_warning`) or unknown outcome, never an internal error. A clear refusal only clears the
  record it had just written (an older uncertainty stays reported).
- An unreadable 2xx answer (truncated JSON) or one without a message after sending: unknown outcome, no longer an
  internal error.

## [0.11.3] - 2026-10-27

### Security
- Sending: the draft is never deleted after sending any more (`draft: kept`, with a note: delete it in Gmail if you
  like). Re-reading, comparing then calling `drafts.delete` was not atomic, and `drafts.delete` is permanent: an edit
  saved in between was lost for good. Keeping the draft was chosen over trashing it, whose effect on a draft through
  the API cannot be checked without a real account. The code no longer calls any `.delete(`.
- Send with an unknown outcome (answer lost after `messages.send`: 5xx, timeout, network): returned as
  `status: unknown`, `code: send_outcome_unknown`, never retried. It is remembered by the SHA-256 of the raw bytes
  (`envois.json`, locked, no email content); the next summary and dialog of that same draft warn (`previous_send`)
  that it may already have been sent, after looking for it in Sent (same subject, To, Cc and text: `found_in_sent`
  with its ID). "Nothing was sent" there becomes "This call sent nothing". A clear refusal from Gmail (4xx) stays an
  ordinary error; a confirmed send clears the warning.
- Usage journal: an unknown tool name is written as `unknown`, never as given (it comes from the client and may hold
  an address).

## [0.11.2] - 2026-10-27

### Added
- Local usage journal (`%LOCALAPPDATA%\NexusJournal\agymail\<ai>\<YYYY-MM>.jsonl`): one line per tool call (tool,
  duration, outcome, stable error code), never an address, subject, body, label, ID or query. It never leaves the
  PC; `NEXUS_JOURNAL=0` turns it off.

### Security
- Sending: the accord now keeps each recipient's role apart (To, Cc, Bcc), plus From, Reply-To and the SHA-256 of
  the draft's raw bytes. Moving an address from Bcc to Cc, or changing From or Reply-To, after the summary voids the
  accord; the summary and the confirmation dialog show From and Reply-To when the draft sets them.
- Sending: the exact approved bytes are sent. The draft is read once in raw format (summary, accord and sent content
  all come from those bytes), sent with `messages.send` in the same thread, then removed with `drafts.delete` only if
  it is still identical; a draft changed after that read is kept, unsent, and reported (`draft: kept_changed`).
  Before, `drafts.send` sent whatever the draft held at that moment. A send is never retried after an ambiguous
  answer (5xx, network), only after a 429. Malformed address headers are read without loss (shown and compared as
  written).
- `send_draft`: `confirm` and the dialog's `send` are strict booleans: `1`, `"yes"`, `"on"` or `"true"` no longer
  send.

### Fixed
- `list_new_messages`: no message lost when a page was partial (`more`) and the history then expired. The saved date
  bound only moves forward once the cursor has caught up; each position handed out keeps its own bound, so an old
  `startHistoryId` given explicitly is caught up by date too instead of restarting from now.
- `history.json`: every read and read-modify-write is locked across threads and processes (lock file), and written
  through a uniquely named temporary file.

### Tests
- The fake Gmail API now really filters `messages.list` by `q` (`after:`/`before:`) and `labelIds`, and fails on any
  other query term; new routes `messages.send` and `drafts.delete`.

## [0.11.1] - 2026-10-27

### Added
- `server.json`: `icons` (Nexus family medallion, PNG and SVG), so the MCP registry and its clients show the product
  icon.

## [0.11.0] - 2026-10-27

### Added
- Native send confirmation (MCP form elicitation): when the client supports it, `send_draft(draftId)` opens the
  client's confirmation dialog with recipients, subject, body (1,618 characters) and attachments; yes sends, decline
  or close sends nothing. Both transports of the mcp 2.3 SDK are served: the `elicitation/create` request (revisions
  up to 2025-11-25) and the 2026-07-28 `input_required` round trip (state sealed by the SDK). The yes is still a
  single-use accord bound to what the dialog showed: the draft is read again before sending and any change voids it
  (new summary). Clients without elicitation, or a failed dialog, fall back to the unchanged `accord` token; a token
  given with `confirm=true` opens no dialog.
- Structured output: a real `outputSchema` and `structuredContent` for `search_threads`, `get_message`,
  `get_thread`, `list_new_messages`, `list_labels`, `list_drafts`, `get_draft` and `auth_status` (the same object as
  the JSON text, kept for older clients); errors set `isError`. Action tools return text only, without the former
  opaque `{"result": string}` schema.
- `list_new_messages` tool: messages that arrived since the previous call, from Gmail history
  (`users.history.list`, `messageAdded`), oldest first, without bodies. The position (`historyId`) is remembered per
  account and label in `history.json` in the state folder; the first call only sets it; pages (`pageSize`, `more`),
  peek without moving (`advance=false`), resume from a `startHistoryId`. Expired history (404, about a week): the
  messages since the last check are found by date (`after:`, `mode: catch_up`), or it restarts from now when no date
  is known (`mode: reset`). Drafts, spam and trash are never listed.

## [0.10.4] - 2026-10-27

### Changed
- Sharper Antigravity plugin logo (256 px): the julienpiron.fr medallion rebuilt at 600 px in the icon kit, sharp up to 512 px instead of a 240 px picture enlarged.

## [0.10.3] - 2026-10-26

### Added
- Nexus family icon: the NexusAGYmail envelope in the julienpiron.fr medallion, the same as on the site. It is the
  logo of the Antigravity plugin (`plugin/logo.png`, 256 px, `logo` key of `plugin.json`, copied by `setup`) and
  heads the README.

## [0.10.2] - 2026-10-26

### Changed
- NexusAGYmail is presented as what it is: Gmail for Google Antigravity (README, PyPI and MCP registry
  descriptions, command help); the Claude Desktop, Cursor and Codex configuration snippets are removed.

## [0.10.1] - 2026-10-26

### Changed
- Guided sign-in, after a real run on a Google account with no project: it first asks which Gmail address to
  connect, then opens every console page on that account (`authuser`) and suggests it at sign-in (`login_hint`).
- Step 3: Google now refuses to publish the app without a home page, privacy policy link and authorized domain; the
  assistant gives the values (the project's GitHub page, the README's Privacy section, `github.com`).
- An unpublished app is blocked at sign-in ("Error 403: access_denied"), not after 7 days: the assistant says so and
  what to do.
- Step 4: leave Google's new "Use this client for an AI-powered agent" box unchecked.
- Ctrl+C during a question or the sign-in prints "Cancelled." (exit code 130) instead of a traceback.

## [0.10.0] - 2026-10-26

### Added
- `get_draft(draftId, maxChars)`: reads one draft (recipients, subject, body as framed text, attachments, reply
  header), never sends it.
- `create_label(displayName, autoCreateParentLabels)`: creates a user label (`A/B` = nested, missing parents created
  by default); an existing label is returned as it is, system labels are refused.

## [0.9.0] - 2026-10-26

### Added
- Guided first sign-in, started by `setup` (interactive, when there is no token) or by `auth`: it opens each Google
  Cloud console page at the right place (project, Gmail API, consent screen, publishing, Desktop client), says what
  to click, and finds the downloaded `client_secret_….json` by itself (Ctrl+C to type a path). No shared OAuth
  client.
- Checks after signing in: Gmail API turned off, or app left in Testing (Google returns a 7-day
  `refresh_token_expires_in`): the page that fixes it opens, then a new sign-in; after three failures, a pointer to
  Google's official Gmail MCP server.
- README: two-step Quick start, and when to prefer Google's own server or built-in connectors.

### Changed
- `auth`: a client that is not a Desktop app is refused with a message (exit code 1).

## [0.8.1] - 2026-10-26

### Added
- Status file `~/.nexus/status/agymail.json` (`product`, `version`, `latest`, `state`, `checked_at`, `message`),
  written by the server at startup in the background; `latest` is read from PyPI at most once a day.
  `NEXUS_AGYMAIL_UPDATE_CHECK=0` turns the request off.

### Changed
- `setup` declares `uvx --python 3.14 nexus-agymail@latest` to Antigravity, so every start of Antigravity runs the
  latest published version. A pinned entry (`==x`) written by an earlier version is replaced.

## [0.8.0] - 2026-10-25

First public release.

### Added
- MCP server `nexus-agymail` for Gmail through the official Gmail API v1, with the tool names and parameters of
  Google's official Gmail MCP server: `search_threads`, `get_message`, `get_thread`, `list_labels`, `create_draft`,
  `list_drafts`, `update_draft`, `label_message`, `unlabel_message`, `label_thread`, `unlabel_thread`,
  `trash_message`, `trash_thread`, `untrash_message`, `untrash_thread`; plus `send_draft`, `download_attachment`
  and `auth_status`.
- Sending only through `send_draft` with `confirm=true` and a single-use `accord` token bound to the exact draft
  (recipients, subject, full body, every part, SHA-256 of each attachment); the draft is read again before sending.
- No permanent deletion: the `https://mail.google.com/` scope is never requested; reversible trash only.
- Email content framed as untrusted data, hidden HTML text and invisible characters removed, `injection_warning` on
  emails that look like instructions aimed at an AI, `provenance` on recipients found in emails read.
- Attachments: saved under a sanitized name, never overwriting, with the Windows mark of the web; local files never
  attached from secrets locations (`NEXUS_AGYMAIL_PROTECTED` adds your own folders).
- Commands: `nexus-agymail auth` (your own Desktop app OAuth client, browser consent), `nexus-agymail setup`
  (Antigravity plugin with a skill; `NEXUS_PACK=1` for the Nexus pack), `nexus-agymail status`.
- Token encrypted with DPAPI on Windows, a `0600` file on macOS and Linux, written atomically under a lock.
- Retries with exponential backoff on 429 and 5xx, batched reads, bounded sizes, exact error codes.
