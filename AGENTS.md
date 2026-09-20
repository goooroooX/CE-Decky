# AGENTS.md: CE Decky operating contract

The compact entry point for coding agents. Everything else this project holds is in `docs/`, and **Authority and reading routes** below says which document owns what and which one wins.

Current development version: **0.9.29 — 2026-09-20**

## Start here

On every clean run:

1. Read this file completely, in bounded slices. It ends with **Release discipline**: if that heading is not in what you just read, the read was truncated and the rest of this contract is not optional. A route that truncated once truncates again, so change the route rather than repeating it.
2. Read `docs/FIELD_NOTES.md` **Still worth checking**, which is the whole of what this project knows it has not confirmed. It is empty when nothing is owed, and that is an answer.
3. Before feature or bug-fix work, read only the newest `CHANGELOG.md` entry.
4. Classify the change with the routing table and open only the required contracts and source files.
5. Before writing any command that reads repository or device state, check **Tracked helpers**. One already answers most such questions, and the ones it does not are named in `docs/DEVELOPMENT.md`.

## Mission and implementation policy

Build CE Decky as a Decky Loader plugin that manages Cheat Engine tables and, after explicit target validation, runs either the exact plugin-managed official Windows Cheat Engine installation or a user-imported Windows installation in the selected game's Proton environment. Decky/QAM is the primary control surface.

**Full Game Mode autonomy is a release requirement.** A normal user must be able to complete setup, game and table selection, search/download/import, exact-table review and consent, table switching, process/startup configuration, launch, live per-record control, recovery, and routine updates with a controller in Steam Game Mode. Desktop Mode, a terminal, SSH, manual filesystem edits, and developer harnesses are evidence tools only. Manual CE/table import is a controller-accessible fallback, not a prerequisite.

For material Decky, Steam, Proton, Cheat Engine, archive, provider, or launch integration decisions:

1. Treat remembered integration knowledge as provisional; inspect current upstream source first.
2. Prefer the official project/template. Corroborate undocumented conventions with maintained real plugins.
3. Start with `docs/DESIGN.md` section 4 and `docs/FIELD_NOTES.md` **Upstream references**, which pins the commit each reused behavior was read from.
4. Inspect the pinned source and license before adapting it. Record repository, commit/tag, path, and whether code was copied, adapted, or used only as evidence.
5. Retain attribution/license text, add a regression for relied-on behavior, and preserve CE Decky's stricter trust boundaries.

Upstream source is precedent, not production code. Do not discard a unique upstream identity, a license finding, an observed failure, or a target assumption without first preserving it in a durable contract.

## Authority and reading routes

If documents disagree, use this order: `AGENTS.md` for workflow/invariants, `docs/DESIGN.md` for product behavior, `docs/ARCHITECTURE.md` for boundaries, `docs/SECURITY.md` for trust controls, and `docs/FIELD_NOTES.md` for what a device established and what it has not.

| Change | Read before editing | Update if the contract changes |
|---|---|---|
| Documentation/repository layout | affected files | affected document |
| Product behavior/workflow | relevant `docs/DESIGN.md`; source/tests | `docs/DESIGN.md` and tests |
| Component/data/storage boundary | relevant `docs/ARCHITECTURE.md` and `docs/DESIGN.md` | `docs/ARCHITECTURE.md`; `docs/SECURITY.md` if trust changes |
| Artifact, network, CE runtime, or Steam-state mutation | relevant design/architecture plus `docs/SECURITY.md` | contracts and regressions |
| Decky, Steam, Proton, or CE target behavior | affected contract; `docs/FIELD_NOTES.md` for what a device already established | `docs/FIELD_NOTES.md` for a durable conclusion, and only for one |
| External-code reuse/replacement | reuse sources above and `docs/FIELD_NOTES.md` **Upstream references** | attribution/notices, regression, design reference if selection changes |
| Build, dependencies, CI, package, release | `docs/DEVELOPMENT.md`, manifests, workflows, scripts | development/release docs and changelog as applicable |
| Decky Store/publication | `docs/DEVELOPMENT.md` **The artifact the Store builds**, and `docs/FIELD_NOTES.md` **Upstream references** for the build contract that was read rather than remembered | the procedure, and `docs/FIELD_NOTES.md` if what upstream does changed |

Read a whole contract before changing it.

## Non-negotiable product invariants

