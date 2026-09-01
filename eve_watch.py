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
import json
import os
import re
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
VALUES = os.path.join(HERE, "values")      # learned glyph masks, per region+value
MODEFILE = os.path.join(HERE, "MODE")      # alert profile, switchable while running
CLIENTSFILE = os.path.join(HERE, "CLIENTS")   # which clients the supervisor runs
TAG = ""                                   # client label prefixed to this process's logs

PROFILES = {
    # how loudly to alert, by what you are doing at the time
    "active": {"popup": False, "voice": True, "beeps": True, "repeat": 2},
    "away":   {"popup": True, "voice": True, "beeps": True, "repeat": 3},
    "silent": {"popup": False, "voice": False, "beeps": False, "repeat": 1},
}

DEFAULTS = {
    "threshold": 110,        # pixel brightness 0-255 that counts as "text"
    "sensitivity": 8,        # changed pixels before we care (mode=change)
    "presence_pixels": 20,   # lit pixels above empty before "occupied" (presence)
    "interval": 1.0,         # seconds between samples
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
}

CSV_COLS = ["iso", "unix", "client", "elapsed_s", "elapsed_hms", "region",
            "event", "detail", "snapshot", "video_file", "video_offset"]

CREATE_NO_WINDOW = 0x08000000
user32 = ctypes.windll.user32


# ---------------------------------------------------------------- config ----

