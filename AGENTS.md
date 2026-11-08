# Development instructions

## Decisions

- NexusAGYmail exists because Antigravity has no official Gmail connector (only Drive and Calendar): never suggest
  checking for an official one or imply the product is redundant. Read the README before proposing a direction.
- For Antigravity only: README, posts, directories, site and images never present it as usable by Claude, Codex,
  Cursor or "any MCP AI" (they have their own Gmail connectors).
- Nothing paid: no Google OAuth app verification (annual security audit for restricted Gmail scopes); each user keeps
  their own OAuth client.
- One account only, no multi-account. In the NexusSystem app: show the signed-in account and a "Use another account"
  button that runs `NexusAGYmail.exe auth`.