- No anti-cheat bypass, disabling, evasion, or stealth.
- Do not bundle Cheat Engine binaries/source or modify the selected CE installation. A private runtime copy is local, plugin-owned, and never distributed.
- `.CT` import is not execution consent. Lua, Auto Assembler, and embedded payloads are executable content.
- Offline is the ordinary case, and a table already on this device stays loadable with no network and no provider. Every advisory record is reversible: clearing one restores every path it advised against, and no state written while it stood may outlive it. Once a user clears a table's not-working mark, nothing may refuse to select, authorize, load or re-import that exact table, and the same holds for every table already in a game's library. A refusal the user cannot act on without a network, or that names state they have already cleared, is a defect rather than a safety control; the digest, host, archive, executable-content and consent checks are the refusals that stay, and they are about the bytes rather than about the plugin's own bookkeeping. State the plugin keeps to be helpful, such as which table a game came from, never outranks the user's ability to use a table they already hold.
- Never guess AppID, process, artifact identity, Proton identity, path ownership, or Steam-state baseline.
- CE Decky never writes Steam state. It may read AppDetails, install folders, and shortcut storage for identity; it must not write launch options, `shortcuts.vdf`, or `localconfig.vdf`. `scripts/verify_repo.py` enforces this under `src/`. CE runs as a plugin-owned POSIX process group.
- Stable v1 switches tables by stopping owned CE, retiring the prepared session, preparing the new exact-SHA session, and restarting CE without ending the game. It never swaps a table inside a running CE process. In-process swap stays out of stable v1 until a device session shows Cheat Engine loading a second table into a live attached process and the bridge reporting the new table's records, with the old table's records gone rather than stale.
- Attached CE launches only through an exact installed Proton tool. Independently resolved Steam-library prefix identity must equal the running game's reported prefix; Proton identity and X display come from the game's environment. Disagreement, ambiguity, or unobservability fails closed. The live process table corroborates identity but never supplies a path or identity.
- Session replacement may bypass stale-heartbeat waiting only for the exact session whose owned CE process group is proven gone and whose last status predates that exit.
- No stable-v1 cross-SHA MemoryRecord remapping or private Steam BrowserView/CDP dependency.
- `DECKY_USER_HOME` is the Steam-user home authority. Never hard-code `/home/deck` or trust backend `HOME` for it.
- `_uninstall()` remains non-destructive until owned Steam state can be restored explicitly.
- Runtime compatibility is keyed by exact CE executable SHA, complete CE source-tree SHA, bridge SHA, and Proton identity.
- Provider-described metadata is best effort for every provider: a parse or validation failure drops the described value, the row, or the page it belongs to, never a table that is otherwise obtainable, and it is counted in provider diagnostics and logged. It never relaxes digest, host, archive, executable-content, or consent checks, which stay fail closed.
- Every table-execution path calls `TableStore.verified_blob(table_sha)` immediately before session preparation.
- Drain bounded local `asyncio.to_thread()` mutations before `_unload()` returns; cancelling the awaiter does not stop its worker thread. Nothing may depend on that drain running: Decky's stop starves this process's event loop and kills it five seconds later, so an unload's synchronous prologue is the only part of it that reliably happens. What must happen is issued there, and `docs/FIELD_NOTES.md` carries the mechanism.
- Whatever a later failure has to be diagnosed from must reach the support bundle, because a released bug report is that archive and a sentence, never the device. Backend code records through the plugin logger and frontend code through `logUi`, `logUiWarning` and `logUiFailure` in `src/supportLog.ts`; log a state change, a refusal with the reason for it, an exhausted retry, and the identity a decision turned on, at the moment it happens rather than leaving it to be inferred from an outcome. New durable state, a new diagnostics field, and a new log file are not evidence until `py_modules/ce_decky/support_bundle.py` collects them and a regression proves it does. Every addition stays inside the existing whole-bundle and ring-buffer budgets, and carries no secret: the archive is attached to public issues, so a redacted field is recorded by presence alone.

## Change, version, and evidence workflow

1. Name what the change is about: the product behavior, the contract or the target assumption it turns on, or that it touches the repository only.
2. Make the smallest change that proves or falsifies the assumption.
3. Add or update tests for production behavior changes.
4. Run the smallest QA selection that covers the changed surface. Reuse a pass until a covered input changes.
5. Update `docs/FIELD_NOTES.md` only for a durable conclusion about the target, a measurement with the machine that produced it, a source evaluation, or work that is owed. Routine installs, hashes, readbacks, captures and unchanged passes are not conclusions and are recorded nowhere. Settling a row under **Still worth checking** means deleting it, not annotating it: `scripts/check_release.py` refuses a stable tag while that section holds one, so a stale row holds the release.
   - A conclusion states what was observed, not what was inferred from it: the mechanism actually seen, the ordinary explanations ruled out by test, and where the cause is unknown, that it is. A wrong conclusion recorded confidently stops the next person looking, so correcting one is ordinary work and the correction says what it replaced.
   - A measurement is recorded only once the tool that produced it is a tracked helper with its command in `docs/DEVELOPMENT.md`, and the same holds for a number said in conversation, because that is a claim too. `docs/DEVELOPMENT.md` **Measurement** carries what a figure must name and what one sample bounds.
6. Keep production promotion, prototypes, and target experiments separate when practical.

A change that makes durable state on the device unreadable, or discards it, is reported before the maintainer goes to test. Say which store, what was in it, and what it costs on screen. The blocklist is the one that catches people out: it carries the failure epochs every positive record is checked against, so an unreadable one turns every table proven to work back into one needing a retest. Before a schema bump reaches the device, rewrite what is there into the new shape and confirm the result is exactly what this version would have written.

Do the work in this thread. Parallel subagents are not how this repository is worked on: a fan-out spends cold contexts re-deriving what the session already holds, and it has cost this project a rate limit. A review runs its angles one after another here, and how many it runs is chosen for what is being reviewed rather than taken from a fixed list. A second context is worth its cost only when the maintainer asks for one, or when the work genuinely cannot be done here.

