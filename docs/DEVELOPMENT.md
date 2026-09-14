# Development, validation, and releases

## Toolchain and repository layout

Portable, token-efficient validation is a project priority. `scripts/qa.py` is the standard-library-only control plane and the default entrypoint on Windows, Linux, CI, and the Steam Machine. A Python-only checkout can perform repository and backend-focused work; Node is a lazy frontend/build dependency, not a universal development prerequisite.

- Python 3.11 or newer
- Node.js 18 or newer
- pnpm 9.15.9, pinned by `package.json`

The Python backend has no runtime package-install step on SteamOS. Pure-Python runtime dependencies are pinned with hashes in `requirements-runtime.lock` and committed as `py_modules/vendor`, because the official Decky Store builder copies `py_modules/` as the repository holds it and installs nothing; both packagers ship that tree. **The committed runtime dependency tree** below is how it is rebuilt and what checks it. `requirements-dev.txt` repeats those exact pins for local tests and adds test tooling. Frontend packages are build dependencies and are not shipped as `node_modules`.

The root is intentionally the repository control plane, not a document folder. Keep these files at the root:

- `main.py`, `plugin.json`, `package.json`, `src/`, `dist/`, and `py_modules/` follow Decky Loader and its build conventions;
- `py_modules/stdlib_fallback/` contains verbatim CPython 3.11.7 `xml`, `html`,
  and `_markupbase` compatibility assets for Decky's stripped PyInstaller
  runtime; keep their pinned source, license, package smoke, and
  import-on-missing behavior synchronized;
- `rollup.config.js`, `tsconfig*.json`, `pytest.ini`, and `requirements-dev.txt` use normal tool discovery and short commands;
- `.editorconfig`, `.gitattributes`, and `.gitignore` must cover the entire tree and keep Windows-to-SteamOS text handling predictable;
- `README.md`, `AGENTS.md`, `CHANGELOG.md`, and `LICENSE` must be easy to discover and are also release/legal entry points;
- `defaults/THIRD_PARTY_NOTICES.md` and `defaults/licenses/` are the same kind of entry point and sit there for one reason: the Decky CLI packages a fixed set of top-level files and strips the `defaults/` prefix from everything under it, so this is the only path by which a notice reaches the Store artifact. Both packagers put them at the root of the archive.

Moving these into a generic `config/` or `docs/` directory would require non-standard flags, complicate upstream Decky reuse, and save no agent context. Generated files belong under ignored `build/` or `artifacts/`.

## Dependency restore

Let the selected validation profile restore only what it needs:

```powershell
python scripts/qa.py --bootstrap
```

Exact pinned packages already present in the active Python are reused; otherwise the Python environment is cached in `.venv` by the SHA-256 of `requirements-dev.txt`. When frontend work is selected, bootstrap invokes exact `pnpm@9.15.9` through Corepack and records the `package.json`/lock fingerprint in `node_modules`; unchanged environments are not restored again. The ordinary runner calls `node`, TypeScript, Vitest, and Rollup directly and never asks whatever globally installed `pnpm` happens to be active to inspect or mutate the checkout.

Use read-only discovery when diagnosing a fresh machine:

```powershell
python scripts/qa.py --check-env
```

Do not update `pnpm-lock.yaml` incidentally. A dependency change must update the manifest and lockfile together, explain why the pin changed, and run the full gate.

### Local frontend fallback

The resident Cheat Engine bridge is Lua, so it is validated by running the exact shipped script under a stock interpreter against a Cheat Engine API stub: the production Python renderers produce the descriptor and control files, the real bridge consumes them, and the production Python parser validates the status it writes. Any `lua`, `lua5.4`, `lua5.3` or `luajit` on `PATH` is discovered lazily and reported by `--check-env`; without one the stage skips locally and remains authoritative in CI, which installs `lua5.4`. Never install an interpreter on a SteamOS target to make it run.

When frontend source must be changed from an environment that cannot restore the pinned Node dependency tree, use the development-only fallback instead of hand-editing `dist/index.js` or introducing a second production bundler:

```powershell
python scripts/frontend_fallback.py
```

The fallback requires any TypeScript 5.x `tsc` on `PATH`, uses `tsconfig.sandbox.json` plus the repository shims, validates relative project imports, and emits inspectable ES modules only under ignored `build/frontend-fallback/`. It does not write `dist/index.js`, does not run Decky's Rollup preset, and does not qualify a plugin package. Its purpose is to let constrained environments make and statically validate normal `src/` changes while keeping the authoritative build path unchanged.

The official build remains `pnpm run build` / `rollup -c`. `rollup.config.js` stamps each successful official bundle with a digest of the frontend sources and build-control files. `scripts/package_plugin.py` requires that digest to match before packaging and excludes the stamp itself from the release ZIP. Therefore a development branch may temporarily carry a stale bootstrap `dist/index.js`, but stale output cannot be packaged accidentally. CI and release always run the pinned Rollup build before bundle smoke and packaging.

## Validation profiles

Use the smallest profile that covers the change.

### Automatic iteration profile

This is the default command after a change:

```powershell
python scripts/qa.py
```

It uses working-tree paths, or the files in `HEAD` when the tree is clean, to select direct pytest dependents, frontend component/core checks, bundling, packaging, and repository guards. Full stdout/stderr and timings are stored under `build/qa/<run-id>/`; the terminal receives only a bounded summary. A non-clean run also writes `qa-failures.json` with the exact failing command and a short tail.

### Focused profile

During implementation, run the narrowest relevant selection. Both halves have one, and they combine into a single run:

```powershell
python scripts/qa.py --pytest tests/test_profiles.py
python scripts/qa.py --pytest tests/test_network_prod.py::test_partial_stream_failure_removes_download_staging
python scripts/qa.py --vitest tests/uiModel.test.ts
python scripts/qa.py --pytest tests/test_profiles.py --vitest tests/providerCatalog.test.tsx
```

`pytest.ini` supplies the production Python path, so commands are the same on PowerShell and POSIX shells.

`qa.py` is the routed entrypoint and stays the one to use. A few things need pytest directly, and mutation checking is the usual one: break the guard you just wrote, confirm the test that names it fails, restore. That run needs the interpreter `qa.py` itself selects, because the system Python has no pytest:

```bash
./.venv/bin/python -m pytest tests/test_session_protocol.py -q
```

Run it from the repository root. `pytest.ini` puts both the root and `py_modules` on the path, so a test importing release tooling from `scripts` resolves the same way a test importing `ce_decky` does.

`--vitest` takes a path or a vitest filename pattern and runs the component stage against that alone, which is seconds against the half a minute the whole stage costs. It type-checks nothing, because vitest compiles through esbuild: `--profile frontend` remains the gate for a frontend change, exactly as the backend profile is after a focused `--pytest`. A pattern that matches no file fails the stage rather than passing empty.

`dist/index.js` is built rather than written, and a failed frontend build removes it. The next run's first stage is the repository check, which then reports it as a missing required file: read that as the build having failed, rebuild with `--profile frontend --stage frontend-build`, and read the failure that stage prints rather than the one the repository check printed. The check names this itself when the bundle is the file that is missing.

Where only the types are in question between two edits, `--stage` runs that one stage of the profile and nothing else, which is under two seconds:

```bash
python scripts/qa.py --profile frontend --stage frontend-typecheck
```

### Proving a regression against an earlier revision

A regression that passes on the commit it was written against says nothing about the defect it was written for, so a review round ends by running the new tests with the production source rolled back to the reviewed head and expecting them to fail. `scripts/qa_baseline_check.py` is that check:

