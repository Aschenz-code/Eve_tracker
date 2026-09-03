# eve-watch

Watches your EVE Online clients and tells you out loud when something changes: a
ship appears in an overview, a new signature shows up, a d-scan comes back
different, a counter on a structure ticks over.

It reads the screen only. It never sends input to the game.

## Install

    pip install -r requirements.txt

Windows only. Python 3.12+.

## First run

1. Start the clients you want watched. They must be running and **not minimised** —
   a minimised window stops rendering and there is nothing to read. They may sit
   behind another game or on a second monitor.
2. Double-click **eve-watch.bat**. That is the hub — every window below opens
   from it, so you never need the other launchers directly.
3. Press **Clients / calibrate**.
4. Press **Calibrate** next to each client. It finds that client's Overview,
   Probe Scanner and Directional Scanner panels, shows you what it found, and
   writes the regions when you press Apply.
5. Tick the clients (and any individual regions) you want, then **Save**.

That's it — Save starts the watchers. A client cannot be ticked until it has been
calibrated, and once calibrated it stays that way.

Before calibrating, make sure **each list has at least two rows** in it. A list
with fewer cannot reveal its row spacing; calibrate will say `ROW SPACING
GUESSED` if it has to fall back. An empty d-scan is skipped entirely, because EVE
stops drawing column headers when there are no results — run a scan first.

## Everyday use

| do this | with |
|---|---|
| open everything from one window | **eve-watch.bat** (the hub) |
| add / remove clients or regions | **pick.bat** |
| watch events as they happen | **events.bat** (Events tab) — updates itself, or press **Refresh** / F5 |
| see who has been seen, in what ship, for which corp | **events.bat** (Pilots tab) |
| check every part of the setup | **Check everything** in the hub, or **doctor.bat** |
| stop alerting, keep watching | **pause.bat** / **resume.bat** (all clients) |
| silence ONE client while you probe on it | **Alerts on / Alerts OFF** beside it in **pick.bat** |
| change how loudly it alerts | **mode-active.bat** / **mode-away.bat** / **mode-silent.bat** |
| check it is alive | **Status** in the hub (live) or **status.bat** (one-shot text) |
| start watching after a reboot | **watch.bat** (single client) or `eve_watch.py supervise` |

Alert profiles:

| mode | beep | voice | popup | repeats |
|---|---|---|---|---|
| `active` | yes | yes | no | 2 |
| `away` | yes | yes | yes | 3, until dismissed |
| `silent` | no | no | no | logs only |

Alerts are gathered for a moment before anything is said, so several changes
at once become one announcement listing them - *"Mr AschenZ. 3 changes. new
contact in overview. Redron Gnosis, OwlShadow Astero, Vega Junk Prospect"* -
rather than three voices talking over each other. Watchers also take turns, so
two clients cannot speak at the same time.

Alerts lead with the client name — *"Scout. new contact in the overview. \<pilot\>
\<ship\>"* — so you know which window to open.

## Taking a wormhole

An overview holding a wormhole gets one more inference. Jumping looks like any
other disappearance, except for *where* it happened: you must be within 5 km of
a hole to activate it. So a pilot who vanishes at the same distance the hole
itself reads is announced as having taken it - which works whatever the watching
client sits at, since a ship on the hole reads the hole's own distance.

Nothing about this depends on graphics settings, unlike the flash the game draws
when a hole fires.

Two things it cannot tell apart from a jump: cloaking up while sitting on the
hole, and warping off in the moment between two reads. The second is guarded -
a ship already opening the range is reported as leaving, not jumping - but the
first is indistinguishable from the outside.

## Polling faster on one client

`interval` in `config.json` is how often a client is sampled, 1 second by
default. `client_interval` overrides it per client:

    "client_interval": { "EVE - Your Character": 0.5 }

Worth doing on a client watching a structure, where catching someone in the
seconds before they can cloak decides whether a dock is attributed to a name.

Measured on one machine, one watcher, four regions:

| interval | that watcher | worst-case detection |
|---|---|---|
| 1.0 s | 24% of a core | ~2 s |
| 0.5 s | 24% | ~1 s |
| 0.25 s | 35% | ~0.5 s |

Halving from 1 s is nearly free because the standing cost is the periodic OCR,
which `roster_period` gates and which does not follow the poll rate. Below that
the per-pass work starts to show.

## Docking

If a client watches both an overview and the structure counter, the two are
read together. The count going up as someone leaves the overview is that person
docking; going down as someone appears is them undocking. It is logged as
`docked` / `undocked` against the pilot and shown in the Pilots tab.

It is an inference, not a fact: the count also moves for people who were never
on your overview, and only the client holding the structure region can pair the
two. On the history it was measured against, all 16 count changes matched the
expected direction within five seconds.

## The structure counter

Calibrate cannot configure this one: it is a bracket floating in space with no
panel title to find. Set it up by hand on whichever client sits on grid:

    eve_watch.py select --name structure --client "Your Character"

Three drags: a rough box, then the number itself, then a nearby piece of steady
text as an anchor.

To have it report the actual number rather than just "it changed", teach it each
value once while that value is on screen — **learn.bat**, or:

    eve_watch.py learn --name structure --value 4