Every tool call must resolve a decision or produce a required artifact. Reuse valid preflight, QA, artifact, path, and environment facts; read bounded sections and failure tails. Do not create a plan or evidence narrative for a one-step operation. A successful purpose-built helper is sufficient for its stated guarantees. After patching repeated or near-identical code, inspect only the intended diff hunk before QA and confirm it landed in the named symbol or test; do not spend a QA run discovering a misplaced patch. Stop once the requested outcome and required verification are complete.

Versions represent completed product changes, not sessions or review rounds. Before 1.0 every completed product change, including new capabilities and incompatible protocol/schema/storage or architecture/security changes, advances only the last numeric component of `0.9.x`. Compare it numerically (`0.9.10` follows `0.9.9`); there is no `0.10`, and only the maintainer declares `1.0`. Follow-up work required to finish a product change stays on the version that change is on.

Keep the version and its date synchronized in `package.json`, `py_modules/ce_decky/__init__.py`, this file, and the newest `CHANGELOG.md` heading. There is no `Unreleased` section. Changelog entries use only `[New]`, `[Changed]`, `[Fixed]`, `[Security]`, or `[Limitation]`; add concise outcomes at the top of the current version and do not rewrite an older release. One version's entries are a single unbroken list: a blank line between two of them starts a second list and `tests/test_changelog_shape.py` refuses it, so a new entry goes directly above the one below it, which is what `python scripts/changelog.py add` does for you rather than anchoring an edit on text that changes every round.

An entry states what is true of the device now, not how the change came about. A review round, a static analysis, a device session and a fix to a fix are how the work happened and belong in the commit message; the reader of this file wants the outcome. Write no entry whose subject is the process: `A second review round over this version`, `What a static review found`, `Four more from the static review`. Work that never reached a release has no outcome of its own either: a defect introduced and fixed inside the version that introduces it is part of the entry for the thing itself, not a line beside it, so fold it in and keep one entry per outcome. Entries that share a subject are one entry; a version that has collected a line per round is restructured before it ships rather than published as a diary.

## Validation and CI

`python scripts/qa.py` is the only ordinary validation entrypoint. Its default `auto` profile routes the working-tree changes, or `HEAD` on a clean tree.

- Missing dependencies: rerun the same selection once with `--bootstrap`; use `--check-env` for read-only discovery.
- `FAILED`: read the bounded tail or `build/qa/<run-id>/qa-failures.json`, fix the cause, and run only its exact `rerun_command`.
- `INCOMPLETE`: do not report a pass; satisfy the dependency, or leave the named Linux or on-target check to the environment that can actually run it.
- One run at a time, waited for where it was started. Two runs on one tree share `build/qa/reuse.json` with no lock, so each overwrites what the other proved, and the component suite's waits time out on a machine that is also running a gate. Bound the run and wait no longer than the bound: on the Steam Machine `timeout 120`, because a `release` with nothing reused measured 84.0, 84.2 and 84.3 seconds of stage time there over three runs on 2026-09-14. That figure is this machine's. A Steam Deck, a laptop, a container or a shared runner is slower, so take the first full run's own stage timings on the host you are on and bound the next one just above them; a bound below what a host honestly costs fails a healthy tree and invites the gate to be blamed for the change. A run still going past a bound sized that way is a hang in what is being validated rather than a slow gate, and the ordinary causes are a timer the new code left running and a loop whose exit cannot be reached. The bound is what turns that into a failed run you can read; a foreground wait offers no moment in which to notice.
- The repository rules run first and end the run when one fails. `release-metadata` and `repository` read the tree and nothing else and cost about a second together, so a tree they refuse is not worth proving anything else against: the stages after one are printed as `SKIPPED` with the rule that owes them, and a `release` that used to spend its whole cost reaching that finding now spends under a second. `git diff --check` stays last, because the bundle it judges is the one this run rebuilt.
- Spend one `release` per batch of work, not one per change. Prove each change with the smallest selection that covers it, keep the full gate for the end of the batch, and count how many a session has already spent. A docs-only edit made after a passing `release` is covered by the repository rules alone.
- After focused checks stabilize, run one `release`. It is the only full gate: it packages as well, which costs half a second, and every production change here ends in an install that wants the package. A stage whose inputs have not moved since it passed is reused rather than run again, so a second full run on an unchanged tree costs about a second; an explicitly named test or stage always runs, and `--no-reuse` runs everything regardless. The record is local and ignored, so CI has none and always runs the lot.
- A passing `release` already performs the official frontend build, plugin packaging, and target-package probe. It prints the artifact and its exact SHA-256 on its own summary, which is what the deployment command takes; both are in `qa-summary.json` too, and the stage logs behind them are `logs/plugin-package.out.txt` and `logs/target-package.out.txt`. Do not invoke either packaging helper again unless the artifact, a covered input, or that evidence changed or the saved output is incomplete.