def load_config():
    cfg = {"regions": [], "settings": dict(DEFAULTS)}
    if os.path.exists(CONFIG):
        with open(CONFIG, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        cfg["regions"] = disk.get("regions", [])
        cfg["settings"].update(disk.get("settings", {}))
    return cfg


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


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
        out.append({"text": text,
                    "key": row_key(normalise_glyphs(" ".join(kept))),
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


def label_by_row(frame, box, scale, pitch):
    """Label rows from ONE whole-box OCR pass, matched by vertical centre.

    OCR of a single 18px row strip drops the leading id column - it needs the
    surrounding lines for context - so read the whole list once and map each
    row back to its slot instead.
    """
    rows = ocr_rows(to_image(crop(frame, box)), scale)

    def label(cell):
        want = cell["top"] - box["top"] + cell["height"] / 2
        best, gap = None, None
        for r in rows:
            d = abs(r["y"] - want)
            if gap is None or d < gap:
                best, gap = r["text"], d
        return best if best is not None and gap <= pitch else "(unreadable)"

    return label


def reconcile_pixels(st, frame, box, threshold, settings, label_fn,
                     allow_depart=True):
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
    occupied = []
    for cell in row_cells(box, st["pitch"], st["row_h"],
                          st["key_width"] or box["width"], st["row_offset"]):
        patch = crop(frame, cell)
        if patch.shape[0] < cell["height"] or patch.shape[1] < cell["width"]:
            continue
        if int(text_mask(patch, threshold).sum()) < st["pix_min_lit"]:
            continue
        wide = {"left": cell["left"] - pad, "top": cell["top"] - pad,
                "width": cell["width"] + 2 * pad,
                "height": cell["height"] + 2 * pad}
        occupied.append((to_gray(crop(frame, wide)), to_gray(patch), cell))

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
            if st["rows"][k]["misses"] >= need:
                departed.append(st["rows"].pop(k)["text"])

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
            st["rows"][f"px{st['next_id']}"] = {"bitmap": exact, "text": label,
                                                "misses": 0}
            # Track it either way so it is not re-reported, but stay silent for
            # permanent scenery the ignore list already rules out.
            if not ignored(label, settings):
                arrived.append(label)
        else:
            still.append((exact, label, hits))
    st["pending"] = still
    return arrived, departed


SIG_LINE = re.compile(r"^([A-Z]{3}-\d{3})\t([^\t\n]*)\t?([^\t\n]*)", re.M)


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
           "region": region, "event": event, "detail": detail,
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


def raise_alarm(phrase, body, opts):
    """Beep instantly, then nag by voice until the popup is acknowledged.

    With several clients watched at once the first thing you need to know is
    WHICH one to switch to, so the client leads both the spoken alert and the
    popup rather than being buried in the log.
    """
    if TAG:
        phrase = f"{TAG}. {phrase}"
        body = f"Client: {TAG}\n\n{body}"
    title = f"EVE watch - {TAG}" if TAG else "EVE watch"
    if opts.popup:
        threading.Thread(target=popup, args=(title, body), daemon=True).start()

    if not getattr(opts, "beeps", True) and not opts.popup and not opts.voice:
        return                      # --quiet: log and snapshot, make no noise

    def nag():
        cycles = 0
        while cycles < 60:
            if getattr(opts, "beeps", True):
                beep(2, opts.sound)
            if opts.voice:
                speak(phrase)
            cycles += 1
            if cycles >= opts.repeat and not (opts.popup and _popup_busy.is_set()):
                break
            time.sleep(0.6)

    threading.Thread(target=nag, daemon=True).start()
    if opts.webhook:
        threading.Thread(target=post_webhook, args=(opts.webhook, f"**EVE watch** - {phrase}"),
                         daemon=True).start()


# ------------------------------------------------------------- select UI ----

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
     "say": "new contact in {label}", "ignore": ["Sun", "Fortizar"]},
    {"kind": "sigs", "title": ["probe", "scanner"], "mode": "roster",
     "headers": ["distance", "id", "name", "group", "signal"],
     "id_upto": "name", "id_from": "id",
     "say": "new signature on the probe scanner",
     # the panel footer sits below the list and would otherwise be tracked as a row
     "ignore": ["launched", "No Results"]},
    {"kind": "dscan", "title": ["directional", "scanner"], "mode": "dscan",
     "headers": ["distance", "name", "type"],
     "id_upto": None, "id_from": "name",
     "say": "d-scan updated", "ignore": ["No Scan Results"]},
]


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


def panel_geometry(panel, words, bounds):
    """Work out the header row, columns and row pitch of one panel."""
    spec = panel["spec"]
    x_lo, x_hi, y_lo, y_hi = bounds
    below = [w for w in words
             if y_lo < w["y"] < min(y_hi, y_lo + 170) and x_lo <= w["x"] < x_hi]

    # the header row is the first line under the title holding known header words
    rows = {}
    for w in below:
        rows.setdefault(round(w["y"] / 6) * 6, []).append(w)
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

    # data rows: lines below the header, spaced evenly
    data_ys = sorted({round(w["y"]) for w in below if w["y"] > header_y + 6})
    merged = []
    for y in data_ys:
        if not merged or y - merged[-1] > 4:
            merged.append(y)
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

    return {"header_y": header_y, "first_row": first_row, "pitch": pitch,
            "measured_pitch": measured, "box_left": box_left,
            "box_right": min(x_hi, right_edge + 6), "key_width": key_width,
            "columns": {k: v["x"] for k, v in header.items()}}


def known_pitch(cfg, kind, win_w, win_h):
    """A row pitch already measured for this kind of panel at this window size.

    A list with fewer than two rows cannot reveal its own spacing, but the same
    panel on another client at the same UI scale can - and that is exact, where
    guessing from the header gap is a pixel or two out and drifts down the list.
    """
    for r in cfg.get("regions", []):
        if (r.get("row_pitch") and r.get("win_width") == win_w
                and r.get("win_height") == win_h
                and re.sub(r"\d+$", "", r["name"]) == kind):
            return r["row_pitch"]
    return None


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
    panels = find_panels(words)
    if not panels:
        sys.exit("Found no Overview / Probe Scanner / Directional Scanner panels.")

    # number repeats: overview, overview2, ...
    seen, proposals = {}, []
    for p in sorted(panels, key=lambda p: (p["spec"]["kind"], p["title_x"])):
        kind = p["spec"]["kind"]
        seen[kind] = seen.get(kind, 0) + 1
        p["label"] = kind if seen[kind] == 1 else f"{kind}{seen[kind]}"
        proposals.append(p)

    # a panel may only claim space up to the next panel to its right / below
    for p in proposals:
        x_lo, x_hi, y_lo, y_hi = panel_bounds(p, proposals, fw, fh)
        geo = panel_geometry(p, words, (x_lo, x_hi, y_lo, y_hi))
        if geo and "error" not in geo and not geo["measured_pitch"]:
            borrowed = known_pitch(cfg, p["spec"]["kind"], fw, fh)
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
            "anchor": anchor, "key_width": geo["key_width"],
            "max_drift": drift, "ignore": list(spec["ignore"]),
            "zoom": True, "alert": spec["mode"] != "dscan",
            "say": spec["say"].format(label=p["label"]),
        }
        if spec["mode"] == "roster":
            region.update(identity="pixels", row_pitch=geo["pitch"],
                          row_height=max(8, geo["pitch"] - 2), row_offset=8,
                          pix_ncc=0.95, pix_min_lit=20)
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
    keep = [r for r in cfg["regions"]
            if r.get("window") != win["title"]
            or r["name"] not in {x["name"] for x in regions}]
    cfg["regions"] = keep + regions
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


def cmd_pick(args):
    """Choose clients and regions in a window.

    A client with no regions cannot be ticked - a watcher pointed at an
    unconfigured client starts happily and watches nothing, which is the
    failure mode hardest to notice. Calibrate it from here instead.
    """
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("eve-watch - clients")
    root.attributes("-topmost", True)
    root.configure(padx=16, pady=12)
    body = tk.Frame(root)
    body.pack(fill="both", expand=True)
    state = {"status": None}

    def calibrate_dialog(title):
        ok, text = _calibrate_report(title, apply=False)
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
            ok2, text2 = _calibrate_report(title, apply=True)
            win.destroy()
            rebuild()
            state["status"].config(
                text=("Calibrated " + short_client(title)) if ok2
                     else ("Calibration failed - see console"),
                fg="#060" if ok2 else "#b00")

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
        cfg = load_config()
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
        msg = (f"Saved: {len(chosen)} client(s)"
               + (f", {off} region(s) switched off" if off else "")
               + ". Watchers restart within a few seconds.")
        state["status"].config(text=msg, fg="#060")
        print(msg)

    rebuild()
    if args.seconds:
        root.after(int(args.seconds * 1000), root.destroy)
    root.mainloop()
    print("clients:", read_clients())


