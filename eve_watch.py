#!/usr/bin/env python3
"""
eve-watch - watch numbers and lists in an EVE Online client, alert on change.

Design choices, each earning its keep:

1. It captures the EVE *window* (Windows Graphics Capture), not the screen. The
   client can be covered by another game, shoved onto a second monitor, or parked
   off to the side. It must be running and NOT minimised - a minimised window
   stops producing frames, and the watcher says so out loud when that happens.

2. It tracks an *anchor*: a nearby piece of text that never changes. Every frame
   it finds the anchor by template matching and reads your region at a fixed
   offset from it. Nudge the camera and the label slides - the watcher follows
   instead of screaming. Optional; skip it for docked panels that never move.

3. It binarises the bright UI text in your box, then applies one of three modes:

     mode=change    alerts when that pattern changes and STAYS changed
                    (a count going 1 -> 2, or anything -> anything)
     mode=presence  alerts when the box goes from empty to occupied
                    (a player appearing in a filtered overview tab)
     mode=roster    alerts AND names who arrived, by reading the rows

   change and presence need no OCR at all. roster uses the OCR engine already
   built into Windows - no install, no model download, ~50 ms a pass - and only
   runs it when the pixels actually moved, so it costs nothing while idle.

4. Every event lands in events.csv with a wall-clock time, an elapsed offset, and
   - if you point it at your OBS folder - the timecode INSIDE the current
   recording, so you can scrub straight to the moment.

    python eve_watch.py windows                      list EVE clients
    python eve_watch.py select --client "Your Character"    pick a region
    python eve_watch.py shot                         check what it sees
    python eve_watch.py tune --apply                 measure noise, set sensitivity
    python eve_watch.py watch                        go
"""

import argparse
import csv
import ctypes
import ctypes.wintypes as wintypes
import datetime as dt
import difflib
import hashlib
import json
import os
import queue
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from PIL import Image
from windows_capture import WindowsCapture

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SHOTS = os.path.join(HERE, "snapshots")
EVENT_SHOTS = os.path.join(SHOTS, "events")     # real detections, timestamped
BASE_SHOTS = os.path.join(SHOTS, "baseline")    # diagnostics: what it sees at start
LOGFILE = os.path.join(HERE, "events.log")
CSVFILE = os.path.join(HERE, "events.csv")
PAUSEFILE = os.path.join(HERE, "PAUSED")   # exists = the running watcher idles
CLIPFILE  = os.path.join(HERE, "CLIPBOARD_OWNER")  # which watcher reads Ctrl+C
PILOTS    = os.path.join(HERE, "pilots.json")      # who has been seen, in what
MARKS     = os.path.join(HERE, "pilot_marks.log")  # corrections from the viewer
SIGFILE   = os.path.join(HERE, "signatures.json")  # which sigs are scanned


def pause_path(client=None):
    """The file whose presence pauses alerting - globally, or for one client.

    Probing on a scout changes that client's own signature list, so its alerts
    are self-inflicted noise while you work. Unticking it does silence them,
    but that tears the watcher down and stands a new one up; a pause keeps the
    process, re-baselines on resume, and leaves every other client alerting.
    """
    if not client:
        return PAUSEFILE
    return os.path.join(HERE, f"PAUSED.{slug(short_client(client))}")


def paused_for(client):
    return os.path.exists(PAUSEFILE) or os.path.exists(pause_path(client))


def paused_clients():
    """Labels of clients paused on their own, ignoring any global pause."""
    return sorted(f[len("PAUSED."):] for f in os.listdir(HERE)
                  if f.startswith("PAUSED.") and len(f) > len("PAUSED."))
VALUES = os.path.join(HERE, "values")      # learned glyph masks, per region+value
MODEFILE = os.path.join(HERE, "MODE")      # alert profile, switchable while running
CLIENTSFILE = os.path.join(HERE, "CLIENTS")   # which clients the supervisor runs
TAG = ""                                   # client label prefixed to this process's logs

PROFILES = {
    # how loudly to alert, by what you are doing at the time
    # One announcement each. Set repeat higher, or nag_until_ack, if you want
    # an alert to keep going until you deal with it.
    "active": {"popup": False, "voice": True, "beeps": True, "repeat": 1},
    "away":   {"popup": True, "voice": True, "beeps": True, "repeat": 1},
    "silent": {"popup": False, "voice": False, "beeps": False, "repeat": 1},
}

DEFAULTS = {
    "threshold": 110,        # pixel brightness 0-255 that counts as "text"
    "sensitivity": 8,        # changed pixels before we care (mode=change)
    "presence_pixels": 20,   # lit pixels above empty before "occupied" (presence)
    "interval": 1.0,         # seconds between samples
    "client_interval": {},   # per-client override, e.g. {"EVE - Scout": 0.5}
    "stable": 3,             # consecutive samples a new state must persist
    "pad": 300,              # context pixels saved around the region on alert
    "search_radius": 200,    # how far the anchor may drift between frames
    "match_min": 0.72,       # template score below which the anchor is "lost"
    "obs_dir": None,         # folder OBS writes recordings into
    "ocr_scale": 3,          # upscale factor before OCR (mode=roster)
    "ignore": [],            # row substrings never worth reporting (mode=roster)
    "roster_period": 5.0,    # force an OCR re-scan at least this often (roster)
    "roster_confirm": 2,     # OCR passes a row must persist to count (roster)
    "roster_fuzzy": 0.86,    # identity similarity that still counts as same row
    "lost_alarm_after": 45,  # seconds a region may stay anchor-lost before it shouts
    "jitter": 0,             # px of glyph misalignment to forgive (per-region)
    "ncc_min": 0.99,         # correlation below this counts as changed (match=ncc)
    "clip": 0,               # zero every pixel below this before correlating
    "reconnect_after": 30,   # seconds without frames before hunting a new window
    "max_drift": None,       # px an anchor may be found from where it was set up
    "voice_name": None,      # substring of a TTS voice name, e.g. "Mark"
    "clipboard_sigs": True,  # parse EVE probe-scanner pastes for exact signature data
    "nag_until_ack": False,  # keep repeating while an unacknowledged popup is up
}

CSV_COLS = ["iso", "unix", "client", "elapsed_s", "elapsed_hms", "region",
            "event", "detail", "snapshot", "video_file", "video_offset"]

CREATE_NO_WINDOW = 0x08000000
user32 = ctypes.windll.user32


# ---------------------------------------------------------------- config ----

def load_config():
    cfg = {"regions": [], "settings": dict(DEFAULTS)}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            prev = CONFIG + ".prev"
            if os.path.exists(prev):
                log(f"!! {os.path.basename(CONFIG)} is unreadable ({exc}); "
                    f"falling back to the previous copy")
                with open(prev, "r", encoding="utf-8") as fh:
                    disk = json.load(fh)
            else:
                raise RuntimeError(
                    f"{CONFIG} is corrupt ({exc}) and there is no .prev copy. "
                    f"Re-run calibrate to rebuild it.") from exc
        cfg["regions"] = disk.get("regions", [])
        cfg["settings"].update(disk.get("settings", {}))
    return cfg


def save_config(cfg):
    """Write the config atomically, keeping the previous one.

    A plain write truncates the file first, so a process killed mid-dump leaves
    a half-written config that will not parse - which took every region with it.
    Writing to a temporary file and replacing is atomic on Windows.
    """
    def plain(o):
        """numpy scalars are not JSON types; write them as the numbers they are."""
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        raise TypeError(f"cannot serialise {type(o).__name__}")

    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, default=plain)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(CONFIG):
        try:
            shutil.copyfile(CONFIG, CONFIG + ".prev")
        except OSError:
            pass
    os.replace(tmp, CONFIG)


def regions_for(regions, window_title):
    """Only the regions belonging to this client (untagged ones count as mine)."""
    return [r for r in regions if r.get("window", window_title) == window_title]


def pick_regions(cfg, name):
    regions = cfg["regions"]
    if not regions:
        sys.exit("No regions configured. Run:  python eve_watch.py select")
    if name:
        hit = [r for r in regions if r["name"] == name]
        if not hit:
            sys.exit(f"No region named {name!r}. Have: {[r['name'] for r in regions]}")
        return hit
    return regions


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_").lower()


def anchor_path(name, window=None):
    """Per-client anchor file, falling back to the pre-multi-client filename."""
    if window:
        scoped = os.path.join(HERE, f"anchor_{slug(window)}_{name}.png")
        if os.path.exists(scoped):
            return scoped
        legacy = os.path.join(HERE, f"anchor_{name}.png")
        if os.path.exists(legacy):
            return legacy
        return scoped
    return os.path.join(HERE, f"anchor_{name}.png")


# --------------------------------------------------------------- windows ----

def list_windows(pattern=None):
    found = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 200 or h < 200:
            return True
        if pattern and pattern.lower() not in buf.value.lower():
            return True
        found.append({"hwnd": hwnd, "title": buf.value, "width": w, "height": h,
                      "minimized": bool(user32.IsIconic(hwnd))})
        return True

    user32.EnumWindows(CB(cb), 0)
    return found


def resolve_window(client):
    pattern = client or "EVE - "
    hits = list_windows(pattern)
    # A browser tab or chat window can easily contain a character's name, so
    # prefer real client windows whenever any of the matches is one.
    eve = [h for h in hits if h["title"].startswith("EVE - ")]
    if eve:
        hits = eve
    if not hits:
        sys.exit(f"No visible window matching {pattern!r}. Is the client running?\n"
                 f"Run:  python eve_watch.py windows")
    if len(hits) > 1:
        titles = "\n".join(f"    --client {h['title']!r}" for h in hits)
        sys.exit(f"{len(hits)} windows match {pattern!r}. Be specific:\n{titles}")
    return hits[0]


class WindowCapture:
    """Latest-frame-wins capture of one window, via Windows Graphics Capture."""

    def __init__(self, hwnd, update_ms=250):
        self._lock = threading.Lock()
        self._frame = None
        self._stamp = 0.0
        self._first = threading.Event()
        self._closed = False
        self._ctl = None
        self._hwnd = hwnd

        cap = WindowsCapture(cursor_capture=False, draw_border=False,
                             minimum_update_interval=max(50, int(update_ms)),
                             window_hwnd=hwnd)

        @cap.event
        def on_frame_arrived(frame, capture_control):
            arr = np.array(frame.frame_buffer, copy=True)
            with self._lock:
                self._frame, self._stamp = arr, time.time()
            self._first.set()

        @cap.event
        def on_closed():
            self._closed = True
            self._first.set()

        self._cap = cap

    def start(self, timeout=10.0):
        self._ctl = self._cap.start_free_threaded()
        if not self._first.wait(timeout):
            raise RuntimeError(f"no frame within {timeout:.0f}s - is it minimised?")
        if self._closed:
            raise RuntimeError("capture session closed immediately")
        return self

    def frame(self):
        with self._lock:
            return self._frame, self._stamp

    @property
    def minimized(self):
        return bool(user32.IsIconic(self._hwnd))

    def stop(self):
        try:
            if self._ctl:
                self._ctl.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- imaging ---

def crop(frame, box):
    h, w = frame.shape[:2]
    t, l = max(0, box["top"]), max(0, box["left"])
    b, r = min(h, t + box["height"]), min(w, l + box["width"])
    return frame[t:b, l:r]


