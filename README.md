# CE Decky compatibility monitor state

This branch is written by `.github/workflows/decky-compatibility.yml` on `main`,
and by nothing else. It shares no history with `main`: never merge it, rebase it
or open a pull request from it, and do not delete it, because its ledger is the
only record of which pairs were already tested.

- `STATUS.md` is the current answer.
- `decky-compatibility.json` is the ledger the workflow reads and writes.

Each commit says what changed. The procedure is in `docs/DEVELOPMENT.md` on
`main`, under **Decky runtime compatibility and upstream watch**.