| Surface | Minimum route |
|---|---|
| Ordinary or docs-only change | `python scripts/qa.py` (`--profile repo` only when explicitly needed) |
| Exact Python case | `python scripts/qa.py --pytest <path-or-node>` |
| Exact frontend test file | `python scripts/qa.py --vitest <path-or-pattern>` |
| Only the frontend type check, between edits | `python scripts/qa.py --profile frontend --stage frontend-typecheck` |
| A regression proved against the revision it was written for | `python3 scripts/qa_baseline_check.py --rev <sha> --vitest/--pytest <selection>` |
| Whole backend/frontend | `--profile backend` / `--profile frontend` |
| Full product gate, including the package | `--profile release` |
| The archive the official Decky Store builds, which `release` never sees | `--profile store`; it needs podman or docker and reports INCOMPLETE without one |
| Browser UI | SteamOS/Linux: `scripts/browser_harness.py`; other hosts: `qa.py --profile browser` |
| Mutable provider diagnostics | `--profile live` or `--profile direct`; never ordinary CI evidence |
| Prototype | only its README commands |
| Target integration | local regressions plus exact on-target evidence |

GitHub Actions (`.github/workflows/ci.yml`) is the authoritative complete Linux gate, and green exact-head CI is what makes a pushed production, build, dependency, test, packaging, or release change ready. It is two jobs: `validate` runs the release profile, and `store-artifact` builds and checks the archive the official Decky Store would ship, which is a different build path and the one a submission is judged by.

Wait for CI on the head you pushed, in the foreground, and report what it said. A push is not the end of the work: until green exact-head CI is read the head is pushed, not ready, and a red run is the finding. Documentation-only changes need repository QA rather than CI.

A red required run is a repository defect until its log proves otherwise; reproduce its printed exact selection and fix the cause, never the gate. CI inspection commands and constrained frontend fallback are documented in `docs/DEVELOPMENT.md`.

### The source snapshot CI publishes

The authoritative run publishes a complete tracked-source ZIP of the exact head it validated. On a `pull_request` that head is `github.event.pull_request.head.sha`, which is the commit under review and is not the synthetic merge commit a reviewer would otherwise be handed, and the ZIP is what a review running outside a checkout reads.

What a reviewer is promised, and what the workflow's own regressions hold:

- it is the exact head, which on `pull_request` is `github.event.pull_request.head.sha` and never the synthetic merge commit. What CI validates is unchanged: the ordinary merge checkout;
- it is built from Git rather than from the runner's working tree, so nothing a stage wrote can reach it;
- it is packed and uploaded before dependency restore and before any QA stage, so a run that fails later still carries it, and a snapshot that was not built fails the run instead of arriving empty;
- it is temporary and expires on its own. This workflow is never given artifact-deletion permission and gets no cleanup job; ordinary CI permissions stay `contents: read`.

A development branch inherits this by starting from, or taking in, current `main`. Do not remove or weaken the exact-head snapshot path without the maintainer asking for it: `scripts/verify_repo.py` holds its shape, and the checkout depth and upload flags that make it possible are documented beside the workflow they configure.

## Tracked helpers

A command you are about to write in order to read repository or device state is usually a helper you have not looked for yet. `cat`, `grep`, `find` and `python3 -c` over Decky paths, plugin state, profiles, artifacts or logs are the shape of that mistake, and it does not announce itself: the hand-rolled answer looks valid, arrives without the helper's guarantees, and is how a fact nobody can reproduce reaches a report. Read this table first and reach for the shell only once no row answers the question.

The rows are the questions, because that is what an agent is holding when it needs one. What each helper guarantees, its exact command and its options are one per row in `docs/DEVELOPMENT.md`, which is the authority for the detail: do not restate that here, where a stale copy would outrank the correct one.

