# Documentation

The documents in this directory have distinct roles. Keep a fact in the narrowest authoritative document instead of copying it into several files.

| Document | Purpose |
|---|---|
| [DESIGN.md](DESIGN.md) | Normative product behavior, scope, reuse decisions, and user workflow |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Production components, data flow, and system boundaries |
| [SECURITY.md](SECURITY.md) | Threat model and mandatory trust boundaries |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Windows and SteamOS development, validation, and packaging workflow |
| [REMOTE_TARGET.md](REMOTE_TARGET.md) | Driving a SteamOS device, such as a Steam Deck, over SSH from the machine the checkout is on |
| [FIELD_NOTES.md](FIELD_NOTES.md) | What real hardware established about Steam, Proton and Cheat Engine, which table sources were evaluated and on what ground, which upstream commits a behavior was read from, and what is still unverified |
| [UI_ACTION_AUDIT.md](UI_ACTION_AUDIT.md) | Every plugin-owned control, the functional route it takes and who owns the result, plus what the verification of those routes cannot reach. `tests/uiActionCoverage.test.ts` reads its inventory, so a control added without a row here fails that test |

`FIELD_NOTES.md` is also read by `scripts/check_release.py`: while its **Still worth checking** section holds a row, a stable release tag is refused. Removing the last row is what permits one, so keep that section true.
