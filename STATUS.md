# Decky compatibility

Whether the latest published CE Decky release still works in Decky's latest stable release and its newest prerelease.
Each pair runs the probe from that release's own tag, beside a control run in the Decky the release was pinned to.
These are backend and package checks on Linux x86-64; the Quick Access Menu, controller navigation and Proton are not tested.

## Now

| CE Decky | Decky | Result | Checked | Detail | Key |
|---|---|---|---|---|---|
| 0.9.32 | [v3.3.0](https://github.com/SteamDeckHomebrew/decky-loader/releases/tag/v3.3.0) (stable) | PENDING | waiting since 2026-10-26 14:24 UTC | Not tested yet. | `github/0.9.32/146dcda009e695b0+v3.3.0/e80d03e641d1766a` |

## Earlier pairs

| CE Decky | Decky | Result | Checked |
|---|---|---|---|
| 0.9.31 | v3.3.0 | COMPATIBLE | [2026-10-24 12:06 UTC](https://github.com/goooroooX/CE-Decky/actions/runs/40092499016/attempts/1) |
| 0.9.30 | v3.3.0 | COMPATIBLE | [2026-10-19 12:47 UTC](https://github.com/goooroooX/CE-Decky/actions/runs/39254729647/attempts/1) |
| 0.9.29 | v3.3.0 | UNSUPPORTED | [2026-10-19 12:22 UTC](https://github.com/goooroooX/CE-Decky/actions/runs/39251444241/attempts/1) |

To test a pair again, run the workflow by hand with `retest` set to its key, or to `current`.
