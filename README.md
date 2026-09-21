# CE Decky

Use Cheat Engine tables on SteamOS, from the controller, without leaving Game Mode.

<img src="docs/assets/hexpaw.png" alt="HexPaw, the CE Decky mascot" align="right" width="300">

CE Decky is a [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin. It downloads and installs Cheat Engine for you, finds a table for the game you are playing, and puts that table's cheats in the Quick Access Menu as ordinary toggles. Cheat Engine runs inside the running game's own Proton environment; you never see its Windows interface, and nothing about the workflow needs Desktop Mode, a terminal, or a file manager.<br clear="right">

![The CE Decky panel with pinned cheats, a table search showing two tables already marked unusable, and the review screen that authorizes one exact table SHA](docs/assets/readme-panels.png)

## Highlights

- **Nothing to install by hand.** One press downloads the official Cheat Engine installer, checks its size and SHA-256 against a pinned manifest, and unpacks it into the plugin's own storage. The installer is never run, and no Cheat Engine code ships in this repository.
- **It finds the table.** Search five community catalogs at once, ranked against the name your library actually shows for the game, with the source, post date and claimed version on every result. Or open a `.CT`, `.zip`, `.7z` or `.rar` you already have.
- **Cheats become toggles.** A cheat that is simply on or off is a switch, with no list and no box to fill in. Pin the ones you use onto the Quick Access panel and flip them mid-game with the controller. `Disable all` switches everything off without stopping Cheat Engine.
- **Only the cheat you asked for comes on.** A table's script usually switches on most of the table by itself, not just the cheat you picked. CE Decky holds the rest off, so what the panel counts is what is running in your game.
- **It checks a table against your copy of the game, and can fix two things itself.** Before you use a table it tells you if Cheat Engine will refuse it, or if it is looking for code your version of the game does not have. One press makes a copy without those problems. See [When a table does not work](#when-a-table-does-not-work).
- **It attaches to the game you are already playing.** Cheat Engine starts in that game's own Proton environment and on its display. Stopping it leaves the game running.
- **Set it up before you play.** Configure cheats with nothing running at all; the choices are saved for that exact table and applied the moment Cheat Engine starts.
- **Scripts are handled for you.** Switching on a cheat that lives inside a script switches on the scripts it needs first, outermost first, instead of failing. Stopping Cheat Engine switches your cheats off first, so the game is left as it was found rather than half patched.
- **It remembers.** Auto-load brings back the same table and the same cheats for that exact game, so the second session is one press instead of another search.
- **It remembers what worked.** A cheat that switched on and read back as active marks that exact table as working for that game. The mark changes when the game updates, so an old table tells you whether it is worth trying again.
- **Every table is authorized on purpose.** A table is executable content, so opening one is never permission to run it: you review it and authorize its exact SHA-256, and a changed file is a different table.
- **You can read what a table would run, on the controller.** **Look inside this table** lists everything Cheat Engine could execute and shows it as text: the table's Lua, each cheat's Auto Assembler script, and any window the table carries. Nothing is run.
- **It updates itself.** When a newer release exists, the panel offers it; the download is checked against the published checksum and Decky installs it. You can switch that off.
- **All of it works on a controller.** Every step above is reachable from Game Mode. You never need Desktop Mode, a terminal or a file manager.

## What you need

- A SteamOS device with **Decky Loader 3.2.8 or newer** installed. An older loader still runs and still writes its log, but it does not appear in the Quick Access menu on current Steam, so nothing you install through it is reachable.
- At least one **Proton** version installed (Valve's or a GE build). CE Decky runs Cheat Engine through the same Proton the game is using.
- About **230 MB** free in your home directory: ~35 MB for the cached Cheat Engine installer, ~94 MB for the installed copy, and ~94 MB more the first time you start Cheat Engine, because it runs from a private copy rather than from the installation. Tables, logs and search caches are small on top of that; the one-off self-test in Advanced also creates its own Proton prefix.
- An internet connection for the first-run Cheat Engine download and for searching tables.

CE Decky is for offline and single-player games. See [What this is not](#what-this-is-not).

## Install

CE Decky is not in the Decky store yet, so install the release ZIP by hand.

1. Download `CE-Decky-v<version>.zip` from [Releases](https://github.com/goooroooX/CE-Decky/releases). Each release also publishes a SHA-256 checksum, so you can verify the file before installing it.
2. In Decky Loader's settings, turn on **Developer mode**, then use its install-from-URL/file action and point it at that ZIP. Decky's own documentation covers this screen.
3. Open the Quick Access Menu (the **…** button) and pick **CE Decky**.

Updating is the same steps with a newer ZIP; Decky replaces the plugin in place and your tables, settings and authorizations are kept.

After the first install CE Decky can update itself. When a newer release exists, the panel shows an orange **Update to vX.Y.Z** button. Pressing it asks you to confirm, warns that Steam's interface restarts, then downloads the release, checks it against the published checksum and hands it to Decky to install. You can stop the download; once the install starts you cannot.

CE Decky checks for updates only after you have searched for a table recently, and at most once every few hours. **Advanced… → Plugin updates** has **Check now** and a switch to turn automatic checking off; with it off, nothing is checked and no button appears. If an install fails, the downloaded release is kept in your home folder and that screen gives you the path, so you can install it from Decky by hand.

## First run: install Cheat Engine

The plugin ships no Cheat Engine code. The first panel row says Cheat Engine is not installed and offers **Install**.

Pressing it downloads the official Cheat Engine 7.7 Windows installer from Cheat Engine's own download host, checks its exact size and SHA-256 against a pinned manifest, and extracts it directly into plugin-owned storage. The installer is never executed and no separate Proton prefix is created for setup. The download is resumable and can be cancelled, and progress is shown on the row.

If that artifact ever stops being downloadable, **Advanced… → Fallback** lets you register a Cheat Engine installation you copied from a Windows machine, either as a folder or as a `.zip` of the installation directory. The archive is validated before a byte is written.

## Using it

The panel is three sections: **Setup**, **Cheats**, and **Auto-load**.

### 1. Pick the game

Start the game first. CE Decky follows the running game, so the game row usually fills itself in. **Choose** picks a Steam game or a non-Steam shortcut by hand when nothing is running, which is how you set a game up in advance. **Change** is greyed out while that game runs, because everything CE Decky keeps is per game: the table, the process, the authorization and any running Cheat Engine. Quit the game and the press comes back.

### 2. Get a table

On the table row:

- **Search** looks the game up on FearLess Cheat Engine, Playground, GitHub, The Cheat Script and VGTimes. Results are ranked against the name your library shows, newest first among equal matches, and say where they came from, when they were posted and which version they claim. Some sources make you wait before a download starts; CE Decky waits it out in a window you can cancel.
- Any of those five sources can be switched off under **Advanced… → Table sources**, and search then says which ones it did not ask.
- **Manage** is everything that is not an online search: your own library, opening a `.CT`, `.zip`, `.7z` or `.rar` you already have, and removing one.

When a cheat refuses to switch on, CE Decky asks whether to stop using that table, and offers to try a repaired copy first where it can make one. Nothing is recorded until you answer. A table you stop using is greyed out in later searches and carries a red mark, so you do not download it again, and it is not offered until you clear the mark. **Retry N** above the results clears those marks, and **Advanced… → Tables that did not work** is the full list.

Downloading or opening a table brings up **Review cheat table**: how many records it has, whether it carries Lua, Auto Assembler code, a window of its own or an embedded file, and which process it expects.

**Opening a table is not permission to run it.** A table is executable content. CE Decky stores it by its exact SHA-256 and asks you to authorize that exact file; change the file and the authorization no longer applies.

Review also says what CE Decky found wrong with the table, where it found anything: that Cheat Engine will refuse it, that it looks for code your copy of the game does not have, or that it switches on most of its own cheats without being asked. A table with nothing wrong says nothing extra. [When a table does not work](#when-a-table-does-not-work) covers what those mean and which of them CE Decky can fix for you.

**Look inside this table**, on that same screen, shows what is in it: the table's Lua, each cheat's Auto Assembler script and any window it carries, as plain text, a page at a time. An embedded file is named and sized, not opened. Nothing runs, and text that was shortened for display says so. The same view is under **Advanced… → Active table → Look inside** for the table you are already using.

#### What the marks on a table mean

Search and Manage show a small round mark beside each table:

| Mark | What it means |
|---|---|
| Green check in a solid ring | A cheat from this table worked for this game, on the version of the game you have now. |
| Green check in a dashed ring | A cheat from this table worked for this game, but CE Decky could not tell which version of the game that was. |
| Amber arrow | It worked before, and the game has been updated since. Try it again. |
| Red cross | You marked this table as not working. It will not be used for any game until you clear the mark. |

Red wins over the others: while it is there, no green mark is shown. Clearing it does not bring an old green mark back, because only a cheat that works again can show that.

Green and amber are about one game, so the same table can be proven for one game and unproven for another. Red is about the file itself and shows in every game's search until you clear it.

**Retry N** in search clears the marks on the rows you are looking at. **Advanced… → Tables that did not work** is the full list and names the reason each one was marked; clearing a mark there makes that table usable again everywhere. If a source now offers a different file than the copy you have, open that row: it names both and has its own **Retry** beside the download it is refusing.

A second, neutral mark of two overlapping sheets means these exact bytes are already on your device from another source. That is why a result you never downloaded yourself can say **Local**.

**Local** and **Imported** say where the file is, not whether it works:

- **Local**: this exact table is on your device and can be used with no network.
- **Imported**: you downloaded from this row before, but what the device holds now is not that file, so using it needs another download.
- **Gone**, **Encrypted**, **Not a table** and **Damaged** describe the download, not whether the table suits your game.

A table marked as not working is still **Local**: the file is here, you just cannot use it until you clear the mark.

#### Your table library

**Manage**, beside Search on the table row, lists every table on this device, not just this game's: which games hold each one, which are free to delete, and the tables another game brought in. It opens with no game selected, so you can tidy the library with nothing running.

- **Local file**, under **Open a file**, imports a table you already have. No game is needed: the file joins the library and waits until you pick a game and press **Use**, which is where it is authorized.
- **Use** picks a table for the selected game, and is hidden when no game is selected. It is disabled for a table you marked as not working until you clear the mark, and the row says so.
- **Revoke** detaches a table from the games holding it. Their authorization and auto-load go; the file stays and can be selected again later. It is also how you free a table held by a game you have removed from Steam.
- **Delete** removes the bytes from this device, for every game that kept them, once nothing holds them.


### 3. Configure the cheats

**Configure cheats** lists the table's controls, one per row, with an **Active** toggle. **More** on a row reveals its value editor, the option to pin it, and its raw record ID.

- You can do this with the game running or with nothing running. Your choices are saved for that exact table and applied the next time Cheat Engine starts.
- Switching on a cheat that lives inside a script switches on the scripts it needs first: a record inside a script has no address until that script has run.
- **Pin** puts a control on the main panel as a live toggle, so the cheats you use are one press away.
- A cheat that is simply on or off is a plain switch: the toggle is the whole control, with no list and no box.
- A cheat that really does choose between things offers that list on the row, and nothing else. If you need a value the author did not list, **Type a value instead** gives you a box. Long lists get a **Find a value** search: type part of the name or the number to narrow it.
- A cheat that takes a number, like a damage multiplier, keeps its box. Those sit under the switch they belong to.
- The scripts a table uses to build its cheats are behind the **Scripts** toggle in the header; CE Decky manages them for you.

### 4. Start Cheat Engine

With the game running, **Load table & start CE** starts Cheat Engine in that game's Proton environment and attaches it to the game. If it cannot start, the row underneath says why: the game is not running, the table is not authorized, the target process is not confirmed, or the game is running a different program than the table expects. In that last case it names the program it found, which you can then set under **Advanced… → Target process**.

Cheat Engine takes the foreground as it starts, so some games drop out for a moment and are brought straight back.

Once connected, the Cheats section shows the live state and your pinned toggles work from the panel. **Disable all** switches every active cheat off without stopping Cheat Engine. **Stop CE** ends the session and leaves the game running.

Stopping switches your cheats off first, and waits for them. This matters: a cheat works by rewriting the game's code, and the table's own script is what puts it back. Killing Cheat Engine with a cheat still on leaves the game rewritten for as long as it runs, and no later session can undo it. If a cheat will not switch off, CE Decky says which one and that restarting the game is what clears it.

### 5. Next time

Turn on **Load last table & cheats** and the next session restores the same table and the same choices for that game, with no search. If you switch off the last saved cheat, auto-load switches off with it.

## When a table does not work

A table is written against one build of a game, and the build you have is often a different one. Some tables are also refused by Cheat Engine before they open at all. CE Decky checks for both before you use a table and says what it found on the review screen, in plain words. Two of those problems it can fix for you.

| What is wrong | How you find out | What CE Decky can do |
|---|---|---|
| The table is signed. Cheat Engine refuses signed tables here and gives no reason for it, so it looks like nothing happened. | Review says so before you use it. | Make a copy without the signature. |
| The table looks for code that is not in your copy of the game. The cheats built on it do nothing when you switch them on. | Review names the code it could not find. | Make a copy without those cheats, and name the ones that go. |
| Both at once. | Review says both. | One press, one copy, both problems gone. |
| The table looks for code that appears in more than one place in your game. Cheat Engine picks one of those places at random, so the cheat may change the wrong thing. | Review says which one. | Nothing, and it says so. That table was written for another build of the game; look for a different one. |
| You already used the table and a cheat came straight back off. | CE Decky asks whether to stop using it. | Offers **Try a repaired copy** first, if it can make one. |

The check reads the game's own program, and the other files the table names where they sit beside it, so a table that finds its code in a game's engine library is checked there too. Code is only ever taken out of a copy when the table says which file it looks in and your game has one file of that name. A table can also tell Cheat Engine to look anywhere in the running game; then not finding the code on disk proves nothing, so review tells you what was not found and the copy leaves those cheats alone. It runs only where CE Decky already knows which program the game runs, which means the game is running now or a cheat from it worked here before. For a game this device has never started, review says nothing about any of this rather than guessing.

**Prepare a copy that works here** is the press that makes the copy. It is on the review screen and on the row in **Manage**.

A few things about that copy, because it is your table that is being changed:

- it is a new table, not an edit of the one you downloaded. The original is untouched and stays in your library;
- it opens its own review screen, which says what was removed and what it cost you, including any cheat that is gone with it. You authorize it there, the same as any other table;
- nothing is done silently, and nothing is guessed. The copy is only made if the rest of the table can be shown to still hold together: every other cheat still there, nothing left pointing at what was taken out, and every other piece of code the table looks for still found in your game. If that cannot be shown, the press refuses and says why, instead of handing you a table nobody can stand behind;
- the copy did not come from any site, so it carries none of the marks a download does. The panel calls it **Fixed** where a downloaded table names its source, and its review screen names the table it was made from.

## Where your data lives

Everything CE Decky creates is under `~/.cheat-engine-decky/`:

| Directory | What it holds |
|---|---|
| `ce/` | the installed Cheat Engine, its cached installer, and the private runtime copy actually launched |
| `tables/` | every table you imported, stored by its SHA-256, with where it came from |
| `state/` | per-game profiles: selected table, authorization, target process, cheat choices, and which table sources you switched off |
| `cache/` | provider search indexes and diagnostics |
| `tmp/` | staging for downloads and imports |

Settings and logs live in Decky's own directories. **Advanced… → Plugin data on disk** lists all of it with sizes and explains what each directory is for.

Removing the plugin through Decky leaves this directory alone, so your tables and authorizations survive an update or a reinstall. Delete `~/.cheat-engine-decky/` yourself if you want it gone.

## Advanced and troubleshooting

**Advanced…** starts with two settings; everything below them is diagnostics.

- **Plugin updates**: the switch for automatic checking, a **Check now**, and the same update press as the panel. A check that failed says so instead of reading as up to date.
- **Panel appearance**: whether the mascot is drawn on the panel. On by default; switching it off gives that space to the cheats, and the choice survives an update and a reinstall.
- **Table sources**: the five sites search asks, each with what it has actually done on this device. All of them are on to begin with; switching one off stops it completely, and switching it back on costs nothing. Tables you already downloaded are unaffected either way.
- **Registered Cheat Engine** and **Target process**: what is registered, and the exact game `.exe` Cheat Engine attaches to, overridable when the automatic choice picks a launcher instead of the game.
- **Proton and prefix**: the Proton build the game is actually running under, its compatibility data directory, the game's Wine prefix, and the Windows executables observed inside it. An attached start depends on all of these.
- **Active table**: where the table came from, when, where it is stored, and whether it is authorized. **Look inside** opens the same read-only view as Review. The source page opens in the Steam browser.
- **Runtime**: the current session, the attached process, and what the in-game bridge last reported.
- **Processes**: pick a different Windows process to attach to when the automatic choice was wrong. With Cheat Engine running it asks Cheat Engine what it sees; without it, it reads the game's own processes, which is the case when Cheat Engine will not start or will not attach.
- **Debug details**: uptime, storage counts, per-provider state, the table-search index, session inventory and the log path.
- **Report a problem**: collects everything a bug report needs into one file. See below.

If a press fails, a window says what failed and keeps the exact backend message on a second row, so you can copy it into a bug report. The backend log path is in **Debug details**.

## Reporting a bug

**Please report bugs this way.** It takes one press and it saves a round of questions.

1. Reproduce the problem, so the logs describe it.
2. Open **Advanced… → Report a problem → Collect**.
3. A dialog gives you the path of the archive it wrote, which is a `ce-decky-support-<version>-<date>.zip` in your home folder (`/home/deck` on a Steam Deck). Note it down and press **OK**.
4. Copy that file off the device: Desktop Mode, or whatever file transfer you already use.
5. Open an issue on this repository, attach the archive, and say what you were doing and what you expected. Add a screenshot if the problem is something you can see. Steam's shortcut is **STEAM + R1** on a Steam Deck, and the shot goes into Steam's screenshot library rather than a folder. To get a file you can attach, turn on **Settings → In Game → Save an uncompressed copy** and set the folder beside it; screenshots then also land there, for example `~/Pictures/Screenshots`.

What is in the archive:

| Inside the archive | What it answers |
|---|---|
| `summary.txt` | versions, the registered Cheat Engine, self-test result and every configured game, at a glance |
| `logs/plugin/` | CE Decky's backend log, across every plugin load |
| `logs/ce-launch/` | what Proton and Cheat Engine actually printed, per game, for a launch that never connected |
| `logs/frontend.json` | what the panel did: which screen, which press, which failure |
| `diagnostics/` | backend state snapshots, the self-test, the live runtime and launch capability, and the environment |
| `state/` | your settings, game profiles, and the session records for each Cheat Engine run |
| `tables/` | the cheat tables your games are currently using |
| `manifest.json` | everything that could **not** be collected, with the reason |

It contains no passwords, no Cheat Engine binary, no installer and no game files. It does contain the names and AppIDs of the games you configured, paths on your device, and your cheat tables. `README.txt` inside the archive says the same. Delete anything you would rather not publish before attaching it; the rest still reads.

Only the five newest archives are kept, so collecting several while you narrow a problem down will not fill your home folder.

## Limitations

- **CE Decky runs the cheats a table already contains, and nothing else.** Cheat Engine's own interface is never shown, so there is no memory scanning, no pointer scan, no editing a table and no writing a new one from the panel. If the table you found does not have the cheat you want, CE Decky cannot make it.
- **A repaired copy is not a rewritten table.** CE Decky can take out what does not work and take off a signature. It cannot write a cheat, move one to a new version of the game, or work out what the author meant. Where a table's cheats are tangled together too tightly to take one out safely, it refuses rather than handing you a copy that quietly lost half the table.
- **A table is bound to its exact bytes.** A newer revision of the same table is a different table: it is imported, reviewed and authorized separately, and your saved cheat choices do not carry across.
- **A very large table cannot be switched on the fly.** Confirming a change while the game runs means reading the whole table back, and past a certain size that cannot finish. The table still works: pick its cheats and values in the panel and they are applied when Cheat Engine starts. The panel says live controls are unavailable for a table that size.
- **The game list leaves out what is not a game.** Proton builds, the Steam Linux Runtimes, Steamworks Common Redistributables and desktop applications you added to Steam yourself are hidden. They are recognised by what Steam records about them, not by name, so a game you added from its own `.desktop` entry, such as a flatpak game, is hidden too. There is no way to bring a hidden entry back yet.
- **A game with no Windows build has nothing to attach to.** A native Linux game runs under the Steam Linux Runtime, which is a compatibility tool but not a Proton one. Force a Proton compatibility tool for that game in Steam and it will run its Windows build, which CE Decky can attach to.
- **A standalone trainer file is not a table.** A `.CETRAINER` runs what it carries without anyone reading it first, so a download that turns out to be one is refused. Look for a plain `.CT` in the same post.
- **An encrypted archive cannot be used.** A `.7z` or `.rar` with encrypted files is refused with the reason. A password published beside the download is handled; one you would have to type is not.
- **Starting Cheat Engine can push a game aside for a moment.** Some games, mostly older ones, minimize themselves when Cheat Engine takes the foreground. CE Decky asks such a game straight back, so what you see is a dip rather than a black screen. A game that stays dark or slow afterwards is a bug worth reporting with a support bundle.
- **Running Cheat Engine alongside a game costs something, and most of it is Cheat Engine itself.** On a Valve Steam Machine (6 cores, 12 threads), with a demanding game holding a steady 39.5 fps and a 142-record table, an attached session costs about 6.6% of one core: Cheat Engine 4.7%, 1.5 points on the game's `wineserver`, and CE Decky's own backend 0.3%. The frame rate does not move. On a Steam Deck LCD (4 cores, 8 threads) with the same game and table and no power limit, about 13.3% of one core: Cheat Engine 9.4%, 3.3 points on `wineserver`, and the backend 0.6%. On a device with nothing to spare that cost comes out of frames, so stop Cheat Engine when you are not using it.

## What this is not

- CE Decky is for offline and single-player use.
- It never writes Steam launch options and never edits your live Steam configuration. It reads your library to identify a game, and that is all.
- It does not disable, bypass, evade or hide from anti-cheat systems, and it will not help you do that.
- No Cheat Engine binary or source is contained in this repository or in any release ZIP. Cheat Engine is downloaded from its official host, on your explicit action, and verified against a pinned checksum.
- No community table is redistributed here. Tables are downloaded from their own sources or opened from your device.
- CE Decky is an independent project. It is not affiliated with or endorsed by Cheat Engine, Valve or any table author.

## Development

Validation is one Python command. It detects what changed, runs only the checks that cover it, keeps full logs under ignored `build/qa/`, and prints an exact rerun command when something fails:

```bash
python scripts/qa.py --bootstrap
python scripts/qa.py
```

`--bootstrap` restores only what the selected profile needs and is unnecessary while the lock files are unchanged. Documentation and backend work need only Python; frontend changes also need Node, because the plugin bundle is built with Decky's pinned official Rollup toolchain.

Run the full gate once after focused iteration. It builds the plugin package too, and a stage whose inputs have not changed since it passed is reused rather than run again:

```bash
python scripts/qa.py --profile release
```

The full backend suite exercises POSIX executables, symlinks and Wine paths, so GitHub Actions on Linux is the authoritative gate, and a green local run on another OS does not clear it.

| Path | Purpose |
|---|---|
| `src/` | Decky frontend, TypeScript and React |
| `py_modules/ce_decky/` | Python backend |
| `tests/` | production tests |
| `dist/` | checked-in frontend bundle used by the plugin package |
| `docs/` | design, architecture, security and development contracts |
| `scripts/` | build, test, packaging and verification helpers |

[AGENTS.md](AGENTS.md) is the working contract for contributors and coding agents; [docs/README.md](docs/README.md) maps the longer documents. Releases are built by GitHub Actions from a reviewed `v<version>` tag: the workflow checks the tag against `package.json` and `CHANGELOG.md`, runs the validation suite, builds the deterministic plugin ZIP, writes its checksum, and attests the archive. Source authority is the reviewed repository tag.

## License

CE Decky is licensed under **GPL-3.0-or-later**. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](defaults/THIRD_PARTY_NOTICES.md) for the third-party components included in the plugin package.

## Credits

- **Dmitry Nikolaenya**: concept, architecture, planning, product design, validation, and testing.
- **Claude Opus 5** and **GPT-5.6 Sol**: AI-assisted implementation, code review, bug hunting, test development, and integration.