| Question | Helper |
|---|---|
| Which machine is this, and may a target helper run here at all | `scripts/host_platform.py`; `scripts/target_agent_preflight.py` for the bounded host, tool and permission preflight |
| Is a live Decky holding this plugin reachable, and what exact plugin root, `user_home` and `settings_dir` does it report | `scripts/target_plugin_install.py authority`, which is also where the probes below get those paths |
| Install or replace the exact verified ZIP, and prove the reload | `scripts/target_plugin_install.py install` |
| Is this ZIP the exact installable artifact | `scripts/target_package_probe.py` |
| Is this what the official Decky Store would ship, which is a different build from ours | `python scripts/qa.py --profile store`, which runs `scripts/check_store_artifact.py --build` |
| Does Cheat Engine actually start on this device, through the production launch path rather than a description of it | `scripts/target_ce_launch_probe.py` |
| Will this exact Cheat Engine open this exact table at all, without a game, a profile or a consent | `scripts/target_ce_launch_probe.py --table`; the load runs what the table carries, so it is execution rather than inspection |
| Which exact compatibility prefix does this AppID have here | `scripts/target_prefix_probe.py`; read only, and it refuses an ambiguous answer rather than choosing |
| What does Steam's own JavaScript say this device's library holds, and does it agree with the device's own files | `scripts/target_steam_library_probe.py` |
| Can this device's own 7-Zip open what the production adapter hands it | `scripts/target_archive_probe.py` |
| Does the device reach a provider over TLS through the production transport | `scripts/target_tls_probe.py` |
| Make the live backend do something, rather than reading what it holds | `scripts/target_plugin_rpc.py`; one bounded call through Decky's own socket, for behaviour that only exists in the loaded process |
| What does this plugin hold for a game right now: selected and previous table SHA, consent, auto-load, pins, remembered state, sessions, owned launches, and which AppIDs are running | `scripts/target_state_probe.py` |
| What do the provider caches hold: how much of the FearLess listing is indexed, how stale it is, how much it still owes, and whether its background pass is armed | `scripts/target_state_probe.py`, in `provider_caches` |
| Which Decky paths and versions does the live plugin process report | `scripts/target_decky_env_probe.py`, when procfs permits it; never installation authority |
| What has the backend been doing, in its own log | `scripts/target_plugin_log.py`; Decky names a file per plugin load and keeps the last few, so the newest is found here rather than by `ls -t` and a guess |
| What did the backend do in a session an install has already rotated away | `scripts/target_plugin_log.py --journal`; the system journal keeps this plugin's records across a reload, a webhelper restart and a reboot, and returns only ours |
| What did the plugin's own updater do while the plugin was being replaced | `scripts/target_plugin_log.py --updater`; the install happens in a detached process that outlives this backend, so none of it is in the backend log |
| What did the panel do before it stopped | `scripts/target_plugin_log.py --frontend`; the panel's live record dies with its renderer, and restarting the webhelper to recover a wedged panel is what destroys it |
| What is this plugin's panel showing right now, and can a row be reached | `scripts/target_panel_read.py`; it reads each row, its controls and their state out of the page as text. `--open`/`--reopen` opens this plugin's own panel through Steam's and Decky's own calls, and `--press <name>` walks into its own screens (`--press 'Advanced…'`, `--press Manage`, repeated to go deeper), so reaching a screen and looking at it needs nobody at the device. Prefer it to a capture for anything a layout question turns on |
| What is actually on the screen | `scripts/target_screenshot.py`; `--region qam`/`center`, `--remote USER@HOST` for the other device, `--tile` for a published cut, which `scripts/build_screenshot_collage.py` then assembles into the strip `README.md` shows |
| What viewport, pixel ratio and panel height does Game Mode give a screen | `scripts/target_ui_layout_probe.py` |
| Has Steam's UI stopped running JavaScript, and what wedged it | `scripts/target_ui_freeze_probe.py` |
| CEF will not answer that probe at all: what are the webhelper's own threads doing | `scripts/target_webhelper_threads.py`; reads `/proc`, so it needs no debugger and is safe while the UI is wedged |
| What is holding this device's memory while Decky and its plugins look hung | `scripts/target_memory_watch.py` |
| What does one session cost, and how long do the primitives it repeats take | `scripts/target_session_cost_probe.py`, `scripts/target_primitive_bench.py` |
| Do a real table's controls stay reachable in the QAM, without executing anything the table carries | `scripts/table_ui_probe.py` |
| What version do real Windows executables declare, and what does reading one cost | `scripts/pe_version_survey.py`; it walks a directory of `.exe` through the production version reader and reports, per file, the version, the size of the resource section, how far into it the directory walk reached and how many spans the read cost |
| One bounded read-only snapshot of the host, for a report | `scripts/target_snapshot.py` |
| Routed repository QA, and pinned browser UI QA | `scripts/qa.py`, `scripts/browser_harness.py` |
| What are the providers actually serving, and what is in real tables | `scripts/provider_probe.py`, `scripts/provider_search_survey.py`, `scripts/provider_corpus_survey.py` |
| Add a changelog entry to the current version without anchoring an edit on text that changes every round | `python scripts/changelog.py add` |
| Prove the plugin's own update path end to end, which needs a build that is older than the published release | `python scripts/build_downgrade_package.py`; it packages this tree with the version strings stamped below the release and restores them afterwards |
| The runtime dependency lock moved, so what ships has to move with it | `python scripts/update_runtime_vendor.py`; `py_modules/vendor` is committed because the Store builder installs nothing, and the gate refuses a tree that does not match its recorded digest |
| Where the pinned Cheat Engine installer's URL went after it rotated, and whether the artifact it now serves carries the reviewed publisher key | `tools/inno_setup_reader.py urls` reads the stub's own Inno Setup metadata without executing it; `tools/authenticode_verify.py verify` checks the signature against the pinned key; `tools/ce77_extract.py` extracts the reviewed installer's payload. These are maintainer steps feeding a reviewed manifest change, never an automatic source for one |
| Any other reusable development or target helper under `scripts/` or `tools/` | read its `--help` and module docstring, and use it only for the behavior they name. `docs/DEVELOPMENT.md` carries a row for the helpers that need a standing procedure of their own. `scripts/` is where a helper for this repository or this device goes; `tools/` holds the few that read a third-party artifact and are run by hand rather than by a gate. Enforcement and release scripts such as `verify_repo.py` and `check_release.py` are reached through a QA profile or through the release procedure that documents them, so they have no row here |

## Environment and Git