```bash
python3 scripts/qa_baseline_check.py --rev <reviewed-head> --vitest tests/providerCatalog.test.tsx
python3 scripts/qa_baseline_check.py --rev <reviewed-head> --pytest tests/test_github_discovery_prod.py
```

It creates a temporary `git worktree` at that revision, puts this checkout's `tests/` on top of it so the tests being proved are the new ones, shares the restored dependency trees rather than restoring them again, runs the same routed selection there, and prints the tests that failed. The checkout being worked in is never written to, which is what makes it safe in a workflow whose every round ends in a commit, a push and an install onto a device. It exits non-zero when the selection passes at that revision, because a regression that passes there proves nothing, and again when the run fails without naming a test, because a run that could not start is not a proved defect. `--expect-pass` inverts it for a control case: a test asserting behaviour that is deliberately unchanged should still pass at the baseline.

It answers for one half only. That the selection passes on the current tree is what the ordinary routed run already said, and this never repeats it.

### Full gate

This is the ordinary CI and pre-handoff gate, and the only full one:

```powershell
python scripts/qa.py --profile release
```

Run it in the foreground, one at a time, and wait for it: `build/qa/reuse.json` is a single file with no lock, so two concurrent runs overwrite each other's record of what passed, and the component suite's `waitFor` budgets are written for a machine that is not simultaneously running a gate.

Run this once after focused iteration. It runs the complete Python suite, strict TypeScript, component/core checks, the official Rollup build, built-bundle smoke, the plugin package and its probe, and repository guards. There is no second, package-less full profile: the packaging costs half a second and every production change here ends in an install that wants it, so a choice between the two was only ever a chance to run both.

`--stage` runs only the stages it names, and it may be repeated: `--stage frontend-build --stage frontend-component` runs both, in the order the profile schedules them. A name the profile does not schedule is refused and the message lists what it does schedule. A run that packages also prints the exact `target_plugin_install.py` command for the artifact and digest it just produced, on any host that can install one.

A stage whose inputs have not moved since it passed is reused rather than run again, printed as `REUSED`. The fingerprint is every tracked file's path and contents, plus the stage's own command and interpreter and the two restored dependency trees' own markers, so any edit anywhere invalidates every stage. Contents rather than timestamps because the gate rewrites `dist/index.js` on every frontend build, with identical bytes, and a timestamp would have it invalidating its own reuse each time it ran. Packaging is never reused, because its work is the artifact rather than its verdict, and neither are the `live` and `direct` provider profiles, which exist to observe somebody else's server as it is now and are the one thing an unchanged tree says nothing about. An explicitly named test or stage always runs too, because naming one is an instruction to run it. `--no-reuse` runs everything regardless.

Two things bound what reuse can cost. The record lives under ignored `build/`, so a fresh checkout has none and the authoritative CI run always executes every stage. And the one stage whose output later stages depend on is the frontend build: a source change moves the fingerprint, so nothing is reused and the build runs, and if it were skipped anyway `package_plugin.py` refuses a `dist/index.js` that is stale for the current sources. That check is about the bundle matching its sources, not about the bundle being untampered: editing the built file directly still packages, exactly as it did before any of this. The official Rollup build is authoritative for `dist/index.js`; constrained development environments may commit normal frontend source/tests after the local fallback, but packaging is blocked until the pinned build has refreshed the source digest.

Because `dist/index.js` is tracked and every profile that builds the frontend rewrites it, run `frontend` or `release` before committing frontend source, and stage the rebuilt bundle in the same commit. A commit of the source alone is followed by the next build rewriting a file that commit already claimed to be finished with, which costs a second commit, a second release run and a second install, and leaves the package that was just verified belonging to no commit.

The complete Python suite uses POSIX executable fixtures, symlink semantics, and POSIX-to-Wine `Z:` conversion. On Windows the runner searches installed WSL distributions and uses one only when Python 3.11+ and the pinned dev dependencies are already valid. Otherwise the backend-full stage is `INCOMPLETE`; it does not run the known-incompatible native suite or attempt privileged OS provisioning. Linux/CI remains authoritative. Never add broad skips to make security/path tests appear portable.

### Reading a screen instead of photographing it

A screenshot is the honest record of a frame and a poor way to answer how many rows fit, whether a button is disabled, or what a footer says. This reads the same screen as text:

```bash
python3 scripts/target_panel_read.py --open --metrics
python3 scripts/target_panel_read.py --testid manage-footer
python3 scripts/target_panel_read.py --json
```

It serialises every surface this plugin is drawing: each row by its `data-testid`, the text actually rendered, the controls beside it with their disabled and focused state, how many of its lines are clipped, and with `--metrics` the height of each row and of the surface. That is a dozen lines where a capture is a megabyte, and it answers the questions a layout change is about. `target_screenshot.py` stays for what a picture is actually for: colour, overlap, and anything about the frame itself.

`--open` opens the quick access panel on this plugin's own page first, through Steam's own `MenuStore.OpenQuickAccessMenu` reached the way `@decky/ui` reaches it, and Decky's own `deckyState.setActivePlugin`. It is an API call rather than injected input, and it is the only thing here that changes what is on screen. Everything past that first screen is still a person with a controller: a modal opens because somebody pressed the control that opens it, and that rule is the point of **SteamOS development**'s refusal of synthetic input.

### What Steam's own JavaScript says the library holds

The panel's game list comes from `SteamClient.InstallFolder.GetInstallFolders` and from `appStore.allApps`, and what those return on a device is not answerable from memory:

```bash
python3 scripts/target_steam_library_probe.py install-folders
python3 scripts/target_steam_library_probe.py library-apps --json
python3 scripts/target_steam_library_probe.py app-details --app-id 220
```

It asks one of those named questions, read only, through Steam's own CEF endpoint in the `SharedJSContext` page, reusing the DevTools client `target_ui_freeze_probe.py` already carries. It evaluates no JavaScript from the caller, injects no input, and prints every field of each record rather than the ones this plugin happens to use, because the field that settles a question is usually one nobody had typed yet. It runs on the device, like every other target helper.

What it is for is comparing Steam's answer with the device's own files: `steamapps/appmanifest_*.acf` is what an install is, and `userdata/*/config/shortcuts.vdf` is what a local non-Steam shortcut is. A disagreement between the two is the finding. One run of this established that a Steam Deck's library offered 39 non-Steam shortcuts while the device's own store held 6, which is Steam syncing a library rather than installs.

### What the gate's suites spend their time on

`qa.py` says what each stage cost and nothing about why. This says which tests those seconds are in:

```powershell
python3 scripts/qa_durations.py --top 25
python3 scripts/qa_durations.py --frontend
python3 scripts/qa_durations.py --json
```

It runs the same suites the gate runs, through the interpreter and Node `qa.py` itself resolves, and adds only a reporting flag: `--durations` for pytest and vitest's own per-file timings for the component suite. It reports the total, the sum of the rows it printed, and each row. `--timeout` bounds it and defaults to fifteen minutes.

Use it before optimising anything about the gate, because the answer is routinely a handful of tests rather than the suite: one run of it found 91 seconds of a 108 second stage in 30 tests out of 906, and the worst of them was a fixture re-summing its own list on every append rather than anything the product does. Two kinds of finding come out of it and they are not the same. A test that is slow by accident is fixed in the test, and the fixture must be proved identical afterwards rather than assumed to be. A test that is slow because it waits on a production interval is fixed by naming that interval and letting the test set its own, and only where the test is about what the code decides rather than about the pacing itself; a test that is about the pacing keeps it.

