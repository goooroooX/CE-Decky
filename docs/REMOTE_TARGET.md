# Working on a SteamOS device over SSH

`docs/DEVELOPMENT.md` **SteamOS development** describes the device the checkout is on. This document is for the other case: the checkout and the agent are on one machine and the SteamOS target is another one across the network. The development machine is ordinarily a plain Linux desktop, and nothing here needs it to be SteamOS; a Steam Deck driven from a Steam Machine is the same arrangement with both ends the same distribution. Everything in **SteamOS development** still applies to the remote device; what follows is only what changes when it is not the local one.

The rule that does not change: the tracked helpers run on the device they are describing. Running them on the host answers for the host, and the answer looks perfectly valid.

## Establishing the connection

The maintainer does one thing, because it is the only step that needs a secret this side does not have. Print the development machine's public key, then add it to the target's `~/.ssh/authorized_keys` from a terminal on the target itself:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '%s\n' '<public key>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```

SteamOS runs `sshd` already; a closed port 22 means the service is off rather than the key being wrong. `sudo` on the target still wants a password, so anything needing root stays with the maintainer. Nothing in this document does, except installing Decky Loader itself.

Confirm the connection answers before trusting anything it says, and record which machine is which, along with the login and home this run will use. Neither is `deck` and `/home/deck` by assumption: the Steam user is whatever the device was set up with, and `DECKY_USER_HOME` is the authority for the home rather than the login's own:

```bash
ssh <user>@<target-ip> 'hostnamectl | head -8; nproc; grep -m1 "model name" /proc/cpuinfo; echo "HOME=$HOME"'
```

A Steam Deck LCD reports `AMD Custom APU 0405` and 8 threads; an OLED reports `AMD Custom APU 0932`. A measurement that does not name which one it came from is not a measurement.

Where the plugin is already installed, take the home and the settings directory from the device's own backend rather than from the login, and use those for every helper that asks for them:

```bash
ssh <user>@<target-ip> 'cd <mirror>; python3 scripts/target_plugin_install.py authority'
```

Its `user_home` and `settings_dir` are what `target_state_probe.py` and `target_ce_launch_probe.py` want. On a device with no install yet there is no authority to ask, so the remote `$HOME` above is what the first install is built on, and it is stated in the run rather than assumed.

## Putting the checkout on the target

The helpers import `py_modules/ce_decky`, so a lone script copied across cannot run. Mirror the working tree instead, into a directory kept for this and nothing else, without the build output, the dependencies or the Git directory:

```bash
ssh <user>@<target-ip> 'mkdir -p <mirror>; rm -rf <mirror>/.git'
rsync -a --delete \
  --exclude '.git/' --exclude 'node_modules/' --exclude 'build/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' \
  ./ <user>@<target-ip>:<mirror>/
ssh <user>@<target-ip> 'test ! -e <mirror>/.git && echo "mirror is not a checkout"'
```

That copy is a copy, not a checkout. Run helpers from it and read its results; never commit, tag or push from it.

Excluding `.git/` is not what makes that true, and assuming it does is the trap. An excluded path is protected from `--delete` as well as from transfer, so a `.git` already sitting in that directory, from an older clone or an earlier way of working, survives every sync and leaves a real checkout wearing a mirror's name. That is why the directory is cleared of it before the sync and checked after: the check is the guarantee, and the exclusion only keeps this repository's own history from being copied over the network.

Every remote command needs its own `cd`, because SSH does not carry a working directory:

```bash
ssh <user>@<target-ip> 'cd <mirror>; python3 scripts/<helper>.py <args>'
```

Re-run the `rsync`, with the check after it, following any change to `scripts/`, `py_modules/` or the packaged artifact. A stale copy on the target is the failure mode this arrangement invites, and it presents as a helper that disagrees with the source in front of you.

## Decky Loader on the target

Check the loader's version before anything else, because a Steam client update can leave an older loader running as a service while its frontend never appears in the UI:

```bash
ssh <user>@<target-ip> 'cat "$HOME"/homebrew/services/.loader.version; systemctl status plugin_loader --no-pager | head -6'
```

`3.2.8` carries `SteamDeckHomebrew/decky-loader` PR 947, which follows Steam renaming its initialization API. A loader without it runs, logs normally, and is simply absent from the quick access menu. The service being `active` proves nothing about the UI. When the versions differ across two devices, the one whose Decky is visible is the evidence for which version the current Steam expects.

The update is the one step that needs the maintainer, since the installer asks for a password:

```bash
curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/install_release.sh | sh
```

Restart Steam afterwards and confirm the Decky icon is in the quick access menu before continuing.

## Installing the plugin on the target

Build and package on the development machine, once, with the route that prints the artifact and its digest:

```bash
python scripts/qa.py --profile release
```

Copy the tree across as above, then install on the target with the exact digest that run printed. A first install has no recorded authority and no live CE Decky for the helper to resolve, so name the root explicitly; later installs resolve it themselves and take `--replace`:

```bash
ssh <user>@<target-ip> 'cd <mirror>; python3 scripts/target_plugin_install.py install \
  artifacts/CE-Decky-v<version>.zip --sha256 <digest> \
  --plugin-root "$HOME"/homebrew/plugins/CE-Decky'
```

Once there is a recorded authority, the helper does that round trip itself and `--remote` is the whole of it:

```bash
python3 scripts/target_plugin_install.py install \
  artifacts/CE-Decky-v<version>.zip --sha256 <digest> \
  --remote <user>@<target-ip> --replace
```

