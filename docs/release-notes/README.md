# Release notes

One file per tag, `v<version>.md`, published as the GitHub Release body with the commit list appended. `scripts/check_release.py` refuses a tag whose file is missing or empty.

## Rules

They are short, and written for a user deciding whether to update: they know the panel and their games, not how CE Decky is built. `CHANGELOG.md` is the complete record; this is not a copy of it.

- **One sentence on why to update**, under the title.
- **Rank by what the release is for, not by `CHANGELOG.md`**, whose newest-first order is when the work landed. The opening sentence and the first lines are the changes that let a user do what they could not do at all; a fix for something they could already get past on screen, such as by pressing **Stop CE**, comes after them and stays out of the opening sentence.
- **One short line per change**, only for what a user would notice: what they can now do, or what no longer goes wrong for them. Use the names on their screen. Say harm plainly (a crash, a cheat switching itself on, a failed start), because that is the reason to update.
- **Never how.** No module, function, file, Python version or other internals; a cause only when the user must act on it, such as an affected Decky Loader version.
- **Leave out** what a user would never notice: internals, tests, tooling, logging, docs.
- Sections `## New`, `## Fixes`, `## Changed`, only those that have something, most important first; then `## Install` (how, and that tables, authorizations and profiles are kept) and the requirements line.
- Fits on one screen: usually three to eight lines of changes. No game, device or account names.

## Examples

| Not this | This |
|---|---|
| It moved Decky's built-in Python from 3.11 to 3.14 and left out `pathlib.types`, which CE Decky's networking library needs. | Search, downloads and updates work again on Decky Loader 3.3.0. |
| A deletion the backend refuses no longer clears panel state. | A table that cannot be deleted no longer vanishes from the panel. |
| Auto-load writes a value only for a cheat it switches on, or for a setting of a script it switches on. | **Load last table & cheats** no longer fails because of a value set for a cheat that was off. |