A measurement taken with this helper is reproducible and worth recording. One taken by hand, with a stopwatch or a pasted `pytest` line, is not.

### Live provider profile

Live catalog checks are explicit, read-only toward providers, and excluded from ordinary CI. They use the production transport, parsers, acquisition manager, archive inspection, and content-addressed `TableStore`; downloaded files and imported tables exist only below pytest's temporary directory and are never executed.

```powershell
python scripts/qa.py --profile live
```

The fixed cases cover known FearLess attachments, GitHub release assets from an exact repository source, one GitHub repository found by searching the index whose table is committed in its tree, direct Playground files, one current The Cheat Script Yandex artifact, and one VGTimes table through the site's own countdown and challenge chain, which is slow by design. The Playground countdowns run concurrently and each provider follows the full production acquisition/import path. Because provider content and challenge policy are mutable external state, record failures as compatibility evidence and keep deterministic local-server/fixture tests authoritative in CI.

### Direct provider acceptance profile

Use this gate while developing the primary no-interaction catalog workflow:

```powershell
python scripts/qa.py --profile direct
```

It runs only known public artifacts marked `direct_provider`, through production transport, parsing, acquisition, archive inspection, SHA verification, import, and temporary storage. Browser handoff or file-picker fallback is a test failure. Terminal output is one bounded stage summary; full mutable-provider output remains under `build/qa/`. The profile is opt-in, excluded from CI, and never replaces the deterministic tests. A red provider is compatibility evidence to investigate.

### Release profile

Before creating a tag:

```powershell
python scripts/qa.py --profile release
```

`release` is the only full gate, and it creates and verifies the deterministic plugin archive as part of itself. Runtime vendoring is cached by the runtime lock SHA, so unchanged packaging does not repeat pip installation. The tag workflow repeats this profile on Ubuntu; a local pass is preparation, not a substitute for GitHub Actions or on-target validation on SteamOS.

### Authoritative CI results

GitHub Actions is the authoritative complete Linux backend gate for pushed production, build, dependency, test, packaging, and release changes. Inspect it without leaving the terminal:

```bash
gh run list --branch <branch> --limit 1 --json headSha,status,conclusion,databaseId
gh run view <run-id> --log-failed
```

The first is what proves a green run belongs to the exact head, which is the
only thing `AGENTS.md` treats as ready: a plain `gh run list` prints no commit,
so it cannot answer that. On a pull request the required run is the
`pull_request` one on the PR branch rather than anything on `main`. Read it when
the maintainer asks whether CI is green, and after pushing a fix for a run that
was red; a push otherwise ends the work without waiting. When they have asked
for the wait, one command does it rather than a series of them:

```bash
until gh run list --branch <branch> --limit 5 --json headSha,status,conclusion \
  -q '.[] | select(.headSha == "<exact-head>" and .status == "completed") | .conclusion' \
  | grep .; do sleep 20; done
```

It ends by printing `success` or `failure`, which is the answer being waited
for; `grep` without `-q` is what puts it on the screen. Five rows rather than
one because a run started after this one, for a head that is not this one,
would otherwise hide it forever.

The runner prints a bounded tail for the first stage that failed and for the first stage that could not run, each headed by its own stage key and followed by its own rerun command. Both are needed here: a stage that could not run is what a collection error looks like, its cause is nowhere else in a CI log, and the run directory holding `qa-failures.json` is thrown away with the runner. Reproduce the exact selection through `scripts/qa.py` before editing; do not replace a red required gate with a narrower one. If `gh` is unavailable, use the repository's existing Git credential for read-only GitHub REST calls and never print the token. Documentation-only changes need repository QA, not a CI wait.

## UI test strategy

UI confidence is split into layers so off-target automation does not pretend to emulate Steam:

1. **Pure behavior on every frontend change.** `tests/ts_core_test.cjs` tests archive member import and its password boundary, Steam identity reads against a client that exposes no launch-option setter at all, Proton classification, and fail-closed identity conflicts, without a browser.
2. **Bundle contract on every build.** `scripts/test_dist_fallback.mjs` imports `dist/index.js` with the minimum Decky/React boundary and verifies the plugin factory. Python RPC contract tests keep frontend call names synchronized with the backend.
3. **Component tests for decomposed workflows.** Vitest and Testing Library cover the extracted provider catalog component with explicit Decky API mocks: loading/results/failures, focus order, direct import, cancellation, and the imported-without-consent boundary. Prefer state/action assertions over large snapshots.
4. **Optional installed-browser harness for stable mocked screens.** On Linux/Steam Machine, `python scripts/browser_harness.py` verifies or bootstraps the exact pinned Chrome for Testing archive under ignored `build/browser-runtime/`, exports its executable only to the child QA process, and runs the browser profile; `--profile auto` combines ordinary change routing and browser QA in the same delegated run. `--check`, `--install-only`, and `--stage browser-headless-probe` cover read-only discovery, one-time bootstrap, and exact reruns without system/Flatpak installation. The pin was selected from GoogleChromeLabs `chrome-for-testing` commit `c1decc26ccd28463f6e2fe3c8d490d7a5a13adbd`, then bound to exact archive and executable size/SHA-256; the browser and archive are local QA cache and never enter plugin/source packages. Other hosts may use `CE_DECKY_BROWSER` or normal Chrome/Chromium discovery with `python scripts/qa.py --profile browser`. The probe starts one isolated process group, waits for a JavaScript-set title through loopback DevTools endpoints, records the product version, and stops the complete group before deleting its disposable profile. Build future QAM-sized screen fixtures on the same raw-CDP lifecycle before adding Playwright. Mocked screenshots are presentation regression evidence, not Decky compatibility evidence.
Two shapes of component test have failed on the CI runner and never here, and
both were the test rather than the screen. A press that starts an async action
commits its state on a microtask, so a fake clock advanced in the same statement
races it: flush the press first. And `findBy` waits for the thing it names, not
for what arrives later inside it, so awaiting a row that renders before its data
and then asserting on that data with `getBy` is the same race one level down.
Moving the focus ring is a commit of its own, after the one that rendered the
control it lands on, so awaiting that control and then reading
`document.activeElement` races it in the same way.
Wait for what the answer puts on the screen, and check an absence only after
something proves the answer arrived.

Keep browser tests independent of Steam private DOM/class names. Prefer stable component boundaries and accessible labels. A UI test should fail for a user-visible regression, not a harmless markup refactor.

## Packaging

Create the Decky archive with:

```powershell
python scripts/package_plugin.py
```

The deterministic ZIP is written to `artifacts/CE-Decky-v<version>.zip`. It includes runtime frontend/backend files, README, licenses, and notices; it excludes source tests, Cheat Engine, tables, and the local frontend source-digest stamp. Packaging fails closed unless `dist/index.js` was produced for the current frontend sources by the official Rollup build, then imports the backend from a freshly unpacked ZIP under Python isolated mode. The release profile also runs `target_package_probe.py` against that exact artifact.

`artifacts/` and staging directories are ignored. Never commit generated archives.

### The artifact the Store builds

This project's release ZIP and the Store's are different build paths, and a
passing `release` says nothing about the second one. The Store build is:

```bash
python scripts/check_store_artifact.py --build
```

It fetches the pinned Decky CLI into ignored `build/`, runs
`decky plugin build -b -e <engine> -o build/decky-store -s directory .` through
podman or docker, and checks what comes out: the members the backend needs, the
notices and licenses that only arrive through the `defaults/` strip, the
committed vendor tree, a `publish` block that would not leave the listing
blank, a version that comes from `package.json`, no table, archive or Windows
binary payload, no development directory, and the same isolated backend import
`package_plugin.py` runs, against the Store ZIP instead. `python scripts/qa.py
--profile store` is the same thing as a stage, and it reports INCOMPLETE rather
than failing on a host with no container engine.

