"""This plugin's own records out of the system journal, where they outlive.

Decky writes one log file per plugin load and keeps only the last few, so the
file holding a failure is routinely gone by the time anybody looks for it: every
install rewrites the set, and development here installs constantly.  That is
already recorded in `docs/FIELD_NOTES.md` as the reason one reported refusal
could not be diagnosed, and it is why the 2026-09-12 panel-close bundle carried
no backend log at all.

The same records also go to the journal, because Decky's loader runs under
systemd and a plugin's output is its stdout.  The journal keeps them across the
plugin reload, the webhelper restart and the reboot, which is exactly the window
a support bundle is collected in.  So the bundle takes them from there too.

**Decky's own lines may not be visible at all.**  The loader runs as root and
writes its lifecycle records as root; this plugin's backend is spawned with
`setuid` to the host user and with no supplementary groups, and journald shows
such a process only the records that user produced.  So a window collected from
the panel routinely holds this plugin's own records and none of Decky's, and it
says so rather than letting four thousand lines read as a complete journal.  The
operator's own route, `scripts/target_plugin_log.py --journal`, runs as a login
that is in the journal group and sees both.

**Only this plugin's own lines are collected.**  Every Decky plugin logs under
the same `PluginLoader` identifier, and this archive is attached to public
issues.  Another plugin's output is not ours to publish, so a line is kept only
if it carries this plugin's own record prefix, or is one of the small set of
loader lines that name this plugin with the version the loader read for it, or
is one of the host lifecycle lines that say the renderer or the plugin host
restarted, which is the thing an incident of this class turns on.  Everything
else is counted and dropped, and the count is reported so a reader knows the
window was not empty.

Each kept shape is written out in full and matched to the end of the message,
which is the property that matters: nothing about a log record's look, this
plugin's name occurring in it, or the component tag it opens with says who wrote
it, and a rule that accepts an arbitrary suffix after a phrase Decky writes
accepts whatever a foreign plugin chose to put there.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from .activity_log import ACTIVITY_PREFIX
from .child_env import child_environment


# Our own activity records, written by `activity_log.log_activity`. Anchored on
# the ownership prefix that module writes: `activity event=` on its own is a
# shape any plugin sharing this identifier could produce, and one that did would
# have been collected here and published in a public archive as ours.
_OURS = re.compile(r"^" + re.escape(ACTIVITY_PREFIX) + r" activity event=")
# The name this plugin is registered under, which is how Decky refers to it,
# always with the version the loader read from its manifest beside it.
_PLUGIN_NAME = "CE Decky"
_NAMED_VERSION = re.escape(_PLUGIN_NAME) + r" \(v[0-9][0-9A-Za-z.+-]{0,31}\)"
# The component tag and level each half of Decky writes. This is message text
# rather than provenance: journald records who wrote a line, but not through
# this identifier, and any plugin sharing it prints its own `[main][INFO]: `
# whenever it likes. So the tag narrows nothing by itself, and every rule below
# carries the whole of its message instead of leaning on it.
_TAG = r"\[(?:main|loader|plugin|injector|localplatformlinux)\]\[[A-Z]{1,12}\]: "


def _exactly(*shapes: str) -> re.Pattern[str]:
    """One of these messages, from one of Decky's components, and nothing more.

    Bounded on both ends on purpose.  An unbounded suffix after a phrase Decky
    writes is the whole of what a foreign plugin needs: it prints the same
    common tag, begins a message with the same words, and continues with its own
    paths, game names and user data, which this then publishes under this
    plugin's name in an archive attached to a public issue.
    """
    return re.compile(_TAG + "(?:" + "|".join(shapes) + r")\Z")


# Decky's own lines about this plugin: loaded, already loaded, a method of ours
# that raised, shut down, stopped, killed after a stop request.
#
# Deliberately not "the line mentions CE Decky somewhere", and no longer "the
# loader's tag and this plugin's name near the start, then whatever follows".
# Both keep a foreign plugin's line: the first because this plugin's name occurs
# in another plugin's failure text as readily as in Decky's own, the second
# because nothing in that shape is out of a foreign plugin's reach either.
#
# The trade runs the other way too, and it is the deliberate half: a loader line
# that is not written out here is dropped and counted like anything else,
# including one a later Decky version words differently. A record missing from a
# bundle is still readable from the device with
# `scripts/target_plugin_log.py --journal`; another plugin's private path
# published under this plugin's name cannot be taken back.
_NAMED = _exactly(
    r"Loaded " + _NAMED_VERSION,
    r"Plugin " + _NAMED_VERSION + r" is already loaded and has requested to not be re-loaded",
    r"Plugin " + _NAMED_VERSION + r" has been stopped in [0-9]{1,4}\.[0-9]{1,3}s",
    r"Plugin " + _NAMED_VERSION + r" still alive [0-9]{1,4} seconds after stop request! Sending SIGKILL!",
    r"Shutting down " + _NAMED_VERSION,
    r"Stopping response listener for " + _NAMED_VERSION,
    r"Method [A-Za-z_][A-Za-z0-9_]{0,63} of plugin " + _NAMED_VERSION
    + r" failed with the following exception:",
)
# Loader and host lifecycle. No plugin state and no user data appear in these,
# and they are what distinguishes "the panel stopped" from "the renderer was
# replaced under it", which is the whole question for a wedge.
#
# Written out in full for the same reason as above, with only the counts and the
# version left open. A `$` here once silently stopped matching
# `CEF has disconnected...`, which is why the rule that replaced it accepted any
# message from those components that mentioned a webhelper and published a
# foreign plugin's paths and game names; the answer to a phrase that carries
# trailing detail is to write the detail down, not to stop bounding the line.
#
# These are the six shapes this device's journal carries for Decky's own halves
# over a week of development, and they are the whole of what it carries. The one
# the crash-window diagnosis turns on is the crash count: three replacements
# inside a minute and Decky stops its own service.
_LIFECYCLE = _exactly(
    r"Restarting steamwebhelper",
    r"CEF has disconnected\.\.\.",
    r"The Tab SharedJSContext socket has been disconnected while listening for messages\.",
    r"Loading Decky frontend!",
    r"Starting Decky version v[0-9][0-9A-Za-z.+-]{0,31}",
    r"webhelper crashed within a minute from last crash! crash count: [0-9]{1,9}",
)


# What one message is, for the two callers that need to tell them apart.
OURS = "ours"
DECKY = "decky"
FOREIGN = "foreign"


def _classify(message: str) -> str:
    """Who wrote this message: us, Decky about us, or somebody else entirely."""
    if _OURS.search(message):
        return OURS
    if _NAMED.match(message) or _LIFECYCLE.match(message):
        return DECKY
    return FOREIGN


DEFAULT_IDENTIFIER = "PluginLoader"
# How far back to ask for. A bundle is collected about something that just
# happened; a longer window is a bigger archive and not a better answer.
DEFAULT_SINCE = "-6h"
# Lines kept. The journal is bounded by the window above as well, and this is
# the hard stop for a device that logs unusually hard.
MAX_LINES = 4000
# The subprocess never runs longer than this. A bundle must be produced even on
# a device whose journal is slow or whose `journalctl` hangs.
TIMEOUT_SECONDS = 20.0


def available() -> bool:
    """Whether this host has a journal to read at all."""
    return shutil.which("journalctl") is not None


def collect(
    *,
    identifier: str = DEFAULT_IDENTIFIER,
    since: str = DEFAULT_SINCE,
    max_lines: int = MAX_LINES,
) -> dict[str, object]:
    """Read the window, keep our own lines, and say what was dropped.

    Never raises.  A journal that cannot be read is a note in the manifest, not
    the reason a user has nothing to attach.
    """
    binary = shutil.which("journalctl")
    if binary is None:
        return {"ok": False, "reason": "journalctl is not present on this host", "lines": []}
    command = [
        binary,
        "-t", identifier,
        "--since", since,
        "--no-pager",
        "-o", "short-iso",
        # Bound what the journal hands back before any of it is filtered, so a
        # device that logged a great deal does not pay for it in memory.
        "-n", str(max(max_lines * 8, max_lines)),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            # Decky's loader is a PyInstaller bundle and points the dynamic
            # loader at its own libraries; a system binary that inherits that
            # resolves the bundle's copies instead of the system's. `journalctl`
            # is one, and on this device it failed every time with
            # `libcrypto.so.3: version OPENSSL_3.4.0 not found`, so a support
            # bundle collected from the panel carried no journal at all and said
            # so in one manifest note. Every other process this plugin starts
            # already goes through `child_environment`; this was the one that
            # did not.
            env=child_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200], "lines": []}
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
        return {"ok": False, "reason": f"journalctl exited {completed.returncode}: {detail}", "lines": []}

    kept: list[tuple[str, bool]] = []
    other = 0
    # Counted by who wrote them, because on this device the answer is not the
    # same for both and the difference is invisible in the result otherwise.
    # Decky's loader runs as root and its own lifecycle lines are root's; the
    # plugin backend is spawned with `setuid` to the host user and no
    # supplementary groups at all, and journald shows such a process only the
    # entries that user produced. So a bundle collected from the panel holds
    # this plugin's own records and never Decky's, however long the window is,
    # and `ok` with four thousand lines in it reads as a complete journal.
    for raw in completed.stdout.decode("utf-8", "replace").splitlines():
        message = _message(raw)
        if message is None:
            continue
        origin = _classify(message)
        if origin == FOREIGN:
            other += 1
        else:
            kept.append((raw, origin == DECKY))
    # Counted over the lines that are actually returned, which is what the tail
    # below leaves. Counting Decky's records over the whole window instead
    # described a set this never hands back: one lifecycle line early in a
    # window, four thousand of our own after it, and `from_decky` reported a
    # record the truncation had just removed, which is the state that count
    # exists to report rather than to hide.
    over_limit = max(len(kept) - max_lines, 0)
    if over_limit:
        kept = kept[-max_lines:]
    return {
        "ok": True,
        "identifier": identifier,
        "since": since,
        "lines": [raw for raw, _ in kept],
        "kept": len(kept),
        "from_decky": sum(1 for _, from_decky in kept if from_decky),
        "dropped_not_ours": other,
        "dropped_over_limit": over_limit,
    }


def is_from_decky(line: str) -> bool:
    """Whether one kept line is one Decky's own halves wrote, rather than ours.

    Takes a whole `short-iso` line, the way the lines are kept, because the
    caller that needs this is asking about bytes it has already written: the
    support bundle truncates a second time, to its own byte budget, after this
    module has truncated to `max_lines`, so whether the archive still carries
    any of Decky's records is a question about that file rather than about what
    was collected for it.
    """
    message = _message(line)
    return message is not None and _classify(message) == DECKY


def _message(line: str) -> str | None:
    """The message half of a `short-iso` line, without its host and unit prefix.

    `short-iso` is `<timestamp> <host> <identifier>[<pid>]: <message>`. Matching
    on the message alone keeps a hostname or a PID from deciding whether a line
    is ours.
    """
    marker = "]: "
    cut = line.find(marker)
    if cut < 0:
        return None
    return line[cut + len(marker):]
