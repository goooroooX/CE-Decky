from __future__ import annotations

from argparse import ArgumentParser
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
CHANGELOG_LABELS = {"New", "Changed", "Fixed", "Security", "Limitation"}


def _unverified_subjects() -> list[str]:
    """Subjects `docs/FIELD_NOTES.md` still lists as unverified.

    Metadata and a green QA run prove the package is well formed. They cannot
    prove behaviour that has only ever been checked on a device, and a tag
    otherwise walks straight from those checks to a published GitHub Release.
    The authority is the one section of a publication document whose whole job
    is to say what is still owed, so closing the last row is what permits a
    stable tag, and there is no second list to keep in step with it.
    """
    notes = ROOT / "docs" / "FIELD_NOTES.md"
    if not notes.is_file():
        return []
    lines = notes.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("## 5. Still worth checking"))
    except StopIteration:
        # The file is here and the section is not, which is a shape change
        # rather than an all-clear. Reading it as one is how an unproven build
        # reaches a published stable release.
        raise SystemExit(
            f"{notes.relative_to(ROOT)} has no 'Still worth checking' section; repair it, or "
            "retire this check deliberately together with the section it reads"
        ) from None
    subjects: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        subject = cells[0] if cells else ""
        if not subject or subject == "Subject" or set(subject) <= set("-: "):
            continue
        subjects.append(subject)
    return subjects


def _release_notes(tag: str) -> Path:
    """The file published as this release's description.

    Written for somebody deciding whether to install the release, and reviewed
    in the commit the tag points at rather than typed into the web form after
    the workflow has already published. A tag without one is refused here, where
    it costs nothing, rather than by the release step after the archives are up.
    """
    notes = ROOT / "docs" / "release-notes" / f"{tag}.md"
    if not notes.is_file():
        raise SystemExit(
            f"missing release notes for {tag}: write {notes.relative_to(ROOT)}, "
            "see docs/release-notes/README.md"
        )
    if not notes.read_text(encoding="utf-8").strip():
        raise SystemExit(f"{notes.relative_to(ROOT)} is empty")
    return notes


def main() -> None:
    parser = ArgumentParser(description="Validate CE Decky release metadata.")
    parser.add_argument("--tag", help="Git tag to validate, for example v0.3.0")
    args = parser.parse_args()

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = package.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise SystemExit(f"package.json version is not valid SemVer: {version!r}")
    if package.get("packageManager") != "pnpm@9.15.9":
        raise SystemExit("packageManager must remain pinned to pnpm@9.15.9")

    backend_init = (ROOT / "py_modules" / "ce_decky" / "__init__.py").read_text(encoding="utf-8")
    backend_version = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']$', backend_init, re.MULTILINE)
    if backend_version is None or backend_version.group(1) != version:
        raise SystemExit("backend __version__ does not match package.json")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "Unreleased" in changelog:
        raise SystemExit("CHANGELOG.md must not contain an Unreleased section")
    heading = re.compile(rf"^## {re.escape(version)} — \d{{4}}-\d{{2}}-\d{{2}}$", re.MULTILINE)
    changelog_heading = heading.search(changelog)
    if changelog_heading is None:
        raise SystemExit(
            f"CHANGELOG.md needs the current version heading: ## {version} — YYYY-MM-DD"
        )
    changelog_date = changelog_heading.group(0).rsplit(" — ", 1)[1]
    current_section = changelog[changelog_heading.end():]
    next_heading = re.search(r"^## ", current_section, re.MULTILINE)
    if next_heading is not None:
        current_section = current_section[:next_heading.start()]
    for line in current_section.splitlines():
        if not line.startswith("- "):
            continue
        label = re.match(r"^- \[([^]]+)] ", line)
        if label is None or label.group(1) not in CHANGELOG_LABELS:
            allowed = ", ".join(f"[{item}]" for item in sorted(CHANGELOG_LABELS))
            raise SystemExit(f"current CHANGELOG.md entries must use an allowed label: {allowed}")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if f"Current development version: **{version} — {changelog_date}**" not in agents:
        raise SystemExit("AGENTS.md current development version/date does not match CHANGELOG.md")

    if args.tag:
        expected_tag = f"v{version}"
        if args.tag != expected_tag:
            raise SystemExit(f"release tag {args.tag!r} does not match {expected_tag!r}")
        _release_notes(args.tag)
        if "-" not in version:
            unverified = _unverified_subjects()
            if unverified:
                raise SystemExit(
                    "a stable tag is refused while work is still unverified: "
                    f"{'; '.join(unverified)}. Settle them and remove their rows from "
                    "docs/FIELD_NOTES.md, or tag a prerelease by setting a prerelease version "
                    f"in package.json first (for example {version}-rc.1) and tagging v{version}-rc.1."
                )
            readme = (ROOT / "README.md").read_text(encoding="utf-8")
            if "> **Pre-release.**" in readme:
                raise SystemExit(
                    "nothing is left to settle but README.md still carries its pre-release "
                    "notice; remove it in the same change that publishes."
                )

    print(f"release metadata: PASS ({version})")


if __name__ == "__main__":
    main()
