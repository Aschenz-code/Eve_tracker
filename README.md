# eve-watch

Watches one or more EVE Online clients and tells you — out loud — when something
changes: a ship appears in your overview, a new signature shows up, a d-scan
comes back different, the pilot count on a structure ticks over.

It reads the screen only. It never sends input to the game.

## Why it works the way it does

**It captures the EVE *window*, not the screen**, via Windows Graphics Capture.
The client can be covered by another game or parked on a second monitor. It must
be running and not minimised — a minimised window stops rendering, and the
watcher says so out loud rather than going quiet.

**Every region is anchored to nearby text.** Each frame it finds that anchor by
template matching and reads your box at a fixed offset from it, so moving the
camera or dragging a panel doesn't break anything. If the anchor is lost for 45
seconds it shouts, because silently not-watching is worse than a false alarm.

**Identity comes from pixels, not OCR, wherever it matters.** OCR of an unchanged
signature row flickers between readings — measured live, it produced six phantom
"new signature" alerts a minute. The same row compared as a bitmap scores 0.9994
against itself and at most 0.71 against a different row. OCR is used only for the
label a human reads.

## Setup

    pip install -r requirements.txt

Or use the bundled `.venv` — every `.bat` here calls it directly, so it does not
matter which Python is on your PATH.

## Quick start

    eve_watch.py windows                       # which clients are running
    eve_watch.py select --client "Name"        # drag a box; repeat per region
    eve_watch.py shot                          # check every anchor still locks
    eve_watch.py watch                         # go   (or double-click watch.bat)

## Region modes

| mode | alerts when | used for |
|---|---|---|
| `change` | the box's contents change and stay changed | a count on a structure |
| `presence` | an empty box gains content | a filtered overview tab |
| `roster` | a new row appears in a list, and names it | overview, signatures |
| `dscan` | the result set changes; logs it, silent by default | directional scanner |

`roster` and `dscan` read row text with the OCR engine built into Windows — no
install, no model download, ~50 ms — and only when the pixels actually moved.

## Alert profiles

| mode | beep | voice | popup | repeats |
|---|---|---|---|---|
| `active` | yes | yes | no | 2 |
| `away` | yes | yes | yes | 3, nags until dismissed |
| `silent` | no | no | no | logs only |

    eve_watch.py mode active        # or double-click mode-active.bat

Switchable while running; the change lands within a couple of seconds.

## Several clients at once

    eve_watch.py clients list                  # configured vs running
    eve_watch.py clients add "Scout"           # start monitoring one
    eve_watch.py clone --from "Main" --to "Scout" --only dscan,overview,sigs
    eve_watch.py supervise                     # one watcher per selected client

Regions belong to a client, so a scout can watch overview + signatures + d-scan
with no structure tracker at all. Alerts lead with the client name — *"Scout. new
contact in the overview. \<pilot\> \<ship\>"* — so you know which window to open.

`clients add` takes effect within seconds; no restart when you log a character in.

## More than one overview window

Regions are independent, so watch as many as you like — give each its own name:

    eve_watch.py select --mode roster --name overview  --client "Name"
    eve_watch.py select --mode roster --name overview2 --client "Name"

One catch worth knowing. Every overview panel's title begins `Overview (`, so
their anchors are **identical templates**. If a region ever fails its local
search it falls back to scanning the whole window, and with two panels open it
can lock onto the wrong one and report the wrong list — silently, because both
matches score about 1.000.

`max_drift` prevents that: it is the furthest, in pixels, an anchor may be found
from where it was set up.

    "max_drift": 200

Set it on every docked panel; 200 is a good default, and it should be comfortably
smaller than the distance between two panels that look alike. Leave it off for a
region anchored to a bracket floating in space, which legitimately moves anywhere
on screen.

If a panel really does move further than `max_drift`, the region reports itself
lost and says so out loud — the safe failure, rather than quietly watching the
wrong window.

## Reading a value, not just "it changed"

`change` mode detects that pixels moved, not what they say. To have it report an
actual number, teach it each value once while that value is on screen:

    eve_watch.py learn --name structure --value 4      # or learn.bat

It compares against everything it already knows and prints the margin, so you can
see a new digit is genuinely distinguishable. Until a value is taught it reports
`?` rather than guessing — detection still works, only the label is unknown.

## Output

- `events.log` — human-readable running log, with a heartbeat every 60 s.
- `events.csv` — one row per event: time, client, region, event, detail, snapshot,
  and the timecode **inside your OBS recording** if you pass `--obs-dir`.
- `snapshots/events/` — a PNG saved at the instant of each alert.
- `snapshots/baseline/` — what each region saw at startup, for checking aim.

## Other commands

    eve_watch.py pause | resume | status       # or the matching .bat files
    eve_watch.py tune --apply                  # measure noise, set sensitivity
    eve_watch.py list                          # dump the config

## Sharing this with other people

Share the **code**, never the coordinates. `config.json`, `anchor_*.png` and
`values/` are all excluded from git on purpose: they are pixel geometry captured
at one person's UI scale, resolution and window layout, and they will not work on
anyone else's machine. `events.log` and `events.csv` are excluded too — they hold
signature IDs and pilot names.

So the install for a corp mate is: clone, `pip install -r requirements.txt`, then
build their own regions with `select` (and `learn` for any counted value).

Two things make that much less painful:

- **Share an overview export** (EVE saves these to `Documents/EVE/Overview`, and
  there is a Share button that drops one into chat). Identical tabs and columns
  mean identical column boundaries, which is what the row parsing depends on.
- **Anchor on panel titles, not column headers.** A header-strip anchor breaks the
  moment someone's column widths differ. Anchor on the word `Overview (`, or
  `Directional Scanner` — but *not* including the overview preset name, which
  changes when you switch tabs.

## Known limits

- The client must be running and not minimised.
- Changing EVE's **UI scale** invalidates every anchor and every taught value —
  they are fixed-size bitmaps. The watcher will tell you, loudly, but you have to
  re-`select`.
- Resizing the EVE window invalidates saved coordinates.
- OCR labels are imperfect (`ABC-12O` for `ABC-120`). This affects only the text
  you read, not detection, because identity is pixel-based.
- A region anchored to a bracket floating in space disappears when that object
  leaves the view. Docked panels are far more reliable targets.
