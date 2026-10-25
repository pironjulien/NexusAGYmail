# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