The CLI version is pinned to the one `decky-plugin-database` downloads in its
own workflow. Moving it is deliberate, because that build is what a submission
is judged by. The builder image is Alpine with Node 20 and pnpm 9, and it runs
`pnpm i --frozen-lockfile` followed by `pnpm run build`, so a lockfile this
repository cannot install there fails the Store build and nothing else.

`-s directory` names the archive and its root folder after the checkout
directory. In `decky-plugin-database` that is the submodule directory, which is
why it has to be added there as `CE-Decky`.

### The committed runtime dependency tree

`py_modules/vendor` is third-party code and it is committed on purpose, which is
the one exception to keeping generated dependencies out of Git. The official
Decky Store builder copies `py_modules/` exactly as the repository holds it and
has no Python dependency phase, so a tree that is produced at packaging time
reaches this project's own ZIP and never reaches the Store artifact. Rebuild it
from the hash-pinned lock with:

```bash
python scripts/update_runtime_vendor.py
```

It installs `requirements-runtime.lock` with `--require-hashes --no-deps
--only-binary=:all:`, drops `bin/`, bytecode and `__pycache__`, refuses a
non-pure artifact (`.so`, `.dll`, `.pyd`, `.exe`, `.dylib`), retains
`*.dist-info` metadata and its license files, and records two digests in
`requirements-runtime.vendor.sha256`: the lock's, and the tree's.

`--check` compares both without touching anything and is what `verify_repo.py`
runs on every profile, so an edited tree and a lock that moved without a rebuild
are each refused before packaging. What it proves is that the committed tree is
the recorded one and that the lock has not moved; it does not re-derive the tree
from the index, because that would put a network fetch in every repository
stage. `--rebuild-check` is the stronger form and installs from the lock into a
temporary directory to compare, which is a maintainer command rather than a
gate. Changing the lock means running the rebuild and committing both the tree
and the recorded digests in the same change.

`.gitattributes` stores this tree with `-text` so that line-ending
normalization cannot make a checkout disagree with the digest recorded for it.

### Changing the pinned Cheat Engine release

`docs/SECURITY.md` **Cheat Engine distribution boundary** is the contract: the
packaged manifest pins the exact HTTPS host, byte size, SHA-256 and the SHA-256
of the reviewed publisher's `SubjectPublicKeyInfo`, the installer is never
executed, and no Cheat Engine bytes are in Git or in a release artifact. This is
the procedure for moving that pin, and it is a maintainer step reviewed like a
security-sensitive dependency update, never an automatic manifest source.

The clean installer URL is published nowhere machine-readable and its path
segment is opaque, so it is read out of the artifact rather than guessed. The
download button on the vendor's site serves a third-party download manager
instead, whose filename is randomized per page load, whose bytes differ per
download, and which is signed by a different publisher.

```bash
python tools/inno_setup_reader.py urls <downloaded stub>
python tools/authenticode_verify.py verify <artifact>
```

The first reads an Inno Setup executable's own setup metadata without executing
it and prints the clean installer URL and silent arguments its compiled script
holds. The second computes the artifact's Authenticode digest, has OpenSSL
verify the PKCS#7 signature over it, and requires the signer's public key to be
the pinned one. It pins the key rather than building a chain, so it does not
depend on a trust store and does not soften when a CA changes: a rotated URL
serving the same key is a routine manifest update, and a different key is the
case that stops and is looked at, which is what separates the clean installer
from the download-page stub.

`tools/ce77_extract.py` extracts the reviewed installer's payload standalone,
without Wine, Proton, 7-Zip or any dependency, and is how a format claim is
checked outside the backend.

A manifest change updates the upstream version, URL and allowed host, the
expected and maximum byte size, the SHA-256, the review date, the publisher key
observed during review, and the extractor tests or fixtures the format change
requires.

## Version and GitHub Release workflow

The current development version is synchronized across `package.json`, `AGENTS.md`, and the newest `CHANGELOG.md` heading. `plugin.json` intentionally carries Decky metadata but no duplicate version. Follow the version-session contract in `AGENTS.md`; there is no `Unreleased` section.

An entry goes at the top of the current version, with no blank line between it
and the one below, because a blank line renders the whole section as a loose
list. Write it with the helper rather than by anchoring an edit on text that
changes every round:

```bash
python scripts/changelog.py add "[Fixed] What changed, and why it was wrong before."
```

It refuses a label that is not one of the five, an em dash, more than one line,
and a version heading that `package.json` has moved past.

To prepare a release without publishing it:

1. Confirm the synchronized development version and date in `package.json`, `AGENTS.md`, and `CHANGELOG.md`.
2. Confirm the newest changelog heading is `## x.y.z — YYYY-MM-DD` and contains only concise labeled outcomes.
3. Run `python scripts/qa.py --profile release --bootstrap`; omit `--bootstrap` when lock fingerprints are already current.
4. Review the package contents and checksums.

Only after explicit user approval, create and push the exact tag `v<version>`. `.github/workflows/release.yml` then:

- rejects a tag that differs from `package.json` or lacks its changelog heading;
- restores pinned dependencies from `pnpm-lock.yaml`;
- runs the full gate and its deterministic packaging;
- creates the plugin ZIP and `SHA256SUMS`; the reviewed repository tag is the source authority;
- creates a GitHub build-provenance attestation for the plugin archive;
- publishes a GitHub Release from the already-pushed tag, marking SemVer prerelease versions as prereleases.

Do not create or move tags to repair a failed release. Fix the cause in a new commit, choose the appropriate new version/tag, and preserve the failed workflow as evidence.

## SteamOS development

The permanent SteamOS routing and safety contract is in `AGENTS.md` under **If you are running on SteamOS**, and which helper answers which question is its **Tracked helpers** table. This section documents the reusable tools and commands themselves, one row per helper, and is the authority for what each one guarantees: `AGENTS.md` routes to a helper and never restates it.

Run the bounded preflight once before the first target mutation in a clean agent run:

```bash
python3 scripts/target_agent_preflight.py
```

It names the machine and checks the Python/Git/workspace boundary every host needs beside the Linux/procfs one only the target has, without reading credentials, changing Steam/Decky state, or reaching the network unless `--network` is explicitly selected. Three answers are reported separately, because a desktop gives a different one to each: `repository_ok` for ordinary repository work, `target_host_ok` for whether a helper that reads the live process table can run here at all, which an ordinary Linux desktop satisfies, and `target_ok` for whether this machine is the device, which needs SteamOS. A Linux desktop developing against a Steam Deck answers yes, yes, no, and `docs/REMOTE_TARGET.md` is that arrangement. `host` carries the model: SteamOS reports `VARIANT_ID=steamdeck` on a Steam Machine as well as on a Steam Deck, so the device comes from DMI, where `Fremont` is the Steam Machine, `Jupiter` the Steam Deck LCD and `Galileo` the Steam Deck OLED. The exit code separates the two answers: 0 when both hold, 3 when the only thing wrong is the machine, and 2 for an ordinary failure. To ask nothing but the machine question:

```bash
python3 scripts/host_platform.py --line
```

Every helper that needs the device asks `host_platform` first and refuses a host it cannot run on by name, with exit code 3, before doing any work; `--help` still works everywhere. Helpers with no platform requirement, such as repository QA, packaging, the changelog tool, the CT inspector probe and the TLS probe, run on Windows and macOS unchanged. Repository QA remains `python3 scripts/qa.py`; browser UI QA uses `python3 scripts/browser_harness.py`, which manages the pinned local Chrome-for-Testing cache described above without installing a system browser.