Until a value is taught it reports `?`. Detection still works; only the label is
unknown. A running watcher picks up new values within seconds.

## Output

- `events.log` — running log, with a heartbeat every 60 s.
- `events.csv` — one row per event: time, client, region, event, detail, snapshot.
- `snapshots/events/` — a PNG saved at the moment of each alert.
- `snapshots/baseline/` — what each region saw at startup, for checking aim.
- `signatures.json` — every signature id seen, with its type and site name once
  known, and whether it has been scanned. Browse it in the **Signatures** tab of
  events.bat, which lists the unscanned ones first and in red. It is kept on
  disk on purpose: after downtime or a relog the question is which of these you
  had already scanned, and EVE does not remember either.

  It is kept per client, because two clients are often in different systems -
  a scout takes the next hole while the other sits at home - and one shared
  list had each of them concluding the other's signatures had all vanished.
  Signature ids are unique to a system, so a list sharing not one id with what
  was there is a different system: the old set is retired at once and the new
  one adopted without alerting, since those are not new spawns.

  The probe scanner alerts on a new **id**, never on a row changing. Scanning
  rewrites the Name and Group columns of a row that was already there, which
  looks like an arrival to anything comparing images - so a row that resolves
  is recorded silently and only an id the file has never held is announced.

  Scanned is not a flag the game shows - it is the absence of one. An unresolved
  signature has an empty Group column and a Name of "Cosmic Signature"; once
  probed the Group names the type, and at full strength the Name becomes the
  site's own. A pasted scan (Ctrl+C in the probe scanner) is exact and is
  preferred over OCR when it arrives.
- `pilots.json` — every name seen in an overview, with the ships they have
  flown, their corp tickers, when they were first and last seen, whether they
  were last coming or going, which client saw them last, and what happened the
  last time they left - docked, undocked, took a wormhole, or simply warped
  off, with the hull they were in. The Pilots tab leads with **in space now** - the
  hull anyone currently on an overview is sitting in, and which client can see
  them - shown in red and blank for everyone else. Browse it in the **Pilots** tab of events.bat. It needs column
  positions, which calibrate reads off the overview header, so a client
  calibrated before this existed records nothing until re-calibrated once -
  doctor.bat says which.

To get the timecode **inside your OBS recording** on every event, set `obs_dir` in
`config.json` to your OBS output folder.

## If something looks wrong

Start with **Check everything** in the hub (or **doctor.bat**). It tests the whole
setup — regions, watchers, mode, OCR, whether the log is still moving — and says
what to do about anything that fails. Then, to see what one client is reading:

    eve_watch.py shot --client "Name"      # does every region still lock?

Anything reporting `ANCHOR LOST` is not being watched. The watcher also says so
out loud after 45 seconds, and logs `...alive but BLIND` if the client stops
producing frames at all.

Common causes:

- **You changed EVE's UI scale.** Anchors and taught values are fixed-size
  bitmaps; they all break. Re-calibrate, and re-teach any counter values.
- **You resized the EVE window.** Same fix.
- **You hovered or selected a row.** Handled: it changes the row enough to
  score 0.77-0.94 against its stored image, where two genuinely different rows
  reach only about 0.64, so the match bar sits at 0.80 for signatures and 0.85
  for overviews. Signature rows also carry a unique id, which settles anything
  in between. If false arrivals ever return, `pix_ncc` on that region is the
  number to lower - the log prints the score it needed.
- **You moved a panel a long way.** Regions refuse to follow further than
  `max_drift` (200 px), because two overview panels look identical to the matcher
  and it must not lock onto the wrong one. Re-calibrate.
- **You switched overview tab and the columns differ.** Re-calibrate.
- **The client restarted.** Handled automatically — it reconnects within 30 s.

Probing on a scout changes that client's own signature list, so its alerts are
self-inflicted while you work. Press **Alerts OFF** beside it in pick.bat rather
than unticking it: the watcher stays up, every other client keeps alerting, and
it re-baselines when you switch alerts back on, so nothing from the quiet window
fires. From the command line that is `pause --client "Name"`.

## Running commands by hand

Double-click **console.bat**. It opens a prompt already in the tool's folder with
the bundled interpreter on PATH, so this works:

    python eve_watch.py windows            # which clients are running
    python eve_watch.py status             # running? paused? recent log
    python eve_watch.py list               # dump the config
    python eve_watch.py shot --client "Name"       # do the regions still lock?
    python eve_watch.py calibrate --client "Name"  # rebuild that client's regions
    python eve_watch.py tune --apply       # measure noise, set sensitivity

From an ordinary PowerShell window, call the bundled interpreter by full path —
plain `python` may be a different install that lacks the dependencies:

    C:\EVE_Dev\tools\eve-watch\.venv\Scripts\python.exe C:\EVE_Dev\tools\eve-watch\eve_watch.py windows

That works from any directory. If you would rather `cd` first, note that Windows
PowerShell 5.1 needs `;` and not `&&`:

    cd C:\EVE_Dev\tools\eve-watch; .venv\Scripts\python.exe eve_watch.py windows

Add `--help` to any command to see its options.
