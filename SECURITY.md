# Security policy

NexusAGYmail gives an AI agent access to a real mailbox, so safety issues matter more than usual: a way to make an
agent send an email without the user's `confirm=true` and the matching `accord` token, to send a draft that changed
after the user approved it, to delete anything for good, to attach a file from a secrets location, to overwrite a
file when saving an attachment, or to read the OAuth token.

Please report such issues privately through
[GitHub private vulnerability reporting](https://github.com/pironjulien/NexusAGYmail/security/advisories/new),
not in a public issue. Include the tool calls, the kind of email involved and what happened. You will get an answer
within a few days.

## Scope and limits

- **Prompt injection**: emails are framed as data and instructions aimed at an AI are flagged, but no filter can
  recognize every manipulation. The real protection is structural: sending needs the user's yes for the exact draft,
  and no tool deletes for good. A model that ignores the framing can still read, draft, label or trash.
- **Local token**: on Windows the refresh token is encrypted with DPAPI for the current account; on macOS and Linux it
  is a file readable only by your account. Neither protects against malicious software running under your account.
- **Your OAuth client**: it lives in your own Google Cloud project; anyone with its JSON and your consent could use it.
  Keep it private, and revoke access in your Google account if it leaks.

Only the latest release is supported.