### Reusable target tools

These are permanent development tooling. They are how the plugin is built, installed, probed and captured on a real device, they are not campaign artifacts, and publication does not remove them: at most they are reorganized into another directory. Use each helper only for the guarantee named by its `--help` text or module docstring.

Every row says when to reach for the tool before it says what the tool does, because the question is what an agent is holding and the mechanism is what it needs once it has chosen. `AGENTS.md` **Tracked helpers** indexes the same tools by question and routes to them; this table is the authority for what each one guarantees and how it is run.

| Tool | When to reach for it, and what it then guarantees |
|---|---|
| `host_platform.py` | **Before assuming which machine you are on.** Say what machine this is: operating-system family, SteamOS identity and the Valve model from DMI, and whether the live process table can actually be read rather than merely being mounted. It is the module every target helper calls to refuse a host it cannot run on, `target_device` is the separate answer to whether this machine is the device, and `--line` prints the one-line answer. |
| `target_decky_env_probe.py` | **When you need Decky's own paths and versions and the install authority could not give them.** It is the fallback and never the authority: SteamOS may deny a same-user sibling process the environment bytes, and a directory basename is not an identity. Read only allowlisted Decky version/path variables from the exact live plugin process. |
| `target_package_probe.py` | **Before installing an artifact you have not already proved.** A `release` profile ran this over the ZIP it built, so run it again only for an artifact that arrived some other way or whose evidence is gone. Verify archive structure, identity, and bounded contents for the exact ZIP that will be installed. |
| `target_plugin_rpc.py` | **To make the live backend do something.** One bounded call to one method on the loaded plugin, through Decky's own authenticated socket, with the answer or the backend's own refusal printed as JSON. It is for behaviour that exists only in the running process and cannot be reached by replacing files or reading state: starting an owned self-test so that a stop can be observed against it, or a search that arms the background listing pass. Positional arguments are one JSON array, and the method names are the ones `src/api.ts` maps. It writes nothing itself; what the named method does is that method's business, so a mutating method is a mutation. Reach for the read-only probes first for anything they already answer. |
| `target_plugin_install.py` | **To find out whether a live Decky holds this plugin, where it lives, and to replace it.** `authority` is also the first question of any device session and the source every probe below takes its paths from. Resolve exact install authority from Decky's authenticated live inventory/backend, falling back to the last durable XDG install record only when Decky is unavailable, and report the digest of the frontend bundle as installed beside the `settings_dir` and `user_home`, which is where the state and launch probes should take those paths from. The live backend holds those two; the recorded fallback reports what the install validated against that backend and wrote down, checked again against the filesystem and reported as absent where it no longer holds, with `source` saying which of the two answered. That fallback is the one moment they are wanted, because a consumer that reached it has already failed to ask the backend; or install/replace the exact ZIP, verify installed members/backend identity, and reload the frontend. Install reports the Steam AppIDs running before and after because reload can disrupt controller input. It prints one summary line; `--json` prints the full report. `--remote USER@HOST` installs on a device across the network instead: it sends this exact artifact to the mirror of this checkout there and runs that device's own copy of this helper against it, refusing when that copy is not this source. |
| `target_plugin_log.py` | **When a question is about what the backend actually did.** Decky writes one log file per plugin load, names it for the moment of that load and keeps only the last few, so the file to read has a different name after every install: this finds the newest by write time rather than by name, tails it bounded, and filters it, instead of `ls -t` piped into `grep` against a path that changes underneath the next call. The log directory is derived from the plugin root, which is Decky's own sibling layout; `--log-dir` overrides it. `--journal` reads the same records out of the system journal instead, which keeps them across a reload, a webhelper restart and a reboot, and is therefore the only route to a session that an install has already rotated away; `--since` bounds how far back it looks. `--frontend` reads what the Quick Access panel recorded, from the copy the panel flushes as it goes rather than the one that dies with its renderer. Read-only: it never writes, rotates or removes a file. |
| `target_state_probe.py` | **When a question is about what the plugin currently holds for a game:** its selected and previous table SHA, consent, auto-load, pins, remembered state, sessions and owned launches, and what its provider caches hold. Reach for this before reading `profiles.json` by hand, which is the same answer without the bounds or the correlation. Correlate bounded config/profile/session/runtime/launch state using exact `DECKY_USER_HOME` and settings paths, and say which AppIDs are running now and what each profile selects. Without `--appid` it reports the running AppIDs and a bounded summary per profile, which is the route from a running game to the table and target process it is configured with; with one it adds that game's session, runtime and owned-launch correlation. |
| `target_ui_freeze_probe.py` | **When the quick access panel or Steam itself has stopped responding.** Say whether Steam's UI is still executing JavaScript and, for a page that is not, break into it and print the call stack that wedged it. Read-only: it evaluates one constant expression, injects no input, and `--resume` lets a paused target run on. A wedged Steam UI takes the controller with it, so this is the only way to learn anything before the power button. |
| `target_screenshot.py` | **When the question is what the user is actually looking at.** It is also what to reach for instead of asking the maintainer to photograph the screen. Capture a composited Gamescope frame containing Steam/Decky UI, or the game-only base plane. `--tile` cuts one plugin window out of that frame as a published lossless screenshot instead. `--remote USER@HOST` captures on a SteamOS device across the network and keeps the frame here, with no checkout needed at the far end. |
| `target_ui_layout_probe.py` | **When a screen may not fit the display it is on.** A handheld gives a page 534 CSS pixels of height where a television gives 844, and that difference is what pushes a heading or a footer off the screen. Report the CSS viewport Steam's Game Mode gives each of its pages, the device pixel ratio behind it, and the height this plugin's own panel and any open modal actually occupy against it. Read-only over Steam's CEF endpoint: it measures and changes nothing. This is what says whether a screen fits, because a screenshot is in output pixels and the scale between the two is exactly the unknown. |
| `target_archive_probe.py` | **When an import fails on the device and nowhere else.** The host 7-Zip is a target tool with its own version and behavior, and it is the half of archive support that off-target regressions cannot cover. Exercise the production 7z adapter against the target host tool with a disposable benign archive. |
| `target_ce_launch_probe.py` | **To prove Cheat Engine actually starts, without a game, a table or a person at the device.** It is the agent-verifiable half of an attached launch; the other half needs the maintainer. Exercise the production CE/Proton launch and bridge path in its bounded self-test mode. |
| `target_session_cost_probe.py` | **When the question is what a running session costs the device.** Measure what one session costs on the machine it runs on: CPU time per process across one bounded window, grouped into Cheat Engine, `wineserver`, the plugin backend, the game, Decky, Gamescope and Steam, with the frame rate read from the live Gamescope statistics pipe over the same window. The same window also carries the backend's own repeating-path counters read at each end and never inside it, per-process wakeups, runqueue delay, resident memory and byte counters where the kernel allows them, package power, GPU load, clocks and temperatures sampled across it, the CPU pressure delta, and the size of the process table. Read-only. |
| `target_primitive_bench.py` | **When the cost is suspected in something the session repeats rather than in one action.** Time the production primitives a live session repeats, on the machine it runs on: the process-table walk, the supervisor's scan, the one-second baseline check, the Proton inventory and the Cheat Engine hash. Reports median and p95 of per-thread CPU and wall time with the size of the process table beside them, and what each pass would cost at a one-second and a three-second cadence. Read-only. A primitive whose inputs were not given is left out rather than run against an invented one, and the Cheat Engine executable is taken from the live backend when it is not named. |
| `target_prefix_probe.py` | **When a compatibility prefix has to be named and never guessed.** Ambiguity is reported as ambiguity: a probe that resolves one by preferring a candidate is how the wrong game's prefix gets written to. Resolve an exact existing compatibility prefix from Steam library authority without mutation. |
| `target_tls_probe.py` | **When a provider fails in a way that looks like the network.** It separates this device's trust store and the production TLS context from the route and the operator, which otherwise present identically. Exercise the production TLS context with one bounded verified HTTPS request. |
| `target_webhelper_threads.py` | **When the freeze probe itself gets no answer, because CEF is not responding at all.** Print what every `steamwebhelper` thread is doing, read from `/proc` rather than from the debugger: name, scheduler state, kernel wait channel, and processor time across two samples, which is the one reading that separates a thread spinning in a loop from a thread waiting on something. Chromium names its threads, so `CrRendererMain` is identified without symbols, and a renderer whose command line still says `zygote` is named from its threads instead. Unprivileged and read-only: no debugger is attached, no thread is stopped, no input is injected, and nothing about Steam, Decky or a running game changes, so it is safe to run while the UI is wedged, which is the only time it is worth running. `parked` is a futex wait and means "waiting for something": normal for a pool worker, a finding for a main thread that will not draw. `--stacks` additionally asks `eu-stack` for native frames. On the Steam Machine this works as the ordinary user for every webhelper process, renderer included, which `kernel.yama.ptrace_scope=1` was expected to refuse and does not; frames arrive as bare addresses where Steam's binaries carry no symbols, and a per-thread walk failure is an unwind-information limit rather than a refusal, so success is judged on whether frames came back. Where none did, the report gives the reason and the elevated command instead of going quiet. `python3 scripts/target_webhelper_threads.py --interval 2` |
| `target_memory_watch.py` | **When Decky, this plugin or the whole device is slow, swapping or unresponsive, and what is holding the memory is the question.** Sample the loader, every plugin backend and the machine's free memory and swap on an interval, one line per sample, with `--out` appending the same samples as JSONL for the curve. Reads `/proc` only: no plugin is called and nothing about Decky or Steam is changed. Leave it across the session that is suspected, because a climb is only attributable with its rate and its trigger, and a reboot takes both. Attribution is by process: the loader relays for every plugin, so a loader that grows is not evidence about any one of them. `python3 scripts/target_memory_watch.py --seconds 7200 --interval 60 --out build/memory-watch.jsonl` |
| `target_snapshot.py` | **When a number or a failure is about to be reported and needs the machine it came from.** Record one bounded read-only snapshot of the host: platform, tool versions, Decky environment variables, the directories this plugin and Steam own, and the TLS trust store. |
| `table_ui_probe.py` | **Before assuming a real table is usable in Game Mode.** A table with thousands of records can put its controls out of reach of a controller, and that is a property of the table rather than of the code. Report whether a real table's controls stay reachable in the QAM control browser, through the production CT inspector and without executing anything the table carries. |
| `provider_probe.py` | **When a source stops answering and it has to be said whether that is the route, its challenge, or us.** Print bounded live route/challenge fingerprints or run one isolated provider download/SHA diagnostic. |
| `provider_corpus_survey.py` | **When a trust or parser decision needs to be ranked by what real tables actually do** rather than by what the format permits. Sample real tables across forum age and report what they contain, so the hazards a table's content can present are ranked by how often tables actually present them. Additive, content-addressed and resumable; reads the plugin's provider index from a copy and imports into a scratch store. Takes the exact `DECKY_USER_HOME` as `--user-home`, or the index path itself, and refuses rather than guessing one. |
| `provider_search_survey.py` | **When the question is coverage: how many of this library's games any source can actually serve.** Run production search across the installed library from a copied cache and report per-game usable results without changing plugin state. Takes the exact `DECKY_USER_HOME` as `--user-home`, or the userdata and cache paths themselves, and refuses rather than guessing one. |