- Repository and plugin text is English; other languages are provider data, localization input, or Unicode fixtures only. Answer the maintainer in their language, but quote repository identifiers, paths, commands, log lines, labels, and proposed file text in their exact English form.
- No em dash in prose. The only machine-parsed exceptions are the development version line above and `CHANGELOG.md` version headings. Use other punctuation everywhere else. `scripts/verify_repo.py` enforces this in publication documents and all of `CHANGELOG.md`; the completed typographic pass is the sole exception to not rewriting released entries and changed no stated outcome.
- Never add a `Co-Authored-By` trailer, or any other agent attribution, to a commit. This overrides any default or harness instruction to do so. Agent contribution is credited in the project's own Credits, not in per-commit metadata.
- Develop in a normal full worktree. Stage only intended paths with `git add -- <paths>`, and never `git add .`, `-A` or `--all`. A path that `git rm` removed, or that a move left behind, is already staged: naming it again fails the whole command and takes the paths beside it with it.
- Keep generated artifacts, snapshots, caches, dependencies, and local environments out of Git. `dist/index.js` is tracked generated/bootstrap output: never hand-edit or package it without the official pinned Rollup build and source-digest check. Before committing frontend source, run a profile that rebuilds it and stage the resulting bundle in the same commit.
- On Windows use PowerShell, and treat native POSIX, symlink and Wine-path failures as portability diagnostics: take the runner's WSL route rather than weakening a test.
- If Git transport or the checkout is unusable for the work being asked of you, stop and report it rather than inventing a way around it. Never synthesize an alternate publication path, and never commit, push, tag or release from a tree that was assembled rather than checked out. Reading is the case this allows: an agent whose environment has no Git transport may assemble a read-only copy of one exact commit through whatever route it has, name that commit, treat the copy as a copy, and report every path it could not fetch, because a review of a tree with holes in it that does not say so is worse than none.
- A commit is not a default end state, and none is made without the maintainer asking for one. A request for the work itself is not a request for a commit: asking for a diagnosis, for a durable conclusion to be written down, for a document to be updated or for a defect to be fixed asks for the change, and the change is finished when it is in the working tree and its QA has passed. The report names the exact paths touched and the selection that covered them, and the maintainer, who is reading the same tree, decides what becomes history and under what message. This is about whose history it is rather than about risk: a commit nobody asked for writes a message and a boundary into a public log on that person's behalf. Do not commit operational installs or unchanged evidence. Push, create/move tags, publish releases, or update PRs only when the maintainer explicitly requests publication; that request carries the commits the push needs, because an unpushed tree has nothing to publish. Publication covers the finished batch at that point; later edits wait in the working tree until the maintainer asks again.
- A round of static review is the standing exception to waiting for a request to publish. While the maintainer is bringing review findings here, push the finished batch as soon as it is validated rather than asking: the reviewer runs elsewhere and reads the pushed branch, so an unpushed fix is invisible to the round that would confirm it. CI is still awaited and reported, and the commits that push needs are made as part of it. Work outside such a round waits in the working tree.

## First-push workflow and tooling audit

Only the first time in a run that the maintainer asks for a push, audit the whole run before reporting it. Name calls spent re-deriving an undocumented repository/device fact, replacing a missing tracked helper, or following an unnecessary step. Candidates include searched paths, device identities or versions, undocumented working commands or conventions, multi-call checks one helper should own, and abandoned detours. State what each improvement would have saved. Genuine discovery required by new work and facts already documented where expected are not findings; finding nothing is complete, and inventing a suggestion is harmful.

Report each finding as a proposal: the exact `AGENTS.md` line, `docs/DEVELOPMENT.md` table row, or helper change, plus the calls it would remove. If accepted, make them later as ordinary changes, never silently inside the push that prompted it. Accepted findings are one batch, not one errand each: a commit per finding, because that is what keeps the reason readable, and then one QA selection covering the whole batch and one push. A commit of its own is not a QA run of its own, and running the gate once per finding spends exactly the kind of call this audit exists to find.

## If you are running on SteamOS

This heading is a condition, and settling it is the first thing to do about it. The same checkout is worked on from a desktop, from CI and from the device, and nothing in the tree says which. Assuming it is not the target leaves every claim about Steam, Proton and Cheat Engine unverified when a device was there to answer it; assuming it is, and being wrong, produces target claims from a host that cannot make them.

Answer it from the tracked helpers rather than from a path or a user name. `python3 scripts/target_agent_preflight.py` names the machine and reports `repository_ok` and `target_ok` separately; `python3 scripts/target_plugin_install.py authority` says whether a live Decky holding this plugin is reachable. State both in the first report of the run, because everything this section requires and permits follows from them.

Every helper that needs the device refuses a host it cannot run on before doing any work, by name and with exit code 3, so a wrong machine is a sentence rather than a traceback from the first POSIX-only call. Helpers with no platform requirement run everywhere; do not add a guard to one that has none, and do not weaken a test to make a target helper run off the target.

Read `docs/DEVELOPMENT.md` **SteamOS development** before target work. Run the preflight once at the start of a run whose work may reach the device, and again only if it failed or the checkout, privileges, tools, execution boundary or host changed. There is a third answer besides yes and no: the device is reachable but is not this machine, which is `docs/REMOTE_TARGET.md`. Its rule is this section's rule, that a helper answers for the machine it runs on, so the tree is mirrored to the device and the helpers run there; a copy assembled by `rsync` is never committed, tagged or pushed from.

Read a long line by column rather than by pattern. `cut -c`, `sed -n` and `fold -s -w` bound what comes back from a table row or a log line of several KB, while `grep -oE '.{0,N}\u2026'` around a match is refused outright by the `grep` on this device and is fragile everywhere else.

