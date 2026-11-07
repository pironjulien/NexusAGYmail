# Contributing

## Environment

- Windows 10 or 11, the .NET SDK pinned by `global.json`, PowerShell 7;
- no Google account is needed by the tests: they run against a fake Gmail API and a fake Google on loopback.

## Rules

1. Open an issue or a pull request against `main`. A change is accepted only when `./test.ps1` passes: the CI
   (`.github/workflows/tests.yml`) runs that same script on every push and pull request.
2. **Every new feature and every bug fix comes with its test** in `tests/` (xUnit v3), against `FakeGmail` with an
   isolated home and fake credentials; no test calls Google or reads the real token. A fix starts with a test that
   fails without it.
3. Keep the safeguards: a send only with the user's approval of the exact bytes and a single-use accord, secret files
   refused as attachments, header injection refused.
4. Never commit a secret, an OAuth token, a client secret, a real message or a build output: builds and test outputs go
   to `~\.nexus\build\nexusagymail`, outside the repository.
5. Comments, commit messages and `CHANGELOG.md` entries are written in English. `CHANGELOG.md` follows Keep a Changelog
   1.1.0: add your entry under `[Unreleased]`; every version heading has its comparison link (`ChangelogTests`).

## Checks

```powershell
./test.ps1    # dotnet test: xUnit v3 suite, no network, no Google account
git diff --check
```

`NEXUS_AGYMAIL_TEST_EXE` runs the suite against an executable already built (what `install.ps1` qualifies).

## Version

One version everywhere: `native/NexusAGYmail.csproj` sets it; `native/plugin/plugin.json`, the skill, `server.json`
and the newest section of `CHANGELOG.md` follow. The `Release` workflow (`.github/workflows/release.yml`) checks them,
runs `test.ps1`, builds with `tools/package.ps1`, attests and publishes a development build on every push to `main`.