### Measurement

What `AGENTS.md` requires of a recorded number, and why.

A measurement is recorded only once the tool that produced it is a tracked helper under `scripts/`, with its command in the table above. The preservation rules cannot save a harness that was never committed: one written in a scratch directory on the device disappears with that directory, and the number it produced outlives it as a figure nobody can reproduce, challenge or repeat on second hardware. That has happened here once, and re-deriving the harness cost more than writing it as a helper would have.

One report is a reading; a measurement is two of them with one thing changed. One sample bounds a quantity from one side only, so say which side. A single transfer or download measures a floor and never a ceiling, because the far end, the route and the one connection each cap it on their own: a figure from one of them names what the path is at least capable of and settles nothing about what limits it. A number is recorded with the hardware, the software identity, the method, the window and the spread.

### Measuring what a session costs

The shape that produced the figures in `docs/FIELD_NOTES.md`, and the shape to repeat:

- hold one scene still, keep the quick access panel closed throughout, and take three 90-second windows: unattached, attached, unattached. The two unattached windows are what says whether a difference is real, because an effect smaller than the baseline's own movement is not yet a result;
- pass the same identity flags to every window being compared. A run read with `--appid` and one read without group their processes differently, and the difference between them is then partly the flags;
- read the backend's repeating-path counters from the same windows. They are what makes a figure for the whole process attributable to one loop, and the probe takes them on each side of its own window rather than inside it, reporting how far outside each end they sit;
- for per-call figures under a live game, `target_primitive_bench.py` in the same session. Cadence arithmetic from it and the counters agreeing to within a few percent is two instruments confirming each other rather than one checked against itself.

Two things the counters must never lose, because they are what makes the reading trustworthy: nothing is logged per call, since a line per invocation adds work at exactly the rate being measured, and the snapshot increments nothing, so reading the counters never becomes one of the counts.

What the compositor's statistics pipe cannot answer, so that no device session is planned around it:

- **Frame pacing.** It reports a rate and nothing about the distribution behind it, so a session that causes hitches without moving the mean cannot be seen at all. Anything that shows frame times, such as MangoHud's own logging, needs per-game launch options, which changes how the game starts and therefore what is being measured. Unexamined, and worth desk time before it is worth a device session.
- **The pipe's full vocabulary.** Only `fps=` and `focus=` have ever been observed, and the pipe is silent whenever nothing is compositing, so the complete set can only be enumerated with a game running. A line naming the application's own rate rather than the composited one would remove the power-limit workaround entirely.
- **RAPL.** `/sys/class/powercap/intel-rapl:0/energy_uj` is denied to a normal user on these devices; package power comes from hwmon instead.

A successful purpose-built result does not need a second hand-written probe.

`target_decky_env_probe.py` reads the live plugin process and may answer `"state": "missing"` even while it is running, because SteamOS may deny a sibling process access to `/proc/<pid>/environ`. It remains useful when all allowlisted Decky paths are needed, but installation authority comes from the authenticated helper instead:

```bash
python3 scripts/target_plugin_install.py authority
```

The command first requires one enabled exact-name plugin in Decky's inventory and the same version from its backend, then validates the backend's own `plugin_dir` against its regular `plugin.json` and `package.json`. A build installed before that field existed may bootstrap once from the backend's exact `settings_dir` through Decky's documented `DECKY_HOME/settings` and `DECKY_HOME/plugins` relation, under the same checks. Every successful install also writes a bounded record under `$XDG_STATE_HOME/ce-decky-development/target-installs/`, or the standard per-user XDG state fallback, so repository cleaning cannot remove it. That record is used only when live Decky is unavailable; a live disagreement fails closed. Treat any combined failure as a blocker and never scan older sessions. The answer also carries `bundle_sha256`, the digest of `dist/index.js` as it is installed, which is what says whether the device is running the build in the working tree: a development version is rebuilt and reinstalled many times without moving, so the root and the version cannot separate two of them. Compare it against the working tree's own `dist/index.js` instead of reaching into Decky's directory for a digest by hand. A bundle that cannot be read reports `null` and leaves the rest of the answer standing.

