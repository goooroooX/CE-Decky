# Release notes

One file per tag, named `v<version>.md`, published as the body of that GitHub Release with the generated commit list appended under it.

It is written for somebody deciding whether to install the release, so it is short: what is new, what is fixed, and how to install it. One line per change, no explanation of how the change was made. `CHANGELOG.md` is the complete record and is written for a different reader; this is not a copy of it.

`scripts/check_release.py` refuses a tag whose file is missing, so the notes are reviewed in the commit the tag points at rather than typed into the web form afterwards.
