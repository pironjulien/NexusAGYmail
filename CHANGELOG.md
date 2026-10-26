# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
