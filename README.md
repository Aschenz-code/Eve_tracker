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

Alerts lead with the client name — *"Scout. new contact in the overview. \<pilot\>
\<ship\>"* — so you know which window to open.

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
- `pilots.json` — every name seen in an overview, with the ships they have
  flown, their corp tickers, when they were first and last seen, and on which
  client. Browse it in the **Pilots** tab of events.bat. It needs column
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
