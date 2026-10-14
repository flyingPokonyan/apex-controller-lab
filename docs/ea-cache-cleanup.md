# EA cache cleanup at the end of an account

After Apex exits, terminal lease cleanup now selects **Menu → Help → App Recovery → Clear cache**, waits for the old EA window to close and a new readable home/login surface to settle, then runs the existing verified sign-out. It leaves EA at the login page for the next account. Already signed-out/pre-login sessions skip App Recovery. Startup repair, update handling, leftover-account sign-out and registration-to-game handoff retain their existing behavior.

The confirmation requires the App Recovery restart title plus Clear cache and Cancel controls. The cache action never starts a second EA process while App Recovery may be restarting it. A missing dialog, unchanged old window, or restart timeout pauses the same lease with `EA_CACHE_CLEANUP_FAILED`. The game result and target/fence are preserved; clearing or restarting EA is not sign-out evidence.

The existing `EA_SIGNING_OUT` checkpoint has an additional `eaCacheCleanup` field: `REQUESTED` is persisted before Clear cache is clicked; `COMPLETE` follows a verified restart; `SKIPPED_NOT_SIGNED_IN` records a pre-login page. A process interruption in `REQUESTED` requires operator inspection and does not automatically retry or assume success. After inspecting the desktop, the existing explicit resume action permits one new attempt. A completed clear is retained across sign-out retries. No checkpoint or runtime files should be deleted to recover this state.

Diagnostics distinguish `cache-clear-requested`, `cache-clear-window-exited`, `cache-clear-restart-ready`, `cache-clear-skipped-login`, and the existing `signed-out` event. Deploy Forge's diagnostic allowlist before releasing the Controller. Synthetic regressions cover menu selection, confirmed transitions, failure retention and restart reconciliation; Windows UI behavior still needs an actual account-end observation after installation.

EA describes App Recovery as clearing EA App cache to address library, download and update issues. It may empty the download queue and may require signing in again. EA does not publish an exhaustive list of files/cookies/tokens removed; this workflow uses only the official UI and does not delete game directories or manually erase account data.

Sources: [EA cache recovery](https://help.ea.com/en/articles/technical-issues/clear-cache/), [EA download queue](https://help.ea.com/en/articles/platforms/download-and-play-ea-app-games/).
