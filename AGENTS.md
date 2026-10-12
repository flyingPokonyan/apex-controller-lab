# Controller operation and delivery

- Preserve local private configs, runtime artifacts and unrelated edits. Never commit account data, tokens, screenshots or generated logs.
- EA registration runs on Windows with BitBrowser and shares the desktop lock with game work. Use `windows\account-cycle.cmd`; an explicit safe-gap update uses `windows\update.cmd`.
- Publish: run changed-module tests once, commit, then push GitHub `main` without force. `.github/workflows/notify-forge-release.yml` validates Windows and notifies Forge. Never claim a push proves a Runner installed it; inspect Forge's version status.
- The Forge server is `inkwild`. Its repository owns deployment: GitHub → `bash scripts/deploy.sh`, using cached builds. No routine database backups or source archive uploads; Roxy is retired.
- Diagnose from Forge first: Apex → Runner → details → download diagnostics, or `ssh inkwild "docker exec apexforge python /app/backend/desktop_registration/support_cli.py D163"`. Do not use remote desktop if the central evidence is sufficient.
- Registration journal uploads are task-bound, idempotent and cursor-acknowledged; upload only `.support.png` screenshots created with masks. Never send original screenshots, arbitrary stdout/stderr, account resources, passwords, tokens, cookies or OTPs to diagnostic endpoints.
- An explicit test reserves one Runner, waits for its current lease to finish, and defaults to staying in test mode. A register-then-game test must use its own newly registered account. Never interrupt a running game merely to speed a rollout.
