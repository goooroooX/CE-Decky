# Release notes

One file per tag, `v<version>.md`, published as the GitHub Release body with the commit list appended. `scripts/check_release.py` refuses a tag whose file is missing or empty.

The same file is what CE Decky's own update window shows a user before they press **Update**, so it has two readers: the release page and a confirmation on a handheld screen. The window reads the body up to the marker the release workflow writes before the commit list, and leaves out the title and `## Install` with everything after it, because the window already names both versions and is itself the install.

## Rules

They are short, and written for a user deciding whether to update: they know the panel and their games, not how CE Decky is built. `CHANGELOG.md` is the complete record; this is not a copy of it.

- **One sentence on why to update**, under the title. The window shows it alone under the two versions, as the reason for the press, so it must read on its own: no "also", no "this release", nothing that needs the lines after it. One paragraph, so the window takes all of it.
- **New capabilities first.** When a release adds something new, the opening sentence names the main new capability and `## New` is the first changes section. Fixes and changed behavior follow it. Within each section, rank by user impact rather than the order work landed in `CHANGELOG.md`.
- **One short line per change**, only for what a user would notice: what they can now do, or what no longer goes wrong for them. Use the names on their screen. Say harm plainly (a crash, a cheat switching itself on, a failed start), because that is the reason to update.
- **Never how.** No module, function, file, Python version or other internals; a cause only when the user must act on it, such as an affected Decky Loader version.
- **Leave out** what a user would never notice: internals, tests, tooling, logging, docs.
- Sections in this order: `## New`, `## Fixes`, `## Changed`, omitting empty sections; then `## Install` (how, and that tables, authorizations and profiles are kept) and the requirements line.
- Fits on one screen: usually three to eight lines of changes. No game, device or account names.
- Only the markup the window draws: `##` headings, one-line `- ` items, `**bold**` for the names on screen and `` `code` `` for a file or program name. A link keeps its words and loses its target there, so never make a link the only place something is said. The window keeps the first 4 KB and caps it at forty lines, far beyond a release that follows these rules.
- `## Install` is the last section and is for the release page only, since the window leaves it out: nothing a user needs before pressing **Update** may live there.

## Examples

| Not this | This |
|---|---|
| It moved Decky's built-in Python from 3.11 to 3.14 and left out `pathlib.types`, which CE Decky's networking library needs. | Search, downloads and updates work again on Decky Loader 3.3.0. |
| A deletion the backend refuses no longer clears panel state. | A table that cannot be deleted no longer vanishes from the panel. |
| Auto-load writes a value only for a cheat it switches on, or for a setting of a script it switches on. | **Load last table & cheats** no longer fails because of a value set for a cheat that was off. |