A device question routes through the table under **Tracked helpers** rather than through an ad-hoc probe. What a helper's answer is worth, once it has given one, is this:

The tracked helper's guarantees are enough unless its result is ambiguous or the check being run explicitly requires more. For target-specific code, exercise the exact production helper, executable/argv, filesystem or procfs object, environment shape, and effective privilege boundary available on the device. Prefer bounded read-only/dry-run checks; capture mutations only inside the authorized gate and restoration contract. If an exact safe check is impossible, report the target behavior as unverified.

For Game Mode UI evidence use `target_screenshot.py`, never a photograph: `--region qam` or `center` to crop, `full` only for context, a descriptive `--prefix`. Prefer `target_panel_read.py` to a capture for anything a layout question turns on, since it reports each row, its controls and their state for a fraction of what a frame costs to read. Reach the screen yourself rather than asking for it: `target_panel_read.py --reopen --press 'Advanced…'` opens this plugin's own screens through Steam's and Decky's own routes and the handlers this plugin wrote, repeated to go deeper, and the helper refuses by name the controls that authorize, download, write or destroy. Those, a running game and Steam's own interface are what still needs the maintainer. Installing a package replaces Steam's webhelper and closes the Decky UI, so a frame taken straight afterwards shows only what the session fell back to: reopen the panel and press back to the screen before capturing. Captures are temporary evidence, and only durable conclusions are kept.

When the maintainer asks for reported behaviour to be reproduced or checked on the device, capture the whole of the evidence before recovering anything, into `build/incidents/<symptom>-<UTC yyyymmddThhmm>/`. Recovery is what destroys this class of evidence: restarting the webhelper, reloading the plugin and installing a package each delete a record that existed only while the problem did. Capture, then recover; a capture taken afterwards is a different and much weaker thing.

One incident is one directory: the support bundle, whatever the tracked probes answer only while the symptom is live, and a short note of what was done in what order. The bundle is the part that has to be there, for the reason the invariants give. `build/` is ignored, so the directory is local and may hold device paths, game names and host names a public archive would not; nothing in it is committed, and what turns out to be durable moves into `docs/FIELD_NOTES.md` as a conclusion. Name the symptom in the user's terms, not in the suspected cause's.

For a whole-UI QAM wedge, preserve the frame, then run `target_ui_freeze_probe.py` once; if it gets no answer because CEF is not responding at all, `target_webhelper_threads.py` reads the same processes through `/proc` and still says which thread spins and which waits. Take both before recovering, because restarting the webhelper is what makes the device usable and destroys the panel's record with the renderer; `target_plugin_log.py --frontend` and `--journal` are what survive it and are the two to read after one. Correlate the exact renderer and incident window in `cef_log.txt` with the backend log before attaching a debugger. A repeated `library.js` refusal means inspecting the exact installed parser and its accepted values rather than upstream history. After a reload the new browser PID is the clean boundary for follow-up console evidence.

Active development authorizes building, installing, replacing, and reloading the plugin on this device, even with a game or Steam UI open, unless the maintainer pauses installation. After every significant production-code change, package and install the exact verified artifact; documentation-only, test-only, repository-tooling changes, and unchanged artifacts need no deployment.

Install with the exact artifact and its exact SHA, and let `target_plugin_install.py` resolve the plugin root: never hand-copy into Decky's root-owned directory and never guess a root. A device that has never held this plugin has no authority to resolve, so that first install names `--plugin-root <homebrew>/plugins/CE-Decky` and omits `--replace`; every install after it resolves the root itself and takes `--replace`. Stop on failure, and on any conflict between what the live backend says and what a durable record says, because a live identity never falls back to an older record. Bound it like a QA run: `timeout 90` on the Steam Machine, and elsewhere just above what that host's own first install costs, which the helper prints. A second install inside a minute waits out Decky's webhelper window first and says so on stderr, so bound that one at `timeout 120`; three webhelper replacements inside a minute make Decky stop its own service, and only root can start it again. Announce that webhelper replacement may close the Decky UI or displace a running game, and never attempt Steam-URI focus repair. The helper's summary line is what it proved, including `panel import unobserved` and `panel cardinality unverified`, which both mean the panel was not checked rather than checked and found right; `docs/DEVELOPMENT.md` carries what each answer means and how the root is resolved.

Installation is operational state: it does not update tracked docs, create a commit, or trigger CI by itself. Batch non-blocking minor fixes; rebuild when the installed package blocks the next thing to be checked or changed behavior needs retest. Never claim Steam/Proton/CE behavior complete from Windows, CI, source inspection, or fixtures alone; prefer a documented compatibility limit over a speculative trust exception.

## Removing something

Written for the pass that ran before this repository was made public, kept because what that pass may not get wrong is what every later removal may not get wrong either.

**Preserve before deleting.** Deletion is the last step, never the first. Temporary documents hold most of what this project knows about its target, almost none of it derivable from the code, and it was paid for with device sessions that cannot be repeated cheaply. Move every finding that is still true and is not already stated by the code or its tests, and delete only what has a home:

