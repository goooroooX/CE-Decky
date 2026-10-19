# Decky compatibility

Whether the latest published CE Decky release still works in Decky's latest stable release and its newest prerelease.
Each pair runs the probe from that release's own tag, beside a control run in the Decky the release was pinned to.
These are backend and package checks on Linux x86-64; the Quick Access Menu, controller navigation and Proton are not tested.

## Now

| CE Decky | Decky | Result | Checked | Detail | Key |
|---|---|---|---|---|---|
| 0.9.29 | [v3.3.0](https://github.com/SteamDeckHomebrew/decky-loader/releases/tag/v3.3.0) (stable) | UNSUPPORTED | [2026-10-19 12:22 UTC](https://github.com/goooroooX/CE-Decky/actions/runs/39251444241/attempts/1) | CE Decky 0.9.29 has no compatibility probe at its tag; publish a release that has one. | `github/0.9.29/9b0705820a192e12+v3.3.0/e80d03e641d1766a` |

To test a pair again, run the workflow by hand with `retest` set to its key, or to `current`.