### Plugin data on the device

The plugin owns one tree, rooted at `<DECKY_USER_HOME>/.cheat-engine-decky/`. Read it directly only for diagnosis, never as a production path, and never hard-code the home: `DECKY_USER_HOME` is the authority and `py_modules/ce_decky/paths.py` is where the tree is defined.

| Path | Holds |
|---|---|
| `state/` | durable records: `profiles.json`, `blocked_tables.json`, provider source selection |
| `cache/` | provider indexes and sitemaps, `provider-results.json` for the last search, provider diagnostics |
| `tables/sha256/<first two hex>/<digest>/table.CT` | imported table bytes, addressed by content |
| `tables/metadata/` | one record per imported table |
| `ce/` | the plugin-owned private Cheat Engine copy |
| `tmp/` | staging for downloads and archive extraction |

Decky owns the rest: `<DECKY_USER_HOME>/homebrew/plugins/CE-Decky` is the installed plugin, `homebrew/settings/CE-Decky/` its Decky-side config, and `homebrew/logs/CE-Decky/` the backend logs, which rotate on every reload.

### A search that was slow, or came back with nothing

Four reads, in this order, and the second is the one that answers it:

1. `homebrew/logs/CE-Decky/` for `operation.completed duration_ms name=search_tables`, which is how long the whole search took, and for the `catalog.search_completed` line beside it, which carries the per-source result counts and the state of the FearLess index. Neither says which source spent the time.
2. `~/.cheat-engine-decky/cache/providers.json` for `last_latency_ms` per provider. This is the attribution: one search measured at 44.9 seconds was 44.8 of Playground against 2.4 of FearLess, and nothing in the log says that. `state`, `last_error` and `cooldown_until_epoch_s` are beside it, so a source that answered nothing because it is in cooldown is distinguishable here from one that had nothing to give.
3. The modification times of the cache files in the same directory, for where inside a source the time went: `playground-sitemap.json` written 37 seconds into that search is the site index being fetched rather than anything about the game.
4. `catalog.provider_failed` and `catalog.provider_partially_available` for a source that stopped, with its own reason. A source reported as `n/a` on the results line with rows still on screen is a partial answer, not an empty one.

A search's own `search=` id is a hash rather than the query: the query is never logged. Correlate by time.

After the package probe and release QA cover an unchanged artifact, install it with exact authorities. The release run prints both of them on its own summary, as `package:` and `sha256:`, which is where they are taken from:

```bash
python3 scripts/target_plugin_install.py install artifacts/CE-Decky-v<version>.zip \
  --sha256 '<exact-package-sha256>' \
  --replace
```

It prints one line: the installed version, the package identity, the files verified, the webhelper replacement, whether a Steam app was running while it happened, and what the install cost in seconds. That last figure is what to size the command's `timeout` from on a host whose cost is not yet known, and it is the source for the install row in `docs/FIELD_NOTES.md` **Measurements**. Pass `--json` for the whole verified report, which is what to read when something failed; it carries the same number as `elapsed_s`.

The SHA is mandatory; the helper resolves the root through the same authority path as its read-only command. `--plugin-root` remains an explicit override for an exact root already established in the current run, never a discovery guess. Omit `--replace` only when that exact root is proven absent. The harness waits for Decky's confirmation RPC, verifies the installed tree and backend identity, waits until the process running the plugin is the one Decky will keep, and only then restarts Steam webhelper so the exact frontend is imported. That reply is what says so; when it never arrives, because Decky closed the install socket after accepting the confirmation, the helper instead waits out longer than the whole life of a generation about to be replaced, since the premature load satisfies every other check it makes. That wait is not politeness: one install of an already installed plugin loads it twice, and a reload requested between the two loads leaves a second CE Decky in the panel. `docs/FIELD_NOTES.md` carries the exact Decky behaviour and the run it was read from. It then reads the panel's own record for what the reloaded frontend ended up holding, which is the only observation that tells one panel row from two: one row, two rows and none leave the same files, the same loader inventory and the same backend. The number is mounts with no dismount against them and never a count of imports, because Decky's `importPlugin` removes the plugin's row before appending its replacement and dismounts it as it goes, so an ordinary re-import is two imports and one row. The boundary it reads from is two things taken just before the reload is requested, and neither is a moment on anybody's clock: where the record ended, and which frontend generations had already written to it. The file outlives the renderer, so a clock corrected backwards would otherwise hand the previous frontend's rows to this reload; and the frontend being replaced is alive at that point, flushes on a five second timer and is told by Decky's last load to import the bundle again, so where the file ends cannot reject what it writes afterwards. Each record names the renderer that produced it, and a renderer already in the record is one this reload is replacing. A frontend that has written nothing yet is in no record, which is what a first install and a reinstall after a removal both look like, so each mount also says when its renderer started. That is what places a row whose renderer the record has never seen, against the moment the reload was asked for: started after it, this reload created it; started before it, it was already there. Both are fixed moments, so the same row reads the same way on every pass over the file, which an age compared against a growing wait does not. A record that says neither is counted and left out of the total, which makes the answer `panel cardinality unverified` rather than a count that might be somebody else's. Exactly one row and nothing unpairable is the quiet case and says nothing. Every other answer is on the summary line, because each is a different thing to do next: `panel lists it N times over one backend` is the duplicate, which one webhelper reload with no install beside it clears; `panel holds no CE Decky row` means the frontend imported the plugin and kept none of it, so the install is real and the row is not; `panel cardinality unverified` means the record cannot answer the question, either because a mount carries no row id or because the file was replaced under the boundary; and `panel import unobserved` means nothing was recorded in time and the panel was not checked. None of the four fails the command, because the files, the inventory and the backend are proven before any of it is read. It does not replace human controller/QAM assertions. Webhelper replacement can close Decky UI or displace a game; announce it and do not attempt Steam-URI focus repair.

Get the exact state-probe path authorities from `target_decky_env_probe.py`, then correlate state without grepping private files:

```bash
python3 scripts/target_state_probe.py \
  --home '<exact-DECKY_USER_HOME>' \
  --settings-dir '<exact-DECKY_PLUGIN_SETTINGS_DIR>' \
  --appid '<exact-AppID>'
```

The state probe emits allowlisted identities and managed-installer PID/PGID presence. It excludes launch-option contents, table payloads, credentials, provider temporary URLs, and installer environment markers, and does not mutate recovery state. It cannot report in-memory installer history; correlate that only from production QAM and the ignored Decky log. `connected=true` requires the exact current identity and a fresh heartbeat.

It also reports `provider_caches`, which is what a question about search answers from: every provider cache on the device by size and age, and the FearLess listing by shape - how many of its pages are indexed of how many, the age of the oldest and newest of them, how many are still due a refresh, and whether the background pass is armed, which it is only for a day after Search was last used. None of the listing's own topics are emitted. That last answer is taken from both of the places the plugin records it, the index itself and `cache/fearless-search.json` beside it, under the same validity rule and the same newer-of-the-two rule the plugin loads by, down to refusing a moment that is not a finite positive number or is further into the future than a page stays fresh and reporting it as `searched_rejected` rather than as no moment at all: the small file is the one a search writes as it happens, while the index cache carries the same value only when a crawl next writes it, so a report that read only the index would answer with a moment the plugin no longer believes. Reach for this rather than reading `cache/fearless-index.json` by hand: the same numbers without the bounds, and a figure read that way is not one this project may record.