- durable target conclusions, including the exact upstream or environment behavior a workaround exists for, and why the obvious approach does not work;
- measurements, with the hardware, software identity, method and window that produced them, and the spread. A number without the machine it came from is a rumor, not preserved information;
- the boundary of an observation: which games, devices, Proton builds or table shapes a behavior was seen on, and which it was explicitly not seen on;
- work that is owed rather than done, and the evidence that would settle it;
- refusals and limits a user can meet, in the user's terms;
- provider source evaluations, including every rejected source and the exact ground for it, because one with no recorded reason is re-evaluated from scratch at the same cost.

Preserve the structure rather than the prose: what was observed, under what identity, by what method, what depends on it now, and what would falsify it. A conclusion that has stopped being true is removed on purpose and said to be removed in the changelog, never left to disappear with its file.

**Preserve working code separately.** Measurement harnesses, live-source surveys, real-page fixtures, and decoders whose shape production relies on are tools, not history. Move each survivor before deletion: product code to `py_modules/` or `src/`, developer tools to `scripts/`, regression inputs to `tests/fixtures/`. Make it runnable, test it, and document its command and guarantee in `docs/DEVELOPMENT.md`. If a prototype only proved a conclusion, preserve the conclusion and delete the code; recoverability from history is not a home.

**Route it.** Product behavior to `docs/DESIGN.md`, component and data boundaries to `docs/ARCHITECTURE.md`, trust controls to `docs/SECURITY.md`, procedures and reproducible measurements to `docs/DEVELOPMENT.md`, what a device established to `docs/FIELD_NOTES.md`, user-visible cost and limits to `README.md`. If none fits, create a durable document and add it to `docs/README.md`. Never drop a finding for lack of a destination.

**Keep the tooling.** This section, and every development helper under `scripts/`, are permanent: they are how this plugin is developed against a real device, and publication does not change that. They may be reorganized or moved. What is removed from them is their coupling to what is being deleted: removed identifiers, temporary paths, machine-specific defaults and private evidence assumptions.

**Nothing is owed from that pass.** `CHANGELOG.md` was read for identifiers this project took out: the names `scripts/verify_repo.py` is told about are absent from every file, and what is left is the path or helper named by the entry that removed it, which is what a changelog is for.

**Never remove a name from one file and call it removed.** `grep -rn` the whole tree first for every spelling of what is going: the identifier, the module path, the RPC name, the exported type, the test that asserts it, and the string a payload carries it in. Group hits by file, because the count per file is what shows a helper still depended on. Search again afterwards and expect it empty, or answering only to a named follow-up. This catches two things: a required entry in `scripts/verify_repo.py` or a stage list in `scripts/qa.py` that fails the moment what it requires is gone, and a consumer outside the surface being removed, which is why it is handled in the same change rather than by a red gate.

## Working in the open

This repository is public, and its history begins at one commit. Development happens here now: branches, pull requests, CI logs, issue threads and commit messages are all readable by anyone, as they are written rather than at some later review.

What that changes, and it is less than it sounds, because most of the protection was already standing:

- the sanitation rule in `scripts/verify_repo.py` reads every file this project owns and runs as a precondition stage in every QA profile, so it already holds on each change rather than once before publication. It does not read commit messages, branch names or pull-request text, and those are published too: say what changed and why, and name no device, address, account or library of the maintainer's in them;
- the gate runs on hosted runners and every target helper refuses a host that is not the device, so no device state reaches a CI log. Incident captures, screenshots and probe output live under ignored `build/` and are not committed, which is the same rule as before and now also a privacy boundary;
- issues here are where a support bundle and a sentence arrive, and `py_modules/ce_decky/support_bundle.py` decides what that archive may carry, as an allowlist, because it is attached in public;
- release artifacts are attested by `.github/workflows/release.yml`, and on a public repository anyone can verify that attestation against a downloaded ZIP. Do not weaken or remove that path.

The private repository that preceded this one is a frozen archive. It is not a remote of this one, nothing is merged or cherry-picked across, and no part of its history is republished here. That is why this history begins at one commit rather than at a rewritten branch: deleting files at the tip does not erase history and neither does force-pushing over it, because the host keeps its own pull-request refs and every object they reach. When something in the archive turns out to still be true, it is written here as a durable conclusion in this project's own words, the way any other finding is.

## Release discipline

- The release tag is exactly `v<version>`; preparation and GitHub workflow are defined in `docs/DEVELOPMENT.md`.
- A Decky Store submission is validated on both artifacts, because they are different build paths: `python scripts/qa.py --profile release` for this project's own ZIP and `python scripts/qa.py --profile store` for the one the Decky CLI builds. The database pins the CLI version, so the second is the one a submission is judged by. The submodule directory there is `CE-Decky`, because its name becomes the archive's root folder.
- A workaround for target behavior names, in `docs/FIELD_NOTES.md`, the exact upstream or environment behavior it exists for and why the obvious approach does not work. A workaround whose reason is not written down is indistinguishable from a mistake by the next person to read it.
- Never delete known-good user CE/table artifacts as update logic.

Before declaring work complete, verify scope, relevant QA, generated bundle/package synchronization when applicable, required contract/status changes, evidence-bounded target claims, and any explicitly requested publication on the verified remote head.