def context_crop(frame, box, pad):
    h, w = frame.shape[:2]
    l = max(0, box["left"] - pad)
    t = max(0, box["top"] - pad // 3)
    return crop(frame, {"left": l, "top": t,
                        "width": min(w, box["left"] + box["width"] + pad) - l,
                        "height": min(h, box["top"] + box["height"] + pad // 3) - t})


def text_mask(patch, threshold):
    return patch[:, :, :3].max(axis=2) >= threshold


def mask_diff(a, b):
    if a.shape != b.shape:
        return 10 ** 9
    return int(np.count_nonzero(a != b))


def ncc(a, b):
    """Normalised cross-correlation of two equal-size greyscale patches.

    For an 11px glyph drawn in space, a binary mask is hopeless: anti-aliased
    edges flicker across any brightness threshold, and the resulting noise (up to
    43 changed px) overlaps the difference between two different digits (41 px).
    Correlation on greyscale holds 0.9997+ for the same glyph and collapses to
    ~0.2 for a different one - three orders of magnitude more margin.
    """
    if a.shape != b.shape:
        return -1.0
    return float(cv2.matchTemplate(np.ascontiguousarray(a, dtype=np.uint8),
                                   np.ascontiguousarray(b, dtype=np.uint8),
                                   cv2.TM_CCOEFF_NORMED)[0][0])


def despeckle(gray):
    """Drop isolated bright pixels, keep glyph strokes.

    EVE's panels are semi-transparent, so the starfield behind them shows through
    the list. Stars are as bright as text, so clipping cannot remove them, and as
    the camera drifts they come and go - one unchanged row measured 793 lit pixels
    in one frame and 586 in the next, dropping its self-match to 0.92 and making
    it look like a different ship. Stars are isolated dots; text is connected.
    """
    return cv2.medianBlur(np.ascontiguousarray(gray, dtype=np.uint8), 3)


def find_best(search, template):
    """Best correlation of `template` anywhere inside `search`.

    Comparing a row at a fixed crop is hopeless: 1px down scores 0.7375, 2px
    right scores 0.2283 - lower than a genuinely different row (0.33). Adding a
    7th signature makes a scrollbar appear and nudges the columns sideways, which
    is exactly how every row came to "depart" and "arrive" at once. Searching a
    small window instead returns 1.0000 at every offset.
    """
    if (search.shape[0] < template.shape[0]
            or search.shape[1] < template.shape[1]):
        return ncc(search, template)
    res = cv2.matchTemplate(np.ascontiguousarray(search, dtype=np.uint8),
                            np.ascontiguousarray(template, dtype=np.uint8),
                            cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(res)[1])


def apply_clip(gray, clip):
    """Flatten everything dimmer than `clip` to black.

    A bracket drawn in space has whatever happens to be behind it - nebula, hull,
    empty void - inside the box, and plain correlation counts that background as
    signal. The same digit over a different backdrop measured 0.9787; clipping
    the background away puts it back at 0.9962, while a different glyph stays
    near 0.23.
    """
    if not clip:
        return gray
    return np.where(gray < clip, 0, gray).astype(np.uint8)


def signature(patch, st, threshold):
    """What this region compares frame to frame: greyscale for ncc, else a mask."""
    if st["match"] == "ncc":
        return apply_clip(to_gray(patch), st.get("clip", 0))
    return text_mask(patch, threshold)


def same_sig(a, b, st):
    """(unchanged?, distance) under whichever comparison the region uses."""
    if st["match"] == "ncc":
        s = ncc(a, b)
        return s >= st["ncc_min"], round(s, 5)
    d = shifted_diff(a, b, st["jitter"])
    return d <= st["sens"], d


def shifted_diff(a, b, jitter=0):
    """Smallest difference allowing `jitter` px of misalignment.

    Brackets drawn in space re-rasterise a pixel left or right as the camera
    drifts, and a 1 px shift of an 11 px glyph flips 30-40 mask pixels - far
    more than any real change. Comparing a cropped core of `a` against every
    offset window of `b` keeps the compared area constant, so no offset is
    favoured just for overlapping less.
    """
    if a.shape != b.shape:
        return 10 ** 9
    if jitter <= 0:
        return mask_diff(a, b)
    h, w = a.shape
    if h <= 2 * jitter or w <= 2 * jitter:
        return mask_diff(a, b)
    core = a[jitter:h - jitter, jitter:w - jitter]
    ch, cw = core.shape
    best = None
    for dy in range(2 * jitter + 1):
        for dx in range(2 * jitter + 1):
            d = int(np.count_nonzero(core != b[dy:dy + ch, dx:dx + cw]))
            if best is None or d < best:
                best = d
    return best


def to_image(patch):
    return Image.fromarray(np.ascontiguousarray(patch[:, :, :3][:, :, ::-1]))


def to_gray(patch):
    return cv2.cvtColor(np.ascontiguousarray(patch[:, :, :3]), cv2.COLOR_BGR2GRAY)


def save_preview(patch, threshold, path, zoom=6):
    rgb = to_image(patch)
    mask = Image.fromarray((text_mask(patch, threshold) * 255).astype(np.uint8)).convert("RGB")
    w, h = rgb.size
    zoom = max(1, min(zoom, 1600 // max(1, w)))
    combo = Image.new("RGB", (w, h * 2 + 3), (40, 40, 40))
    combo.paste(rgb, (0, 0))
    combo.paste(mask, (0, h + 3))
    combo.resize((w * zoom, (h * 2 + 3) * zoom), Image.NEAREST).save(path)
    return path


# -------------------------------------------------------------------- ocr ---

_ocr_engine = None
_ocr_failed = False


def ocr_engine():
    """Windows' own OCR: no install, no model download, ~50 ms a pass."""
    global _ocr_engine, _ocr_failed
    if _ocr_engine is not None or _ocr_failed:
        return _ocr_engine
    try:
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine
        _ocr_engine = (OcrEngine.try_create_from_language(Language("en-US"))
                       or OcrEngine.try_create_from_user_profile_languages())
        if _ocr_engine is None:
            raise RuntimeError("no OCR language pack")
    except Exception as exc:
        log(f"!! OCR unavailable ({exc}) - roster rows cannot be named")
        _ocr_failed = True
    return _ocr_engine


def ocr_words(pil, scale=2, origin=(0, 0)):
    """Every word with its box, in the coordinates of the frame it came from."""
    eng = ocr_engine()
    if eng is None:
        return []
    import asyncio
    from winsdk.windows.graphics.imaging import (BitmapAlphaMode, BitmapPixelFormat,
                                                 SoftwareBitmap)
    from winsdk.windows.security.cryptography import CryptographicBuffer

    im = pil.resize((pil.width * scale, pil.height * scale), Image.LANCZOS).convert("RGBA")
    raw = im.tobytes()
    buf = bytearray(raw)
    buf[0::4], buf[2::4] = raw[2::4], raw[0::4]
    try:
        bmp = SoftwareBitmap.create_copy_from_buffer(
            CryptographicBuffer.create_from_byte_array(bytes(buf)),
            BitmapPixelFormat.BGRA8, im.width, im.height, BitmapAlphaMode.STRAIGHT)

        async def _go():
            return await eng.recognize_async(bmp)

        res = asyncio.run(_go())
    except Exception as exc:
        log(f"  OCR pass failed: {exc}")
        return []

    out = []
    for line in res.lines:
        for w in line.words:
            r = w.bounding_rect
            out.append({"text": w.text,
                        "x": round(origin[0] + r.x / scale),
                        "y": round(origin[1] + r.y / scale),
                        "w": round(r.width / scale),
                        "h": round(r.height / scale)})
    return out


def looks_like(word, expected, cutoff=0.68):
    """Fuzzy header match - OCR renders Type as 'Tupe' and Group as 'Gro'."""
    a = re.sub(r"[^a-z]", "", word.lower())
    b = expected.lower()
    if not a:
        return False
    if a == b or b.startswith(a) and len(a) >= 3:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= cutoff


def ocr_rows(pil, scale=3, key_width=None):
    """One string per visible table row, words ordered left to right."""
    eng = ocr_engine()
    if eng is None:
        return []
    import asyncio
    from winsdk.windows.graphics.imaging import (BitmapAlphaMode, BitmapPixelFormat,
                                                 SoftwareBitmap)
    from winsdk.windows.security.cryptography import CryptographicBuffer

    im = pil.resize((pil.width * scale, pil.height * scale), Image.LANCZOS).convert("RGBA")
    raw = im.tobytes()
    buf = bytearray(raw)
    buf[0::4], buf[2::4] = raw[2::4], raw[0::4]          # RGBA -> BGRA
    try:
        bmp = SoftwareBitmap.create_copy_from_buffer(
            CryptographicBuffer.create_from_byte_array(bytes(buf)),
            BitmapPixelFormat.BGRA8, im.width, im.height, BitmapAlphaMode.STRAIGHT)

        async def _recognize():        # recognize_async returns an IAsyncOperation,
            return await eng.recognize_async(bmp)   # which asyncio.run will not take

        res = asyncio.run(_recognize())
    except Exception as exc:
        log(f"  OCR pass failed: {exc}")
        return []

    words = []
    for line in res.lines:
        for w in line.words:
            r = w.bounding_rect
            words.append((r.y + r.height / 2, r.x, w.text))
    if not words:
        return []

    words.sort()
    tol, rows, cur, cy = 8 * scale, [], [], None
    centres, cur_ys = [], []
    for y, x, t in words:
        if cy is None or abs(y - cy) <= tol:
            cur.append((x, t))
            cur_ys.append(y)
            cy = y if cy is None else (cy + y) / 2
        else:
            rows.append(cur)
            centres.append(sum(cur_ys) / len(cur_ys) / scale)
            cur, cur_ys, cy = [(x, t)], [y], y
    rows.append(cur)
    centres.append(sum(cur_ys) / len(cur_ys) / scale)
    row_y = centres

    out = []
    for r in rows:
        r.sort()
        text = normalise_glyphs(" ".join(t for _, t in r))
        # Identity must ignore columns that churn. Velocity ticks constantly, and
        # EVE fills a new row in left-to-right, so corp/alliance can land a beat
        # after the name - keying on the whole line double-reports one arrival.
        kept = [t for x, t in r if key_width is None or x / scale <= key_width]
        kept_text = normalise_glyphs(" ".join(kept))
        out.append({"text": text, "kept": kept_text,
                    "key": row_key(kept_text),
                    # box-relative word positions, so the caller can split the
                    # row into named columns without a second OCR pass
                    "words": [{"x": x / scale, "text": t} for x, t in r],
                    "y": row_y[len(out)]})
    return out


_TAIL_NUMBER = re.compile(r"\s+[-+]?\d[\d.,]*\s*(?:m/s|km|au)?$", re.I)


def normalise_glyphs(text):
    """EVE draws zero with a slash, which Windows OCR reads as O-with-stroke.

    Only rewritten inside tokens that already contain a digit, so a signature id
    like ABC-1O2 becomes ABC-102 while a Nordic character in a player name is
    left alone.
    """
    out = []
    for tok in text.split(" "):
        if any(ch.isdigit() for ch in tok) and ("Ø" in tok or "ø" in tok):
            tok = tok.replace("Ø", "0").replace("ø", "0")
        out.append(tok)
    return " ".join(out)


def row_key(text):
    """Normalised identity: case-folded, whitespace-collapsed, no trailing speed."""
    return _TAIL_NUMBER.sub("", " ".join(text.split())).strip().lower()


def fuzzy_match(key, candidates, threshold):
    """Nearest known identity, so an OCR wobble is not mistaken for an arrival."""
    best, score = None, 0.0
    for c in candidates:
        r = difflib.SequenceMatcher(None, key, c).ratio()
        if r > score:
            best, score = c, r
    return best if score >= threshold else None


def value_dir(name, window=None):
    """Per-client taught values, falling back to the pre-multi-client folder."""
    if window:
        scoped = os.path.join(VALUES, slug(window), name)
        if os.path.isdir(scoped):
            return scoped
        legacy = os.path.join(VALUES, name)
        if os.path.isdir(legacy):
            return legacy
        return scoped
    return os.path.join(VALUES, name)


def values_stamp(name, window=None):
    """Fingerprint of a region's taught values.

    Keyed on the files themselves, not the directory: Windows does not bump a
    directory's mtime when a file inside it is overwritten in place, so
    re-teaching an existing value would otherwise go unnoticed.
    """
    d = value_dir(name, window)
    if not os.path.isdir(d):
        return ()
    out = []
    for f in sorted(os.listdir(d)):
        if f.lower().endswith(".png"):
            try:
                st_ = os.stat(os.path.join(d, f))
                out.append((f, st_.st_mtime, st_.st_size))
            except OSError:
                pass
    return tuple(out)


def load_values(name, gray=False, window=None):
    """Learned glyphs for a region: {value_string: greyscale or boolean mask}."""
    d = value_dir(name, window)
    if not os.path.isdir(d):
        return {}
    out = {}
    for f in sorted(os.listdir(d)):
        if f.lower().endswith(".png"):
            arr = np.array(Image.open(os.path.join(d, f)).convert("L"))
            out[os.path.splitext(f)[0]] = arr if gray else arr > 127
    return out


def classify(sig, learned, tolerance, jitter=0, use_ncc=False, ncc_min=0.99):
    """Which learned value does this mask match?

    EVE's UI font is a fixed bitmap at a fixed size, so the same number renders
    pixel-identically every time. Exact matching succeeds here where OCR flatly
    fails on 11px glyphs over a busy space background.
    """
    best, score = None, None
    if use_ncc:
        for value, ref in learned.items():
            s = ncc(sig, ref)
            if score is None or s > score:
                best, score = value, s
        if best is not None and score >= ncc_min:
            return best, round(score, 5)
        return None, None if score is None else round(score, 5)
    for value, ref in learned.items():
        if ref.shape != sig.shape:
            continue
        d = shifted_diff(sig, ref, jitter)
        if score is None or d < score:
            best, score = value, d
    if best is not None and score <= tolerance:
        return best, score
    return None, score


def row_cells(box, pitch, height, width, offset=0):
    """The fixed row slots of a list, as boxes, top to bottom.

    `offset` lines slot 0 up with the first real row; without it every slot
    straddles two rows and the bitmaps churn whenever the list re-sorts.
    """
    out, i = [], 0
    while True:
        top = box["top"] + offset + i * pitch
        if top + height > box["top"] + box["height"]:
            return out
        out.append({"left": box["left"], "top": top,
                    "width": min(width, box["width"]), "height": height})
        i += 1


def label_by_row(frame, box, scale, pitch, label_width=None):
    """Label rows from ONE whole-box OCR pass, matched by vertical centre.

    OCR of a single 18px row strip drops the leading id column - it needs the
    surrounding lines for context - so read the whole list once and map each
    row back to its slot instead.
    """
    rows = ocr_rows(to_image(crop(frame, box)), scale, label_width)

    def label(cell):
        want = cell["top"] - box["top"] + cell["height"] / 2
        best, gap = None, None
        field = "kept" if label_width is not None else "text"
        for r in rows:
            d = abs(r["y"] - want)
            if gap is None or d < gap:
                best, gap = r[field] or r["text"], d
        return best if best is not None and gap <= pitch else "(unreadable)"

    return label


def split_columns(words, columns, box_left):
    """Assign OCR words to named columns by x, returning {column: text}.

    Splitting a row on whitespace cannot work: "Taron Badasaz Buzzard 29" is a
    two-word pilot name, a ship and a speed, and nothing in the string says
    where the name stops. The column positions read off the header at
    calibration do say, so each word goes to the rightmost column that starts
    at or before it.
    """
    if not columns:
        return {}
    order = sorted(columns.items(), key=lambda kv: kv[1])
    out = {}
    for w in sorted(words, key=lambda w: w["x"]):
        rel = w["x"] - box_left
        owner = order[0][0]
        for name, x in order:
            if rel >= x - 6:
                owner = name
            else:
                break
        out.setdefault(owner, []).append(w["text"])
    return {k: " ".join(v).strip() for k, v in out.items()}


def mark_pilot(key, what):
    """Record a correction for the watchers to pick up.

    Editing pilots.json from the viewer would not survive: a watcher holds the
    book in memory and writes it back. So corrections go in their own
    append-only file with a timestamp, and each watcher applies whatever is
    newer than the last line it saw. Idempotent, and no watcher has to consume
    the file for the others to see it.
    """
    try:
        with open(MARKS, "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f}\t{what}\t{key}\n")
        return True
    except OSError:
        return False


def read_marks(since=0.0):
    """Corrections newer than `since`, as (stamp, what, key)."""
    out = []
    try:
        with open(MARKS, "r", encoding="utf-8") as fh:
            for line in fh:
                bits = line.rstrip("\n").split("\t")
                if len(bits) != 3:
                    continue
                try:
                    at = float(bits[0])
                except ValueError:
                    continue
                if at > since:
                    out.append((at, bits[1], bits[2]))
    except OSError:
        pass
    return out


# What EVE puts in the Group column once a signature resolves. Until then the
# Name column reads "Cosmic Signature" (or Anomaly) and Group is empty, which
# is exactly the distinction between scanned and not.
SITE_TYPES = ("Data Site", "Relic Site", "Gas Site", "Combat Site",
              "Wormhole", "Ore Site", "Ghost Site")
UNRESOLVED = ("cosmic signature", "cosmic anomaly")


def classify_sig(name, group):
    """(type, site_name, scanned) from the Name and Group columns.

    Scanned is not a flag EVE shows; it is the absence of "Cosmic Signature".
    A signature below the resolving threshold has an empty Group and a Name of
    "Cosmic Signature"; once probed the Group names the type, and at full
    strength the Name becomes the site's own.
    """
    name = clean_field(name)
    group = clean_field(group)
    kind = ""
    for want in SITE_TYPES:
        if group and difflib.SequenceMatcher(
                None, group.casefold(), want.casefold()).ratio() >= 0.75:
            kind = want
            break
    low = name.casefold()
    generic = any(difflib.SequenceMatcher(None, low, u).ratio() >= 0.6
                  or low.startswith(u[:6]) for u in UNRESOLVED)
    site = "" if generic or not name else name
    return kind, site, bool(kind or site)


# A signature id is three letters, a dash and three digits - always. Knowing
# the shape means a misread can be repaired rather than discarded: the glyphs
# OCR confuses are known, and which side of the dash a character sits on says
# whether it must be a letter or a digit.
_AS_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1",
             "S": "5", "B": "8", "Z": "2", "G": "6", "T": "7", "A": "4",
             "Ø": "0", "ø": "0"}   # EVE slashes its zero
_AS_LETTER = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}


def repair_sig_id(raw, known=()):
    """A signature id from a wobbly reading, or "" if it cannot be made into one.

    Two steps. Fix characters that sit on the wrong side of the dash - "PTG-6O4"
    is a zero, not a letter O. Then, for a reading cut short, adopt a stored id
    it uniquely prefixes: "EMT-6" can only be "EMT-600" if that is the only
    match, and ids are unique enough that a five-character prefix decides it.
    """
    t = normalise_glyphs(clean_field(raw) or "").upper().replace(" ", "")
    for dash in ("—", "–", "¯", "_", "~"):
        t = t.replace(dash, "-")
    if "-" not in t and len(t) >= 4:
        t = t[:3] + "-" + t[3:]
    head, _, tail = t.partition("-")
    head = "".join(_AS_LETTER.get(c, c) for c in head)[:3]
    tail = "".join(_AS_DIGIT.get(c, c) for c in tail)[:3]
    cand = f"{head}-{tail}"
    if SIG_ID.match(cand):
        return cand
    if len(cand) >= 5:
        hits = [k for k in known if k.startswith(cand)]
        if len(hits) == 1:
            return hits[0]
    return ""


def load_sigs():
    try:
        with open(SIGFILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_sigs(book):
    tmp = SIGFILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(book, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SIGFILE)
    except OSError as exc:
        log(f"  signatures.json write failed: {exc}")


def note_sig(book, sid, kind, site, client, when=None, exact=False):
    """Fold one signature sighting in. Returns True if anything changed.

    Never downgrades: a signature seen resolved and later read as a bare
    "Cosmic Signature" - because OCR missed the Group column, or because the
    row scrolled - keeps what it had. Scanning only goes one way in a session,
    and the point of the file is to still know it after a relog.
    """
    if not SIG_ID.match(sid or ""):
        return False
    when = when or dt.datetime.now().isoformat(timespec="seconds")
    rec = book.setdefault(sid, {"id": sid, "type": "", "name": "",
                                "scanned": False, "first_seen": when,
                                "clients": []})
    changed = False
    if kind and (kind != rec["type"] or (exact and not rec.get("exact"))):
        rec["type"] = kind
        changed = True
    if site and site != rec["name"]:
        rec["name"] = site
        changed = True
    if (kind or site) and not rec["scanned"]:
        rec["scanned"] = True
        changed = True
    if exact:
        rec["exact"] = True
    if client and client not in rec["clients"]:
        rec["clients"].append(client)
        changed = True
    rec["last_seen"] = when
    return changed


def load_pilots():
    try:
        with open(PILOTS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_pilots(book):
    tmp = PILOTS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(book, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, PILOTS)
    except OSError as exc:
        log(f"  pilots.json write failed: {exc}")


TICKER = re.compile(r"^[\[(]([A-Z0-9.' -]{1,6})[\])]$")
_EDGE  = re.compile(r"^[^0-9A-Za-z]+|[^0-9A-Za-z')]+$")


def clean_field(text):
    """Strip the punctuation OCR hangs on a column edge.

    The same sighting produced "OwlShadow", "OwlShadow_" and "OwlShadow-" on
    three consecutive passes, and each became its own pilot. EVE character
    names cannot end in an underscore and ship types are letters and spaces,
    so trailing marks are artifacts every time.
    """
    return _EDGE.sub("", " ".join((text or "").split()))


def clean_ticker(text):
    """A corp ticker, or nothing. Tickers are bracketed; "(AXAPII" is a misread."""
    # Match BEFORE cleaning the edges: cleaning strips the very brackets that
    # tell a real ticker from a misread of one. A bracket must be present on at
    # least one side - that is what marks this as the corp column and not a
    # word that drifted in - but the closing one is often read as a letter
    # ("(AXAPII" for "[AXAPI]"), so repair that rather than discard the corp.
    raw = " ".join((text or "").split())
    if not raw:
        return ""
    # Brackets are optional. Requiring one was a stand-in for "this came from
    # the corporation column", which column assignment by x now guarantees on
    # its own - and OCR drops the brackets often enough that the rule was
    # discarding real tickers: "[-418-]" came back as "-418-" and was refused.
    bracketed = raw[0] in "[(" or raw[-1] in "])"
    inner = raw.lstrip("[(").rstrip("])")
    ok = re.compile(r"[A-Z0-9.'\-]{2,5}")
    if ok.fullmatch(inner):
        return inner                    # reads cleanly, do not "repair" it
    # Only repair a closing bracket read as a letter when an opening one is
    # actually there - otherwise a bare ticker ending in I or 1 gets trimmed.
    if bracketed and raw[-1] not in "])":
        trimmed = re.sub(r"[Iil1]$", "", inner)
        if ok.fullmatch(trimmed):
            return trimmed
    return ""


# EVE's overview right-click menu is a fixed vocabulary, and it is painted
# straight over the list. The row grid keeps almost all of it out; this catches
# an item that happens to land on a slot with a plausible-looking second column
# ("Remove Frigate from" / "Overview").
MENU_WORDS = ("show info", "look at", "track", "approach", "orbit",
              "keep at range", "align to", "warp to", "dock", "jump through",
              "add to watch", "remove ", "bookmark", "pilot ", "corporation ",
              "alliance ", "set destination", "lock target", "open cargo")

# Character names are letters, digits, spaces, hyphens and apostrophes. Nothing
# else. A parenthesis or a stray glyph means it is not a name.
NAME_OK = re.compile(r"^[0-9A-Za-z' -]+$")


# Things the overview lists that are not people. The first word is enough, and
# it is matched loosely because OCR damages these as readily as anything else.
SCENERY = ("wormhole", "asteroid", "beacon", "moon", "planet", "star",
           "stargate", "station", "customs", "gate", "cloud", "container")


def is_environment(name, ship):
    """Whether a row is scenery rather than a pilot.

    Equality of name and type was the original test - a wormhole shows the same
    text twice - but OCR breaks it: "Wormhole K162" arrived beside a type read
    as "Wormhnlp", so the row passed as a pilot and was announced as having
    taken the hole. Resemblance rather than equality, plus the vocabulary of
    things that appear on a grid, holds up when a reading is damaged.
    """
    a, b = name.casefold(), ship.casefold()
    if not b:
        return False
    if a == b:
        return True
    if len(b) >= 4 and b in a or len(a) >= 4 and a in b:
        return True
    # The vocabulary applies to the NAME only, and needs the type to agree.
    # Applied to the type it misfires badly: the hull "Astero" resembles
    # "asteroid" at 0.857, which would have written off the most-seen pilot in
    # the book. Requiring the type to echo the name keeps a pilot called
    # "Star ..." in a Loki out of it too - scenery names its own type.
    head = (a.split() or [""])[0]
    tail = (b.split() or [""])[0]
    if any(looks_like(head, term, 0.72) for term in SCENERY):
        if difflib.SequenceMatcher(None, head, tail).ratio() >= 0.6:
            return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def looks_like_pilot(name, ship):
    if len(name) < 3 or len(ship) < 3:
        return False                    # no ship, nothing worth recording
    if not NAME_OK.match(name):
        return False
    low = name.casefold()
    if any(low.startswith(w) for w in MENU_WORDS):
        return False
    # No EVE first name is one or two characters. Now that a truncated fragment
    # no longer folds into a longer name, "a junk" would otherwise become a
    # pilot of its own instead of merely being ignored.
    if len(low.split()[0]) < 3:
        return False
    return not is_environment(name, ship)


def _word_alike(x, y):
    """One word of a name against the same word read differently."""
    if x == y:
        return True
    short, long_ = sorted((x, y), key=len)
    if (len(short) >= 4 and short in long_
            and len(short) / len(long_) >= 0.6):
        return True                     # "edro" inside "redron"
    # A word cut short: "he" for "hega". OCR loses trailing glyphs, so a prefix
    # is real damage - and it is what separates that from "a" against "rasta",
    # which is not a prefix and was bridging three different pilots.
    if (len(short) >= 2 and long_.startswith(short)
            and len(long_) - len(short) <= 3):
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.8


def same_pilot(a, b):
    """Whether two OCR spellings are the same name.

    Two failure modes, two rules. OCR drops glyphs off the ENDS, so a bad
    reading is often a piece of the good one - "edro" inside "redron" - which
    containment catches. It also mangles the MIDDLE of a long name:
    "Iranama Hega Zirud" came back as "Iranama He Tirud" and "Iranama He a Z",
    three separate pilots in the book. Those score 0.73-0.88, while every pair
    of genuinely different names recorded scored at most 0.40.

    A similarity bar alone would still be unsafe - "Meki Raz" against a real
    "Neki Raz" scores 0.88 - so it is paired with a shared opening. Damage to
    the front is what containment is for; this rule is for damage after it.
    """
    # Word by word when the word counts agree. Whole-string containment was
    # the bug that put three pilots in one row: "a junk" is a substring of
    # "rasta junk", "ultra junk" AND "nega junk", so one truncated pass bridged
    # all three. Corresponding words have to match, which "a" against "rasta"
    # does not.
    aw, bw = a.split(), b.split()
    if aw and len(aw) == len(bw):
        return all(_word_alike(x, y) for x, y in zip(aw, bw))

    # Different word counts mean OCR joined or split a word. Containment still
    # applies, but only if the shorter is most of the longer - losing a glyph
    # or two, not half a name.
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and short in long_ and len(short) / len(long_) >= 0.66:
        return True
    lead = 0
    for x, y in zip(a, b):
        if x != y:
            break
        lead += 1
    if lead < 6 or min(len(a), len(b)) < 8:
        return False
    if abs(len(a) - len(b)) > max(4, min(len(a), len(b)) // 2):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72


def resolve_key(book, name, extra=()):
    """The key this name belongs under, folding OCR damage into one pilot.

    Presence has to use the same answer the book does, or a pilot flickers:
    "Redron" one pass and "edro" the next would look like one leaving and
    another arriving.
    """
    key = pilot_key(name)
    if key in book:
        return key
    # Candidates include names merely SEEN before, not just recorded ones. A
    # first sighting has nothing in the book to fold against, so "Russian
    # Revolution" arriving as "RussianRevolution" then "Russian -Revolution"
    # became two keys, neither repeated, and the pilot was never recorded at
    # all - present in the log, absent from the book.
    pool = list(book) + [k for k in extra if k not in book]
    if key in pool:
        return key
    hits = [k for k in pool if same_pilot(key, k)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:                   # ambiguous: prefer the closest
        return max(hits, key=lambda k: difflib.SequenceMatcher(None, key, k).ratio())
    return key


def ship_alike(a, b):
    """Whether two readings are the same hull.

    Plain similarity is unsafe here and was rejected once already: "Naglfar"
    against "Na Ifar" scores 0.714, and so do Punisher and Purifier, which are
    different hulls. What separates them is that OCR SPLITS a word - it does
    not turn one hull's name into another's. So the test is whether joining the
    gaps out of the readings makes them agree, which only applies when the
    readings disagree about how many words there are.
    """
    a, b = a.casefold(), b.casefold()
    if a == b:
        return True
    if (" " in a) == (" " in b):
        return False                    # both whole, or both split the same way
    ja, jb = a.replace(" ", ""), b.replace(" ", "")
    # 0.75, because "naifar" against "naglfar" is 0.769 - OCR lost a glyph as
    # well as inserting the gap. Only readings that DISAGREE about word count
    # reach this line, so the pairs a loose bar would endanger - two whole
    # single-word hulls like Punisher and Purifier - never get here. Real
    # multi-word hulls are far below it: "Vexor Navy Issue" against "Vexor"
    # scores 0.53.
    return difflib.SequenceMatcher(None, ja, jb).ratio() >= 0.75


def name_score(text):
    """Rank two spellings of one name. Lower is better.

    Length alone picks the wrong one: "Russian -Revolution" is longer than
    "Russian Revolution" only because of a stray dash. But hyphens and
    apostrophes are legal in EVE names, so they cannot simply be penalised -
    "O'Brien-Smith" is a real name. What separates them is position: a legal
    mark sits BETWEEN letters, an OCR artifact sits beside a space or an end.
    """
    stray = 0
    for i, ch in enumerate(text):
        if ch.isalnum() or ch == " ":
            continue
        before = text[i - 1] if i else " "
        after = text[i + 1] if i + 1 < len(text) else " "
        if not (before.isalnum() and after.isalnum()):
            stray += 1
    return (stray, -len(text))


def pilot_key(name):
    return clean_field(name).casefold()


def note_pilot(book, name, ship, corp, client, when=None, visit=True):
    """Fold one sighting into the book. Returns True if anything is new.

    Keyed on the name, because that is the thing that persists - a pilot swaps
    ships and changes corp, and the point of the record is to show exactly that.
    """
    name = clean_field(name)
    if len(name) < 3:
        return False
    when = when or dt.datetime.now().isoformat(timespec="seconds")
    key = pilot_key(name)

    # OCR wobbles a character now and then, and a long-lived record must not
    # split one pilot across those spellings. Cleaning the edges catches most of
    # it; a near-match catches the rest. The bar is high on purpose - a wrong
    # merge fuses two real pilots for good, which is worse than a duplicate.
    hit = resolve_key(book, key)
    if hit != key and hit in book:
        book[hit].setdefault("aka", [])
        if name not in book[hit]["aka"] and name != book[hit]["name"]:
            book[hit]["aka"].append(name)
    key = hit

    # The fullest spelling wins as the display name. OCR loses characters and
    # joins words - "Russian Revolution" came back as "RussianRevolution" - so
    # the longest reading is the one closest to what is on screen. (Ships go the
    # other way: there OCR SPLITS a word, so fewest gaps wins.)
    if key in book:
        held = book[key].get("name", "")
        loser = name if name_score(held) <= name_score(name) else held
        winner = held if loser == name else name
        book[key]["name"] = winner
        aka = book[key].setdefault("aka", [])
        if loser and loser != winner and loser not in aka:
            aka.append(loser)
        if winner in aka:
            aka.remove(winner)

    who = book.setdefault(key, {"name": name, "ships": {}, "corps": {},
                                "clients": [], "seen": 0, "aka": [],
                                "first_seen": when, "last_seen": when})
    fresh = False
    for field, value in (("ships", clean_field(ship)),
                         ("corps", clean_ticker(corp))):
        if not value:
            continue
        if field == "ships" and value not in who[field]:
            # Fold a split reading into the whole one. Fewest gaps wins: OCR
            # breaks a word apart, it does not join two.
            for held in list(who[field]):
                if ship_alike(value, held):
                    keep = min((value, held), key=lambda t: (t.count(" "), -len(t)))
                    other = held if keep == value else value
                    if keep != held:
                        who[field][keep] = who[field].pop(held)
                    value = keep
                    break
        slot = who[field].setdefault(value, {"first": when, "count": 0})
        if slot["count"] == 0:
            fresh = True
        # count sightings, not polls: the roster is re-read every few seconds
        # and counting those made "seen" a measure of uptime, not of encounters.
        # A ship or corp read for the first time counts even mid-visit - it is
        # new information, and OCR often only resolves the corp on a later pass.
        if visit or slot["count"] == 0:
            slot["count"] += 1
        slot["last"] = when
    if client and client not in who["clients"]:
        who["clients"].append(client)
    if visit:
        who["seen"] += 1
    who["last_seen"] = when
    return fresh


def field_samples(frame, box, scale, columns, pads=(0, 4, 12)):
    """Read the same rows several times at different crops, keep the best.

    Windows OCR is deterministic for a given input but wildly sensitive to the
    crop it is handed, and not monotonically: the same saved row read
    "OwlShadow Prospect" at 4 and 12px of margin and "os ect" at 0, 8, 16, 24
    and 48. There is no margin that is simply better, so stop looking for one
    and take several readings of the same pixels instead.

    Merged per column, preferring the longest value: OCR drops and splits
    glyphs, it does not invent them, so between "Prospect" and "os ect" the
    longer one is the one that survived intact. Returns {slot_y: {column: text}}.
    """
    # Some columns sit LEFT of the box: it starts at the name column, so
    # distance is at a negative offset and was never in the crop. Reach out far
    # enough to include every column the header gave us - it is all the same
    # panel, so nothing else can be pulled in.
    reach = max(0, -min(columns.values())) + 8 if columns else 0

    out = {}
    for pad in pads:
        left = max(0, box["left"] - pad - reach)
        top = max(0, box["top"] - pad)
        dx, dy = box["left"] - left, box["top"] - top
        crop_box = {"left": left, "top": top,
                    "width": box["width"] + dx + pad,
                    "height": box["height"] + dy + pad}
        patch = crop(frame, crop_box)
        if patch.shape[0] < 8 or patch.shape[1] < 8:
            continue
        for row in ocr_rows(to_image(patch), scale):
            y = row["y"] - dy
            slot = next((k for k in out if abs(k - y) <= 6), None)
            if slot is None:
                slot = y
                out[slot] = {}
            fields = split_columns(row.get("words") or [], columns, dx)
            for key, value in fields.items():
                value = clean_field(value)
                if not value:
                    continue
                tally = out[slot].setdefault("_votes", {}).setdefault(key, {})
                tally[value] = tally.get(value, 0) + 1

    # Pick per field once every crop has voted. Majority first, then the
    # cleanest reading, then the longest. Longest alone was wrong: a row read
    # "J144944 - Rooftop" by two crops and "J 14 4 9 - Rooftop" by one, and
    # length handed it to the single damaged vote.
    for slot, fields in out.items():
        for key, tally in (fields.get("_votes") or {}).items():
            fields[key] = max(tally, key=lambda v: (tally[v],) + tuple(
                -x for x in name_score(v)))
    return out


_DIST = re.compile(r"^([0-9][0-9 .,]*)[ ]*(m|km|au)$", re.I)
_AU_M = 149_597_870_700.0


def parse_distance(text):
    """An overview distance in metres, or None.

    EVE writes "1 933 km", "27,9 AU", "350 m" - space as the thousands mark and
    comma as the decimal, which is the opposite of what float() expects.
    """
    hit = _DIST.match(" ".join((text or "").split()))
    if not hit:
        return None
    number = hit.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    unit = hit.group(2).lower()
    return value * (1000.0 if unit == "km" else _AU_M if unit == "au" else 1.0)


def reportable(label, settings, require=None):
    """Whether a detected row is worth telling the user about.

    Pixel identity is deliberately independent of OCR, because OCR wobbles on
    unchanged text. But a row the box picked up from OUTSIDE the list - EVE's
    "Probes launched." notification sitting below a short signature list - is a
    real row of real pixels, so pixels alone cannot rule it out. Where a list
    has a known row shape the label is the only thing that can: 58% of logged
    signature rows were text like "s launched." or "(unreadable)" that no
    signature id could ever match.
    """
    if ignored(label, settings):
        return False
    if require is not None and not require.match(row_key(label)):
        return False
    return True


def reconcile_pixels(st, frame, box, threshold, settings, label_fn,
                     allow_depart=True, id_fn=None):
    """Identify list rows by pixel bitmap rather than by OCR'd text.

    Measured live on the probe scanner: the same signature row scores >= 0.9994
    against itself across passes, while two different rows score at most 0.711.
    OCR of that same unchanged row flickered between "ABC-123" and "AbC-123",
    producing six phantom arrivals a minute - and fuzzy matching cannot help,
    because an OCR wobble and a genuinely different id score identically (0.857).
    So identity comes from pixels; OCR is only used for the label a human reads.
    """
    need = settings["roster_confirm"]
    nmin = st["pix_ncc"]

    pad = st["pix_pad"]
    # Search wide sideways, tight vertically. A row growing the list makes a
    # scrollbar appear and nudges every column ~10px across; at a 3px search
    # window that scored 0.2275 against itself, so every row "departed" and
    # "arrived" in the same second - which is how a wormhole that had not moved
    # kept announcing itself. Nothing sits beside a row to be confused with, so
    # x can be generous; rows are one pitch apart, so y must not be.
    pad_x = st["pix_pad_x"]
    # Vertical search was 3px. Measured on both clients, a row scores 1.0000
    # against itself and at most 0.61 against any other row even with a 10px
    # window, so there is no risk of matching a neighbour and there is no
    # reason to be this tight about jitter.
    pad = max(pad, st.get("pix_pad_y") or 0)
    clip = st["pix_clip"]

    # Filter the whole box ONCE, then cut cells out of the filtered image.
    # Cropping first and filtering after gives the 3x3 median no neighbours at
    # the crop border, so a tight template does not equal those same pixels
    # inside the padded search window: measured 0.9055 against itself, not
    # 1.0000, on rows whose text touches the cell edge (18-30 lit border px).
    # Four of one client's eleven rows fell under the 0.90 bar that way and were
    # never confirmed, so a new signature everyone else saw went unreported,
    # while a client whose rows sat clear of their borders scored 1.0000.
    filt = despeckle(apply_clip(to_gray(crop(frame, box)), clip))
    bx, by = box["left"], box["top"]

    def cut(left, top, width, height):
        y0, x0 = max(0, top - by), max(0, left - bx)
        return filt[y0:min(filt.shape[0], top - by + height),
                    x0:min(filt.shape[1], left - bx + width)]

    occupied = []
    for cell in row_cells(box, st["pitch"], st["row_h"],
                          st["key_width"] or box["width"], st["row_offset"]):
        patch = crop(frame, cell)
        if patch.shape[0] < cell["height"] or patch.shape[1] < cell["width"]:
            continue
        if int(text_mask(patch, threshold).sum()) < st["pix_min_lit"]:
            continue
        # Zero the background before correlating. EVE re-shades a row when the
        # list around it changes - the same row measured 48 then 30 - and raw
        # correlation reads that as a different row (0.87 against a 0.95 bar).
        # Clipping to the text leaves only glyph pixels: the same row scores
        # 1.0000, a different one 0.35.
        occupied.append((cut(cell["left"] - pad_x, cell["top"] - pad,
                             cell["width"] + 2 * pad_x, cell["height"] + 2 * pad),
                         cut(cell["left"], cell["top"],
                             cell["width"], cell["height"]), cell))

    # A list that has just lost most of its rows is being redrawn, not
    # emptied. Re-scanning clears the probe scanner for a pass or two, and
    # acting on that reported every row as gone and then back again - 74 log
    # lines from one scan. Wait it out; if the rows really have gone they will
    # still be missing in a moment.
    # Timed, not counted: the poll interval differs per client - 0.25s on one
    # and 1.0s on another - so a fixed number of passes would give one client
    # a quarter of the tolerance of the other for the same redraw.
    tracked = len(st["rows"])
    if tracked >= 3 and len(occupied) * 2 < tracked:
        since = st.get("collapsed_at")
        if since is None:
            st["collapsed_at"] = time.time()
            return [], []
        if time.time() - since <= 4.0:
            return [], []
    else:
        st["collapsed_at"] = None

    used, fresh = set(), []
    for search, exact, cell in occupied:
        best_k, best_s = None, -2.0
        for k, v in st["rows"].items():
            if k in used:
                continue
            sc = find_best(search, v["bitmap"])
            if sc > best_s:
                best_k, best_s = k, sc
        if best_k is not None and best_s >= nmin:
            used.add(best_k)
            st["rows"][best_k]["misses"] = 0
        else:
            fresh.append((search, exact, cell))

    departed = []
    if allow_depart:
        for k in list(st["rows"]):
            if k in used:
                continue
            st["rows"][k]["misses"] += 1
            if st["rows"][k]["misses"] >= need and id_fn and st["rows"][k].get("uid"):
                # Pixels are not the only identity available. A signature row
                # carries a unique id, and hovering or selecting a row changed
                # it enough to score 0.77 where the bar is 0.95 - well above
                # the 0.65 two different rows reach, but not enough to match.
                # If the id is still on screen the row never left: re-bind it
                # to where it now is and carry on.
                want = st["rows"][k]["uid"]
                # Only cells nothing has claimed yet. And claiming one means
                # taking it OUT of `fresh`: leaving it there re-bound the row
                # and then confirmed the same cell as an arrival, so one row
                # was announced again and again.
                for entry in list(fresh):
                    se, ex, cell = entry
                    if id_fn(cell) != want:
                        continue
                    fresh.remove(entry)
                    st["rows"][k]["bitmap"] = ex
                    st["rows"][k]["misses"] = 0
                    st["rows"][k]["text"] = label_fn(cell) or st["rows"][k]["text"]
                    used.add(k)
                    break
                if st["rows"][k]["misses"] == 0:
                    continue

            if st["rows"][k]["misses"] >= need:
                gone = st["rows"].pop(k)
                text = gone["text"]
                if reportable(text, settings, st.get("require")):
                    # How close it came, and to what. A departure that is real
                    # scores near nothing against every cell on screen; one
                    # that is a misjudgement scores just under the bar, and
                    # without this there is no way to tell them apart after
                    # the fact.
                    best = max((find_best(se, gone["bitmap"])
                                for se, _ex, _c in occupied), default=0.0)
                    st.setdefault("why", []).append(
                        f"best {best:.4f} vs bar {nmin} across "
                        f"{len(occupied)} rows on screen")
                    departed.append(text)

    arrived, still = [], []
    for search, exact, cell in fresh:
        prev = None
        for idx, (pg, _lbl, _h) in enumerate(st["pending"]):
            if find_best(search, pg) >= nmin:
                prev = idx
                break
        hits = st["pending"][prev][2] + 1 if prev is not None else 1
        label = st["pending"][prev][1] if prev is not None else label_fn(cell)
        if hits >= need:
            st["next_id"] += 1
            st["rows"][f"px{st['next_id']}"] = {
                "bitmap": exact, "text": label, "misses": 0,
                "uid": id_fn(cell) if id_fn else ""}
            # Track it either way so it is not re-reported, but stay silent for
            # permanent scenery and for rows that cannot be what this list holds.
            if reportable(label, settings, st.get("require")):
                arrived.append(label)
            else:
                st["junk"] = st.get("junk", 0) + 1
        else:
            still.append((exact, label, hits))
    st["pending"] = still
    return arrived, departed


SIG_LINE = re.compile(r"^([A-Z]{3}-\d{3})\t([^\t\n]*)\t?([^\t\n]*)", re.M)
SIG_ID   = re.compile(r"^[A-Z]{3}-\d{3}$")
SIG_ROW  = re.compile(r"^([A-Z]{3}-\d{3})\t(.*)$", re.M)


def parse_signature_rows(text):
    """Signatures from an EVE probe-scanner copy, split into fields.

    The paste is exact where OCR is not, so it is the better source when
    it is available. Fields are matched by CONTENT rather than position:
    the one naming a site type is the type, the one saying "Cosmic
    Signature" is the group, and what is left is the site name. That
    survives a column being reordered or hidden in the scanner.
    """
    out = {}
    for sid, rest in SIG_ROW.findall(text or ""):
        cells = [c.strip() for c in rest.split("\t")]
        kind, site = "", ""
        for cell in cells:
            if not cell or "%" in cell or cell.lower().endswith(("au", "km", "m")):
                continue
            k, n, _ = classify_sig("", cell)
            if k and not kind:
                kind = k
                continue
            low = cell.casefold()
            if any(low.startswith(u[:6]) for u in UNRESOLVED):
                continue                # the group, nothing to learn
            if not site and not k:
                site = cell
        out[sid] = {"type": kind, "name": site}
    return out


def read_mode(default="away"):
    try:
        with open(MODEFILE, "r", encoding="utf-8") as fh:
            name = fh.read().strip().lower()
        return name if name in PROFILES else default
    except OSError:
        return default


def apply_profile(args, name):
    """Set any alert option the user did not pass explicitly."""
    for key, value in PROFILES[name].items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)
    return name


def read_clipboard():
    """Current clipboard text, or None. Read-only - never writes."""
    CF_UNICODETEXT = 13
    k = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(0):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.c_wchar_p(p).value
        finally:
            k.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clipboard_owner(stale=20.0):
    """Whether THIS watcher is the one that reads the clipboard.

    Ctrl+C is a single global buffer. Every watcher polling it means one
    keypress produces one alert per client - and each alert names its own
    client as the source, which the clipboard cannot actually tell you, since
    an EVE paste carries no character or system name.

    So exactly one watcher reads it. Ownership is a file holding a pid, with
    the holder refreshing its mtime; any watcher takes over once it goes stale,
    so ownership survives the owner being stopped or crashing.
    """
    me = os.getpid()
    try:
        with open(CLIPFILE, "r", encoding="utf-8") as fh:
            holder = int(fh.read().strip() or 0)
        age = time.time() - os.path.getmtime(CLIPFILE)
    except (OSError, ValueError):
        holder, age = 0, stale + 1

    if holder != me and age <= stale:
        return False
    if holder == me and age < stale / 4:
        return True                     # still ours, no need to touch the file
    try:
        with open(CLIPFILE, "w", encoding="utf-8") as fh:
            fh.write(str(me))
        if holder != me:
            log(f"   clipboard: this watcher now owns Ctrl+C reads "
                f"(previous owner {holder or 'none'} is gone)")
        return True
    except OSError:
        return False


def parse_signatures(text):
    """Signatures from an EVE probe-scanner copy: {id: 'id  type  group'}.

    EVE lets you select the scan results and copy them, which is how every
    mapping tool gets its data. Parsing that paste is exact - no OCR, no
    ambiguity between O and 0 - and it costs nothing but the Ctrl+C you were
    already pressing.
    """
    out = {}
    for sig, col2, col3 in SIG_LINE.findall(text or ""):
        rest = "  ".join(x.strip() for x in (col2, col3) if x.strip())
        out[sig] = f"{sig}  {rest}".strip()
    return out


def reconcile_roster(st, seen, settings, allow_depart=True):
    """Fold one OCR pass into the known roster. Returns (arrived, departed) texts.

    Two rules keep this honest. Identities are fuzzy-matched, because OCR wobbles
    on unchanged text (a structure name reads clipped now and then) and a wobble must not
    look like an arrival. And a row must persist across `roster_confirm` passes
    before it counts, in either direction, so one bad pass invents nothing.
    """
    fuzz = settings["roster_fuzzy"]
    need = settings["roster_confirm"]

    matched, fresh = {}, {}
    for k, text in seen.items():
        hit = fuzzy_match(k, st["rows"].keys(), fuzz)
        if hit:
            matched[hit] = text
        else:
            fresh[k] = text

    departed = []
    for k in list(st["rows"]):
        if k in matched:
            st["rows"][k]["misses"] = 0
            st["rows"][k]["text"] = matched[k]
        elif allow_depart:
            st["rows"][k]["misses"] += 1
            if st["rows"][k]["misses"] >= need:
                departed.append(st["rows"].pop(k)["text"])

    arrived, still_pending = [], {}
    for k, text in fresh.items():
        prev = fuzzy_match(k, st["pending"].keys(), fuzz)
        hits = st["pending"][prev]["hits"] + 1 if prev else 1
        if hits >= need:
            st["rows"][k] = {"text": text, "misses": 0}
            arrived.append(text)
        else:
            still_pending[k] = {"text": text, "hits": hits}
    st["pending"] = still_pending

    return arrived, departed


def ignored(text, settings):
    low = text.lower()
    return any(p.lower() in low for p in settings.get("ignore", []) if p)


NOISE_ROW = re.compile(r"^[\d\s.,:%/-]+$")


def is_noise_row(text):
    """A row of nothing but digits is scenery, not a contact.

    EVE panels are semi-transparent, so tactical-overlay range rings ("150",
    "30") read straight through the list and would otherwise register as an
    arrival.
    """
    return bool(NOISE_ROW.match(text.strip()))


def region_settings(region, settings):
    """Settings with this region's own overrides applied.

    `ignore` in particular must be per-region: it exists to drop permanent
    scenery from the overview, but applied globally it also censored those same
    objects out of the d-scan log - and inconsistently, since it only matched
    when OCR happened to read the name cleanly.
    """
    out = dict(settings)
    if "ignore" in region:
        out["ignore"] = region["ignore"]
    return out


# --------------------------------------------------------------- tracking ---

class Tracker:
    """Finds the target box in each frame, following the anchor if it moved."""

    def __init__(self, region, settings):
        self.name = region["name"]
        self.target = dict(region["target"])
        self.settings = settings
        self.tmpl = None
        self.lost = False
        self.drift = (0, 0)

        self.origin = None
        self.max_drift = region.get("max_drift", settings.get("max_drift"))
        anchor = region.get("anchor")
        if anchor and os.path.exists(anchor_path(self.name, region.get("window"))):
            self.origin = (anchor["left"], anchor["top"])
            self.tmpl = np.array(Image.open(anchor_path(self.name, region.get("window"))).convert("L"))
            self.anchor_pos = [anchor["left"], anchor["top"]]
            self.offset = (self.target["left"] - anchor["left"],
                           self.target["top"] - anchor["top"])

    @property
    def tracking(self):
        return self.tmpl is not None

    def locate(self, frame):
        """Return the current target box, or None if the anchor was lost."""
        if not self.tracking:
            return dict(self.target)

        gray = to_gray(frame)
        th, tw = self.tmpl.shape[:2]
        rad = self.settings["search_radius"]

        for radius in (rad, None):              # local search, then the whole window
            if radius is None:
                sub, ox, oy = gray, 0, 0
            else:
                ox = max(0, self.anchor_pos[0] - radius)
                oy = max(0, self.anchor_pos[1] - radius)
                x1 = min(gray.shape[1], self.anchor_pos[0] + tw + radius)
                y1 = min(gray.shape[0], self.anchor_pos[1] + th + radius)
                sub = gray[oy:y1, ox:x1]
            if sub.shape[0] < th or sub.shape[1] < tw:
                continue
            res = cv2.matchTemplate(sub, self.tmpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if score >= self.settings["match_min"]:
                found = [ox + loc[0], oy + loc[1]]
                # Two overview panels have identical titles, so their anchors are
                # identical templates. Without a bound, a whole-window fallback
                # can lock a region onto its sibling and report the wrong list.
                if self.max_drift is not None and self.origin is not None:
                    if (abs(found[0] - self.origin[0]) > self.max_drift
                            or abs(found[1] - self.origin[1]) > self.max_drift):
                        continue
                self.drift = (found[0] - self.anchor_pos[0],
                              found[1] - self.anchor_pos[1])
                self.anchor_pos = found
                self.lost = False
                box = dict(self.target)
                box["left"] = found[0] + self.offset[0]
                box["top"] = found[1] + self.offset[1]
                if (box["left"] < 0 or box["top"] < 0
                        or box["left"] + box["width"] > frame.shape[1]
                        or box["top"] + box["height"] > frame.shape[0]):
                    self.lost = True
                    return None
                return box

        self.lost = True
        return None


# ------------------------------------------------------------- event log ----

def log(msg):
    where = f"[{TAG}] " if TAG else ""
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {where}{msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def hms(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def find_recording(obs_dir, max_age=120):
    """Newest video in obs_dir that is still being written = the live recording."""
    if not obs_dir or not os.path.isdir(obs_dir):
        return None
    exts = (".mkv", ".mp4", ".flv", ".mov", ".ts", ".m4v")
    best, best_m = None, 0.0
    for f in os.listdir(obs_dir):
        if f.lower().endswith(exts):
            p = os.path.join(obs_dir, f)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best_m:
                best, best_m = p, m
    if best and time.time() - best_m <= max_age:
        return best
    return None


def csv_safe(value):
    """Defang a spreadsheet formula without altering what the text says.

    Ship and character names are chosen by other players, and they reach this
    file verbatim. A name beginning =, +, - or @ is a formula to Excel and
    LibreOffice, so opening the log could run it. Prefixing a zero-width-free
    apostrophe is the standard defusal: spreadsheets treat the cell as text and
    hide the mark, and grep still finds the name one character in.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def record_event(started, region, event, detail="", snapshot="", obs_dir=None):
    """Append one row to events.csv; return (video_name, offset) for logging."""
    now = time.time()
    vid = find_recording(obs_dir)
    offset = ""
    if vid:
        try:
            offset = hms(now - os.path.getctime(vid))
        except OSError:
            vid = None
    row = {"iso": dt.datetime.now().isoformat(timespec="seconds"),
           "unix": round(now, 3), "client": TAG,
           "elapsed_s": round(now - started, 1),
           "elapsed_hms": hms(now - started),
           "region": region, "event": event, "detail": csv_safe(detail),
           "snapshot": os.path.relpath(snapshot, HERE) if snapshot else "",
           "video_file": os.path.basename(vid) if vid else "",
           "video_offset": offset}
    try:
        new = not os.path.exists(CSVFILE)
        with open(CSVFILE, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as exc:
        log(f"  csv write failed: {exc}")
    return (os.path.basename(vid) if vid else None), offset


# ----------------------------------------------------------------- alerts ---

_popup_busy = threading.Event()


def beep(rounds=2, wav=None):
    try:
        import winsound
        if wav:
            winsound.PlaySound(wav, winsound.SND_FILENAME)
            return
        for _ in range(rounds):
            winsound.Beep(1250, 160)
            winsound.Beep(880, 160)
    except Exception:
        print("\a", end="", flush=True)


VOICE = None            # substring of a voice name, e.g. "Mark"; None = system default
_voice_warned = False


def _speak_powershell(text):
    """Fallback: shell out to SAPI. Costs ~1.4s of process startup per alert."""
    safe = text.replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.Rate=1; $s.Volume=100; $s.Speak('{safe}');")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=CREATE_NO_WINDOW, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak(text):
    """Windows TTS, synthesised in-process.

    Measured overhead before any sound: 0.05s here versus 1.36s shelling out to
    PowerShell, which spawned a process per alert. It also removes the need to
    escape the phrase into a shell command line - a pilot called O'Brien was one
    quoting slip away from silence.
    """
    global _voice_warned
    try:
        import asyncio
        import winsound
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
        from winsdk.windows.storage.streams import DataReader

        async def render():
            synth = SpeechSynthesizer()
            if VOICE:
                for v in SpeechSynthesizer.all_voices:
                    if VOICE.lower() in v.display_name.lower():
                        synth.voice = v
                        break
            stream = await synth.synthesize_text_to_stream_async(text)
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(stream.size)
            return bytes(reader.read_buffer(stream.size))

        winsound.PlaySound(asyncio.run(render()), winsound.SND_MEMORY)
    except Exception as exc:
        if not _voice_warned:
            _voice_warned = True
            log(f"  in-process voice unavailable ({exc}); using PowerShell instead")
        try:
            _speak_powershell(text)
        except Exception as exc2:
            log(f"  voice failed: {exc2}")


def popup(title, msg):
    if _popup_busy.is_set():
        return
    _popup_busy.set()
    try:
        user32.MessageBoxW(0, msg, title, 0x30 | 0x1000 | 0x10000)
    except Exception:
        pass
    finally:
        _popup_busy.clear()


def post_webhook(url, text):
    import urllib.request
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"content": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        log(f"  webhook failed: {exc}")


_alerts = queue.Queue()
_alert_worker = None
ALERT_HOLD = 0.8            # seconds spent gathering before anything is said


def speech_gate(seconds=8.0):
    """A machine-wide turn to speak, so two watchers do not talk over each other.

    A named mutex, because the OS releases it if a watcher dies - a lock file
    would need stale detection and would silence everything if one leaked.
    Returns a release callable, or None if the wait ran out, in which case
    speak anyway: a late alert overlapping beats a dropped one.
    """
    try:
        k = ctypes.windll.kernel32
        h = k.CreateMutexW(None, False, "eve_watch_speech_gate")
        if not h:
            return lambda: None
        if k.WaitForSingleObject(h, int(seconds * 1000)) not in (0, 128):
            return None

        def release():
            try:
                k.ReleaseMutex(h)
                k.CloseHandle(h)
            except Exception:
                pass
        return release
    except Exception:
        return lambda: None


def _say_batch(batch):
    """Announce one gathered batch as a single alert."""
    opts = batch[-1][2]
    tag = f"{TAG}. " if TAG and batch[0][3] else ""
    items = [raw for raw, _body, _o, _a in batch]
    if len(items) == 1:
        phrase = tag + items[0]
    else:
        # Group by what each alert is ABOUT, so three arrivals read as one
        # sentence listing three names rather than repeating "new contact in
        # the overview" three times over.
        groups, order = {}, []
        for raw in items:
            head, _, tail = raw.partition(". ")
            if not tail:
                head, tail = "", raw
            if head not in groups:
                groups[head] = []
                order.append(head)
            groups[head].append(tail)
        parts = []
        for head in order:
            found = groups[head]
            shown = ", ".join(found[:3])
            if len(found) > 3:
                shown += f", and {len(found) - 3} more"
            parts.append(f"{head}. {shown}" if head else shown)
        phrase = f"{tag}{len(items)} changes. " + "; ".join(parts)

    title = f"EVE watch - {TAG}" if TAG else "EVE watch"
    if opts.popup:
        body = ("\n\n").join(b for _r, b, _o, _a in batch)
        if TAG:
            body = f"Client: {TAG}\n\n{body}"
        threading.Thread(target=popup, args=(title, body), daemon=True).start()

    if not getattr(opts, "beeps", True) and not opts.popup and not opts.voice:
        return                      # --quiet: log and snapshot, make no noise

    cycles = 0
    while cycles < 60:
        if getattr(opts, "beeps", True):
            beep(2, opts.sound)
        if opts.voice:
            release = speech_gate()
            try:
                speak(phrase)
            finally:
                if release:
                    release()
        cycles += 1
        nagging = getattr(opts, "nag_until_ack", False) and opts.popup
        if cycles >= opts.repeat and not (nagging and _popup_busy.is_set()):
            break
        time.sleep(0.6)

    if opts.webhook:
        threading.Thread(target=post_webhook,
                         args=(opts.webhook, f"**EVE watch** - {phrase}"),
                         daemon=True).start()


def _alert_loop():
    """One announcer. Everything queues behind it, so nothing overlaps."""
    while True:
        try:
            batch = [_alerts.get()]
            end = time.time() + ALERT_HOLD
            while True:
                left = end - time.time()
                if left <= 0:
                    break
                try:
                    batch.append(_alerts.get(timeout=left))
                except queue.Empty:
                    break
            _say_batch(batch)
        except Exception as exc:        # an announcer that dies goes silent
            log(f"  alert failed: {exc}")


def raise_alarm(phrase, body, opts, attribute=True):
    """Queue an alert. One announcer speaks them, gathering what arrives close
    together into a single announcement.

    Each alert used to start its own thread, so two arrivals a second apart
    talked over each other and a busy grid was unintelligible. Holding briefly
    costs less than that: nothing is dropped, and what changed is read out as
    one list.

    With several clients watched the first thing you need to know is WHICH one
    to switch to, so the client still leads.
    """
    global _alert_worker
    if _alert_worker is None:
        _alert_worker = threading.Thread(target=_alert_loop, daemon=True)
        _alert_worker.start()
    _alerts.put((phrase, body, opts, attribute))

def drag_box(pil_img, caption, optional=False):
    """Show an image, let the user drag a box; return (l, t, w, h) in image px."""
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("eve-watch")
    root.attributes("-topmost", True)
    sw = root.winfo_screenwidth() - 120
    sh = root.winfo_screenheight() - 240
    scale = min(1.0, sw / pil_img.width, sh / pil_img.height)
    shown = pil_img if scale == 1.0 else pil_img.resize(
        (max(1, int(pil_img.width * scale)), max(1, int(pil_img.height * scale))),
        Image.LANCZOS)

    hint = caption + ("     (Esc to skip)" if optional else "     (Esc to cancel)")
    tk.Label(root, text=hint, font=("Segoe UI", 11), pady=6,
             wraplength=max(400, shown.width)).pack()
    canvas = tk.Canvas(root, width=shown.width, height=shown.height,
                       cursor="crosshair", highlightthickness=0)
    canvas.pack()
    photo = ImageTk.PhotoImage(shown)
    canvas.create_image(0, 0, anchor="nw", image=photo)

    st = {"x0": 0, "y0": 0, "rect": None, "out": None}

    def press(ev):
        st["x0"], st["y0"] = ev.x, ev.y
        if st["rect"]:
            canvas.delete(st["rect"])
        st["rect"] = canvas.create_rectangle(ev.x, ev.y, ev.x, ev.y,
                                             outline="#4ade80", width=2)

    def drag(ev):
        canvas.coords(st["rect"], st["x0"], st["y0"], ev.x, ev.y)

    def release(ev):
        x0, y0 = min(st["x0"], ev.x), min(st["y0"], ev.y)
        x1, y1 = max(st["x0"], ev.x), max(st["y0"], ev.y)
        if x1 - x0 < 3 or y1 - y0 < 3:
            return
        st["out"] = (int(x0 / scale), int(y0 / scale),
                     int((x1 - x0) / scale), int((y1 - y0) / scale))
        root.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
    return st["out"]


def one_frame(hwnd, settle=0.4):
    cap = WindowCapture(hwnd, update_ms=100).start()
    time.sleep(settle)
    frame, _ = cap.frame()
    cap.stop()
    return frame


# --------------------------------------------------------------- commands ---

def cmd_windows(args):
    hits = list_windows(args.filter)
    if not hits:
        print("No matching windows.")
        return
    for h in hits:
        flag = "   [MINIMISED - cannot be captured]" if h["minimized"] else ""
        print(f"  {h['width']}x{h['height']}  hwnd={h['hwnd']}  {h['title']!r}{flag}")


PANELS = [
    {"kind": "overview", "title": ["overview"], "mode": "roster",
     "headers": ["distance", "name", "type", "corporation", "alliance", "velocity"],
     "id_upto": "corporation", "id_from": "name",
     # A wormhole entering the overview is almost always you warping to it,
     # not a hole opening. A hole that actually opens shows up as a new
     # signature id, which is announced there and is the reliable signal.
     "say": "new contact in {label}",
     "ignore": ["Sun", "Fortizar", "Wormhole", "Scanner Probe"]},
    {"kind": "sigs", "title": ["probe", "scanner"], "mode": "roster",
     "headers": ["distance", "id", "name", "group", "signal"],
     "id_upto": "name", "id_from": "id", "id_column": "id",
     # Signal strength counts up while probes resolve, so it churns every label
     # it appears in - and it says nothing about WHICH signature the row is.
     "label_upto": "signal",
     "say": "new signature on the probe scanner",
     # the panel footer sits below the list and would otherwise be tracked as a row
     "ignore": ["launched", "No Results"]},
    {"kind": "dscan", "title": ["directional", "scanner"], "mode": "dscan",
     "headers": ["distance", "name", "type"],
     "id_upto": None, "id_from": "name",
     "say": "d-scan updated", "ignore": ["No Scan Results"]},
]


HEADER_NAMES = ["distance", "name", "type", "corporation", "alliance",
                "velocity", "id", "group", "signal"]


def classify_headers(found):
    """Which kind of panel a set of column headers belongs to."""
    if found & {"id", "group", "signal"}:
        return "sigs"
    if found & {"corporation", "alliance", "velocity"}:
        return "overview"
    if {"name", "type"} <= found:
        return "dscan"
    return None


def find_panel_rects(frame):
    """Rectangles of EVE's panels, found by their flat backgrounds.

    A panel occludes the starfield, so its empty area is almost perfectly uniform
    (std ~0.3) while open space varies with stars and nebula (std 1.5-4.7).
    Panels sit edge to edge, so the closing kernel has to stay small - a large one
    bridges the border between neighbours and merges them into a single blob.
    """
    g = to_gray(frame).astype(np.float32)
    mean = cv2.blur(g, (9, 9))
    sq = cv2.blur(g * g, (9, 9))
    std = np.sqrt(np.maximum(sq - mean * mean, 0))
    flat = ((std < 1.2) & (g < 60)).astype(np.uint8) * 255
    flat = cv2.morphologyEx(flat, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    flat = cv2.morphologyEx(flat, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    _n, _lab, stats, _c = cv2.connectedComponentsWithStats(flat, 8)
    out = []
    for st in stats[1:]:
        x, y, w, h, area = st[0], st[1], st[2], st[3], st[4]
        if w >= 150 and h >= 70 and area >= 18000:
            # OpenCV hands back numpy int32; these end up in the saved config,
            # where json refuses them and the write dies half-finished.
            out.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h)})
    return out


def find_panels_anywhere(frame, words):
    """Panels found by rectangle, then identified by the headers inside them.

    Independent of the window title, which EVE lets you rename and which compact
    layouts drop altogether.
    """
    found = []
    for rect in find_panel_rects(frame):
        # The header can sit just above the flat interior - a panel's control
        # strip is textured, so the uniform region often starts below it.
        top = rect["y"] - 110
        inside = [w for w in words
                  if rect["x"] - 8 <= w["x"] < rect["x"] + rect["w"] + 8
                  and top <= w["y"] < rect["y"] + rect["h"]]
        if not inside:
            continue
        line_h = max(6, round(statistics.median([w["h"] for w in inside])))
        lines = {}
        for w in sorted(inside, key=lambda w: w["y"]):
            key = next((k for k in lines if abs(k - w["y"]) <= line_h * 0.9), w["y"])
            lines.setdefault(key, []).append(w)
        for y in sorted(lines):
            names = {n for n in HEADER_NAMES
                     if any(looks_like(w["text"], n) for w in lines[y])}
            kind = classify_headers(names)
            if kind and len(names) >= 2:
                spec = next(p for p in PANELS if p["kind"] == kind)
                xs = sorted(lines[y], key=lambda w: w["x"])
                found.append({"spec": spec, "rect": rect, "headers": names,
                              "title_x": rect["x"] + 2, "title_y": y,
                              "title_h": line_h,
                              "title_end": min(xs[-1]["x"] + xs[-1]["w"],
                                               rect["x"] + rect["w"] - 4)})
                break
    return found


def find_panels(words):
    """Locate each known panel by its title text. Returns one entry per panel."""
    found = []
    for spec in PANELS:
        first = spec["title"][0]
        for w in words:
            if not looks_like(w["text"], first):
                continue
            # multi-word titles must have the rest following on the same line
            ok, cursor = True, w
            for nxt in spec["title"][1:]:
                cand = [z for z in words
                        if abs(z["y"] - w["y"]) <= w["h"]
                        and 0 < z["x"] - (cursor["x"] + cursor["w"]) < 30
                        and looks_like(z["text"], nxt)]
                if not cand:
                    ok = False
                    break
                cursor = cand[0]
            if ok:
                found.append({"spec": spec, "title_x": w["x"], "title_y": w["y"],
                              "title_h": w["h"], "title_end": cursor["x"] + cursor["w"]})
    return found


def text_bands(frame, left, width, top, height, threshold):
    """Vertical runs of text in a list area, as (start_row, end_row).

    Row spacing measured from OCR word positions is unreliable: words within one
    rendered row sit at slightly different heights, so a single row can look like
    two lines six pixels apart and the derived pitch comes out short. The lit
    pixels themselves have no such jitter.
    """
    if width < 4 or height < 4:
        return []
    mask = text_mask(crop(frame, {"left": left, "top": top,
                                  "width": width, "height": height}), threshold)
    profile = mask.sum(axis=1) > 2
    out, start = [], None
    for i, on in enumerate(profile):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= 3:
                out.append((top + start, top + i))
            start = None
    if start is not None and len(profile) - start >= 3:
        out.append((top + start, top + len(profile)))
    return out


def panel_bounds(panel, panels, frame_w, frame_h):
    """The screen area this panel owns, stopping at its neighbours.

    Panels sit side by side with their column headers on the same line, so a
    search window that is merely "near the title" swallows the neighbour's
    columns and places the box on the wrong panel entirely.
    """
    x_lo = panel["title_x"] - 20
    rights = [q["title_x"] for q in panels
              if q is not panel and q["title_x"] > panel["title_x"] + 40
              and abs(q["title_y"] - panel["title_y"]) < 40]
    x_hi = (min(rights) - 10) if rights else frame_w - 4

    y_lo = panel["title_y"]
    belows = [q["title_y"] for q in panels
              if q is not panel and q["title_y"] > panel["title_y"] + 60
              and x_lo - 40 < q["title_x"] < x_hi]
    y_hi = (min(belows) - 10) if belows else frame_h - 8
    return x_lo, x_hi, y_lo, y_hi


def panel_geometry(panel, words, bounds, frame=None, threshold=110):
    """Work out the header row, columns and row pitch of one panel."""
    spec = panel["spec"]
    x_lo, x_hi, y_lo, y_hi = bounds
    below = [w for w in words
             if y_lo < w["y"] < min(y_hi, y_lo + 170) and x_lo <= w["x"] < x_hi]

    # the header row is the first line under the title holding known header words
    # Cluster by a tolerance derived from glyph height. A fixed bucket splits one
    # rendered line into two - the header came back as "Type Corporation Alliance"
    # and "Name" separately - and the stray half then looks like the first data row.
    line_h = max(6, round(statistics.median([w["h"] for w in below]))) if below else 10
    rows = {}
    for w in sorted(below, key=lambda w: w["y"]):
        key = next((k for k in rows if abs(k - w["y"]) <= line_h * 0.9), w["y"])
        rows.setdefault(key, []).append(w)
    header_y, header = None, {}
    for y in sorted(rows):
        hits = {}
        for w in rows[y]:
            for name in spec["headers"]:
                if name not in hits and looks_like(w["text"], name):
                    hits[name] = w
        if len(hits) >= 2:
            header_y, header = y, hits
            break
    if not header:
        return {"error": "no column headers visible - the list is empty, so EVE "
                         "is not drawing them. Put something in it and re-run."}

    # data rows start a full line below the header, never within it
    head_h = max(w["h"] for w in header.values())
    floor_y = header_y + head_h + 3
    data_ys = sorted({round(w["y"]) for w in below if w["y"] >= floor_y})
    merged = []
    for y in data_ys:
        if not merged or y - merged[-1] > 4:
            merged.append(y)
    samples = 0
    gaps = [b - a for a, b in zip(merged, merged[1:]) if 8 <= b - a <= 60]
    pitch = round(statistics.median(gaps)) if gaps else None
    measured = pitch is not None
    if pitch is None and merged:
        # one row only: the header-to-row gap is within a pixel of the true pitch,
        # whereas guessing from glyph height is not close enough to align slots
        pitch = max(16, merged[0] - header_y)
    if pitch is None:
        pitch = max(16, round(panel["title_h"] * 2.1))

    first_row = merged[0] if merged else header_y + pitch
    left_col = header.get(spec["id_from"])
    if left_col is None:
        # the id column header is often unreadable; take the column just right
        # of Distance rather than falling back to Distance itself, which churns
        ordered = sorted(header.values(), key=lambda w: w["x"])
        if "distance" in header:
            d = header["distance"]
            left_col = {"x": d["x"] + d["w"] + 15, "w": 0}
        else:
            left_col = ordered[0]
    right_edge = max(min(w["x"] + w["w"], x_hi) for w in header.values())

    box_left = left_col["x"] - 7
    key_width = None
    if spec["id_upto"] and spec["id_upto"] in header:
        key_width = header[spec["id_upto"]]["x"] - box_left - 4
    if key_width is not None and key_width < 20:
        key_width = None

    label_width = None
    if spec.get("label_upto") and spec["label_upto"] in header:
        label_width = header[spec["label_upto"]]["x"] - box_left - 4
    if label_width is not None and label_width < 20:
        label_width = None

    text_h = round(statistics.median([w["h"] for w in header.values()]))
    # Prefer pixel-measured spacing; OCR word jitter makes the gaps unreliable.
    if frame is not None:
        bands = text_bands(frame, box_left, max(4, right_edge - box_left),
                           floor_y, min(320, max(8, y_hi - floor_y)), threshold)
        if bands:
            first_row = bands[0][0]
        if len(bands) >= 2:
            centres = [(a + b) / 2 for a, b in bands]
            band_gaps = [round(y - x) for x, y in zip(centres, centres[1:])
                         if 8 <= y - x <= 60]
            samples = len(band_gaps)
            if band_gaps:
                pitch = round(statistics.median(band_gaps))
                # One gap is one sample: a two-row list gave 19 where the rest of
                # the client measured 20, and being a pixel out compounds down the
                # list. Treat it as weak so it is reported rather than trusted.
                measured = len(band_gaps) >= 2

    # Only borrow when this panel showed no spacing at all. A weak local sample
    # still describes THIS layout; a confident value from a client using a
    # different one (compact rows are 20px where normal are 24) does not.
    if frame is not None and samples:
        borrowable = False
    else:
        borrowable = True

    if pitch < head_h + 5:
        return {"error": f"row spacing came out implausibly small ({pitch}px for "
                         f"{head_h}px text) - the header and first row were "
                         f"probably confused. Put a couple of rows in the list "
                         f"and re-run."}
    if first_row < floor_y:
        first_row = floor_y

    cols = {k: v["x"] for k, v in header.items()}
    # The probe scanner's first column carries no header - the row above reads
    # "Name", "Group", "Signal" and nothing over the ids - so nothing recorded
    # where the ids begin, and they were filed under whichever column happened
    # to sit leftmost. The box starts at that column by construction.
    if spec.get("id_column") and spec["id_column"] not in cols:
        cols[spec["id_column"]] = box_left

    return {"header_y": header_y, "first_row": first_row, "pitch": pitch,
            "text_h": text_h, "borrowable": borrowable,
            "measured_pitch": measured, "box_left": box_left,
            "box_right": min(x_hi, right_edge + 6), "key_width": key_width,
            "label_width": label_width,
            "columns": cols}


def known_pitch(cfg, kind, win_w, win_h, text_h=None, window=None):
    """A row pitch already measured for this kind of panel at this window size.

    A list with fewer than two rows cannot reveal its own spacing, but the same
    panel on another client at the same UI scale can - and that is exact, where
    guessing from the header gap is a pixel or two out and drifts down the list.
    """
    candidates = [r for r in cfg.get("regions", [])
                  if r.get("row_pitch") and r.get("win_width") == win_w
                  and r.get("win_height") == win_h
                  and re.sub(r"\d+$", "", r["name"]) == kind]
    # Window size does not change when EVE's UI scale does, so matching on it
    # alone would happily borrow a pitch from a client still configured at the
    # old scale. Glyph height tracks the scale: prefer a donor that agrees, and
    # only fall back to one of unknown scale when there is no better option.
    if text_h:
        for r in candidates:
            if r.get("text_h") and abs(r["text_h"] - text_h) <= 1:
                return r["row_pitch"]
        if any(r.get("text_h") for r in candidates):
            return None                 # a known-scale donor exists and disagrees
    for r in candidates:
        if not r.get("text_h"):
            return r["row_pitch"]

    # Failing a donor of the same kind, take one from ANY list on this client.
    # Row spacing follows the UI scale, which is a property of the client and
    # not of the panel - measured on one client, an overview and a probe
    # scanner both came to exactly 22. Restricting the donor by kind meant an
    # overview calibrated on an empty grid guessed 18, 20 or 24 while the
    # scanner beside it had the answer, and being a couple of pixels out
    # compounds down the list until the rows stop lining up at all.
    if window and text_h:
        for r in cfg.get("regions", []):
            if (r.get("window") == window and r.get("row_pitch")
                    and r.get("text_h") and abs(r["text_h"] - text_h) <= 1):
                return r["row_pitch"]
    return None


def cmd_add_panel(args):
    """Calibrate one panel the user points at, whatever its title says.

    Automatic discovery keys on the panel title, which EVE lets you rename, and
    on column headers, which are ambiguous between adjacent panels. When it
    cannot find a panel, dragging a box around it is unambiguous - everything
    inside (columns, row spacing, anchor) is then derived exactly as usual.
    """
    win = resolve_window(args.client)
    frame = one_frame(win["hwnd"], 0.5)
    cfg = load_config()
    s = cfg["settings"]

    spec = next((p for p in PANELS if p["kind"] == args.kind), None)
    if spec is None:
        sys.exit(f"Unknown panel kind {args.kind!r}")

    picked = drag_box(to_image(frame),
                      f"Drag a box around the whole {args.kind} panel - include its "
                      f"column headers and the rows below them.")
    if not picked:
        sys.exit("Cancelled.")
    px, py, pw, ph = picked
    bounds = (px, px + pw, py, py + ph)

    fake = {"spec": spec, "title_x": px, "title_y": py, "title_h": 12,
            "title_end": px + 60}
    words = ocr_words(to_image(crop(frame, {"left": px, "top": py,
                                            "width": pw, "height": ph})), 2, (px, py))
    if not words:
        sys.exit("No text found in that box - is the panel visible?")
    geo = panel_geometry(fake, words, bounds, frame, s["threshold"])
    if geo is None or "error" in geo:
        sys.exit(f"Could not read it: {(geo or {}).get('error', 'no column headers')}")
    geo["box_right"] = min(geo["box_right"], px + pw)
    geo["box_bottom"] = min(py + ph, geo["first_row"] + 520)

    name = args.name or args.kind
    top = geo["first_row"] - 8
    anchor = {"left": px + 2, "top": geo["header_y"] - 4,
              "width": min(140, pw - 4), "height": (geo.get("text_h") or 11) + 8}
    region = {
        "name": name, "window": win["title"], "mode": spec["mode"],
        "win_width": frame.shape[1], "win_height": frame.shape[0],
        "target": {"left": geo["box_left"], "top": top,
                   "width": max(60, geo["box_right"] - geo["box_left"]),
                   "height": max(2 * geo["pitch"], geo["box_bottom"] - top)},
        "anchor": anchor, "key_width": geo["key_width"], "max_drift": 200,
        "label_width": geo.get("label_width"),
        "columns": {k: int(x) - geo["box_left"]
                    for k, x in (geo.get("columns") or {}).items()},
        "ignore": list(spec["ignore"]), "zoom": True,
        "alert": spec["mode"] != "dscan", "text_h": geo.get("text_h"),
        "say": spec["say"].format(label=name),
    }
    if spec["mode"] == "roster":
        region.update(identity="pixels", row_pitch=geo["pitch"],
                      row_height=max(8, geo["pitch"] - 2), row_offset=8,
                      pix_ncc=0.90, pix_min_lit=20)

    print(f"\n{name}: box {region['target']['width']}x{region['target']['height']} "
          f"at ({region['target']['left']},{top})  pitch {geo['pitch']}"
          f"{'' if geo['measured_pitch'] else ' (GUESSED)'}  "
          f"key_width {geo['key_width']}")
    rows = ocr_rows(to_image(crop(frame, region["target"])), s["ocr_scale"],
                    region.get("key_width"))
    print("reads:")
    for t in [r["text"] for r in rows[:6]] or ["(empty)"]:
        print(f"     {t}")
    if not args.yes:
        print("\nRe-run with --yes to save it.")
        return
    Image.fromarray(to_gray(crop(frame, anchor))).save(
        os.path.join(HERE, f"anchor_{slug(win['title'])}_{name}.png"))
    cfg["regions"] = [r for r in cfg["regions"]
                      if not (r.get("window") == win["title"] and r["name"] == name)]
    cfg["regions"].append(region)
    save_config(cfg)
    print(f"\nSaved {name!r}. Check it with:  eve_watch.py shot --client "
          f"{short_client(win['title'])!r}")


def cmd_calibrate(args):
    """Build this client's regions by reading its panels off the screen.

    Coordinates are never portable - they are pixel geometry tied to one UI
    scale, resolution and window layout. Rather than ship a config, each install
    derives its own: find every panel by its title, read the column headers to
    place the box, measure the row pitch from real rows, and cut anchors from
    this screen.
    """
    win = resolve_window(args.client)
    frame = one_frame(win["hwnd"], 0.5)
    fh, fw = frame.shape[0], frame.shape[1]
    cfg = load_config()
    s = cfg["settings"]

    log(f"calibrating {win['title']!r} ({fw}x{fh})")
    words = ocr_words(to_image(frame), 2)
    if not words:
        sys.exit("OCR returned nothing - is the client rendering?")
    # Rectangle-first: a panel is a flat region that occludes the starfield, and
    # the column headers inside say what it is. Titles are optional - EVE lets you
    # rename windows, and a compacted layout has no title bar at all.
    panels = find_panels_anywhere(frame, words)
    if not panels:
        panels = find_panels(words)          # fall back to titles
    if not panels:
        sys.exit("Found no panels. If your windows are compacted, make sure each "
                 "list shows its column headers, or use add-panel to point at one.")

    # number repeats: overview, overview2, ...
    seen, proposals = {}, []
    for p in sorted(panels, key=lambda p: (p["spec"]["kind"], p["title_x"])):
        kind = p["spec"]["kind"]
        seen[kind] = seen.get(kind, 0) + 1
        p["label"] = kind if seen[kind] == 1 else f"{kind}{seen[kind]}"
        proposals.append(p)

    # a panel may only claim space up to the next panel to its right / below
    for p in proposals:
        if p.get("rect"):
            r0 = p["rect"]
            x_lo, x_hi = r0["x"] - 8, r0["x"] + r0["w"] + 8
            y_lo, y_hi = min(p["title_y"] - 4, r0["y"]), r0["y"] + r0["h"]
        else:
            x_lo, x_hi, y_lo, y_hi = panel_bounds(p, proposals, fw, fh)
        geo = panel_geometry(p, words, (x_lo, x_hi, y_lo, y_hi), frame,
                             s["threshold"])
        if (geo and "error" not in geo and not geo["measured_pitch"]
                and geo.get("borrowable", True)):
            borrowed = known_pitch(cfg, p["spec"]["kind"], fw, fh,
                                   text_h=geo.get("text_h"),
                                   window=win["title"])
            if borrowed:
                geo["pitch"] = borrowed
                geo["borrowed_pitch"] = True
        if geo is None or "error" in geo:
            p["geo"] = None
            p["error"] = (geo or {}).get("error", "could not read its columns")
            continue
        geo["box_right"] = min(geo["box_right"], x_hi)
        geo["box_bottom"] = min(y_hi, geo["first_row"] + 520)
        p["geo"] = geo

    regions, report = [], []
    for p in proposals:
        geo, spec = p["geo"], p["spec"]
        if geo is None:
            report.append((p["label"], "SKIPPED - " + p.get("error", "unknown")))
            continue
        anchor = {"left": p["title_x"] - 3, "top": p["title_y"] - 4,
                  "width": (p["title_end"] - p["title_x"]) + 14,
                  "height": p["title_h"] + 8}
        # never let a region reach a lookalike panel: half the gap to the nearest
        # same-kind title, capped
        same = [abs(q["title_x"] - p["title_x"]) + abs(q["title_y"] - p["title_y"])
                for q in proposals if q is not p and q["spec"]["kind"] == spec["kind"]]
        drift = max(40, min(200, (min(same) // 2) - 10)) if same else 200
        top = geo["first_row"] - 8
        region = {
            "name": p["label"], "window": win["title"], "mode": spec["mode"],
            "win_width": fw, "win_height": fh,
            "target": {"left": geo["box_left"], "top": top,
                       "width": max(60, geo["box_right"] - geo["box_left"]),
                       "height": max(2 * geo["pitch"], geo["box_bottom"] - top)},
            # Signature ids are the one list with a fixed row shape, so a label
            # that cannot be one is not a row of this list. Lower-case because
            # row_key case-folds.
            **({"require": r"^[a-z]{3}-\d{3}"}
               if spec["mode"] == "roster" and p["label"].startswith("sigs")
               else {}),
            # Column offsets, so a row can be split into fields instead of
            # guessed at by whitespace: pilot names contain spaces ("Taron
            # Badasaz Buzzard 29" is a two-word name, a ship and a speed) and
            # no amount of splitting tells you where the name ends.
            "columns": {k: int(x) - geo["box_left"]
                        for k, x in (geo.get("columns") or {}).items()},
            "anchor": anchor, "key_width": geo["key_width"],
            "label_width": geo.get("label_width"),
            "max_drift": drift, "ignore": list(spec["ignore"]),
            "zoom": True, "alert": spec["mode"] != "dscan",
            "say": spec["say"].format(label=p["label"]),
        }
        region["text_h"] = geo.get("text_h")
        if spec["mode"] == "roster":
            region.update(identity="pixels", row_pitch=geo["pitch"],
                          row_height=max(8, geo["pitch"] - 2), row_offset=8,
                          # 0.95 was far tighter than the data supports. Two
                          # different rows reach 0.64 on a probe scanner and
                          # about 0.71 on an overview, while a row that is
                          # merely hovered or selected scores 0.77-0.94 - so a
                          # bar of 0.95 called those departures. Signature rows
                          # have the id as a second opinion below the bar;
                          # overview rows have none, so they keep more room.
                          pix_ncc=0.80 if p["spec"]["kind"] == "sigs" else 0.85,
                          pix_min_lit=20)
        regions.append(region)
        report.append((p["label"],
                       f"box {region['target']['width']}x{region['target']['height']} "
                       f"at ({region['target']['left']},{top})  "
                       f"pitch {geo['pitch']}"
                       f"{'' if geo['measured_pitch'] else (' (from another client)' if geo.get('borrowed_pitch') else ' (ASSUMED)')}  "
                       f"key_width {geo['key_width']}  max_drift {drift}"))

    print(f"\nFound {len(regions)} panel(s) in {win['title']!r}:\n")
    for label, line in report:
        print(f"   {label:12s} {line}")
    for r in regions:
        box = crop(frame, r["target"])
        rows = [x["text"] for x in ocr_rows(to_image(box), s["ocr_scale"],
                                            r.get("key_width"))]
        print(f"\n   {r['name']} currently reads:")
        for t in rows[:6] or ["(empty)"]:
            print(f"        {t}")

    if not any(g for g in (p["geo"] for p in proposals)):
        sys.exit("\nNothing usable found.")
    solid = {p["label"] for p in proposals if p["geo"]
             and (p["geo"].get("measured_pitch") or p["geo"].get("borrowed_pitch"))}
    guessed = [r["name"] for r in regions
               if r.get("row_pitch") and r["name"] not in solid]
    if guessed:
        print(f"\n  !! ROW SPACING GUESSED for {', '.join(guessed)} - those lists")
        print("     hold fewer than two rows, so the spacing could not be measured")
        print("     and rows further down may drift out of alignment.")
        print("     Re-run calibrate once each list has at least two entries.")

    if not args.yes:
        print("\nRe-run with --yes to write these regions "
              "(existing regions for this client are replaced).")
        return

    for r in regions:
        Image.fromarray(to_gray(crop(frame, r["anchor"]))).save(
            os.path.join(HERE, f"anchor_{slug(win['title'])}_{r['name']}.png"))
    # Replace every calibratable region for this client, not just the ones found
    # now: recalibrating with one fewer overview must drop the leftover, or it
    # sits in the config forever reporting its anchor lost.
    # ...but only for panel kinds that actually calibrated. A skipped panel (an
    # empty d-scan draws no column headers) must not take its working region with
    # it - that would delete configuration for something merely not visible now.
    found_kinds = {re.sub(r"\d+$", "", r["name"]) for r in regions}
    def replaceable(name):
        return re.sub(r"\d+$", "", name) in found_kinds
    dropped = [r["name"] for r in cfg["regions"]
               if r.get("window") == win["title"] and replaceable(r["name"])
               and r["name"] not in {x["name"] for x in regions}]
    keep = [r for r in cfg["regions"]
            if r.get("window") != win["title"] or not replaceable(r["name"])]
    cfg["regions"] = keep + regions
    if dropped:
        print(f"Removed {len(dropped)} region(s) no longer present: "
              f"{', '.join(sorted(dropped))}")
    save_config(cfg)
    print(f"\nWrote {len(regions)} region(s). Check them with:")
    print(f"    eve_watch.py shot --client {short_client(win['title'])!r}")
    print("Anything reporting LOST needs its panel visible, or a manual select.")


def cmd_select(args):
    win = resolve_window(args.client)
    if win["minimized"]:
        sys.exit("That window is minimised - restore it first. It may sit behind "
                 "other windows, it just may not be minimised.")
    frame = one_frame(win["hwnd"])
    os.makedirs(BASE_SHOTS, exist_ok=True)
    full = to_image(frame)
    presence = args.mode == "presence"
    roster = args.mode == "roster"

    if roster:
        step1 = ("Step 1/3  -  drag a rough box around the WHOLE overview panel, "
                 "headers included. You will pick the exact rows next.")
    elif presence:
        step1 = ("Step 1/3  -  drag a rough box around the LIST you want to watch, "
                 "plus a steady label near it.")
    else:
        step1 = ("Step 1/3  -  drag a rough box around the number AND some steady "
                 "text next to it. You will refine next.")
    rough = drag_box(full, step1)
    if not rough:
        sys.exit("Cancelled.")
    rl, rt, rw, rh = rough

    zoom = max(1, min(10, 1400 // max(1, rw)))
    sub = full.crop((rl, rt, rl + rw, rt + rh)).resize((rw * zoom, rh * zoom), Image.LANCZOS)

    if roster:
        step2 = ("Step 2/3  -  box the ROWS AREA: from just below the column "
                 "headers to the bottom of the list. Start just LEFT of the Name "
                 "column so the Distance column is EXCLUDED - it changes constantly.")
    elif presence:
        step2 = ("Step 2/3  -  box the EMPTY LIST AREA where a new row would appear. "
                 "Leave out the header and any row that is always there.")
    else:
        step2 = ("Step 2/3  -  box THE NUMBER tightly. Include only what should "
                 "trigger the alert - no timers, no distances.")
    fine = drag_box(sub, step2)
    if not fine:
        sys.exit("Cancelled.")
    target = {"left": int(rl + fine[0] / zoom), "top": int(rt + fine[1] / zoom),
              "width": max(3, int(fine[2] / zoom)), "height": max(3, int(fine[3] / zoom))}

    anc = drag_box(sub, "Step 3/3  -  box an ANCHOR: nearby text that never "
                        "changes. The watcher follows it if things move. Skip "
                        "this for docked panels that never move.", optional=True)

    region = {"name": args.name, "window": win["title"], "mode": args.mode,
              "win_width": frame.shape[1], "win_height": frame.shape[0],
              "target": target, "anchor": None, "key_width": args.key_width,
              "zoom": args.zoom, "alert": args.alert,
              "say": args.say or (f"new contact in {args.name}"
                                  if presence or roster
                                  else f"{args.name} changed")}

    if anc:
        anchor = {"left": int(rl + anc[0] / zoom), "top": int(rt + anc[1] / zoom),
                  "width": max(6, int(anc[2] / zoom)), "height": max(6, int(anc[3] / zoom))}
        region["anchor"] = anchor
        Image.fromarray(to_gray(crop(frame, anchor))).save(anchor_path(args.name, win["title"]))

    cfg = load_config()
    cfg["regions"] = [r for r in cfg["regions"] if r["name"] != args.name]
    cfg["regions"].append(region)
    save_config(cfg)

    patch = crop(frame, target)
    p = save_preview(patch, cfg["settings"]["threshold"],
                     os.path.join(BASE_SHOTS, f"preview_{args.name}.png"))
    lit = int(text_mask(patch, cfg["settings"]["threshold"]).sum())
    print(f"Saved {args.name!r} (mode={args.mode}): {target['width']}x{target['height']} "
          f"at ({target['left']}, {target['top']}) in {win['title']!r}")
    print(f"Anchor: {'yes' if anc else 'none (fixed position)'}   "
          f"Says: {region['say']!r}")
    print(f"Lit pixels right now: {lit}")
    if presence:
        print("  For presence mode this should be ~0 while the list is empty.")
    print(f"Preview (top = pixels, bottom = what is compared): {p}")
    if roster:
        rows = ocr_rows(to_image(patch), cfg["settings"]["ocr_scale"],
                        region.get("key_width"))
        print(f"\nOCR reads {len(rows)} row(s) right now - these become the baseline:")
        for row in rows:
            mark = "(ignored) " if ignored(row["text"], cfg["settings"]) else ""
            print(f"    {mark}{row['text']}")
            print(f"        identity: {row['key']!r}")
        print("If a distance or a timer shows up above, re-run select and start the "
              "box further right.")


def cmd_shot(args):
    cfg = load_config()
    regions = pick_regions(cfg, args.name)
    win = resolve_window(args.client or regions[0]["window"])
    regions = regions_for(regions, win["title"])
    if not regions:
        sys.exit(f"No regions configured for {win['title']!r}")
    frame = one_frame(win["hwnd"], 0.3)
    os.makedirs(BASE_SHOTS, exist_ok=True)
    for r in regions:
        tr = Tracker(r, cfg["settings"])
        box = tr.locate(frame)
        if box is None:
            print(f"{r['name']}: ANCHOR LOST - not found in the current frame.")
            continue
        patch = crop(frame, box)
        p = save_preview(patch, cfg["settings"]["threshold"],
                         os.path.join(BASE_SHOTS, f"preview_{r['name']}.png"))
        lit = int(text_mask(patch, cfg["settings"]["threshold"]).sum())
        where = f" anchor@{tuple(tr.anchor_pos)}" if tr.tracking else ""
        print(f"{r['name']} [{r.get('mode', 'change')}]: lit={lit}{where}  {p}")


def cmd_list(args):
    print(json.dumps(load_config(), indent=2))


def cmd_learn(args):
    """Teach the watcher what the region looks like at a known value."""
    cfg = load_config()
    regions = pick_regions(cfg, args.name)
    if len(regions) > 1:
        sys.exit(f"Say which region: --name {[r['name'] for r in regions]}")
    reg = regions[0]
    win = resolve_window(args.client or reg["window"])
    frame = one_frame(win["hwnd"], 0.3)
    tr = Tracker(reg, cfg["settings"])
    box = tr.locate(frame)
    if box is None:
        sys.exit("Anchor lost - cannot see the region right now.")
    use_ncc = reg.get("match") == "ncc"
    patch = crop(frame, box)
    clip = reg.get("clip", cfg["settings"]["clip"])
    sig = (apply_clip(to_gray(patch), clip) if use_ncc
           else text_mask(patch, cfg["settings"]["threshold"]))
    os.makedirs(value_dir(reg["name"], reg.get("window")), exist_ok=True)
    path = os.path.join(value_dir(reg["name"], reg.get("window")), f"{args.value}.png")
    Image.fromarray(sig if use_ncc else
                    np.where(sig, 255, 0).astype(np.uint8)).save(path)
    print(f"Learned {reg['name']} = {args.value!r} -> {path}")

    learned = {k: apply_clip(v, clip) if use_ncc else v
               for k, v in load_values(reg["name"], use_ncc, reg.get("window")).items()}
    tol = reg.get("sensitivity", cfg["settings"]["sensitivity"])
    jit = reg.get("jitter", cfg["settings"]["jitter"])
    nmin = reg.get("ncc_min", cfg["settings"]["ncc_min"])
    hit, score = classify(sig, learned, tol, jit, use_ncc, nmin)
    print(f"Knows {len(learned)} value(s): {sorted(learned)}")
    print(f"Reads right now as: {hit!r} ({'corr' if use_ncc else 'diff'} {score})")
    for k, v in learned.items():
        if k == str(args.value):
            continue
        if use_ncc:
            print(f"   vs {k!r}: corr {ncc(sig, v):.4f}  "
                  f"(must fall below {nmin} to be told apart)")
        elif v.shape == sig.shape:
            print(f"   vs {k!r}: {shifted_diff(sig, v, jit)} px apart "
                  f"(must exceed tol {tol})")


def short_client(title):
    """'EVE - Your Character' -> 'Your Character'."""
    return title.split(" - ", 1)[1].strip() if " - " in title else title


def configured_clients(cfg):
    return sorted({r["window"] for r in cfg.get("regions", []) if r.get("window")})


def read_clients():
    try:
        with open(CLIENTSFILE, "r", encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()
                    and not ln.startswith("#")]
    except OSError:
        return []


def write_clients(names):
    with open(CLIENTSFILE, "w", encoding="utf-8") as fh:
        fh.write("# one EVE window title per line; the supervisor watches these\n")
        for n in names:
            fh.write(n + "\n")


def cmd_clients(args):
    cfg = load_config()
    known = configured_clients(cfg)
    live = {h["title"] for h in list_windows("EVE - ")}
    current = read_clients()

    if args.action == "list":
        print("configured (have regions):")
        for c in known:
            marks = []
            if c in current:
                marks.append("MONITORED")
            if c in live:
                marks.append("running")
            names = [r["name"] for r in cfg["regions"] if r.get("window") == c]
            print(f"   {c:30s} {' '.join(marks):18s} regions: {', '.join(sorted(names))}")
        extra = sorted(live - set(known))
        if extra:
            print("\nrunning but no regions configured yet:")
            for c in extra:
                print(f"   {c}")
        return

    picked = []
    for want in args.names:
        hit = [c for c in set(known) | live if want.lower() in c.lower()]
        if not hit:
            sys.exit(f"No client matches {want!r}. Try:  eve_watch.py clients list")
        if len(hit) > 1:
            sys.exit(f"{want!r} matches {hit} - be more specific")
        picked.append(hit[0])

    if args.action == "add":
        new = current + [c for c in picked if c not in current]
    elif args.action == "remove":
        new = [c for c in current if c not in picked]
    else:
        new = picked
    write_clients(new)
    print("now monitoring:" if new else "monitoring nothing:")
    for c in new:
        print(f"   {c}{'' if c in known else '   (no regions configured yet!)'}")
    print("\nA running supervisor picks this up within a few seconds.")


def cmd_clone(args):
    """Copy one client's regions (and their anchors/values) to another client.

    Panel layouts are usually identical between your own clients, so cloning
    beats re-dragging every box. Always check the result with `shot --client
    <dst>` before trusting it - if that character arranges windows differently
    the coordinates will be wrong and the anchors will simply fail to lock.
    """
    import shutil

    cfg = load_config()
    src_regions = [r for r in cfg["regions"]
                   if args.src.lower() in (r.get("window") or "").lower()]
    if not src_regions:
        sys.exit(f"No configured regions matching {args.src!r}. "
                 f"Have: {configured_clients(cfg)}")
    src_window = src_regions[0]["window"]

    hits = list_windows(args.dst)
    if len(hits) > 1:
        sys.exit(f"{args.dst!r} matches {[h['title'] for h in hits]} - be specific")
    if hits:
        dst_window = hits[0]["title"]
        offline = False
    else:
        # Let a scout be set up before it is logged in; the title is predictable.
        dst_window = (args.dst if args.dst.lower().startswith("eve - ")
                      else f"EVE - {args.dst}")
        offline = True
    if dst_window == src_window:
        sys.exit("Source and destination are the same client.")

    wanted = {n.strip() for n in args.only.split(",")} if args.only else None
    copied = []
    for r in src_regions:
        if wanted and r["name"] not in wanted:
            continue
        new = json.loads(json.dumps(r))
        new["window"] = dst_window
        cfg["regions"] = [x for x in cfg["regions"]
                          if not (x["name"] == new["name"]
                                  and x.get("window") == dst_window)]
        cfg["regions"].append(new)

        if r.get("anchor"):
            src_a = anchor_path(r["name"], src_window)
            dst_a = os.path.join(HERE, f"anchor_{slug(dst_window)}_{r['name']}.png")
            if os.path.exists(src_a):
                shutil.copyfile(src_a, dst_a)

        src_v = value_dir(r["name"], src_window)
        if os.path.isdir(src_v):
            dst_v = os.path.join(VALUES, slug(dst_window), r["name"])
            os.makedirs(dst_v, exist_ok=True)
            for f in os.listdir(src_v):
                if f.lower().endswith(".png"):
                    shutil.copyfile(os.path.join(src_v, f),
                                    os.path.join(dst_v, f))
        copied.append(r["name"])

    save_config(cfg)
    print(f"Cloned {copied} from {src_window!r} to {dst_window!r}")
    if offline:
        print(f"NOTE: {dst_window!r} is not running, so the title is assumed and "
              f"nothing could be checked against its screen.")
    print(f"\nVERIFY BEFORE RELYING ON IT:")
    print(f"    eve_watch.py shot --client {short_client(dst_window)!r}")
    print("Every anchor must report a match; if one is LOST that panel sits "
          "somewhere else on this character and needs its own select.")


def _capture_output(fn, ns):
    """Run a command, capture what it printed, and say whether it succeeded."""
    import contextlib, io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
        return True, buf.getvalue()
    except SystemExit as exc:
        return False, buf.getvalue() + f"\n{exc}"
    except Exception as exc:
        return False, buf.getvalue() + f"\nfailed: {exc}"


def _calibrate_report(client, apply=False):
    """Run calibrate for one client and capture what it printed."""
    import contextlib, io
    buf = io.StringIO()
    ns = argparse.Namespace(client=client, yes=apply)
    try:
        with contextlib.redirect_stdout(buf):
            cmd_calibrate(ns)
        return True, buf.getvalue()
    except SystemExit as exc:
        return False, buf.getvalue() + "\n" + str(exc)
    except Exception as exc:
        return False, buf.getvalue() + f"\nfailed: {exc}"


def collect_health(capture=True):
    """Everything that has to be true for an alert to reach you.

    Most of the ways this tool fails are silent: a region whose anchor no longer
    matches, a watcher that died, a client left minimised, a mode set to silent.
    Each looks exactly like "nothing is happening".
    """
    cfg = load_config()
    s = cfg["settings"]
    selected = read_clients()
    live = {h["title"]: h for h in list_windows("EVE - ")}
    supervisors = _supervisor_pids()
    watchers = _watcher_pids()
    mode = read_mode(s.get("mode", "away"))
    profile = PROFILES.get(mode, {})

    checks = []

    def add(ok, label, detail="", fix=""):
        checks.append({"ok": ok, "label": label, "detail": detail, "fix": fix})

    add(bool(cfg.get("regions")), "regions configured",
        f"{len(cfg.get('regions', []))} across "
        f"{len(configured_clients(cfg))} client(s)",
        "run calibrate, or open pick.bat")
    add(bool(selected), "clients selected for watching",
        ", ".join(short_client(c) for c in selected) or "none",
        "tick at least one client in pick.bat")
    add(bool(supervisors), "supervisor running",
        f"pid {supervisors}" if supervisors else "not running",
        "press Save in pick.bat, or run: eve_watch.py supervise")
    add(len(supervisors) <= 1, "exactly one supervisor",
        f"{len(supervisors)} running" if len(supervisors) != 1 else "yes",
        "each supervisor starts its own watchers - every alert would repeat. "
        "Stop the extras.")
    add(len(watchers) == len(selected) and bool(selected), "a watcher per client",
        f"{len(watchers)} running for {len(selected)} selected",
        "too few: check the supervisor. too many: orphans from a supervisor "
        "that died, or a second supervisor - alerts will repeat. Restarting "
        "the supervisor clears orphans.")
    blind_cols = [f"{short_client(r.get('window',''))}/{r['name']}"
                  for r in cfg["regions"]
                  if r["name"].startswith("overview") and not r.get("columns")]
    add(not blind_cols, "overviews record pilots",
        ", ".join(blind_cols) + " have no column data" if blind_cols
        else f"all {sum(1 for r in cfg['regions'] if r['name'].startswith('overview'))} overview(s)",
        "re-calibrate those clients once - column positions are read off the "
        "header, and without them a row cannot be split into name/ship/corp")
    if paused_clients():
        add(True, "paused on their own", ", ".join(paused_clients()))
    add(not os.path.exists(PAUSEFILE), "not paused",
        "PAUSED file present" if os.path.exists(PAUSEFILE) else "running",
        "run resume.bat")
    add(any(profile.get(k) for k in ("beeps", "voice", "popup")),
        "alerts can reach you", f"mode {mode!r}",
        "switch mode with mode-active.bat")
    add(ocr_engine() is not None, "OCR engine available", "",
        "roster and d-scan regions need it; change Windows language settings")

    fresh = ""
    if os.path.exists(LOGFILE):
        age = time.time() - os.path.getmtime(LOGFILE)
        fresh = f"last wrote {age:.0f}s ago"
        add(age < 120 or not watchers, "log is being written", fresh,
            "watchers should log a heartbeat every 60s")

    clients = []
    for title in selected:
        info = {"title": title, "short": short_client(title),
                "running": title in live,
                "minimized": live.get(title, {}).get("minimized", False),
                "regions": []}
        mine = [r for r in cfg.get("regions", []) if r.get("window") == title]
        frame = None
        if capture and info["running"] and not info["minimized"]:
            try:
                frame = one_frame(live[title]["hwnd"], 0.4)
            except Exception as exc:
                info["capture_error"] = str(exc)
        for r in sorted(mine, key=lambda r: r["name"]):
            row = {"name": r["name"], "mode": r.get("mode", "change"),
                   "enabled": r.get("enabled", True), "state": "?", "detail": ""}
            if not row["enabled"]:
                row["state"] = "off"
            elif frame is None:
                row["state"] = "?"
                row["detail"] = "client not visible"
            else:
                tr = Tracker(r, s)
                box = tr.locate(frame)
                if box is None:
                    row["state"] = "LOST"
                    row["detail"] = "anchor not found - re-calibrate"
                else:
                    row["state"] = "ok"
                    if r.get("mode") == "change":
                        learned = {k: apply_clip(v, r.get("clip", 0))
                                   for k, v in load_values(
                                       r["name"], True, title).items()}
                        val = classify(signature(crop(frame, box),
                                                 {"match": r.get("match", "mask"),
                                                  "clip": r.get("clip", 0)},
                                                 s["threshold"]),
                                       learned, s["sensitivity"], 0,
                                       r.get("match") == "ncc",
                                       r.get("ncc_min", 0.95))[0] if learned else None
                        if not learned:
                            row["detail"] = "no values taught - reports '?'"
                        elif val is None:
                            row["detail"] = ("current value not recognised - "
                                             "teach it with learn.bat")
                        else:
                            row["detail"] = f"reads {val!r}"
                    else:
                        rows = ocr_rows(to_image(crop(frame, box)),
                                        s["ocr_scale"], r.get("key_width"))
                        row["detail"] = f"{len(rows)} row(s) visible"
            info["regions"].append(row)
        clients.append(info)

    return {"checks": checks, "clients": clients, "mode": mode}


def cmd_doctor(args):
    """Walk the whole chain and say what would stop an alert reaching you."""
    h = collect_health(capture=not args.fast)
    bad = 0
    print()
    for c in h["checks"]:
        mark = "PASS" if c["ok"] else "FAIL"
        bad += 0 if c["ok"] else 1
        print(f"  [{mark}] {c['label']:32s} {c['detail']}")
        if not c["ok"] and c["fix"]:
            print(f"         -> {c['fix']}")

    for info in h["clients"]:
        print(f"\n  {info['short']}")
        if not info["running"]:
            print("     [FAIL] client is not running")
            bad += 1
            continue
        if info["minimized"]:
            print("     [FAIL] client is MINIMISED - it renders nothing to read")
            bad += 1
            continue
        if info.get("capture_error"):
            print(f"     [FAIL] cannot capture: {info['capture_error']}")
            bad += 1
            continue
        if not info["regions"]:
            print("     [FAIL] no regions configured - run calibrate")
            bad += 1
            continue
        for r in info["regions"]:
            mark = {"ok": "PASS", "LOST": "FAIL", "off": "----", "?": "????"}[r["state"]]
            bad += 1 if r["state"] == "LOST" else 0
            print(f"     [{mark}] {r['name']:12s} {r['mode']:8s} {r['detail']}")

    print(f"\n  {bad} problem(s) found." if bad else
          "\n  All clear - an alert would reach you.")
    return bad


EVENT_GROUPS = {
    "overview": lambda r: r["region"].startswith("overview"),
    "signatures": lambda r: r["region"] in ("sigs",) or r["region"] == "clipboard",
    "dscan": lambda r: r["region"] == "dscan",
    "structure": lambda r: r["region"] == "structure",
    "system": lambda r: r["region"] in ("-", ""),
}
ALERTING = {"arrive", "change", "present", "new_sig", "dscan"}


LEGACY_COLS = [c for c in CSV_COLS if c != "client"]


def migrate_csv():
    """Rewrite events.csv if its header predates a column being added.

    The header is only written when the file does not exist, so adding the
    client column left every later row one field wider than the header it was
    appended under - fine for a tolerant reader, but the file itself opens
    misaligned in a spreadsheet. Rewrite it once, keeping a .bak.
    """
    if not os.path.exists(CSVFILE):
        return False
    try:
        with open(CSVFILE, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return False
    if not rows or rows[0] == CSV_COLS:
        return False
    body = [r for r in rows[1:] if r]
    fixed = []
    for r in body:
        if len(r) == len(CSV_COLS):
            fixed.append(dict(zip(CSV_COLS, r)))
        elif len(r) == len(LEGACY_COLS):
            d = dict(zip(LEGACY_COLS, r))
            d["client"] = ""
            fixed.append(d)
    try:
        shutil_copy = CSVFILE + ".bak"
        with open(shutil_copy, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
        with open(CSVFILE, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
            w.writeheader()
            for d in fixed:
                w.writerow(d)
    except OSError as exc:
        log(f"  csv migration failed: {exc}")
        return False
    log(f"  events.csv rewritten with the current columns "
        f"({len(fixed)} rows; previous file kept as events.csv.bak)")
    return True


def read_events(limit=4000):
    """Rows from events.csv, newest first. Tolerates both column layouts."""
    if not os.path.exists(CSVFILE):
        return []
    try:
        with open(CSVFILE, "r", encoding="utf-8", newline="") as fh:
            raw = list(csv.reader(fh))
    except OSError:
        return []
    if not raw:
        return []
    # Map by field count rather than trusting the header, which may predate a
    # column being added and therefore mislabels every row written since.
    rows = []
    for r in raw[1:]:
        if len(r) == len(CSV_COLS):
            rows.append(dict(zip(CSV_COLS, r)))
        elif len(r) == len(LEGACY_COLS):
            d = dict(zip(LEGACY_COLS, r))
            d["client"] = ""
            rows.append(d)
    out = []
    for r in rows:
        r.setdefault("client", "")
        r["region"] = r.get("region") or "-"
        out.append(r)
    return out[-limit:][::-1]


def stop_everything():
    """Stop supervisors first, then any watcher they leave behind."""
    killed = []
    for pid in _supervisor_pids() + _watcher_pids():
        if pid in killed:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=20,
                           creationflags=CREATE_NO_WINDOW)
            killed.append(pid)
        except Exception:
            pass
    return killed


def cmd_hub(args):
    """One window to launch everything, and to say what is happening."""
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("eve-watch")
    root.configure(padx=16, pady=12)
    root.attributes("-topmost", True)

    state = tk.Label(root, text="checking...", font=("Segoe UI", 11, "bold"),
                     justify="left")
    state.pack(anchor="w")
    sub = tk.Label(root, text="", font=("Segoe UI", 9), fg="#666", justify="left")
    sub.pack(anchor="w", pady=(0, 12))

    def spawn(*cmd):
        exe = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        subprocess.Popen([exe, os.path.abspath(__file__), *cmd], cwd=HERE,
                         creationflags=CREATE_NO_WINDOW)

    def show_text(title, fn):
        win = tk.Toplevel(root)
        win.title(title)
        win.attributes("-topmost", True)
        win.configure(padx=12, pady=10)
        box = scrolledtext.ScrolledText(win, width=96, height=28,
                                        font=("Consolas", 9))
        box.pack()
        box.insert("1.0", "working...")
        box.configure(state="disabled")

        def work():
            import contextlib, io
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    fn()
            except SystemExit as exc:
                buf.write(f"\n{exc}")
            except Exception as exc:
                buf.write(f"\nfailed: {exc}")
            text = buf.getvalue()

            def paint():
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.insert("1.0", text or "(no output)")
                box.configure(state="disabled")
            root.after(0, paint)

        threading.Thread(target=work, daemon=True).start()

    def start():
        if start_supervisor():
            sub.config(text="starting watchers...")
        refresh()

    def stop():
        n = stop_everything()
        sub.config(text=f"stopped {len(n)} process(es)")
        refresh()

    def toggle_pause():
        if os.path.exists(PAUSEFILE):
            cmd_resume(argparse.Namespace())
        else:
            cmd_pause(argparse.Namespace())
        refresh()

    def set_mode(name):
        cmd_mode(argparse.Namespace(name=name))
        refresh()

    grid = tk.Frame(root)
    grid.pack(anchor="w")

    def button(parent, text, cmd, col, row, width=18, **kw):
        b = tk.Button(parent, text=text, width=width, command=cmd, **kw)
        b.grid(row=row, column=col, padx=3, pady=3, sticky="w")
        return b

    run_btn = button(grid, "Start watching", start, 0, 0)
    stop_btn = button(grid, "Stop watching", stop, 1, 0)
    pause_btn = button(grid, "Pause", toggle_pause, 2, 0)

    button(grid, "Clients / calibrate", lambda: spawn("pick"), 0, 1)
    button(grid, "Status", lambda: spawn("dash"), 1, 1)
    button(grid, "Events", lambda: spawn("events"), 2, 1)

    button(grid, "Check everything", lambda: show_text(
        "doctor", lambda: cmd_doctor(argparse.Namespace(fast=False))), 0, 2)
    button(grid, "Show config", lambda: show_text(
        "config", lambda: cmd_list(argparse.Namespace())), 1, 2)
    button(grid, "Console", lambda: subprocess.Popen(
        ["cmd", "/c", "start", "", os.path.join(HERE, "console.bat")],
        cwd=HERE, shell=False), 2, 2)

    modes = tk.Frame(root)
    modes.pack(anchor="w", pady=(10, 0))
    tk.Label(modes, text="alerts:", font=("Segoe UI", 9)).pack(side="left")
    mode_btns = {}
    for name in ("active", "away", "silent"):
        b = tk.Button(modes, text=name, width=9,
                      command=lambda n=name: set_mode(n))
        b.pack(side="left", padx=3)
        mode_btns[name] = b

    def refresh():
        sup = _supervisor_pids()
        wat = _watcher_pids()
        sel = read_clients()
        paused = os.path.exists(PAUSEFILE)
        mode = read_mode(load_config()["settings"].get("mode", "away"))

        if not sup:
            state.config(text="Not watching", fg="#c22")
        elif paused:
            state.config(text="PAUSED", fg="#c80")
        elif len(sup) > 1:
            state.config(text=f"{len(sup)} supervisors - alerts will repeat",
                         fg="#c22")
        elif len(wat) != len(sel):
            state.config(text=f"{len(wat)} watcher(s) for {len(sel)} client(s)",
                         fg="#c80")
        else:
            state.config(text=f"Watching {len(sel)} client(s)", fg="#0a7")

        sub.config(text=("  ".join(short_client(c) for c in sel) or "no clients selected")
                        + f"     mode {mode}")
        pause_btn.config(text="Resume" if paused else "Pause",
                         state="normal" if sup else "disabled")
        run_btn.config(state="disabled" if sup else "normal")
        stop_btn.config(state="normal" if sup or wat else "disabled")
        for name, b in mode_btns.items():
            b.config(relief="sunken" if name == mode else "raised",
                     font=("Segoe UI", 9, "bold" if name == mode else "normal"))
        root.after(3000, refresh)

    refresh()
    if args.seconds:
        root.after(int(args.seconds * 1000), root.destroy)
    root.mainloop()


def cmd_events(args):
    """Live view of events.csv, filterable by what produced them."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("eve-watch - events")
    root.configure(padx=10, pady=8)
    root.geometry("1150x620")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    tab_ev = tk.Frame(nb, padx=6, pady=6)
    tab_pl = tk.Frame(nb, padx=6, pady=6)
    tab_sg = tk.Frame(nb, padx=6, pady=6)
    nb.add(tab_ev, text="Events")
    nb.add(tab_pl, text="Pilots")
    nb.add(tab_sg, text="Signatures")

    top = tk.Frame(tab_ev)
    top.pack(fill="x")
    tk.Label(top, text="Show:", font=("Segoe UI", 9, "bold")).pack(side="left")
    group_vars = {}
    for name in EVENT_GROUPS:
        v = tk.IntVar(value=1)
        group_vars[name] = v
        tk.Checkbutton(top, text=name, variable=v,
                       command=lambda: refresh(force=True)).pack(side="left")
    alerts_only = tk.IntVar(value=0)
    tk.Checkbutton(top, text="alerts only", variable=alerts_only,
                   command=lambda: refresh(force=True)).pack(side="left", padx=(16, 0))

    tk.Label(top, text="client:").pack(side="left", padx=(16, 4))
    client_var = tk.StringVar(value="all")
    client_box = ttk.Combobox(top, textvariable=client_var, width=16,
                              state="readonly", values=["all"])
    client_box.pack(side="left")
    client_box.bind("<<ComboboxSelected>>", lambda e: refresh(force=True))

    tk.Label(top, text="find:").pack(side="left", padx=(16, 4))
    search = tk.StringVar()
    ent = tk.Entry(top, textvariable=search, width=22)
    ent.pack(side="left")
    ent.bind("<KeyRelease>", lambda e: refresh(force=True))

    tk.Button(top, text="Refresh", width=10,
              command=lambda: both(force=True)).pack(side="left", padx=(16, 0))

    cols = ("time", "client", "region", "event", "detail")
    tree = ttk.Treeview(tab_ev, columns=cols, show="headings", height=24)
    for c, w in zip(cols, (150, 110, 95, 80, 640)):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="w")
    sb = ttk.Scrollbar(tab_ev, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True, pady=(8, 0))
    sb.pack(side="right", fill="y", pady=(8, 0))

    tree.tag_configure("arrive", foreground="#c22")
    tree.tag_configure("depart", foreground="#888")
    tree.tag_configure("dscan", foreground="#06a")
    tree.tag_configure("problem", foreground="#c80")

    status = tk.Label(tab_ev, text="", font=("Segoe UI", 8), fg="#666")
    status.pack(side="bottom", anchor="w")

    # ---- Pilots tab: who has been seen, in what, for whom ------------------
    ptop = tk.Frame(tab_pl)
    ptop.pack(fill="x")
    tk.Label(ptop, text="find:", font=("Segoe UI", 9, "bold")).pack(side="left")
    pfind = tk.StringVar()
    pent = tk.Entry(ptop, textvariable=pfind, width=26)
    pent.pack(side="left", padx=(4, 0))
    tk.Button(ptop, text="Refresh", width=10,
              command=lambda: both(force=True)).pack(side="left", padx=(16, 0))
    def mark_selected(what):
        """Correct the selected pilot. The watchers pick it up within seconds."""
        rows = ptree.selection()
        if not rows:
            pcount.configure(text="select a pilot first", fg="#b00")
            return
        done = []
        for iid in rows:
            name = ptree.item(iid, "values")[0]
            if name and mark_pilot(pilot_key(name), what):
                done.append(name)
        pcount.configure(
            text=f"{what}: {', '.join(done)} - applying...", fg="#060")
        root.after(3000, lambda: pilots_refresh(force=True))

    tk.Button(ptop, text="Mark docked", width=13,
              command=lambda: mark_selected("docked")).pack(side="left",
                                                            padx=(16, 0))
    tk.Button(ptop, text="Forget", width=9,
              command=lambda: mark_selected("forget")).pack(side="left",
                                                            padx=(4, 0))
    pcount = tk.Label(ptop, text="", font=("Segoe UI", 9), fg="#666")
    pcount.pack(side="left", padx=(12, 0))

    pcols = ("name", "in space now", "ships flown", "corporations", "seen",
             "last", "status", "last seen by")
    ptree = ttk.Treeview(tab_pl, columns=pcols, show="headings", height=24)
    for c, w in zip(pcols, (155, 145, 220, 95, 50, 120, 215, 100)):
        ptree.heading(c, text=c)
        ptree.column(c, width=w, anchor="w")
    ptree.tag_configure("here", foreground="#c22")
    psb = ttk.Scrollbar(tab_pl, orient="vertical", command=ptree.yview)
    ptree.configure(yscrollcommand=psb.set)
    ptree.pack(side="left", fill="both", expand=True, pady=(8, 0))
    psb.pack(side="right", fill="y", pady=(8, 0))
    pstate = {"mtime": None}

    def pilots_refresh(force=False):
        try:
            stamp = os.path.getmtime(PILOTS)
        except OSError:
            stamp = None
        if not force and stamp == pstate["mtime"]:
            return
        pstate["mtime"] = stamp
        book = load_pilots()
        need = pfind.get().strip().lower()
        ptree.delete(*ptree.get_children())
        rows = sorted(book.values(), key=lambda w: w.get("last_seen", ""),
                      reverse=True)
        shown = 0
        for who in rows:
            # most-flown ship first: what someone usually undocks in is the
            # useful fact, not whatever they happened to be in last
            def listed(book_field, counts=True):
                """Every reading, most-seen first.

                Ships carry their count: "Astero x17, Naglfar x1" says what
                someone usually undocks in and what they brought once, and
                hiding the rare one would drop the most interesting fact in the
                book. A corp count says nothing - a pilot is in one corp - so
                the ticker stands on its own.
                """
                items = sorted(book_field.items(),
                               key=lambda kv: (-kv[1].get("count", 0), kv[0]))
                if not counts:
                    return ", ".join(k for k, _ in items)
                return ", ".join(f"{k} x{v.get('count', 0)}" for k, v in items)

            ships = list(who.get("ships", {}))
            corps = list(who.get("corps", {}))
            ship_s = listed(who.get("ships", {}))
            corp_s = listed(who.get("corps", {}), counts=False)
            blob = (f"{who.get('name','')} {ship_s} {corp_s} "
                    f"{who.get('now_ship','')} {who.get('last_by','')} "
                    f"{who.get('last_note','')}").lower()
            if need and need not in blob:
                continue
            # Held until they dock, not until they leave the overview: a pilot
            # that warped off is still out there in that hull, which is the
            # thing worth knowing.
            dirn = who.get("now_ship") or ""
            dock = who.get("last_note", "")
            ptree.insert("", "end", tags=(("here",) if dirn else ()), values=(
                who.get("name", ""), dirn, ship_s, corp_s, who.get("seen", 0),
                who.get("last_seen", "")[:16].replace("T", " "),
                dock,
                who.get("last_by") or ", ".join(who.get("clients", []))))
            shown += 1
        pcount.configure(text=f"{shown} of {len(book)} pilot(s)")

    pent.bind("<KeyRelease>", lambda e: pilots_refresh(force=True))
    pilots_refresh(force=True)

    # ---- Signatures tab: what is scanned and what is not ------------------
    stop_ = tk.Frame(tab_sg)
    stop_.pack(fill="x")
    only_new = tk.IntVar(value=0)
    tk.Checkbutton(stop_, text="not yet scanned only", variable=only_new,
                   command=lambda: sigs_refresh(force=True)).pack(side="left")
    scount = tk.Label(stop_, text="", font=("Segoe UI", 9), fg="#666")
    scount.pack(side="left", padx=(12, 0))

    scols = ("id", "scanned", "type", "site name", "first seen", "last seen",
             "seen by")
    stree = ttk.Treeview(tab_sg, columns=scols, show="headings", height=24)
    for c, w in zip(scols, (95, 75, 105, 260, 125, 125, 105)):
        stree.heading(c, text=c)
        stree.column(c, width=w, anchor="w")
    stree.tag_configure("todo", foreground="#c22")
    ssb = ttk.Scrollbar(tab_sg, orient="vertical", command=stree.yview)
    stree.configure(yscrollcommand=ssb.set)
    stree.pack(side="left", fill="both", expand=True, pady=(8, 0))
    ssb.pack(side="right", fill="y", pady=(8, 0))
    sstate = {"mtime": None}

    def sigs_refresh(force=False):
        try:
            stamp = os.path.getmtime(SIGFILE)
        except OSError:
            stamp = None
        if not force and stamp == sstate["mtime"]:
            return
        sstate["mtime"] = stamp
        book = load_sigs()
        stree.delete(*stree.get_children())
        # unscanned first, then most recently seen: the list is a to-do list
        rows = sorted(book.values(),
                      key=lambda v: (v.get("scanned", False),
                                     v.get("last_seen", "")), reverse=False)
        rows = [r for r in rows if not r.get("scanned")] +                sorted([r for r in rows if r.get("scanned")],
                      key=lambda v: v.get("id", ""))
        shown = 0
        for rec in rows:
            if only_new.get() and rec.get("scanned"):
                continue
            stree.insert("", "end",
                         tags=(() if rec.get("scanned") else ("todo",)),
                         values=(rec.get("id", ""),
                                 "yes" if rec.get("scanned") else "NOT YET",
                                 rec.get("type", ""), rec.get("name", ""),
                                 (rec.get("first_seen") or "")[:16].replace("T", " "),
                                 (rec.get("last_seen") or "")[:16].replace("T", " "),
                                 ", ".join(rec.get("clients", []))))
            shown += 1
        todo = sum(1 for r in book.values() if not r.get("scanned"))
        scount.configure(text=f"{shown} shown - {todo} of {len(book)} still to scan")

    sigs_refresh(force=True)
    shots = {}
    seen = {"sig": None}

    def open_shot(_event=None):
        for iid in tree.selection():
            path = shots.get(iid)
            if path:
                full = path if os.path.isabs(path) else os.path.join(HERE, path)
                if os.path.exists(full):
                    os.startfile(full)
                else:
                    status.config(text=f"snapshot missing: {path}")
    tree.bind("<Double-1>", open_shot)

    def refresh(force=False):
        sig = None
        if os.path.exists(CSVFILE):
            st_ = os.stat(CSVFILE)
            sig = (st_.st_mtime, st_.st_size)
        if not force and sig == seen["sig"]:
            return
        seen["sig"] = sig
        rows = read_events()

        clients = sorted({r["client"] for r in rows if r["client"]})
        client_box.configure(values=["all"] + clients)

        wanted = [g for g, v in group_vars.items() if v.get()]
        needle = search.get().strip().lower()
        tree.delete(*tree.get_children())
        shots.clear()
        shown = 0
        for r in rows:
            group = next((g for g, fn in EVENT_GROUPS.items() if fn(r)), "system")
            if group not in wanted:
                continue
            if alerts_only.get() and r.get("event") not in ALERTING:
                continue
            if client_var.get() != "all" and r["client"] != client_var.get():
                continue
            if needle and needle not in " ".join(
                    (r.get("detail", ""), r["region"], r.get("event", ""),
                     r["client"])).lower():
                continue
            ev = r.get("event", "")
            tag = ("arrive" if ev in ("arrive", "present", "new_sig", "change")
                   else "depart" if ev in ("depart", "clear")
                   else "dscan" if ev == "dscan"
                   else "problem" if ev in ("blind", "anchor_lost", "lost_alarm")
                   else "")
            iid = tree.insert("", "end", tags=(tag,), values=(
                r.get("iso", "").replace("T", "  "), r["client"], r["region"],
                ev, (r.get("detail", "") or "")[:220]))
            if r.get("snapshot"):
                shots[iid] = r["snapshot"]
            shown += 1
        status.config(text=f"{shown} of {len(rows)} events   "
                           f"double-click a row to open its snapshot   "
                           f"updated {dt.datetime.now():%H:%M:%S}")

    def both(force=False):
        """Redraw both tabs. A watcher writes the two files on its own
        schedule - pilots.json is flushed every 20s or so - and the automatic
        pass only redraws what changed, so a button that redraws regardless is
        the difference between waiting and knowing."""
        refresh(force=force)
        pilots_refresh(force=force)
        sigs_refresh(force=force)

    def tick():
        both()
        root.after(int(args.every * 1000), tick)

    root.bind("<F5>", lambda e: both(force=True))
    root.bind("<Control-r>", lambda e: both(force=True))

    tick()
    if args.seconds:
        root.after(int(args.seconds * 1000), root.destroy)
    root.mainloop()


def cmd_dash(args):
    """A window that keeps saying whether you are actually being watched."""
    import tkinter as tk

    GREEN, RED, GREY, AMBER = "#0a7", "#c22", "#888", "#c80"

    root = tk.Tk()
    root.title("eve-watch - status")
    root.attributes("-topmost", True)
    root.configure(padx=14, pady=10)
    head = tk.Label(root, text="checking...", font=("Segoe UI", 11, "bold"))
    head.pack(anchor="w")
    stamp = tk.Label(root, text="", font=("Segoe UI", 8), fg=GREY)
    stamp.pack(anchor="w", pady=(0, 8))
    body = tk.Frame(root)
    body.pack(fill="both", expand=True)
    busy = {"running": False}

    def render(h):
        for w in body.winfo_children():
            w.destroy()
        bad = sum(1 for c in h["checks"] if not c["ok"])
        row = 0
        for c in h["checks"]:
            tk.Label(body, text="OK" if c["ok"] else "!!", width=3,
                     font=("Consolas", 9, "bold"),
                     fg=GREEN if c["ok"] else RED).grid(row=row, column=0, sticky="w")
            tk.Label(body, text=c["label"], font=("Segoe UI", 9)
                     ).grid(row=row, column=1, sticky="w")
            tk.Label(body, text=c["detail"], font=("Segoe UI", 9), fg=GREY
                     ).grid(row=row, column=2, sticky="w", padx=(10, 0))
            row += 1

        for info in h["clients"]:
            row += 1
            tk.Label(body, text=info["short"], font=("Segoe UI", 10, "bold")
                     ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            if not info["running"]:
                tk.Label(body, text="not running", fg=RED, font=("Segoe UI", 9)
                         ).grid(row=row, column=2, sticky="w", pady=(8, 0))
                bad += 1
                row += 1
                continue
            if info["minimized"]:
                tk.Label(body, text="MINIMISED - renders nothing", fg=RED,
                         font=("Segoe UI", 9)).grid(row=row, column=2, sticky="w",
                                                    pady=(8, 0))
                bad += 1
                row += 1
                continue
            row += 1
            for r in info["regions"]:
                mark, colour = {"ok": ("OK", GREEN), "LOST": ("!!", RED),
                                "off": ("--", GREY), "?": ("??", AMBER)}[r["state"]]
                if r["state"] == "LOST":
                    bad += 1
                tk.Label(body, text=mark, width=3, font=("Consolas", 9, "bold"),
                         fg=colour).grid(row=row, column=0, sticky="w")
                tk.Label(body, text=f"   {r['name']}  ({r['mode']})",
                         font=("Segoe UI", 9)).grid(row=row, column=1, sticky="w")
                tk.Label(body, text=r["detail"], font=("Segoe UI", 9), fg=GREY
                         ).grid(row=row, column=2, sticky="w", padx=(10, 0))
                row += 1

        head.config(text="Watching normally" if bad == 0
                    else f"{bad} problem(s) - you may not be covered",
                    fg=GREEN if bad == 0 else RED)
        stamp.config(text=f"checked {dt.datetime.now():%H:%M:%S}"
                          f"   mode {h['mode']!r}   refreshing every "
                          f"{args.every:.0f}s")

    def refresh():
        if busy["running"]:
            return
        busy["running"] = True
        holder = {}

        def work():
            try:
                holder["h"] = collect_health(capture=True)
            except Exception as exc:
                holder["error"] = str(exc)

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                root.after(150, poll)
                return
            busy["running"] = False
            if "h" in holder:
                render(holder["h"])
            else:
                head.config(text=f"check failed: {holder.get('error')}", fg=RED)

        root.after(150, poll)

    def tick():
        refresh()
        root.after(int(args.every * 1000), tick)

    bar = tk.Frame(root)
    bar.pack(anchor="w", pady=(10, 0))
    tk.Button(bar, text="Refresh", width=10, command=refresh).pack(side="left")
    tk.Button(bar, text="Clients...", width=10,
              command=lambda: subprocess.Popen(
                  [sys.executable, os.path.abspath(__file__), "pick"],
                  cwd=HERE, creationflags=CREATE_NO_WINDOW)).pack(side="left", padx=6)
    tk.Button(bar, text="Close", width=10, command=root.destroy).pack(side="left")

    tick()
    if args.seconds:
        root.after(int(args.seconds * 1000), root.destroy)
    root.mainloop()


def cmd_pick(args):
    """Choose clients and regions in a window.

    A client with no regions cannot be ticked - a watcher pointed at an
    unconfigured client starts happily and watches nothing, which is the
    failure mode hardest to notice. Calibrate it from here instead.
    """
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    def with_progress(parent, caption, fn):
        """Run fn on a worker thread behind an indeterminate progress bar."""
        win = tk.Toplevel(parent)
        win.title("working")
        win.attributes("-topmost", True)
        win.configure(padx=20, pady=16)
        win.resizable(False, False)
        tk.Label(win, text=caption, font=("Segoe UI", 10)).pack(anchor="w")
        bar = ttk.Progressbar(win, mode="indeterminate", length=340)
        bar.pack(pady=(10, 0))
        bar.start(12)
        holder = {}

        def work():
            try:
                holder["value"] = fn()
            except Exception as exc:
                holder["value"] = (False, f"failed: {exc}")

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                parent.after(120, poll)
            else:
                bar.stop()
                win.destroy()

        parent.after(120, poll)
        parent.wait_window(win)
        return holder.get("value", (False, "no result"))

    root = tk.Tk()
    root.title("eve-watch - clients")
    root.attributes("-topmost", True)
    root.configure(padx=16, pady=12)
    body = tk.Frame(root)
    body.pack(fill="both", expand=True)
    state = {"status": None}

    def add_panel_dialog(title):
        """For a panel automatic discovery cannot find - a renamed one, say."""
        win = tk.Toplevel(root)
        win.title(f"add a panel - {short_client(title)}")
        win.attributes("-topmost", True)
        win.configure(padx=14, pady=12)
        tk.Label(win, text="Which kind of panel do you want to add?",
                 font=("Segoe UI", 10)).pack(anchor="w")
        tk.Label(win, text="You will then drag a box around it, headers included.",
                 font=("Segoe UI", 9), fg="#666").pack(anchor="w", pady=(0, 10))
        namevar = tk.StringVar()
        row = tk.Frame(win); row.pack(anchor="w", pady=(0, 10))
        tk.Label(row, text="name (optional):", font=("Segoe UI", 9)).pack(side="left")
        tk.Entry(row, textvariable=namevar, width=18).pack(side="left", padx=6)

        def go(kind):
            win.destroy()
            ns = argparse.Namespace(kind=kind, client=title,
                                    name=namevar.get().strip() or None, yes=False)
            ok, text = with_progress(root, f"Reading that panel...",
                                     lambda: _capture_output(cmd_add_panel, ns))
            show = tk.Toplevel(root)
            show.title("add panel")
            show.attributes("-topmost", True)
            show.configure(padx=12, pady=10)
            box = scrolledtext.ScrolledText(show, width=88, height=16,
                                            font=("Consolas", 9))
            box.pack()
            box.insert("1.0", text or "(no output)")
            box.configure(state="disabled")

            def apply_now():
                show.destroy()
                ns2 = argparse.Namespace(kind=kind, client=title,
                                         name=namevar.get().strip() or None, yes=True)
                with_progress(root, "Saving...",
                              lambda: _capture_output(cmd_add_panel, ns2))
                rebuild()

            bar = tk.Frame(show); bar.pack(anchor="w", pady=(8, 0))
            tk.Button(bar, text="Apply", width=12,
                      state=("normal" if ok else "disabled"),
                      command=apply_now).pack(side="left")
            tk.Button(bar, text="Cancel", width=12,
                      command=show.destroy).pack(side="left", padx=8)

        btns = tk.Frame(win); btns.pack(anchor="w")
        for kind in [p["kind"] for p in PANELS]:
            tk.Button(btns, text=kind, width=12,
                      command=lambda k=kind: go(k)).pack(side="left", padx=3)

    def calibrate_dialog(title):
        ok, text = with_progress(
            root, f"Reading the panels on {short_client(title)}...",
            lambda: _calibrate_report(title, apply=False))
        win = tk.Toplevel(root)
        win.title(f"calibrate - {short_client(title)}")
        win.attributes("-topmost", True)
        win.configure(padx=12, pady=10)
        box = scrolledtext.ScrolledText(win, width=96, height=24,
                                        font=("Consolas", 9))
        box.pack()
        box.insert("1.0", text or "(no output)")
        box.configure(state="disabled")

        def apply_now():
            win.destroy()
            ok2, text2 = with_progress(
                root, f"Writing regions for {short_client(title)}...",
                lambda: _calibrate_report(title, apply=True))
            rebuild()
            state["status"].config(
                text=("Calibrated " + short_client(title)) if ok2
                     else "Calibration failed - details below",
                fg="#060" if ok2 else "#b00")
            if not ok2:
                # There is no console under pythonw, so show it here instead of
                # telling someone to go and look at output that was discarded.
                err = tk.Toplevel(root)
                err.title("calibration failed")
                err.attributes("-topmost", True)
                err.configure(padx=12, pady=10)
                box2 = scrolledtext.ScrolledText(err, width=94, height=22,
                                                 font=("Consolas", 9))
                box2.pack()
                box2.insert("1.0", text2 or "(no output)")
                box2.configure(state="disabled")
                tk.Button(err, text="Close", width=12,
                          command=err.destroy).pack(anchor="w", pady=(8, 0))

        bar = tk.Frame(win)
        bar.pack(pady=(8, 0), anchor="w")
        tk.Button(bar, text="Apply", width=12,
                  state=("normal" if ok else "disabled"),
                  command=apply_now).pack(side="left")
        tk.Button(bar, text="Cancel", width=12,
                  command=win.destroy).pack(side="left", padx=8)

    def rebuild():
        for w in body.winfo_children():
            w.destroy()
        try:
            cfg = load_config()
        except Exception as exc:
            tk.Label(body, text="Could not read the configuration",
                     font=("Segoe UI", 11, "bold"), fg="#c22").grid(sticky="w")
            tk.Label(body, text=str(exc), font=("Segoe UI", 9), fg="#666",
                     wraplength=560, justify="left").grid(sticky="w", pady=(4, 8))
            tk.Button(body, text="Retry", width=12,
                      command=rebuild).grid(sticky="w")
            return
        by_client = {}
        for r in cfg.get("regions", []):
            by_client.setdefault(r.get("window"), []).append(r)
        live = {h["title"] for h in list_windows("EVE - ")}
        watching = set(read_clients())
        titles = sorted(live | set(by_client) | watching)

        tk.Label(body, text="Clients to watch", font=("Segoe UI", 12, "bold")
                 ).grid(row=0, column=0, columnspan=4, sticky="w")
        tk.Label(body, text=f"{len(_watcher_pids())} watcher(s) running",
                 font=("Segoe UI", 9), fg="#666"
                 ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))

        client_vars, region_vars = {}, {}
        row = 2
        for title in titles:
            regions = sorted(by_client.get(title, []), key=lambda r: r["name"])
            calibrated = bool(regions)
            cv = tk.IntVar(value=1 if title in watching and calibrated else 0)
            client_vars[title] = cv
            cb = tk.Checkbutton(body, variable=cv, text=short_client(title),
                                font=("Segoe UI", 11, "bold"))
            if not calibrated:
                cb.configure(state="disabled")
            cb.grid(row=row, column=0, sticky="w")
            note = "" if title in live else "not running"
            tk.Label(body, text=note, font=("Segoe UI", 9), fg="#888"
                     ).grid(row=row, column=1, sticky="w", padx=(8, 8))
            if not calibrated:
                tk.Label(body, text="must be calibrated first",
                         font=("Segoe UI", 9), fg="#b00"
                         ).grid(row=row, column=2, sticky="w")
            tk.Button(body, text=("Re-calibrate" if calibrated else "Calibrate"),
                      width=13,
                      state=("normal" if title in live else "disabled"),
                      command=lambda t=title: calibrate_dialog(t)
                      ).grid(row=row, column=3, sticky="w", padx=(10, 0))
            tk.Button(body, text="Add panel...", width=12,
                      state=("normal" if title in live else "disabled"),
                      command=lambda t=title: add_panel_dialog(t)
                      ).grid(row=row, column=4, sticky="w", padx=(4, 0))

            # Probing on a scout churns its own signature list, so its alerts
            # are self-inflicted while you work. This silences that one client
            # without stopping its watcher, and it re-baselines on resume.
            pbtn = tk.Button(body, width=14)

            def toggle_alerts(t=title, b=pbtn):
                if os.path.exists(pause_path(t)):
                    os.remove(pause_path(t))
                else:
                    with open(pause_path(t), "w", encoding="utf-8") as fh:
                        fh.write(f"paused {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
                paint_alerts(t, b)

            def paint_alerts(t=title, b=pbtn):
                off = os.path.exists(pause_path(t))
                b.configure(text=("Alerts OFF" if off else "Alerts on"),
                            fg=("#b00" if off else "#060"))

            pbtn.configure(command=toggle_alerts)
            paint_alerts()
            pbtn.grid(row=row, column=5, sticky="w", padx=(4, 0))
            row += 1
            for r in regions:
                rv = tk.IntVar(value=1 if r.get("enabled", True) else 0)
                region_vars[(title, r["name"])] = rv
                label = f"{r['name']}   ({r.get('mode', 'change')})"
                tk.Checkbutton(body, variable=rv, text=label,
                               font=("Segoe UI", 9)
                               ).grid(row=row, column=0, columnspan=3,
                                      sticky="w", padx=(28, 0))
                row += 1
            row += 1

        state["client_vars"], state["region_vars"] = client_vars, region_vars
        state["status"] = tk.Label(body, text="", font=("Segoe UI", 9), fg="#060")
        state["status"].grid(row=row, column=0, columnspan=4, sticky="w",
                             pady=(6, 4))
        bar = tk.Frame(body)
        bar.grid(row=row + 1, column=0, columnspan=4, sticky="w")
        tk.Button(bar, text="Save", width=12, command=save).pack(side="left")
        tk.Button(bar, text="Close", width=12,
                  command=root.destroy).pack(side="left", padx=8)

    def save():
        cfg = load_config()
        chosen = [t for t, v in state["client_vars"].items() if v.get()]
        write_clients(chosen)
        off = 0
        for r in cfg.get("regions", []):
            key = (r.get("window"), r["name"])
            if key in state["region_vars"]:
                on = bool(state["region_vars"][key].get())
                r["enabled"] = on
                off += 0 if on else 1
        save_config(cfg)
        started = start_supervisor()
        # give the supervisor a moment to act, then report what is actually up
        def report(tries=0):
            live = _watcher_pids()
            if tries < 8 and len(live) == 0 and chosen:
                state["status"].config(text="starting watchers...", fg="#666")
                root.after(1200, lambda: report(tries + 1))
                return
            bits = [f"{len(chosen)} client(s) selected"]
            if off:
                bits.append(f"{off} region(s) off")
            bits.append("supervisor started" if started else "supervisor already running")
            bits.append(f"{len(live)} watcher(s) live")
            msg = "Saved: " + ", ".join(bits)
            state["status"].config(text=msg, fg="#060")
            print(msg)
        report()

    rebuild()
    if args.seconds:
        root.after(int(args.seconds * 1000), root.destroy)
    root.mainloop()
    print("clients:", read_clients())


def client_fingerprint(cfg, title):
    """What a given client's watcher actually depends on.

    Saving from the picker rewrites config.json whether or not anything changed,
    so reacting to the file's timestamp bounces every watcher. Comparing this
    instead means a client is only restarted when its own regions - or the shared
    settings - really differ.
    """
    mine = sorted([r for r in cfg.get("regions", []) if r.get("window") == title],
                  key=lambda r: r["name"])
    blob = json.dumps({"regions": mine, "settings": cfg.get("settings", {})},
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def cmd_supervise(args):
    """Run one watcher per selected client, following the CLIENTS file."""
    others = [p for p in _supervisor_pids() if p != os.getpid()]
    if others and not args.force:
        sys.exit(f"A supervisor is already running (pid {others}). Each one starts "
                 f"its own watcher per client, so a second copy means every alert "
                 f"fires twice.\nStop the other first, or pass --force if you "
                 f"are certain.")

    # A supervisor that dies leaves its watchers running - they are separate
    # processes and nothing reaps them. The next supervisor then starts a second
    # set alongside and every alert fires twice. Clear the strays before adding
    # ours; the guard above means no live supervisor owns them.
    strays = [p for p in _watcher_pids()]
    if strays:
        log(f"supervisor: clearing {len(strays)} orphaned watcher(s) left by a "
            f"previous run: {strays}")
        for pid in strays:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
            except Exception as exc:
                log(f"supervisor: could not stop stray {pid}: {exc}")
        time.sleep(1.5)
        left = _watcher_pids()
        if left:
            log(f"!! supervisor: {len(left)} stray watcher(s) survived: {left}. "
                f"Alerts may repeat - stop them by hand.")

    children = {}                       # title -> Popen
    prints = {}                         # title -> fingerprint it was started with
    cfg_stamp = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0

    def spawn(title):
        cmd = [sys.executable, os.path.abspath(__file__), "watch",
               "--client", title, "--tag", short_client(title)]
        for flag in ("mode", "interval", "sensitivity", "stable", "obs_dir",
                     "webhook", "sound", "repeat"):
            val = getattr(args, flag, None)
            if val is not None:
                cmd += ["--" + flag.replace("_", "-"), str(val)]
        if args.quiet:
            cmd.append("--quiet")
        log(f"supervisor: starting watcher for {title!r}")
        return subprocess.Popen(cmd, cwd=HERE, creationflags=CREATE_NO_WINDOW)

    def stop(title):
        prints.pop(title, None)
        proc = children.pop(title, None)
        if proc and proc.poll() is None:
            log(f"supervisor: stopping watcher for {title!r}")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    try:
        while True:
            now_stamp = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0
            if now_stamp != cfg_stamp:
                cfg_stamp = now_stamp
                fresh = load_config()
                changed = [t for t in list(children)
                           if client_fingerprint(fresh, t) != prints.get(t)]
                if changed:
                    log(f"supervisor: config changed for {changed} - restarting "
                        f"those only")
                    for title in changed:
                        stop(title)
            wanted = read_clients()
            if not wanted:
                wanted = configured_clients(load_config())
                if wanted:
                    write_clients(wanted)
                    log(f"supervisor: no CLIENTS file, defaulting to {wanted}")
            for title in list(children):
                if title not in wanted:
                    stop(title)
                elif children[title].poll() is not None:
                    log(f"!! supervisor: watcher for {title!r} exited "
                        f"(code {children[title].returncode}) - restarting")
                    children[title] = spawn(title)
            for title in wanted:
                if title not in children:
                    children[title] = spawn(title)
                    prints[title] = client_fingerprint(load_config(), title)
            if not children:
                log("supervisor: nothing to monitor - add a client with "
                    "'eve_watch.py clients add <name>'")
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log("supervisor: stopping all watchers")
        for title in list(children):
            stop(title)


def cmd_mode(args):
    with open(MODEFILE, "w", encoding="utf-8") as fh:
        fh.write(args.name + "\n")
    p = PROFILES[args.name]
    print(f"Alert mode set to {args.name!r}: beep {p['beeps']} | voice {p['voice']} "
          f"| popup {p['popup']} | up to {p['repeat']} cycles")
    print("A running watcher picks this up within a couple of seconds.")


def cmd_pause(args):
    who = getattr(args, "client", None)
    with open(pause_path(who), "w", encoding="utf-8") as fh:
        fh.write(f"paused {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    if who:
        label = short_client(who)
        print(f"Paused {label!r} only - every other client keeps alerting.")
        print(f"Resume with:  python eve_watch.py resume --client {label!r}")
    else:
        print("Paused every client. The watchers keep running but will not alert.")
        print("Resume with:  python eve_watch.py resume")


def cmd_resume(args):
    who = getattr(args, "client", None)
    path = pause_path(who)
    if os.path.exists(path):
        os.remove(path)
        label = f" {short_client(who)!r}" if who else ""
        print(f"Resumed{label}. It re-baselines first, so nothing that happened "
              f"while paused will fire.")
        if who and os.path.exists(PAUSEFILE):
            print("Note: every client is still paused globally - clear that "
                  "with:  python eve_watch.py resume")
    elif who:
        print(f"{short_client(who)!r} was not paused on its own.")
    else:
        print("Not paused.")
        if paused_clients():
            print(f"Still paused on their own: {', '.join(paused_clients())}")


def cmd_status(args):
    running = [p for p in _watcher_pids()]
    print(f"watcher process: {'running, pid ' + ', '.join(map(str, running)) if running else 'NOT running'}")
    print(f"paused:          {'YES (all)' if os.path.exists(PAUSEFILE) else 'no'}")
    if paused_clients():
        print(f"paused on their own: {', '.join(paused_clients())}")
    if os.path.exists(LOGFILE):
        with open(LOGFILE, "r", encoding="utf-8") as fh:
            tail = fh.readlines()[-5:]
        print("last log lines:")
        for line in tail:
            print("   " + line.rstrip())


def _matching_pids(pattern):
    r"""Leaf PIDs whose command line matches `pattern`.

    A venv's Scripts\python.exe is a launcher that spawns the base interpreter,
    so both match and one process looks like two. The parent is only waiting;
    reporting it as well makes a single supervisor look like a duplicate.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -in "
             "'python.exe','pythonw.exe' -and $_.CommandLine -like '" + pattern +
             "' } | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
            capture_output=True, text=True, timeout=25,
            creationflags=CREATE_NO_WINDOW)
        pairs = []
        for line in out.stdout.splitlines():
            bits = line.split()
            if len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit():
                pairs.append((int(bits[0]), int(bits[1])))
        parents = {ppid for _, ppid in pairs}
        return [pid for pid, _ in pairs if pid not in parents]
    except Exception:
        return []


def _supervisor_pids():
    """PIDs actually running `eve_watch.py supervise`."""
    return _matching_pids("*eve_watch*supervise*")


def start_supervisor():
    """Launch a detached supervisor if none is running. Returns True if started."""
    if _supervisor_pids():
        return False
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "supervise"],
                     cwd=HERE, creationflags=CREATE_NO_WINDOW)
    return True


def _watcher_pids():
    """PIDs actually running the watch loop."""
    return _matching_pids("*eve_watch*watch --client*")



def cmd_tune(args):
    cfg = load_config()
    regions = pick_regions(cfg, args.name)
    win = resolve_window(args.client or regions[0]["window"])
    regions = regions_for(regions, win["title"])
    thr = cfg["settings"]["threshold"]
    cap = WindowCapture(win["hwnd"], update_ms=200).start()
    time.sleep(0.5)

    print(f"Sampling {args.seconds}s. Leave the values alone - moving the camera "
          f"is fine, that is what the anchor is for.\n")
    trackers = {r["name"]: Tracker(r, cfg["settings"]) for r in regions}
    frame, _ = cap.frame()
    refs, peaks, lost, lits = {}, {}, {}, {}
    for r in regions:
        box = trackers[r["name"]].locate(frame)
        refs[r["name"]] = None if box is None else text_mask(crop(frame, box), thr)
        peaks[r["name"]], lost[r["name"]] = 0, 0
        lits[r["name"]] = []

    end = time.time() + args.seconds
    while time.time() < end:
        frame, _ = cap.frame()
        for r in regions:
            n = r["name"]
            box = trackers[n].locate(frame)
            if box is None:
                lost[n] += 1
                continue
            m = text_mask(crop(frame, box), thr)
            lits[n].append(int(m.sum()))
            if refs[n] is None:
                refs[n] = m
                continue
            peaks[n] = max(peaks[n], mask_diff(m, refs[n]))
        print(f"\r  {int(end - time.time()):3d}s left   peak: "
              + "  ".join(f"{k}={v}" for k, v in peaks.items()), end="", flush=True)
        time.sleep(0.25)
    cap.stop()

    print("\n")
    for r in regions:
        n = r["name"]
        seq = lits[n] or [0]
        print(f"{n} [{r.get('mode', 'change')}]: max diff {peaks[n]} px, "
              f"lit {min(seq)}-{max(seq)} px")
        if lost[n]:
            print(f"  !! anchor lost on {lost[n]} samples - pick a bigger anchor.")
    worst = max(peaks.values())
    suggested = max(4, worst * 3)
    print(f"\nSuggested sensitivity: {suggested}")
    if args.apply:
        cfg["settings"]["sensitivity"] = suggested
        save_config(cfg)
        print("Written to config.json.")
    else:
        print("Apply with:  python eve_watch.py tune --apply")


def cmd_watch(args):
    cfg = load_config()
    s = cfg["settings"]
    # Per-client, because the cost is per client and so is the need: the one
    # watching a structure wants to catch a pilot in the seconds before they
    # can cloak, a scout on a quiet hole does not.
    want = args.client or ""
    per = s.get("client_interval") or {}
    interval = args.interval or next(
        (v for k, v in per.items() if k and (k == want or k in want)),
        s["interval"])
    sensitivity = args.sensitivity or s["sensitivity"]
    stable_needed = args.stable or s["stable"]
    thr = s["threshold"]
    obs_dir = args.obs_dir or s.get("obs_dir")
    global VOICE
    VOICE = args.voice_name or s.get("voice_name")
    if getattr(args, "nag_until_ack", None) is None:
        args.nag_until_ack = s.get("nag_until_ack", False)
    mode = args.mode or read_mode(s.get("mode", "away"))
    if getattr(args, "quiet", False):
        mode = "silent"
    apply_profile(args, mode)
    mode_stamp = os.path.getmtime(MODEFILE) if os.path.exists(MODEFILE) else 0
    global TAG
    regions = pick_regions(cfg, args.name)
    win = resolve_window(args.client or regions[0]["window"])
    # With several clients configured, only watch the ones belonging to this window.
    mine = [r for r in regions_for(regions, win["title"])
            if r.get("enabled", True)]
    if not mine:
        sys.exit(f"No regions configured for {win['title']!r}. "
                 f"Configured clients: {sorted({r.get('window','?') for r in cfg['regions']})}")
    regions = mine
    TAG = args.tag or short_client(win["title"])
    os.makedirs(EVENT_SHOTS, exist_ok=True)
    os.makedirs(BASE_SHOTS, exist_ok=True)
    started = time.time()

    cap = WindowCapture(win["hwnd"], update_ms=int(interval * 500)).start()
    time.sleep(0.5)
    frame, _ = cap.frame()

    if (frame.shape[1], frame.shape[0]) != (regions[0].get("win_width"),
                                            regions[0].get("win_height")):
        log(f"!! window is {frame.shape[1]}x{frame.shape[0]} but regions were picked "
            f"at {regions[0].get('win_width')}x{regions[0].get('win_height')} - re-run select")

    log(f"watching {[r['name'] for r in regions]} in {win['title']!r}")
    log(f"  every {interval}s | sensitivity {sensitivity}px | presence "
        f"{s['presence_pixels']}px | confirm {stable_needed} samples")
    log(f"  alert mode {mode!r}: beep {args.beeps} | voice {args.voice} | "
        f"popup {args.popup} | up to {args.repeat} cycles")
    log(f"  csv {CSVFILE}")
    if not (args.popup or args.voice or args.beeps):
        log("  QUIET MODE - events are logged but nothing will alert you")
    if obs_dir:
        vid = find_recording(obs_dir)
        log(f"  OBS folder {obs_dir} -> "
            f"{'recording ' + os.path.basename(vid) if vid else 'no active recording yet'}")
    log("  EVE may be covered or on another monitor, but must not be minimised.")
    log("  Ctrl+C to stop.")

    state = {}
    for r in regions:
        tr = Tracker(r, s)
        box = tr.locate(frame)
        mask = None if box is None else text_mask(crop(frame, box), thr)
        st = {"tr": tr, "ref": mask, "cand": None, "count": 0, "changes": 0,
              "lost_since": None, "mode": r.get("mode", "change"),
              "say": args.say or r.get("say") or f"{r['name']} changed",
              "floor": 0 if mask is None else int(mask.sum()),
              "zoom": r.get("zoom", True),
              "match": r.get("match", "mask"),
              "fuzzy": r.get("roster_fuzzy", s["roster_fuzzy"]),
              "require": re.compile(r["require"]) if r.get("require") else None,
              "identity": r.get("identity", "text"),
              "pitch": r.get("row_pitch", 20),
              "row_h": r.get("row_height", r.get("row_pitch", 20) - 2),
              "key_width": r.get("key_width"),
              "label_width": r.get("label_width"),
              "columns": r.get("columns") or None,
              "pilots_seen": set(),
              "pilot_reads": {},
              "seen_names": set(),
              "last_dist": {},
              "hole_dist": None,
              "pix_ncc": r.get("pix_ncc", 0.90),
              "pix_min_lit": r.get("pix_min_lit", 20),
              "row_offset": r.get("row_offset", 0),
              "pix_pad": r.get("pix_pad", 3),
              "pix_pad_x": r.get("pix_pad_x", 16),
              "pix_pad_y": r.get("pix_pad_y", 8),
              "pix_clip": r.get("pix_clip", s["threshold"]),
              "cfg": region_settings(r, s),
              "next_id": 0,
              "values_mtime": values_stamp(r["name"], r.get("window")),
              "bad_reads": 0,
              "ncc_min": r.get("ncc_min", s["ncc_min"]),
              "clip": r.get("clip", s["clip"]),
              "learned": {k: apply_clip(v, r.get("clip", s["clip"]))
                          if r.get("match") == "ncc" else v
                          for k, v in load_values(
                              r["name"], r.get("match") == "ncc",
                              r.get("window")).items()},
              "jitter": r.get("jitter", s["jitter"]),
              "sens": r.get("sensitivity", sensitivity),
              "value": None,
              "alert": r.get("alert", r.get("mode", "change") != "dscan"),
              "occupied": False}
        if st["mode"] == "presence":
            st["occupied"] = st["floor"] >= s["presence_pixels"]
        st["rows"], st["pending"], st["last_ocr"] = {}, {}, 0.0
        if st["identity"] == "pixels":
            st["pending"] = []
        st["last_set"], st["lost_alarmed"] = None, False
        if st["learned"] and box is not None:
            base_sig = signature(crop(frame, box), st, thr)
            mask = base_sig if st["match"] == "ncc" else mask
            st["ref"] = base_sig if st["match"] == "ncc" else st["ref"]
            st["value"] = classify(base_sig, st["learned"], st["sens"], st["jitter"],
                                   st["match"] == "ncc", st["ncc_min"])[0]
        if st["mode"] == "roster" and box is not None:
            if st["identity"] == "pixels":
                _label = label_by_row(frame, box, s["ocr_scale"], st["pitch"])
                reconcile_pixels(st, frame, box, thr,
                                 {**st["cfg"], "roster_confirm": 1}, _label)
            else:
                for row in ocr_rows(to_image(crop(frame, box)), s["ocr_scale"],
                                    r.get("key_width")):
                    if not ignored(row["text"], st["cfg"])                             and not is_noise_row(row["text"]):
                        st["rows"][row["key"]] = {"text": row["text"], "misses": 0}
        state[r["name"]] = st
        if box is not None:
            to_image(context_crop(frame, box, s["pad"])).save(
                os.path.join(BASE_SHOTS, f"{r['name']}_baseline.png"))
        reads = ""
        if st["learned"]:
            reads = (f", reads {st['value']!r}" if st["value"] is not None
                     else f", value NOT RECOGNISED (knows {sorted(st['learned'])})")
        log(f"  {r['name']} [{st['mode']}]: anchor "
            f"{'ON' if tr.tracking else 'off (fixed)'}, lit {st['floor']} px{reads}"
            f"{', OCCUPIED at start' if st['occupied'] else ''}")
        if st["mode"] == "roster" and not r.get("key_width"):
            log(f"      !! no key_width set - identity uses the whole row, so a "
                f"ticking velocity or a late-loading corp will double-report one "
                f"arrival. Re-run select with --key-width.")
        for v in st["rows"].values():
            log(f"      baseline row: {v['text']}")

    record_event(started, "-", "start", detail=win["title"], obs_dir=obs_dir)

    def rebaseline(frame):
        """Forget the paused window entirely - resume from what is on screen now."""
        for reg in regions:
            stt = state[reg["name"]]
            bx = stt["tr"].locate(frame)
            stt["cand"], stt["count"], stt["pending"] = None, 0, {}
            stt["ref"] = None if bx is None else text_mask(crop(frame, bx), thr)
            stt["floor"] = 0 if stt["ref"] is None else int(stt["ref"].sum())
            if stt["mode"] == "presence":
                stt["occupied"] = stt["floor"] >= s["presence_pixels"]
            if stt["mode"] == "roster" and bx is not None:
                stt["rows"] = {}
                for row in ocr_rows(to_image(crop(frame, bx)), s["ocr_scale"],
                                    reg.get("key_width")):
                    if not ignored(row["text"], st["cfg"]):
                        stt["rows"][row["key"]] = {"text": row["text"], "misses": 0}

    next_beat = time.time() + 60
    last_rec = 0.0
    stale_warned = False
    paused = False
    current_hwnd = win["hwnd"]
    last_reconnect = 0.0
    clip_seq = user32.GetClipboardSequenceNumber() if s["clipboard_sigs"] else 0
    known_sigs = {}
    clip_primed = False

    def fire(name, st, box, frame, event, detail, phrase=None, alarm=True):
        tag = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        shot = os.path.join(EVENT_SHOTS, f"{name}_{tag}.png")
        to_image(context_crop(frame, box, s["pad"])).save(shot)
        if st["zoom"]:
            to_image(crop(frame, box)).resize((box["width"] * 4, box["height"] * 4),
                                              Image.NEAREST).save(
                os.path.join(EVENT_SHOTS, f"{name}_{tag}_zoom.png"))
        vid, off = record_event(started, name, event, detail, shot, obs_dir)
        at = f"  [{vid} @ {off}]" if vid else ""
        log(f"*** {event.upper()} in {name!r}: {detail}  ->  {os.path.basename(shot)}{at}")
        if alarm:
            raise_alarm(phrase or st["say"], f"{name}: {detail}\n\n{shot}", args)

    sigs_book, sigs_dirty = load_sigs(), False
    book, book_dirty, book_saved = load_pilots(), False, time.time()
    # Anything newer than the book's last save has not been folded in yet -
    # including a mark written while the watchers were down, which starting
    # from the newest line in the file would have discarded for good.
    try:
        marks_at = os.path.getmtime(PILOTS)
    except OSError:
        marks_at = 0.0
    # Overview comings and goings, and structure counts that have not been
    # explained yet. The counter can fire up to five seconds EITHER side of the
    # overview event - measured across 16 changes, all within 5s - so both
    # sides have to be able to complete the pair.
    moves, pending_counts = [], []
    DOCK_WINDOW = 20.0
    try:
        book_stamp = os.path.getmtime(PILOTS)
    except OSError:
        book_stamp = None

    def note_move(key, direction, track=None, hole_at=None):
        """One pilot appearing on or leaving an overview."""
        nonlocal book_dirty
        who = book.get(key)
        name = who["name"] if who else key
        flying = ""
        if who is not None:
            who["last_dir"] = direction
            who["last_by"] = TAG
            # Dropping off the overview means they warped, cloaked or took a
            # hole - all of which leave them in space, still in that hull. Only
            # docking puts a ship away, so only a dock clears this.
            flying = who.get("now_ship") or who.get("last_ship") or ""
            if flying:
                who["last_ship"] = flying
            book_dirty = True

        # Taking a wormhole looks the same as any other disappearance, except
        # for WHERE it happened: you must be within 5km of a hole to activate
        # it. So compare the last distance seen against the hole's own distance
        # rather than against zero - this client sits 1900km off the hole, and
        # a ship on it reads the same distance the hole does.
        was_at = track[-1] if track else None
        # Warping off the hole also ends with the last sample showing them ON
        # it, because they are gone before the next read. A ship that was
        # already opening the range was leaving under its own power; one that
        # sat still and vanished took the hole.
        leaving = (len(track or []) >= 2
                   and track[-1] - track[-2] > 5_000)
        if (direction == "out" and was_at is not None and hole_at is not None
                and abs(was_at - hole_at) <= 10_000 and not leaving):
            in_what = f" in {flying}" if flying else ""
            if who is not None:
                who["jumps"] = who.get("jumps", 0) + 1
                who["last_note"] = f"took the hole {dt.datetime.now():%H:%M:%S}"
                book_dirty = True
            log(f"** {name}{in_what} TOOK THE WORMHOLE - vanished "
                f"{abs(was_at - hole_at)/1000:.1f}km from it, last seen at "
                f"{was_at/1000:.0f}km")
            record_event(started, "overview", "hole", f"{name}{in_what}",
                         obs_dir=obs_dir)
            raise_alarm(f"someone took the wormhole. {name}{in_what}",
                        f"{name}{in_what} vanished on the wormhole "
                        f"({abs(was_at - hole_at)/1000:.1f} km from it)", args)
        elif direction == "out":
            # Every pilot gets a status. Warping off is the commonest way to
            # leave and it matched no inference, which left the tab blank and
            # looking like something had failed.
            why = " while opening range" if leaving else ""
            at = f" at {was_at/1000:.0f}km" if was_at is not None else ""
            in_what = f" in {flying}" if flying else ""
            if who is not None:
                who["last_note"] = f"left {dt.datetime.now():%H:%M:%S}{at}"
                book_dirty = True
            log(f"   {name} left{at}{why}{in_what}"
                + (f" (hole is at {hole_at/1000:.0f}km)" if hole_at else ""))
        moves.append((time.time(), key, direction))
        del moves[:-40]
        settle_counts()

    def settle_counts():
        """Pair a structure count change with the overview event that explains it.

        A count going up while someone leaves the overview is that person
        docking; going down as someone appears is them undocking. Measured
        against 16 real changes, every one matched the expected direction
        inside five seconds. It stays an inference - the count moves for people
        who were never on your overview too - so it is logged as one.
        """
        nonlocal book_dirty
        now = time.time()
        del pending_counts[:max(0, len(pending_counts) - 8)]
        for entry in list(pending_counts):
            at, up = entry
            if now - at > DOCK_WINDOW:
                pending_counts.remove(entry)
                continue
            want = "out" if up else "in"
            hit = None
            for m_at, key, direction in reversed(moves):
                if direction == want and abs(m_at - at) <= DOCK_WINDOW:
                    hit = (m_at, key)
                    break
            if not hit:
                continue
            pending_counts.remove(entry)
            _, key = hit
            # Re-resolve: the move may have been filed under a spelling the
            # book has since folded in, and reporting the raw key printed a
            # pilot that does not exist ("meki baz docked").
            key = resolve_key(book, key)
            who = book.get(key)
            name = who["name"] if who else key
            verb = "docked" if up else "undocked"
            flying = ""
            if who is not None:
                flying = who.get("now_ship") or who.get("last_ship") or ""
                who["last_note"] = f"{verb} {dt.datetime.now():%H:%M:%S}"
                if up:                  # docked: the hull is put away
                    who.pop("now_ship", None)
                    who.pop("now_at", None)
                    who.pop("now_by", None)
                book_dirty = True
            in_what = f" in {flying}" if flying else ""
            log(f"** {name}{in_what} {verb} (structure count "
                f"{'up' if up else 'down'} within {abs(hit[0] - at):.0f}s of "
                f"the overview change)")
            record_event(started, "structure", verb, f"{name}{in_what}",
                         obs_dir=obs_dir)

    def record_sigs(st, frame, box, s):
        """Fold the probe scanner's rows into the signature file.

        Kept because it has to outlive the session: after downtime or a relog
        the question is which of these were already scanned, and EVE does not
        remember either.
        """
        nonlocal sigs_dirty
        for y, f in field_samples(frame, box, s["ocr_scale"], st["columns"]).items():
            if y < 0:
                continue
            sid = repair_sig_id(f.get("id"), sigs_book)
            if not sid:
                continue                # not a signature row, or unreadable
            kind, site, _ = classify_sig(f.get("name"), f.get("group"))
            if note_sig(sigs_book, sid, kind, site, TAG):
                sigs_dirty = True
                log(f"   sig {sid}: "
                    + (f"{kind}{' - ' + site if site else ''}" if kind or site
                       else "seen, not yet scanned"))

    def record_pilots(st, frame, box, s):
        """Fold every visible overview row into the pilot book.

        Every row, not only arrivals: the point is a record of who has been
        seen in what, and a pilot sitting on grid when the watcher starts is
        just as much a sighting as one that warps in.
        """
        nonlocal book_dirty
        best = field_samples(frame, box, s["ocr_scale"], st["columns"])
        here = set()

        # Only text sitting in an occupied row slot counts. Reading every line
        # in the box instead put a right-click menu into the book as eight
        # pilots - "Show Info", "Orbit (1 000 m)", "Remove Frigate from" - all
        # real text, none of it a row of the list. The grid is what separates
        # the list from whatever EVE paints on top of it.
        slots = []
        for cell in row_cells(box, st["pitch"], st["row_h"],
                              st["key_width"] or box["width"],
                              st["row_offset"]):
            patch = crop(frame, cell)
            if patch.shape[0] < cell["height"] or patch.shape[1] < cell["width"]:
                continue
            if int(text_mask(patch, thr).sum()) < st["pix_min_lit"]:
                continue
            slots.append(cell["top"] - box["top"] + cell["height"] / 2)

        for y, f in best.items():
            if not any(abs(y - c) <= max(3, st["pitch"] / 4) for c in slots):
                continue
            # Only the text fields. field_samples also returns the per-crop
            # vote tallies under "_votes", and joining those in crashed every
            # watcher on its first overview pass.
            whole = " ".join(v for k, v in f.items()
                             if k != "_votes" and isinstance(v, str) and v)
            if ignored(whole, st["cfg"]) or is_noise_row(whole):
                continue
            who = clean_field(f.get("name"))
            ship = clean_field(f.get("type"))
            if not looks_like_pilot(who, ship):
                # Not a pilot - but a wormhole in the list is the thing every
                # departure is measured against, so note where it is.
                if ship and who.casefold() == ship.casefold()                         and who.casefold().startswith("wormhole"):
                    st["hole_dist"] = parse_distance(f.get("distance"))
                continue

            # Being on the overview and being worth recording are different
            # questions. Presence must not wait for corroboration: the corp
            # column is the flakiest of the three, and folding it into the
            # confirmation made a pass that missed it look like the pilot had
            # gone - a false departure, and now a false "took the wormhole".
            key = resolve_key(book, who, st["seen_names"])
            st["seen_names"].add(key)
            here.add(key)

            # What gets WRITTEN still has to be corroborated, because the same
            # pixels read as "Porpoise", "Po Oise" and "os ect" depending on
            # nothing the tool controls. Two of the three crops agreeing inside
            # ONE pass is the better evidence, and unlike a count of passes it
            # survives a restart - which is what kept a pilot read correctly
            # twice out of the book, because the watcher was restarted between
            # the two sightings and the tally started again.
            votes = f.get("_votes") or {}
            agreed = (votes.get("name", {}).get(who, 0) >= 2
                      and votes.get("type", {}).get(ship, 0) >= 2)
            token = (key, ship)
            st["pilot_reads"][token] = st["pilot_reads"].get(token, 0) + 1
            if not agreed and st["pilot_reads"][token] < 2:
                continue
            far = parse_distance(f.get("distance"))
            if far is not None:
                seen_at = st["last_dist"].setdefault(key, [])
                seen_at.append(far)
                del seen_at[:-3]        # only the last few matter
            # A visit, not a poll: the roster is re-read every few seconds, so
            # counting every pass measured how long the client was open rather
            # than how often this pilot was actually seen.
            if note_pilot(book, who, ship, f.get("corporation"), TAG,
                          visit=key not in st["pilots_seen"]):
                book_dirty = True
            # What they are in NOW, as opposed to the tally of everything they
            # have ever flown. Stamped every pass so a watcher that dies cannot
            # leave someone looking like they are still on grid - the reader
            # decides how fresh is fresh.
            entry = book.get(key)
            if entry is not None:
                entry["now_ship"] = ship
                entry["now_by"] = TAG
                entry["now_at"] = dt.datetime.now().isoformat(timespec="seconds")
                book_dirty = True
        st["arrived_names"] = []
        for key in here - st["pilots_seen"]:
            entry = book.get(key)
            if entry:
                hull = entry.get("now_ship") or ""
                st["arrived_names"].append(
                    f"{entry['name']} {hull}".strip())
            note_move(key, "in")
        for key in st["pilots_seen"] - here:
            note_move(key, "out", track=st["last_dist"].get(key),
                      hole_at=st["hole_dist"])
        if here != st["pilots_seen"]:
            book_dirty = True
        st["pilots_seen"] = here

    try:
        while True:
            if paused_for(args.client or regions[0]["window"]):
                if not paused:
                    paused = True
                    mine = os.path.exists(pause_path(args.client
                                                      or regions[0]["window"]))
                    how = (f'resume --client "{TAG}"' if mine and TAG
                           else "resume")
                    log(f"|| PAUSED - resume with:  python eve_watch.py {how}")
                    record_event(started, "-", "pause", obs_dir=obs_dir)
                time.sleep(min(2.0, max(0.5, interval)))
                continue

            frame, stamp = cap.frame()
            now = time.time()
            age = now - stamp

            if paused:
                paused = False
                rebaseline(frame)
                log("|> RESUMED - re-baselined; nothing from the paused window alerts")
                record_event(started, "-", "unpause", obs_dir=obs_dir)
                next_beat = now + 60

            if age > 10 or cap.minimized:
                if not stale_warned:
                    why = "minimised" if cap.minimized else f"no new frames for {age:.0f}s"
                    log(f"!! EVE is {why} - BLIND until it renders again.")
                    record_event(started, "-", "blind", why, obs_dir=obs_dir)
                    threading.Thread(target=beep, args=(1,), daemon=True).start()
                    stale_warned = True

                # The client restarting (downtime, a relog) gives EVE a NEW window
                # handle, and the old capture session is dead for good. Without
                # this the process sits alive and silent forever.
                if (not cap.minimized and age > s["reconnect_after"]
                        and now - last_reconnect > s["reconnect_after"]):
                    last_reconnect = now
                    hits = list_windows(args.client or regions[0]["window"])
                    live = [h for h in hits if not h["minimized"]]
                    if live and live[0]["hwnd"] != current_hwnd:
                        try:
                            cap.stop()
                            cap = WindowCapture(
                                live[0]["hwnd"],
                                update_ms=int(interval * 500)).start()
                            current_hwnd = live[0]["hwnd"]
                            time.sleep(0.5)
                            fresh, _ = cap.frame()
                            rebaseline(fresh)
                            log(f"   reconnected to a new window handle "
                                f"{current_hwnd} - re-baselined")
                            record_event(started, "-", "reconnected",
                                         str(current_hwnd), obs_dir=obs_dir)
                            stale_warned = False
                        except Exception as exc:
                            log(f"!! reconnect failed: {exc}")

                # Heartbeat while blind too - silence must not look like health.
                if time.time() >= next_beat:
                    next_beat = time.time() + 60
                    log(f"   ...alive but BLIND ({age:.0f}s without a frame)")
                time.sleep(interval)
                continue
            if stale_warned:
                log("   ...frames are back.")
                record_event(started, "-", "resumed", obs_dir=obs_dir)
                stale_warned = False

            for r in regions:
                name = r["name"]
                st = state[name]
                box = st["tr"].locate(frame)

                if box is None:
                    if st["lost_since"] is None:
                        st["lost_since"] = now
                        st["lost_alarmed"] = False
                        log(f"!! {name}: anchor lost - staying quiet until found again")
                        record_event(started, name, "anchor_lost", obs_dir=obs_dir)
                    elif (not st["lost_alarmed"]
                          and now - st["lost_since"] >= s["lost_alarm_after"]):
                        # Going quiet is right for a brief loss, but staying quiet
                        # forever means silently not watching. Say so out loud.
                        st["lost_alarmed"] = True
                        gone = now - st["lost_since"]
                        log(f"!! {name}: STILL lost after {gone:.0f}s - this region "
                            f"is NOT being watched. Re-run select if the UI changed.")
                        record_event(started, name, "lost_alarm", f"{gone:.0f}s",
                                     obs_dir=obs_dir)
                        raise_alarm(f"watcher lost the {name} region",
                                    f"{name}: anchor lost for {gone:.0f}s.\n\n"
                                    f"This region is not being watched. If you "
                                    f"changed the EVE UI scale or moved a panel, "
                                    f"re-run select.",
                                    args)
                    st["cand"], st["count"] = None, 0
                    continue
                if st["lost_since"] is not None:
                    gone = time.time() - st["lost_since"]
                    log(f"   {name}: anchor re-acquired after {gone:.0f}s")
                    record_event(started, name, "anchor_found", f"{gone:.0f}s",
                                 obs_dir=obs_dir)
                    st["lost_since"], st["lost_alarmed"] = None, False

                cur = text_mask(crop(frame, box), thr)

                # ---- presence: empty list -> something in it ----------------
                if st["mode"] == "presence":
                    lit = int(cur.sum())
                    now_occupied = lit >= st["floor"] + s["presence_pixels"]
                    if now_occupied == st["occupied"]:
                        st["count"] = 0
                        continue
                    st["count"] += 1
                    if st["count"] < stable_needed:
                        continue
                    st["occupied"] = now_occupied
                    st["count"] = 0
                    if now_occupied:
                        st["changes"] += 1
                        fire(name, st, box, frame, "present",
                             f"{lit} lit px in the list", alarm=st["alert"])
                    else:
                        log(f"   {name}: list is empty again ({lit} px)")
                        record_event(started, name, "clear", f"{lit} px", obs_dir=obs_dir)
                    continue

                # ---- dscan: log the whole result set every time it changes ---
                if st["mode"] == "dscan":
                    if st["ref"] is None:
                        st["ref"], st["last_set"] = cur, None
                        continue
                    moved = mask_diff(cur, st["ref"]) > sensitivity
                    st["count"] = st["count"] + 1 if moved else 0
                    if not (moved and st["count"] >= stable_needed):
                        continue
                    st["count"], st["ref"] = 0, cur

                    rows = [row["text"] for row in
                            ocr_rows(to_image(crop(frame, box)), s["ocr_scale"],
                                     r.get("key_width"))
                            if not ignored(row["text"], st["cfg"])
                            and not is_noise_row(row["text"])]
                    sig = tuple(sorted(row_key(t) for t in rows))
                    if sig == st.get("last_set"):
                        continue
                    st["last_set"] = sig
                    st["changes"] += 1
                    detail = (f"{len(rows)} result(s): " + " | ".join(rows)
                              if rows else "0 results")
                    log(f"   {name}: {detail}")
                    fire(name, st, box, frame, "dscan", detail,
                         alarm=st["alert"])
                    continue

                # ---- roster: who is in the list, by name ---------------------
                if st["mode"] == "roster":
                    if st["ref"] is None:
                        st["ref"] = cur
                        continue

                    moved = mask_diff(cur, st["ref"]) > sensitivity
                    st["count"] = st["count"] + 1 if moved else 0
                    # A departure needs two passes to confirm, but the pixels only
                    # move ONCE when someone leaves - so a pixel-gated pass alone
                    # would strand it at one miss forever. Re-scan on a timer too.
                    #
                    # A row waiting on its second look must NOT wait out that
                    # timer. Confirming took the full roster_period because the
                    # row is static by then, so nothing moves to trigger the
                    # follow-up: an arrival cost three passes to notice plus five
                    # seconds to confirm, and a ship can warp off or dock inside
                    # eight seconds. Come straight back while anything is
                    # in flight - which is exactly when it is worth the OCR.
                    waiting = bool(st.get("pending")) or any(
                        v.get("misses") for v in st["rows"].values())
                    need_stable = 2 if st["identity"] == "pixels" else stable_needed

                    # A row appearing adds a row's worth of lit pixels. A number
                    # ticking in a column does not, so this separates "someone
                    # arrived" from "a velocity changed" for free - the mask is
                    # already computed - and lets an arrival skip the settling
                    # passes without OCR running every time a distance updates.
                    # Five times the per-row floor: a measured row carries
                    # 545-906 lit px, while digits appearing in a velocity
                    # column carry well under a hundred. Getting it wrong costs
                    # one extra OCR pass, never a wrong arrival - identity still
                    # needs its two confirmations - so the margin is generous.
                    grew = (int(cur.sum()) - int(st["ref"].sum())
                            >= st["pix_min_lit"] * 5)
                    due = ((moved and st["count"] >= need_stable)
                           or grew
                           or waiting
                           or now - st["last_ocr"] >= s["roster_period"])
                    if not due:
                        continue
                    st["count"], st["ref"], st["last_ocr"] = 0, cur, now

                    if st["identity"] == "pixels":
                        _label = label_by_row(frame, box, s["ocr_scale"],
                                              st["pitch"], st["label_width"])
                        # Signature rows carry a unique id, so give the
                        # matcher a second opinion for them: pixels decide
                        # normally, and the id settles a row that changed
                        # appearance rather than actually leaving.
                        _idf = None
                        if name.startswith("sigs") and st["columns"]:
                            def _idf(cell, _st=st, _lab=_label):
                                return repair_sig_id(_lab(cell), sigs_book)
                        arrived, departed = reconcile_pixels(
                            st, frame, box, thr, st["cfg"], _label, id_fn=_idf)
                        # Three OCR passes at 313ms, and on a quiet grid they
                        # re-read text that has not changed. Run them when the
                        # roster actually moved, and otherwise only often enough
                        # to keep "in space now" fresh. Must come AFTER the
                        # reconcile, which is what says whether it moved.
                        if st["columns"] and name.startswith("overview"):
                            if arrived or departed or now - st.get("pilots_at", 0) >= 25:
                                st["pilots_at"] = now
                                record_pilots(st, frame, box, s)
                        elif st["columns"] and name.startswith("sigs"):
                            # A signature is its id, not its pixels. Scanning
                            # rewrites the Name and Group columns of a row that
                            # was already there, which looks like an arrival to
                            # anything comparing images - so announce only ids
                            # the file has never held. Note the known set BEFORE
                            # recording, or every id looks familiar by the time
                            # the alert is decided.
                            known_ids = set(sigs_book)
                            if arrived or departed or now - st.get("sigs_at", 0) >= 20:
                                st["sigs_at"] = now
                                record_sigs(st, frame, box, s)
                            keep = []
                            for label in arrived:
                                sid = repair_sig_id(label, sigs_book)
                                if sid and sid not in known_ids:
                                    keep.append(label)
                                elif sid:
                                    log(f"   {name}: {sid} changed, not new "
                                        f"- no alert")
                                else:
                                    log(f"   {name}: unreadable row changed "
                                        f"- no alert")
                            arrived = keep
                        reasons = st.pop("why", [])
                        for i, gone in enumerate(departed):
                            why = reasons[i] if i < len(reasons) else ""
                            log(f"   {name}: left - {gone}"
                                + (f"   [{why}]" if why else ""))
                            record_event(started, name, "depart", gone,
                                         obs_dir=obs_dir)
                        if arrived:
                            st["changes"] += len(arrived)
                            # Prefer the three-pass merged reading over the
                            # single-pass label: the label is what put
                            # "_RussianRevolution -Helios" in the log.
                            shown = st.get("arrived_names") or arrived
                            st["arrived_names"] = []
                            detail = " | ".join(shown)
                            phrase = (f"{st['say']}. {shown[0]}"
                                      if len(shown) == 1
                                      else f"{st['say']}. {len(shown)} new")
                            fire(name, st, box, frame, "arrive", detail, phrase,
                                 alarm=st["alert"])
                        continue

                    seen, malformed = {}, []
                    for row in ocr_rows(to_image(crop(frame, box)), s["ocr_scale"],
                                        r.get("key_width")):
                        if ignored(row["text"], st["cfg"]) or is_noise_row(row["text"]):
                            continue
                        if st["require"] and not st["require"].match(row["key"]):
                            malformed.append(row["key"])
                            continue
                        seen[row["key"]] = row["text"]

                    # A row whose id does not even match the expected shape is a
                    # bad OCR pass, not news. Acting on it invents arrivals and
                    # departures for rows that never changed, so wait for a clean
                    # read instead - the next one is at most roster_period away.
                    if malformed:
                        st["bad_reads"] += 1
                        if st["bad_reads"] in (1, 25, 250):
                            log(f"   {name}: unreadable row(s) {malformed} - new "
                                f"rows still detected, departures deferred to a "
                                f"clean pass ({st['bad_reads']} so far)")

                    arrived, departed = reconcile_roster(
                        st, seen, {**st["cfg"], "roster_fuzzy": st["fuzzy"]},
                        allow_depart=not malformed)
                    for gone in departed:
                        log(f"   {name}: left - {gone}")
                        record_event(started, name, "depart", gone, obs_dir=obs_dir)

                    if arrived:
                        st["changes"] += len(arrived)
                        detail = " | ".join(arrived)
                        phrase = (f"{st['say']}. {arrived[0]}" if len(arrived) == 1
                                  else f"{st['say']}. {len(arrived)} new contacts")
                        fire(name, st, box, frame, "arrive", detail, phrase,
                             alarm=st["alert"])
                    continue

                # ---- change: the value is no longer what it was -------------
                if st["ref"] is None:
                    st["ref"] = cur
                    continue
                if st["match"] == "ncc":
                    cur = signature(crop(frame, box), st, thr)
                if same_sig(cur, st["ref"], st)[0]:
                    st["cand"], st["count"] = None, 0
                    continue
                if st["cand"] is not None and same_sig(cur, st["cand"], st)[0]:
                    st["count"] += 1
                else:
                    st["cand"], st["count"] = cur, 1
                if st["count"] >= stable_needed:
                    st["changes"] += 1
                    d = same_sig(st["cand"], st["ref"], st)[1]
                    unit = "corr" if st["match"] == "ncc" else "px differ"
                    detail, phrase = f"{d} {unit}", None
                    if st["learned"]:
                        was = st["value"]
                        now_val = classify(st["cand"], st["learned"], st["sens"],
                                           st["jitter"], st["match"] == "ncc",
                                           st["ncc_min"])[0]
                        st["value"] = now_val
                        shown = now_val if now_val is not None else "?"
                        detail = f"{was if was is not None else '?'} -> {shown}"
                        phrase = (f"{st['say']}. now {shown}"
                                  if now_val is not None else st["say"])
                        try:
                            if int(now_val) != int(was):
                                pending_counts.append(
                                    (time.time(), int(now_val) > int(was)))
                                settle_counts()
                        except (TypeError, ValueError):
                            pass         # a count that reads "?" explains nothing
                    fire(name, st, box, frame, "change", detail, phrase,
                         alarm=st["alert"])
                    st["ref"], st["cand"], st["count"] = st["cand"], None, 0

            ms = os.path.getmtime(MODEFILE) if os.path.exists(MODEFILE) else 0
            if ms != mode_stamp:
                mode_stamp = ms
                new_mode = read_mode(s.get("mode", "away"))
                if new_mode != mode:
                    mode = new_mode
                    for key, value in PROFILES[mode].items():
                        setattr(args, key, value)
                    log(f"   alert mode -> {mode!r}: beep {args.beeps} | voice "
                        f"{args.voice} | popup {args.popup} | {args.repeat} cycles")

            for r in regions:
                st = state[r["name"]]
                mt = values_stamp(r["name"], r.get("window"))
                if mt != st["values_mtime"]:
                    st["values_mtime"] = mt
                    st["learned"] = {
                        k: apply_clip(v, st["clip"]) if st["match"] == "ncc" else v
                        for k, v in load_values(
                            r["name"], st["match"] == "ncc", r.get("window")).items()}
                    log(f"   {r['name']}: reloaded taught values "
                        f"{sorted(st['learned'])} - no restart needed")

            if s["clipboard_sigs"] and clipboard_owner():
                seq = user32.GetClipboardSequenceNumber()
                if seq != clip_seq:
                    clip_seq = seq
                    sigs = parse_signatures(read_clipboard())
                    if sigs:
                        if not clip_primed:
                            # Nothing to compare the first paste against, so adopt
                            # it as the baseline rather than calling all of it new.
                            clip_primed = True
                            known_sigs = sigs
                            log(f"   clipboard: baselined {len(sigs)} signature(s) "
                                f"- later pastes report only what is new")
                            record_event(started, "clipboard", "sigs_baseline",
                                         f"{len(sigs)}: " + ", ".join(sorted(sigs)),
                                         obs_dir=obs_dir)
                            continue
                        # A paste carries no system name, so jumping looks exactly
                        # like every signature in one system changing at once.
                        # Sharing not one id with what we knew means you moved:
                        # adopt the new list quietly rather than alarm on all of it.
                        if not (set(sigs) & set(known_sigs)):
                            known_sigs = sigs
                            log(f"   clipboard: {len(sigs)} signature(s) from a "
                                f"different system - re-baselined, no alarm")
                            record_event(started, "clipboard", "sigs_system",
                                         f"{len(sigs)}: " + ", ".join(sorted(sigs)),
                                         obs_dir=obs_dir)
                            continue

                        fresh = {k: v for k, v in sigs.items() if k not in known_sigs}
                        gone = [k for k in known_sigs if k not in sigs]
                        known_sigs = sigs
                        log(f"   clipboard: {len(sigs)} signature(s) pasted"
                            + (f", {len(fresh)} new" if fresh else ""))
                        record_event(started, "clipboard", "sigs",
                                     f"{len(sigs)} total: " + ", ".join(sorted(sigs)),
                                     obs_dir=obs_dir)
                        exact = parse_signature_rows(read_clipboard() or "")
                        for sid, info in exact.items():
                            if note_sig(sigs_book, sid, info["type"],
                                        info["name"], TAG, exact=True):
                                sigs_dirty = True
                        if fresh:
                            detail = " | ".join(fresh[k] for k in sorted(fresh))
                            log(f"*** NEW SIGNATURE(S): {detail}")
                            record_event(started, "clipboard", "new_sig", detail,
                                         obs_dir=obs_dir)
                            raise_alarm(
                                f"{len(fresh)} new signature"
                                f"{'s' if len(fresh) > 1 else ''}",
                                f"New signature(s):\n\n{detail}", args, attribute=False)
                        if gone:
                            record_event(started, "clipboard", "sig_gone",
                                         ", ".join(sorted(gone)), obs_dir=obs_dir)

            if args.record and time.time() - last_rec >= args.record:
                last_rec = time.time()
                rec = os.path.join(SHOTS, "record")
                os.makedirs(rec, exist_ok=True)
                b = state[regions[0]["name"]]["tr"].locate(frame) or regions[0]["target"]
                to_image(context_crop(frame, b, s["pad"])).save(
                    os.path.join(rec, dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"))

            fresh_marks = read_marks(marks_at)
            if fresh_marks:
                for at, what, key in fresh_marks:
                    marks_at = max(marks_at, at)
                    who = book.get(resolve_key(book, key))
                    if who is None:
                        continue
                    if what == "docked":
                        for f in ("now_ship", "now_at", "now_by"):
                            who.pop(f, None)
                        who["last_note"] = (f"docked "
                                            f"{dt.datetime.now():%H:%M:%S}"
                                            f" (marked by hand)")
                    elif what == "forget":
                        book.pop(resolve_key(book, key), None)
                    log(f"   pilot mark applied: {what} {key!r}")
                    book_dirty = True

            if sigs_dirty:
                save_sigs(sigs_book)
                sigs_dirty = False

            if book_dirty and time.time() - book_saved > 20:
                # Someone editing the file while a watcher runs would otherwise
                # lose the edit: the in-memory copy is flushed straight back
                # over it. That is exactly how a pruned entry reappeared.
                try:
                    disk = os.path.getmtime(PILOTS)
                except OSError:
                    disk = None
                if disk != book_stamp:
                    log("   pilots.json changed on disk - reloading before save")
                    fresh = load_pilots()
                    for key, who in book.items():
                        if key not in fresh:
                            continue        # dropped outside; leave it dropped
                        fresh[key] = who
                    book = fresh
                save_pilots(book)
                try:
                    book_stamp = os.path.getmtime(PILOTS)
                except OSError:
                    book_stamp = None
                book_dirty, book_saved = False, time.time()

            if time.time() >= next_beat:
                next_beat = time.time() + 60
                bits = []
                for k, v in state.items():
                    tag = f"{k}: {v['changes']} chg"
                    if v["learned"]:
                        tag += f", now {v['value'] if v['value'] is not None else '?'}"
                    if v["mode"] == "presence":
                        tag += "(occupied)" if v["occupied"] else "(empty)"
                    if v["mode"] == "roster":
                        tag += f"({len(v['rows'])} rows)"
                    if v["mode"] == "dscan":
                        tag += f"({len(v['last_set'] or ())} results)"
                    if v["tr"].tracking and v["tr"].drift != (0, 0):
                        tag += f" drift{v['tr'].drift}"
                    bits.append(tag)
                log("   ...alive  " + "  ".join(bits))

            time.sleep(interval)
    except KeyboardInterrupt:
        log("stopped by user.")
        record_event(started, "-", "stop", obs_dir=obs_dir)
    finally:
        cap.stop()


# ------------------------------------------------------------------- main ---

def main():
    p = argparse.ArgumentParser(
        description="Watch numbers and lists in an EVE Online client window.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("windows", help="list candidate windows")
    sp.add_argument("--filter", default="EVE - ")
    sp.set_defaults(func=cmd_windows)

    sp = sub.add_parser("add-panel",
                        help="point at a panel and calibrate it, whatever its title")
    sp.add_argument("kind", choices=[p["kind"] for p in PANELS])
    sp.add_argument("--client")
    sp.add_argument("--name", help="region name (defaults to the kind)")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_add_panel)

    sp = sub.add_parser("calibrate",
                        help="find this client's panels and build its regions")
    sp.add_argument("--client")
    sp.add_argument("--yes", action="store_true", help="write the config")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("select", help="pick a region to watch")
    sp.add_argument("--name", default="structure")
    sp.add_argument("--client", help="window title substring, e.g. 'Your Character'")
    sp.add_argument("--mode", choices=["change", "presence", "roster", "dscan"],
                    default="change",
                    help="change = value is no longer what it was; "
                         "presence = an empty list gained a row; "
                         "roster = OCR the list and name who arrived; "
                         "dscan = log the whole result set whenever it changes")
    sp.add_argument("--say", help="what the voice says for this region")
    sp.add_argument("--no-zoom", dest="zoom", action="store_false",
                    help="do not save the zoomed crop, only the wide context shot")
    sp.add_argument("--silent", dest="alert", action="store_false",
                    help="log this region but never sound the alarm")
    sp.add_argument("--key-width", dest="key_width", type=int,
                    help="roster: identity uses only words this many px from the "
                         "left of the box, keeping churning columns out of it")
    sp.set_defaults(func=cmd_select, zoom=True, alert=True)

    sp = sub.add_parser("shot", help="save a zoomed preview of what is watched")
    sp.add_argument("--name")
    sp.add_argument("--client")
    sp.set_defaults(func=cmd_shot)

    sp = sub.add_parser("list", help="print config")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("learn", help="teach it the region's current value")
    sp.add_argument("--value", required=True, help="what it reads right now, e.g. 4")
    sp.add_argument("--name")
    sp.add_argument("--client")
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("hub", help="one window to launch and control everything")
    sp.add_argument("--seconds", type=float, help="auto-close (for testing)")
    sp.set_defaults(func=cmd_hub)

    sp = sub.add_parser("events", help="live, filterable view of events.csv")
    sp.add_argument("--every", type=float, default=2.0,
                    help="seconds between checks for new events")
    sp.add_argument("--seconds", type=float, help="auto-close (for testing)")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("dash", help="live status window")
    sp.add_argument("--every", type=float, default=6.0,
                    help="seconds between refreshes")
    sp.add_argument("--seconds", type=float, help="auto-close (for testing)")
    sp.set_defaults(func=cmd_dash)

    sp = sub.add_parser("doctor", help="check everything an alert depends on")
    sp.add_argument("--fast", action="store_true",
                    help="skip capturing frames (no anchor check)")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("pick", help="tick which clients to watch, in a window")
    sp.add_argument("--seconds", type=float,
                    help="auto-close after N seconds (used for testing)")
    sp.set_defaults(func=cmd_pick)

    sp = sub.add_parser("clients", help="choose which clients to monitor")
    sp.add_argument("action", choices=["list", "add", "remove", "set"])
    sp.add_argument("names", nargs="*")
    sp.set_defaults(func=cmd_clients)

    sp = sub.add_parser("clone", help="copy one client's regions to another")
    sp.add_argument("--from", dest="src", required=True)
    sp.add_argument("--to", dest="dst", required=True)
    sp.add_argument("--only", help="comma-separated region names, e.g. dscan,overview")
    sp.set_defaults(func=cmd_clone)

    sp = sub.add_parser("supervise", help="run a watcher per selected client")
    sp.add_argument("--poll", type=float, default=5.0)
    sp.add_argument("--force", action="store_true",
                    help="start even if another supervisor is running")
    sp.add_argument("--mode", choices=sorted(PROFILES))
    sp.add_argument("--interval", type=float)
    sp.add_argument("--sensitivity", type=int)
    sp.add_argument("--stable", type=int)
    sp.add_argument("--obs-dir", dest="obs_dir")
    sp.add_argument("--webhook")
    sp.add_argument("--sound")
    sp.add_argument("--repeat", type=int)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_supervise)

    sp = sub.add_parser("mode", help="switch alert profile while it runs")
    sp.add_argument("name", choices=sorted(PROFILES))
    sp.set_defaults(func=cmd_mode)

    sp = sub.add_parser("pause", help="stop alerting without stopping the watcher")
    sp.add_argument("--client", help="pause only this client, e.g. while you "
                                     "probe on a scout (default: all)")
    sp.set_defaults(func=cmd_pause)

    sp = sub.add_parser("resume", help="start alerting again (re-baselines first)")
    sp.add_argument("--client", help="resume only this client (default: all)")
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("status", help="is it running, is it paused, recent log")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("tune", help="measure noise, suggest a sensitivity")
    sp.add_argument("--name")
    sp.add_argument("--client")
    sp.add_argument("--seconds", type=int, default=30)
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_tune)

    sp = sub.add_parser("watch", help="run the monitor")
    sp.add_argument("--name")
    sp.add_argument("--client")
    sp.add_argument("--interval", type=float)
    sp.add_argument("--sensitivity", type=int)
    sp.add_argument("--stable", type=int)
    sp.add_argument("--obs-dir", dest="obs_dir",
                    help="OBS recording folder; logs the timecode inside the video")
    sp.add_argument("--record", type=float, metavar="SECS",
                    help="also save a context frame every SECS seconds")
    sp.add_argument("--webhook", help="POST a Discord-style JSON message on change")
    sp.add_argument("--say", metavar="TEXT", help="override every region's phrase")
    sp.add_argument("--sound", metavar="WAV", help="play this .wav instead of beeps")
    sp.add_argument("--repeat", type=int,
                    help="alert cycles; keeps going while the popup is unacknowledged")
    sp.add_argument("--no-voice", dest="voice", action="store_false")
    sp.add_argument("--no-popup", dest="popup", action="store_false")
    sp.add_argument("--no-beep", dest="beeps", action="store_false")
    sp.add_argument("--nag-until-ack", dest="nag_until_ack", action="store_true",
                    help="keep repeating until the popup is dismissed")
    sp.add_argument("--tag", help="label for this client in the log and csv")
    sp.add_argument("--voice-name", dest="voice_name",
                    help="TTS voice to use, e.g. Mark / Zira / David")
    sp.add_argument("--mode", choices=sorted(PROFILES),
                    help="alert profile: active = sound only x2, away = sound + "
                         "voice + popup, silent = log only")
    sp.add_argument("--quiet", action="store_true",
                    help="log and snapshot everything, but make no noise at all")
    sp.set_defaults(func=cmd_watch, popup=None, voice=None, beeps=None,
                    nag_until_ack=None)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