Read what the backend did, without selecting the file by hand:

```bash
python3 scripts/target_plugin_log.py \
  --plugin-root '<exact-plugin-root>' \
  --lines 200 --grep 'fearless|session'
```

`--files N` reads the N newest files and prints them oldest first, for a
question that spans a reload. `--json` returns the whole report, including how
many log files the directory holds: an install rotates the set, and a question
about a session before one is a question about a file that is no longer there.

When it is, ask the journal instead, which keeps the same records across the
reload, the webhelper restart and the reboot:

```bash
python3 scripts/target_plugin_log.py --journal --since=-2h --lines 400
```

The `=` is required rather than stylistic: `journalctl` windows start with a
`-`, and argparse reads a separate `-2h` as an option rather than as this
option's value.

Only this plugin's own records come back, plus the loader lines that say the
renderer or the plugin host restarted. Every Decky plugin logs under the same
identifier and a support bundle is attached to public issues, so the filter is
a privacy property rather than a convenience, and it is the same filter the
bundle uses: what is printed here is what a bug report would carry.

Read what the panel did, from the copy that outlives its renderer:

```bash
python3 scripts/target_plugin_log.py --frontend
```

The first line is the one to read. A panel that closed normally ends on
`panel.dismounted`; a panel that was killed while wedged ends on whatever it was
doing, and a new `session` value after it is a renderer that was replaced. This
is the record that answers a frozen panel at all: the panel's live ring buffer
lives in that renderer, and restarting Steam's webhelper to recover the UI
destroys it, which is why a bundle collected after a recovery reports
`frontend_entries=0`.

### Capturing an incident

When the maintainer asks for reported behaviour to be reproduced or checked on
the device, everything goes into one directory before anything is recovered:

```bash
mkdir -p "build/incidents/<symptom>-$(date -u +%Y%m%dT%H%M)"
```

Capture first, recover second. Restarting the webhelper, reloading the plugin
and installing a package each destroy a record that existed only while the
symptom was live, and they are also the three things that make the device
usable again, so the temptation to reach for them first is exactly the problem.

What the directory holds:

| Part | How |
|---|---|
| The support bundle | Advanced → the bundle button, then copy the archive in. This is the one that has to be there: a released bug report is that archive and a sentence |
| What the screen showed | `target_screenshot.py --region qam --prefix '<symptom>'`, before any mutation |
| Whether the UI still runs JavaScript | `target_ui_freeze_probe.py`, once |
| What the webhelper threads are doing, when that probe gets no answer | `target_webhelper_threads.py --json --out <dir>/threads.json` |
| What the backend did | `target_plugin_log.py --journal --since=-1h --json`, which survives the install that follows |
| What the panel did | `target_plugin_log.py --frontend --json` |
| What was done, and in what order | A short `notes.md`. A capture nobody can sequence is a pile of files |

`build/` is ignored, so the directory is local and never committed. That is
deliberate: it may hold device paths, game names and this machine's host name,
which a public archive would not. What turns out to be durable is moved into
`docs/FIELD_NOTES.md` as a conclusion, and the directory is not a substitute for
doing that - nobody reads someone else's `build/`.

Name the directory for the symptom in the user's terms rather than for the
suspected cause: `panel-close-20260912T0127` stayed correct while every theory
about what caused it was replaced.

Capture what the user sees with Gamescope itself:

```bash
python3 scripts/target_screenshot.py --region qam --prefix '<symptom-or-workflow>'
python3 scripts/target_screenshot.py --region center --prefix '<modal-name>'
python3 scripts/target_screenshot.py --remote '<user>@<target>' --prefix '<symptom-or-workflow>'
```

A capture is named after the machine it came from, then the label above, then the timestamp and region, and lands in
`~/Pictures/Screenshots` unless `--output-dir` says otherwise. The machine comes from Valve's own DMI product name, read on
whichever end actually took the frame, so one directory holding a handheld's screens and a television's reads as one set and no
network address is written into a filename. `--remote` needs nothing on the device but an SSH key and its own Python: only the
capture runs there, and the cropping, scaling and tile cutting stay on this machine, so no mirrored checkout is involved.

The default uses Gamescope's composited-frame request so Steam, Decky, and the running game are visible together. `gamescopectl screenshot`, exposed as `--base-plane`, contains only the game plane and cannot answer overlay questions. The helper resolves `DISPLAY` from the Steam user's published `gamescope-environment`, downsizes and JPEG-encodes the result, and accepts `--output-dir` rather than requiring a maintainer-specific path. Use `--region full` only for whole-screen context. Never synthesize controller/keyboard input to reach a screen; ask the human to open it and then capture. Captures are temporary evidence, not a release archive.

The published screenshots in `docs/assets/screenshots/` are cut by the same helper, and are the one result it keeps losslessly at the frame's own scale:

```bash
python3 scripts/target_screenshot.py --tile panel --shape qam
python3 scripts/target_screenshot.py --tile consent --shape modal
python3 scripts/target_screenshot.py --tile advanced --shape modal --trim-bottom 275
```

Two tracked assets are generated rather than hand-edited, and both regenerate from their own tracked master rather than from whatever is on a device:

| Tool | When to reach for it, and what it then guarantees |
|---|---|
| `build_screenshot_collage.py` | **When the tiles above have changed and the README strip has to follow.** It composes the strip from the tiles in `docs/assets/screenshots/`, which are the durable part; the strip is derived and is never edited on its own. |
| `build_mascot_asset.py` | **When the panel mascot changes.** The Decky bundle has no asset pipeline, so the mascot travels as a data URI compiled into `dist/index.js`; this regenerates it from the tracked master artwork instead of anybody pasting base64. |

A tile's width is Steam's own geometry rather than a measurement: Game Mode lays out in a fixed 1280 px logical grid, so the quick access panel is the right-hand 256 logical px with its border and a modal sheet is 460 logical px centred, and every modal tile is therefore the same width. Only the height is measured, by finding where the window starts and ends inside those columns: the panel against its own surface colour, since it is as tall as the screen whether it is full or empty, and a sheet by the wide unbroken run of surface colour its top and bottom edges are, which is what makes the cut independent of how bright the game behind it is. Steam's button-hint bar is kept out of that search. Each tile is then padded with an equal margin of the surface colour, so it has air around the window when it is published on its own and `scripts/build_screenshot_collage.py` only has to close that margin off with an edge.

A capture corroborates; it does not prove a sequence. Controller focus traversal, scroll-follow, the OSK returning focus, and any interaction that is only true while it happens cannot be established from a still frame, and no coding agent here can drive Game Mode to produce one: those observations belong to a person at the device. Ask the human to open each screen, capture, and look at the result before the next one; `--keep-png` saves the whole frame so `--source` can recut a tile without the device, and `--trim-bottom` takes a screen that continues into content a published tile must not carry, which is why `advanced` stops above this device's prefix paths. Check every tile for personal paths, account or machine names, and library contents before committing it.

Provider ranking or identity changes require the whole-library survey, not a one-game sample. Compare per-game usable-result counts with the latest valid baseline and investigate every loss. `provider_probe.py --routes` only proves the diagnostic completed; zero exit does not mean every provider route was direct. `--playground-api` is one isolated mutable production check, not CI evidence.