def cmd_supervise(args):
    """Run one watcher per selected client, following the CLIENTS file."""
    children = {}                       # title -> Popen
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
                if children:
                    log("supervisor: config changed - restarting watchers")
                    for title in list(children):
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
    with open(PAUSEFILE, "w", encoding="utf-8") as fh:
        fh.write(f"paused {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    print("Paused. The watcher keeps running but will not alert.")
    print("Resume with:  python eve_watch.py resume")


def cmd_resume(args):
    if os.path.exists(PAUSEFILE):
        os.remove(PAUSEFILE)
        print("Resumed. It re-baselines first, so nothing that happened while "
              "paused will fire.")
    else:
        print("Not paused.")


def cmd_status(args):
    running = [p for p in _watcher_pids()]
    print(f"watcher process: {'running, pid ' + ', '.join(map(str, running)) if running else 'NOT running'}")
    print(f"paused:          {'YES' if os.path.exists(PAUSEFILE) else 'no'}")
    if os.path.exists(LOGFILE):
        with open(LOGFILE, "r", encoding="utf-8") as fh:
            tail = fh.readlines()[-5:]
        print("last log lines:")
        for line in tail:
            print("   " + line.rstrip())


def _watcher_pids():
    """PIDs actually running the watch loop.

    A venv's Scripts\\python.exe is a launcher stub that spawns the base
    interpreter as a child, so both match. Report only the leaves - the parent
    is just waiting, and counting it looks like a duplicate watcher.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*eve_watch*watch*' } | "
             "ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
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
    interval = args.interval or s["interval"]
    sensitivity = args.sensitivity or s["sensitivity"]
    stable_needed = args.stable or s["stable"]
    thr = s["threshold"]
    obs_dir = args.obs_dir or s.get("obs_dir")
    global VOICE
    VOICE = args.voice_name or s.get("voice_name")
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
              "pix_ncc": r.get("pix_ncc", 0.95),
              "pix_min_lit": r.get("pix_min_lit", 20),
              "row_offset": r.get("row_offset", 0),
              "pix_pad": r.get("pix_pad", 3),
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

    try:
        while True:
            if os.path.exists(PAUSEFILE):
                if not paused:
                    paused = True
                    log("|| PAUSED - resume with:  python eve_watch.py resume")
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
                    due = ((moved and st["count"] >= stable_needed)
                           or now - st["last_ocr"] >= s["roster_period"])
                    if not due:
                        continue
                    st["count"], st["ref"], st["last_ocr"] = 0, cur, now

                    if st["identity"] == "pixels":
                        _label = label_by_row(frame, box, s["ocr_scale"],
                                              st["pitch"])
                        arrived, departed = reconcile_pixels(
                            st, frame, box, thr, st["cfg"], _label)
                        for gone in departed:
                            log(f"   {name}: left - {gone}")
                            record_event(started, name, "depart", gone,
                                         obs_dir=obs_dir)
                        if arrived:
                            st["changes"] += len(arrived)
                            detail = " | ".join(arrived)
                            phrase = (f"{st['say']}. {arrived[0]}"
                                      if len(arrived) == 1
                                      else f"{st['say']}. {len(arrived)} new")
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

            if s["clipboard_sigs"]:
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
                        fresh = {k: v for k, v in sigs.items() if k not in known_sigs}
                        gone = [k for k in known_sigs if k not in sigs]
                        known_sigs = sigs
                        log(f"   clipboard: {len(sigs)} signature(s) pasted"
                            + (f", {len(fresh)} new" if fresh else ""))
                        record_event(started, "clipboard", "sigs",
                                     f"{len(sigs)} total: " + ", ".join(sorted(sigs)),
                                     obs_dir=obs_dir)
                        if fresh:
                            detail = " | ".join(fresh[k] for k in sorted(fresh))
                            log(f"*** NEW SIGNATURE(S): {detail}")
                            record_event(started, "clipboard", "new_sig", detail,
                                         obs_dir=obs_dir)
                            raise_alarm(
                                f"{len(fresh)} new signature"
                                f"{'s' if len(fresh) > 1 else ''}",
                                f"New signature(s):\n\n{detail}", args)
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
    sp.set_defaults(func=cmd_pause)

    sp = sub.add_parser("resume", help="start alerting again (re-baselines first)")
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
    sp.add_argument("--tag", help="label for this client in the log and csv")
    sp.add_argument("--voice-name", dest="voice_name",
                    help="TTS voice to use, e.g. Mark / Zira / David")
    sp.add_argument("--mode", choices=sorted(PROFILES),
                    help="alert profile: active = sound only x2, away = sound + "
                         "voice + popup, silent = log only")
    sp.add_argument("--quiet", action="store_true",
                    help="log and snapshot everything, but make no noise at all")
    sp.set_defaults(func=cmd_watch, popup=None, voice=None, beeps=None)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
