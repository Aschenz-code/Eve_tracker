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
2. Double-click **pick.bat**.
3. Press **Calibrate** next to each client. It finds that client's Overview,
   Probe Scanner and Directional Scanner panels, shows you what it found, and
   writes the regions when you press Apply.
4. Tick the clients (and any individual regions) you want, then **Save**.

That's it — Save starts the watchers. A client cannot be ticked until it has been
calibrated, and once calibrated it stays that way.

Before calibrating, make sure **each list has at least two rows** in it. A list
with fewer cannot reveal its row spacing; calibrate will say `ROW SPACING
GUESSED` if it has to fall back. An empty d-scan is skipped entirely, because EVE
stops drawing column headers when there are no results — run a scan first.

## Everyday use

| do this | with |
|---|---|
| add / remove clients or regions | **pick.bat** |
| stop alerting, keep watching | **pause.bat** / **resume.bat** (all clients) |
| change how loudly it alerts | **mode-active.bat** / **mode-away.bat** / **mode-silent.bat** |
| check it is alive | **status.bat** |
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

To get the timecode **inside your OBS recording** on every event, set `obs_dir` in
`config.json` to your OBS output folder.

## If something looks wrong

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

## Sharing it

Share the code, never the configuration. `config.json`, `anchor_*.png` and
`values/` are excluded from git deliberately: they are pixel geometry tied to one
person's UI scale, resolution and window layout, and will not work anywhere else.
`events.log` and `events.csv` are excluded too — they hold signature IDs and pilot
names.

A new user clones, installs, and runs **pick.bat** → Calibrate. Sharing an
overview export (EVE writes these to `Documents/EVE/Overview`) makes everyone's
columns match, which helps but is not required.

## Other commands

    eve_watch.py windows                   # which clients are running
    eve_watch.py list                      # dump the config
    eve_watch.py shot --client "Name"      # check regions
    eve_watch.py tune --apply              # measure noise, set sensitivity