It sends this exact artifact rather than trusting whatever the mirror holds, and it refuses outright when the mirror's copy of that helper is not the one being run here, which is the staleness this arrangement invites and which otherwise presents as a helper disagreeing with the source in front of you. `--remote-root` names the mirror when it is somewhere other than `ce-decky-mirror` under the remote home. Every other option reaches the helper at the far end, quoted for the shell that carries it there: `--plugin-root` for a first install on a fresh device, whatever it spells, `--decky-url` for a loader that is not on the default port, and `--json`, which answers with that helper's whole report and keeps anything it wrote about the install on stderr, where it cannot turn the report into something no parser accepts. Run the `rsync` above first all the same: the artifact is what `--remote` carries, and every other helper on the target is still whatever was last synced.

The mandatory reload replaces Steam's webhelper, which closes the Decky UI and can displace a running game. Announce it before running it, and never capture a screenshot straight afterwards: the frame can only show whatever the session fell back to.

## Measuring what a session costs

`scripts/target_session_cost_probe.py` is the stopwatch. It samples every process twice across one window, groups the CPU time by the part of a session it belongs to, and over the same window reads the frame rate from the live Gamescope statistics pipe.

One report is a reading. A measurement is two reports with one thing different between them, so run it unattached and attached, with everything else held still:

```bash
ssh <user>@<target-ip> 'cd <mirror>; python3 scripts/target_session_cost_probe.py \
  --window 30 --label unattached --appid <appid>'
```

The frame rate comes from the pipe the running compositor was given on its own command line, which the probe finds itself. Two details decide whether it finds anything:

- the compositor's process name on SteamOS is `gamescope-wl`, not `gamescope`, and the launcher script beside it is `start-gamescope`;
- a previous boot leaves its `/run/user/1000/gamescope.*` directory behind, so the path is read from the live process rather than from a glob.

Gamescope writes to that pipe only while it is compositing. An idle session with a static screen produces nothing at all, which the probe reports as zero samples rather than as a frame rate. Frames are therefore only measurable while a game is actually running, and CPU is measurable either way.

The number it reports is composited frames, and those are capped at the panel's refresh rate. A game rendering faster than the panel reads as exactly 60.00 with zero variance no matter what it is really doing, so nothing can be shown to cost it anything, and raising graphics settings will not change that while the engine still outruns the panel. Remove the headroom instead: hold the device at a power limit low enough that the game is the constraint, through the frame limiter and TDP controls in Steam's own performance panel, and confirm the pipe is reporting a real spread below the refresh rate before measuring anything. On a handheld that is not a workaround; it is the condition the question is about.

Take at least three windows, unattached either side of attached. What decides whether an effect is real is the difference between the two unattached windows: if the thing being measured moves the frame rate by less than the baseline moves on its own, there is no result yet, whatever the middle window says. Hold one scene throughout, and keep the quick access panel closed, since a session's own backend and an open panel are separate costs.

What the numbers mean is in the probe's own module docstring, and the results belong in `docs/FIELD_NOTES.md` section 1 with the machine that produced them.

## Driving this plugin's own screens

A question about a row, a control's state or what a footer says is answered from the device without anybody touching it, and without a picture. `target_panel_read.py` runs on the device like every other target helper, so over SSH it runs from the mirror:

```bash
ssh <user>@<target-ip> 'cd <mirror>; python3 scripts/target_panel_read.py --open --press Manage --metrics'
```

`--open` opens the quick access menu on this plugin's page through Steam's own `MenuStore.OpenQuickAccessMenu` and Decky's own `setActivePlugin`, which is the route `@decky/ui` gives every plugin. `--press` then opens one of this plugin's screens by the name on its control, repeated to go deeper, and activates the handler the plugin wrote for it. It reads back each row, its controls, their state and the text actually rendered, which is a fraction of what a frame costs to read and answers more.

Two bounds are what make it safe to have. The name has to be one of the presses that opens a screen, and anything that authorizes, downloads, writes or destroys is refused by name, so **Use this table**, **Delete these** and **Apply** are not reachable through it. And a disabled control is reported as disabled rather than activated, because a press Steam would have refused is not evidence about anything.

Nothing here fabricates a controller event and nothing reaches Steam's own interface: these are this plugin's own buttons, activated through the handlers it wrote, which is what makes reaching one of its screens a thing to do rather than a thing to ask for.

Two other helpers drive rather than read, and they differ in where they run:

- `target_screenshot.py --remote <user>@<target-ip>` runs on the development machine and captures the device across the network. Only the capture belongs to the device; the cropping and scaling are image work and stay here. Use it when the question is about the whole screen, Steam's own interface or a game, which is what the panel reader cannot see.
- `target_plugin_rpc.py` makes the live backend do something, rather than reading what it holds, and runs on the device from the mirror like the readers.

## What the maintainer still has to do

Reaching a running game and a live attached session needs a person at the device, and no amount of tooling changes that. What the section above reaches is this plugin's own screens, through its own handlers. A game, Steam's own interface and the controls that authorize or destroy are outside it, and they are what to ask for. Ask, then measure:

- start the game and bring it to a steady frame rate;
- install Cheat Engine through the plugin, and attach the session from the quick access menu;
- say when the screen being measured is the one on the display.

Choosing the game is theirs too, and it is not only about frame rate. Attaching Cheat Engine to an always-online game is what anti-cheat looks for whether or not a single cheat is enabled, so a measurement that needs an attach belongs on a single-player title. Say so plainly, name the alternatives, and let the maintainer decide.
