"""Standalone badge checks: python3 -m unittest discover -s tests -p test_readme_badges.py.

Uses only the standard library, without pytest's plugin-importing conftest.
The real workflow shell step runs against a local stand-in for GitHub CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "readme-badges.yml"
COUNT_STEP = textwrap.dedent(
    WORKFLOW.read_text(encoding="utf-8").split("        run: |\n", 1)[1]
    .split("      - name: Upload badge data", 1)[0]
)


def release(count: int, *, draft: bool = False, prerelease: bool = False) -> dict:
    return {
        "draft": draft,
        "prerelease": prerelease,
        "published_at": None if draft else "2026-11-01T00:00:00Z",
        "assets": [{"name": "CE-Decky-v0.9.33.zip", "download_count": count}],
    }


class ReadmeBadgeTests(unittest.TestCase):
    def run_count(self, pages, *, api_exit: int = 0, previous: str | None = None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "bin" / "gh"
            fake.parent.mkdir()
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "assert sys.argv[1:4] == ['api', '--paginate', '--slurp']\n"
                "assert sys.argv[-1] == 'repos/example/project/releases?per_page=100'\n"
                "sys.stdout.write(os.environ['BADGE_TEST_RESPONSE'])\n"
                "sys.exit(int(os.environ['BADGE_TEST_API_EXIT']))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            output = root / "_site" / "downloads.json"
            if previous is not None:
                output.parent.mkdir()
                output.write_text(previous, encoding="utf-8")
            answer = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", COUNT_STEP],
                cwd=root,
                env={**os.environ, "PATH": str(fake.parent) + os.pathsep + os.environ["PATH"],
                     "GITHUB_REPOSITORY": "example/project", "BADGE_TEST_RESPONSE": json.dumps(pages),
                     "BADGE_TEST_API_EXIT": str(api_exit)},
                capture_output=True, text=True, timeout=10,
            )
            return answer, output.read_text(encoding="utf-8") if output.exists() else None

    def test_only_plugin_zips_from_published_releases_including_prereleases(self):
        stable = release(1234)
        stable["assets"] += [
            {"name": name, "download_count": 9999}
            for name in ("CE-Decky-v0.9.33.zip.sha256", "SHA256SUMS", "source.zip", "other.zip")
        ]
        unpublished = release(99)
        unpublished["published_at"] = None
        answer, data = self.run_count([[stable, release(7, prerelease=True), release(9999, draft=True), unpublished]])
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(data), {"schemaVersion": 1, "label": "Downloads", "message": "1241", "color": "blue"})

    def test_all_pages_are_counted_beyond_the_first_hundred_releases(self):
        answer, data = self.run_count([[release(1) for _ in range(100)], [release(7)]])
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(data)["message"], "107")

    def test_api_failure_does_not_replace_the_previous_count_with_a_partial_one(self):
        answer, data = self.run_count([[release(3)]], api_exit=1, previous="last good count")
        self.assertNotEqual(answer.returncode, 0)
        self.assertEqual(data, "last good count")

    def test_malformed_later_page_or_count_does_not_publish_a_partial_result(self):
        for pages in ([[release(3)], {"message": "API error"}], [[release(3), release(-1)]], [[release(True)]]):
            with self.subTest(pages=pages):
                answer, data = self.run_count(pages, previous="last good count")
                self.assertNotEqual(answer.returncode, 0)
                self.assertEqual(data, "last good count")

    def test_a_valid_empty_release_list_can_report_zero(self):
        answer, data = self.run_count([[]])
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(data)["message"], "0")

    def test_readme_badges_share_one_line_and_use_the_expected_urls(self):
        lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "# CE Decky")
        badges = re.findall(r"\[!\[([^]]+)\]\(([^)]+)\)\]\(([^)]+)\)", lines[2])
        self.assertEqual([label for label, _, _ in badges], ["Downloads", "Release", "License"])
        for _, source, _ in badges:
            self.assertEqual(urlsplit(source).netloc, "img.shields.io")
            self.assertEqual(parse_qs(urlsplit(source).query)["style"], ["flat-square"])
        endpoint = parse_qs(urlsplit(badges[0][1]).query)["url"]
        self.assertEqual(endpoint, ["https://gooorooox.github.io/CE-Decky/downloads.json"])
        self.assertEqual(badges[0][2], "https://github.com/goooroooX/CE-Decky/releases")
        self.assertEqual(badges[1][2], "https://github.com/goooroooX/CE-Decky/releases/latest")
        self.assertTrue((ROOT / badges[2][2]).is_file())


if __name__ == "__main__":
    unittest.main()
