#!/usr/bin/env python3
"""
SLC Video Merger — processing pipeline (framework-agnostic).
Ported from the original Streamlit app's core functions, unchanged in
behavior. All text is rendered by Pillow (no FFmpeg drawtext = no escaping
bugs). FFmpeg only does: overlay PNG on video, normalise, transitions,
concatenate. This module has no web-framework dependency — main.py wraps
it with FastAPI job endpoints.
"""

import os, json, subprocess, tempfile, time, uuid, re, html, hmac, unicodedata, base64
from pathlib import Path
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFont
import numpy as np

try:
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

# ── Embedded SLC logo (base64) — written to assets/ on startup ───────────
_SLC_LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAHcAAABNCAYAAACc2PtBAAAtpElEQVR4nO29WY8lyZXv9ztm5mvsuWctXc1ukk1OX1KjucQVpAcBepAAfWJ9AAkC9HAx94ozHK69VFdlVVbusftiZnpwN0/PrC2bPcTVDHmAzIjwcHeLsGNn/x8LqWpHn4QPvwbAK7wAqNvz/DvO618iqvfKIR4Qh/LgpHded/67jzefCVT/ADT3e+e4vVF7z5VX773m3wupj5/yDpIPM/L7DPs+xvbJ37lG4e9d9zd6N5kfdvktkwNDgjTcldTbE5oH34jfvbV1n6lBOptRbpnb/H/YAhN/e9++tP97l1r4gcy9z4z+RPYZ30zkXbV8/9r3D6J6V/YYLHfH+BD9NTDyXdQx9522FfDthPbf97Q2sX+iuG7q+5MpuLvy2dpYj3qnalX+3ZbirVPfWjCB3Fvn37HPvnnT8fbi/PdGP1At39LbTHHvdHxoj3luFexb9C4G9zkh4eqPS243/v3Hf+eMBTDvk9j75O8xQflWIltG9L3nO2r4jpvrOsm5le53M/Itp+rOa6FbHm85d3fHda1H/tdIf560HC729FRxE1qIFzwC7Z+Tvjus6NvN22t6N265GI69/1F793yburetkwNDgjTcldTbE5oH34jfvbV1n6lBOptRbpnb/H/YAhN/e9++tP97l1r4gcy9z4z+RPYZ30zkXbV8/9r3D6J6V/YYLHfH+BD9NTDyXdQx9522FfDthPbf97Q2sX+iuG7q+5MpuLvy2dpYj3qnalX+3ZbirVPfWjCB3Fvn37HPvnnT8fbi/PdGP1At39LbTHHvdHxoj3luFexb9C4G9zkh4eqPS243/v3Hf+eMBTDvk9j75O8xQflWIltG9L3nO2r4jpvrOsm5le53M/Itp+rOa6FbHm85d3fHda1H/tdIf560HC729FRxE1qIFzwC7Z+Tvjus6NvN22t6N265GI69/1F793yburetkwNDgjTcldTbE5oH34jfvbV1n6lBOptRbpnb/H/YAhN/e9++tP97l1r4gcy9z4z+RPYZ30zkXbV8/9r3D6J6V/YYLHfH+BD9NTDyXdQx9522FfDthPbf97Q2sX+iuG7q+5MpuLvy2dpYj3qnalX+3ZbirVPfWjCB3Fvn37HPvnnT8fbi/PdGP1At39LbTHHvdHxoj3luFexb9C4G9zkh4eqPS243/v3Hf+eMBTDvk9j75O8xQflWIltG9L3nO2r4jpvrOsm5le53M/Itp+rOa6FbHm85d3fHda1H/tdIf560HC729FRxE1qIFzwC7Z+Tvjus6NvN22t6N265GI69/1F793yburetkwNDgjTcldTbE5oH34jfvbV1n6lBOptRbpnb/H/YAhN/e9++tP97l1r4gcy9z4z+RPYZ30zkXbV8/9r3D6J6V/YYLHfH+BD9NTDyXdQx9522FfDthPbf97Q2sX+iuG7q+5MpuLvy2dpYj3qnalX+3ZbirVPfWjCB3Fvn37HPvnnT8fbi/PdGP1At39LbTHHvdHxoj3luFexb9C4G9zkh4eqPS243/v3Hf+eMBTDvk9j75O8xQflWIltG9L3nO2r4jpvrOsm5le53M/Itp+rOa6FbHm85d3fHda1H/tdIf560HC729FRAAAAA="

BASE_DIR  = Path(__file__).parent
INTRO_TPL = BASE_DIR / "assets" / "intro_template.mp4"
SLC_LOGO  = BASE_DIR / "assets" / "slc_logo.png"
GEMINI_NOTEBOOK_TEMPLATE = BASE_DIR / "assets" / "gemini_notebook_template.png"

# ── Watermark / badge cover ───────────────────────────────────────────────
# NOTE: Google has since moved the persistent "Gemini Notebook" wordmark
# slightly lower/further right than these coordinates originally assumed.
# Measured from a real 1920x1080 export (Aug 2026): the wordmark's own
# pixels sit at roughly x=1738-1907, y=1046-1064. The box below is padded
# generously around that so the cover (and the SLC logo placed inside it)
# fully hides the wordmark instead of sitting above/short of it.
WM_BR_X, WM_BR_Y, WM_BR_W, WM_BR_H = 1645, 950, 275, 125
WM_TOP_X, WM_TOP_Y, WM_TOP_W, WM_TOP_H = 700, 36, 520, 110

BOX_RADIUS = 10
WM_EC_X, WM_EC_Y, WM_EC_W, WM_EC_H = 448, 310, 1024, 420
EC_RADIUS  = 14

LOGO_H            = 44
LOGO_RIGHT_MARGIN = 113
LOGO_BOTTOM_MARGIN = 53


TEAL, WHITE = (96, 204, 190), (255, 255, 255)

# ── Folder rotation config ────────────────────────────────────────────────
FOLDER_MAX_ITEMS = 50   # create a new batch folder after this many files

# ── Security config ───────────────────────────────────────────────────────
MAX_UPLOAD_MB        = 500          # hard cap on any single video upload
MAX_TEXT_FIELD_LEN   = 200          # course name / unit number length limit


# ──────────────────── SECURITY HELPERS ────────────────────────────────────
def _esc(s) -> str:
    """HTML-escape any value that will be rendered with unsafe_allow_html.
    Converts None/non-strings to empty string. Prevents stored XSS."""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _clean_text_field(s: str, max_len: int = MAX_TEXT_FIELD_LEN) -> str:
    """Normalise and strip control characters from a user text field.
    Keeps printable characters, drops anything in the Cc/Cf Unicode
    category (control/format), collapses whitespace, and caps length."""
    if not s:
        return ""
    # Normalise unicode so visually-identical lookalikes collapse
    s = unicodedata.normalize("NFKC", s)
    # Drop control and format characters (including nulls, newlines, RTL overrides)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    # Collapse runs of whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def _safe_filename(name: str, max_len: int = 120) -> str:
    """Produce a safe filename for Google Drive / local filesystems.
    Removes path separators, reserved Windows names, control characters,
    and anything outside a conservative allow-list."""
    if not name:
        return "video.mp4"
    # Strip directory components defensively
    name = os.path.basename(name)
    # Normalise unicode
    name = unicodedata.normalize("NFKC", name)
    # Replace path separators and NTFS-reserved chars with underscore
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "_", name)
    # Conservative allow-list: letters, digits, space, dot, dash, underscore,
    # parentheses, pipe (already removed above). Everything else becomes "_".
    name = re.sub(r"[^A-Za-z0-9 ._\-()]+", "_", name)
    # Collapse repeated underscores/spaces, strip leading dots (hidden files)
    name = re.sub(r"_+", "_", name).strip(" ._")
    if not name:
        name = "video"
    # Reserved Windows device names
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
        | {f"LPT{i}" for i in range(1, 10)}
    stem = name.rsplit(".", 1)[0].upper()
    if stem in reserved:
        name = "_" + name
    # Cap length, preserving extension if present
    if len(name) > max_len:
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def _sanitise_error(e: Exception, max_len: int = 300) -> str:
    """Turn an exception into a short, non-leaking user-facing message.
    Strips absolute paths (which may expose tempdir/internal layout) and
    caps the total length."""
    msg = str(e) if e else "unknown error"
    # Hide absolute POSIX and Windows paths
    msg = re.sub(r"(/[^\s'\"]+)+", "<path>", msg)
    msg = re.sub(r"[A-Za-z]:\\[^\s'\"]+", "<path>", msg)
    if len(msg) > max_len:
        msg = msg[:max_len] + "…"
    return msg


def _font(name):
    for c in [str(BASE_DIR / "fonts" / name),
              f"/usr/share/fonts/truetype/google-fonts/{name}",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if os.path.exists(c): return c
    return None

BOLD, MEDIUM = _font("Poppins-Bold.ttf"), _font("Poppins-Medium.ttf")


def _ft(path, size):
    try:    return ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except: return ImageFont.load_default()


def _make_logo_composite(logo_path, box, W=1920, H=1080, bg=(255,255,255,255), logo_h=None):
    brx, bry, brw, brh = box
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Solid white "plate" first, sized to fully hide the watermark behind it.
    draw.rounded_rectangle([brx, bry, brx+brw, bry+brh], radius=BOX_RADIUS, fill=bg)
    # The logo itself stays a fixed, modest size regardless of how tall/wide
    # the cover box is — the box is often padded larger than the logo just
    # to guarantee the watermark underneath is fully hidden, so scaling the
    # logo to fill that whole box makes it look oversized.
    logo_h_px = logo_h if logo_h else min(60, brh - 12)
    logo_img  = Image.open(str(logo_path)).convert("RGBA")
    ratio     = logo_img.width / logo_img.height
    logo_w_px = int(logo_h_px * ratio)
    max_w = brw - 24
    if logo_w_px > max_w:
        logo_w_px = max_w
        logo_h_px = int(logo_w_px / ratio)
    logo_img  = logo_img.resize((logo_w_px, logo_h_px), Image.LANCZOS)
    cx     = brx + brw // 2; cy = bry + brh // 2
    logo_x = cx - logo_w_px // 2; logo_y = cy - logo_h_px // 2
    img.paste(logo_img, (logo_x, logo_y), logo_img)
    out = Path(str(logo_path)).parent / "logo_composite.png"
    img.save(str(out), "PNG")
    return out


def _make_ec_png(path, W=1920, H=1080):
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([WM_EC_X, WM_EC_Y, WM_EC_X+WM_EC_W, WM_EC_Y+WM_EC_H],
                            radius=EC_RADIUS, fill=(255, 255, 255, 255))
    img.save(str(path), "PNG")
    return path


def _make_box_png(boxes, path, W=1920, H=1080, colour=(255,255,255,255)):
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for (x, y, w, h, r) in boxes:
        draw.rounded_rectangle([x, y, x+w, y+h], radius=r, fill=colour)
    img.save(str(path), "PNG")
    return path


# ──────────────────── PILLOW OVERLAYS ────────────────────────────────────
def _fit_bold_text(draw, text, pad, start_size=52, min_size=28):
    """Shrink-to-fit a bold line of text within `pad` px, returning the
    chosen font and its (width, height). Shared by the awarding body and
    course name lines so they always render with identical size/style
    logic — only the specific text differs."""
    size = start_size
    fn = _ft(BOLD, size)
    while size > min_size:
        bb = draw.textbbox((0, 0), text, font=fn)
        if bb[2] - bb[0] <= pad:
            break
        size -= 2
        fn = _ft(BOLD, size)
    asc, desc = fn.getmetrics()
    return fn, asc + desc


def render_intro_overlay(course, unit_num, chapter_number, awarding_body="", W=1920, H=1080):
    img  = Image.new("RGBA", (W, H), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    pad  = W - 200

    # Awarding body (optional) renders centered above the course name,
    # using the exact same bold/shrink-to-fit styling as the course name
    # itself — only omitted from the layout when left blank.
    has_awarding = bool(awarding_body and awarding_body.strip())
    if has_awarding:
        afn, a_h = _fit_bold_text(draw, awarding_body, pad)
    else:
        a_h = 0

    cfn, c_h = _fit_bold_text(draw, course, pad)

    # Unit and chapter live in the same badge, e.g. "UNIT 2 | CHAPTER 3" —
    # chapter is appended only when present so a video with no chapter
    # number still gets a clean "UNIT 2" badge.
    badge_parts = [p.strip() for p in (unit_num, chapter_number) if p and p.strip()]
    utxt = " | ".join(badge_parts).upper()
    ufn_size = 28
    ufn  = _ft(BOLD, ufn_size)
    bb   = draw.textbbox((0,0), utxt, font=ufn)
    badge_w = bb[2]-bb[0]+70; badge_h = 56
    while badge_w > pad and ufn_size > 16:
        ufn_size -= 2
        ufn  = _ft(BOLD, ufn_size)
        bb   = draw.textbbox((0,0), utxt, font=ufn)
        badge_w = bb[2]-bb[0]+70

    gap0 = 20   # awarding body -> course name
    gap1 = 45   # course name -> badge
    block_h = (a_h + gap0 if has_awarding else 0) + c_h + gap1 + badge_h
    cur_y = (H//2-60)-block_h//2

    if has_awarding:
        draw.text((W//2, cur_y+a_h//2), awarding_body, fill=WHITE, font=afn, anchor="mm")
        cur_y += a_h + gap0

    draw.text((W//2, cur_y+c_h//2), course, fill=WHITE, font=cfn, anchor="mm")
    bx = (W-badge_w)//2; by = cur_y+c_h+gap1
    draw.rounded_rectangle([bx,by,bx+badge_w,by+badge_h], radius=14, fill=TEAL+(230,))
    draw.text((bx+badge_w//2, by+badge_h//2), utxt, fill=WHITE, font=ufn, anchor="mm")
    return img


def render_end_overlay(W=1920, H=1080):
    img  = Image.new("RGBA", (W, H), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    fn   = _ft(BOLD, 42); bb = draw.textbbox((0,0), "END", font=fn)
    bw, bh = bb[2]-bb[0]+90, 72; bx, by = (W-bw)//2, (H-bh)//2-20
    draw.rounded_rectangle([bx,by,bx+bw,by+bh], radius=16, fill=TEAL+(230,))
    draw.text((bx+bw//2, by+bh//2), "END", fill=WHITE, font=fn, anchor="mm")
    return img


# ──────────────────── FFMPEG HELPERS ────────────────────────────────────
def _ff(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.strip().split("\n")
        raise RuntimeError("\n".join(err[-6:]) if len(err)>6 else r.stderr)
    return r


def _probe_resolution(path):
    r = subprocess.run(["ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height","-of","csv=p=0",str(path)],
        capture_output=True, text=True)
    try:    w, h = r.stdout.strip().split(","); return (int(w), int(h))
    except: return (1920, 1080)


def _probe_duration(path):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",str(path)],
        capture_output=True, text=True)
    if r.returncode!=0 or not r.stdout.strip():
        raise RuntimeError(f"Cannot read duration: {path}")
    return float(r.stdout.strip())


def _has_audio(path):
    r = subprocess.run(["ffprobe","-v","error","-select_streams","a",
        "-show_entries","stream=index","-of","csv=p=0",str(path)],
        capture_output=True, text=True)
    return bool(r.stdout.strip())


def _detect_end_card_start(path, progress_cb=None):
    """Detect where the NotebookLM end card begins using OpenCV template matching.

    Returns the trim timestamp, or the full duration if no end card is found.
    """
    total = _probe_duration(path)

    def _grab_cv(t):
        """Extract frame at *t* as a 640x360 BGR OpenCV image (or None)."""
        fd, tf = tempfile.mkstemp(suffix=".png"); os.close(fd)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                 "-vframes", "1", "-s", "640x360", tf],
                capture_output=True, timeout=10)
            if os.path.getsize(tf) < 100:
                return None
            if CV2_AVAILABLE:
                return cv2.imread(tf)
            else:
                return np.asarray(Image.open(tf).convert("L"), dtype=np.float32)
        except Exception:
            return None
        finally:
            try: os.unlink(tf)
            except OSError: pass

    def _score_frame(frame):
        """Return template-match score (0..1) for *frame*, or -1."""
        if frame is None:
            return -1.0
        if CV2_AVAILABLE:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            try:
                res = cv2.matchTemplate(gray, template_gray, cv2.TM_CCOEFF_NORMED)
                _, mx, _, _ = cv2.minMaxLoc(res)
                return float(mx)
            except cv2.error:
                return -1.0
        else:
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.shape != template_gray.shape:
                return -1.0
            diff = float(np.mean(np.abs(crop.astype(float) - template_gray.astype(float))))
            return max(0.0, 1.0 - diff / 50.0)

    HARD_THRESH  = 0.70   # definite end-card match
    SOFT_THRESH  = 0.35   # transition / fade-in region

    if progress_cb: progress_cb("   Capturing end-card reference…")

    # ── Get the last READABLE frame ─────────────────────────────────────
    end_frame = None
    for offset in [1.0, 2.0, 3.0, 5.0]:
        t_try = max(0.0, total - offset)
        end_frame = _grab_cv(t_try)
        if end_frame is not None:
            if progress_cb: progress_cb(f"   End frame captured at t={t_try:.1f}s")
            break
    if end_frame is None:
        if progress_cb: progress_cb("   Cannot read any frame near the end")
        return total

    # Content reference from 40 % of video
    content_frame = _grab_cv(total * 0.40)

    # ── Extract centre 60 % crop as template ────────────────────────────
    h, w = end_frame.shape[:2]
    cx1, cy1 = int(w * 0.20), int(h * 0.20)
    cx2, cy2 = int(w * 0.80), int(h * 0.80)
    template = end_frame[cy1:cy2, cx1:cx2]

    if CV2_AVAILABLE:
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    else:
        template_gray = template

    # ── Safety checks ───────────────────────────────────────────────────
    end_score = _score_frame(end_frame)
    if end_score < HARD_THRESH:
        if progress_cb: progress_cb(f"   End frame score {end_score:.2f} — not an end card")
        return total

    if content_frame is not None and _score_frame(content_frame) >= HARD_THRESH:
        if progress_cb: progress_cb("   Content matches end-card template — skipping trim")
        return total

    if progress_cb: progress_cb("   End-card confirmed. Scanning backward…")

    # ── Phase 1 — coarse backward scan (1 s steps, hard threshold) ─────
    scan_limit = max(0.0, total - 60.0)
    boundary = total
    t = total - 1.0
    while t > scan_limit:
        frame = _grab_cv(t)
        if frame is not None and _score_frame(frame) >= HARD_THRESH:
            boundary = t
            t -= 1.0
        else:
            break

    # ── Phase 2 — fine forward scan (0.1 s steps) to find precise edge ─
    fine_start = max(scan_limit, boundary - 2.0)
    fine_end   = min(total, boundary + 1.0)
    precise    = boundary
    t = fine_start
    while t <= fine_end:
        frame = _grab_cv(t)
        sc = _score_frame(frame)
        if sc >= HARD_THRESH:
            precise = t
            break
        t += 0.10

    # ── Phase 3 — walk backward in 0.10 s steps (hard threshold) ───────
    t = precise - 0.10
    while t > scan_limit:
        frame = _grab_cv(t)
        sc = _score_frame(frame)
        if sc >= HARD_THRESH:
            precise = t
            t -= 0.10
        else:
            break

    # ── Phase 4 — detect transition zone (soft threshold) ──────────────
    # The end card often fades in over 0.3-0.5 s before the hard match.
    # Walk further back with the lower threshold to catch that.
    transition_start = precise
    t = precise - 0.10
    while t > scan_limit:
        frame = _grab_cv(t)
        sc = _score_frame(frame)
        if sc >= SOFT_THRESH:
            transition_start = t
            t -= 0.10
        else:
            break

    # Use the transition start (catches the fade-in)
    precise = transition_start

    # ── Phase 5 — content-divergence detection ──────────────────────────
    # The transition often starts with a white flash / dissolve BEFORE the
    # end-card template fades in.  These frames score low on the template
    # but look nothing like the preceding content.  Compare each frame
    # against a confirmed-content reference; if the pixel diff is abnormally
    # high, the frame is still part of the transition.
    content_ref_t = max(0.0, precise - 5.0)
    content_ref = _grab_cv(content_ref_t)
    if content_ref is not None:
        if CV2_AVAILABLE:
            ref_gray = cv2.cvtColor(content_ref, cv2.COLOR_BGR2GRAY).astype(np.float32)
        else:
            ref_gray = content_ref.astype(np.float32)

        def _content_diff(frame):
            if frame is None:
                return 0.0
            if CV2_AVAILABLE:
                g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            else:
                g = frame.astype(np.float32)
            return float(np.mean(np.abs(g - ref_gray)))

        # Measure baseline diff — sample a frame right next to the reference
        baseline_diff = _content_diff(_grab_cv(content_ref_t + 1.0))
        # Threshold: anything more than 4x the baseline (or > 3.0 absolute)
        # is a diverged frame (transition / flash)
        diff_thresh = max(3.0, baseline_diff * 4.0)

        t = precise - 0.10
        while t > scan_limit:
            frame = _grab_cv(t)
            d = _content_diff(frame)
            if d > diff_thresh:
                precise = t
                t -= 0.10
            else:
                break

    ec_len = total - precise
    if ec_len < 0.5:
        if progress_cb: progress_cb(f"   End card {ec_len:.2f}s — too short, skipping")
        return total

    if progress_cb:
        progress_cb(f"   End card: {precise:.1f}s → {total:.1f}s  ({ec_len:.1f}s)")
    return precise


def make_intro(course, unit_num, chapter_number, awarding_body, tmp):
    png = str(tmp/"intro_overlay.png"); out = str(tmp/"intro.mp4")
    render_intro_overlay(course, unit_num, chapter_number, awarding_body).save(png, "PNG")
    y = "if(lt(t\\,0.8)\\,300*pow(1-t/0.8\\,2)\\,0)"
    _ff(["ffmpeg","-y","-i",str(INTRO_TPL),"-loop","1","-i",png,"-filter_complex",
        f"[1:v]format=rgba[ovr];[0:v][ovr]overlay=x=0:y='{y}':shortest=1[out]",
        "-map","[out]","-map","0:a?","-c:v","libx264","-preset","ultrafast",
        "-crf","23","-c:a","aac","-b:a","128k","-ar","48000","-ac","2",
        "-r","30","-pix_fmt","yuv420p",out], timeout=60)
    return Path(out)


def make_outro(tmp):
    png = str(tmp/"end_overlay.png"); out = str(tmp/"outro.mp4")
    render_end_overlay().save(png, "PNG")
    y = "if(lt(t\\,0.8)\\,250*pow(1-t/0.8\\,2)\\,0)"
    _ff(["ffmpeg","-y","-i",str(INTRO_TPL),"-loop","1","-i",png,"-filter_complex",
        f"[1:v]format=rgba[ovr];[0:v][ovr]overlay=x=0:y='{y}':shortest=1[out]",
        "-map","[out]","-map","0:a?","-c:v","libx264","-preset","ultrafast",
        "-crf","23","-c:a","aac","-b:a","128k","-ar","48000","-ac","2",
        "-r","30","-pix_fmt","yuv420p",out], timeout=60)
    return Path(out)


def normalise(inp, out):
    ha = _has_audio(inp); cmd = ["ffmpeg","-y","-i",str(inp)]
    if not ha: cmd += ["-f","lavfi","-i","anullsrc=r=48000:cl=stereo"]
    cmd += ["-vf",
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black",
        "-r","30","-c:v","libx264","-preset","ultrafast","-crf","23",
        "-c:a","aac","-b:a","128k","-ar","48000","-ac","2","-pix_fmt","yuv420p"]
    if not ha: cmd += ["-shortest"]
    cmd += [str(out)]; _ff(cmd); return Path(out)


def _detect_notebooklm_logo_cv(video_path, progress_cb=None):
    """Use OpenCV to detect the NotebookLM logo on the front page.
    Extracts the bottom-right logo from a middle frame as a template,
    then searches the front page (top half) for the same logo via
    multi-scale template matching.
    Returns (x, y, w, h) in 1920x1080 coordinates, or None.
    """
    if not CV2_AVAILABLE:
        if progress_cb: progress_cb("OpenCV not available")
        return None
    try:
        duration = _probe_duration(str(video_path))
    except Exception:
        return None

    mid_t = min(duration * 0.3, max(5.0, duration - 10))
    fd1, tf_mid = tempfile.mkstemp(suffix=".png"); os.close(fd1)
    fd2, tf_front = tempfile.mkstemp(suffix=".png"); os.close(fd2)
    try:
        subprocess.run(["ffmpeg","-y","-ss",f"{mid_t:.2f}","-i",str(video_path),
                        "-vframes","1",tf_mid], capture_output=True, timeout=10)
        subprocess.run(["ffmpeg","-y","-ss","0.5","-i",str(video_path),
                        "-vframes","1",tf_front], capture_output=True, timeout=10)
        mid_img = cv2.imread(tf_mid)
        front_img = cv2.imread(tf_front)
        if mid_img is None or front_img is None:
            return None

        fh, fw = front_img.shape[:2]
        mh, mw = mid_img.shape[:2]

        # --- Step 1: extract bottom-right logo from middle frame as template ---
        sx_m, sy_m = mw / 1920, mh / 1080
        bx = max(0, int(WM_BR_X * sx_m)); by = max(0, int(WM_BR_Y * sy_m))
        bw = min(int(WM_BR_W * sx_m), mw - bx)
        bh = min(int(WM_BR_H * sy_m), mh - by)
        template = mid_img[by:by+bh, bx:bx+bw]
        if template.size == 0:
            return None

        # --- Step 2: multi-scale template matching on front page top half ---
        search_h = int(fh * 0.50)
        search_region = front_img[0:search_h, :]
        gray_region = cv2.cvtColor(search_region, cv2.COLOR_BGR2GRAY)
        gray_tmpl  = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        th, tw = gray_tmpl.shape[:2]

        best_match = None
        best_val   = 0.50

        for scale in np.arange(0.5, 1.6, 0.1):
            sw = int(tw * scale); sh = int(th * scale)
            if sw >= search_region.shape[1] or sh >= search_region.shape[0] or sw < 10 or sh < 10:
                continue
            scaled_tmpl = cv2.resize(gray_tmpl, (sw, sh))
            res = cv2.matchTemplate(gray_region, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_val:
                best_val = max_val
                best_match = (max_loc[0], max_loc[1], sw, sh)

        if best_match:
            sx_f, sy_f = 1920 / fw, 1080 / fh
            pad = 8
            rx = max(0, int((best_match[0] - pad) * sx_f))
            ry = max(0, int((best_match[1] - pad) * sy_f))
            rw = int((best_match[2] + pad * 2) * sx_f)
            rh = int((best_match[3] + pad * 2) * sy_f)
            if progress_cb: progress_cb(f"   CV match at ({rx},{ry}) {rw}x{rh}  conf={best_val:.2f}")
            return (rx, ry, rw, rh)

        return None

    except Exception as e:
        if progress_cb: progress_cb(f"   CV detection error: {e}")
        return None
    finally:
        try: os.unlink(tf_mid)
        except: pass
        try: os.unlink(tf_front)
        except: pass


def _detect_fixed_top_badge(path, progress_cb=None):
    """Fallback detector for the known top-centre Gemini/Notebook badge area.

    Newer NotebookLM exports can show a ``Gemini Notebook`` wordmark on the
    title card that is visually different from the persistent bottom-right
    watermark. Template matching can therefore miss it. This detector checks
    the known title-card badge region for dark, text-like pixels on a light
    background and returns the fixed 1920x1080 cover box when present.
    """
    try:
        src_w, src_h = _probe_resolution(path)
    except Exception:
        src_w, src_h = 1920, 1080

    sx = src_w / 1920; sy = src_h / 1080
    rx = max(0, int(WM_TOP_X * sx)); ry = max(0, int(WM_TOP_Y * sy))
    rw = max(1, int(WM_TOP_W * sx)); rh = max(1, int(WM_TOP_H * sy))

    best = None
    # Check a few early frames in case the title card fades in.
    for t in (0.25, 0.50, 1.00):
        fd, tf = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
        try:
            subprocess.run(["ffmpeg","-y","-ss",f"{t:.2f}","-i",str(path),
                             "-vframes","1",tf], capture_output=True, timeout=8)
            img = Image.open(tf).convert("RGB")
            crop = np.array(img)[ry:ry+rh, rx:rx+rw]
            if crop.size == 0:
                continue
            gray = crop.mean(axis=2)
            dark_frac = float((gray < 180).mean())
            very_dark_frac = float((gray < 110).mean())
            bright_frac = float((gray > 210).mean())
            # Weight near-black strokes more heavily; the faint Notebook grid
            # remains above the dark thresholds and should not trigger this.
            score = dark_frac + 0.5 * very_dark_frac
            if best is None or score > best[0]:
                best = (score, dark_frac, very_dark_frac, bright_frac, t)
        except Exception:
            continue
        finally:
            try: os.unlink(tf)
            except OSError: pass

    if best is None:
        return None

    _, dark_frac, very_dark_frac, bright_frac, sample_t = best
    # The logo is black/dark text on a predominantly pale title-card area.
    # A low threshold is deliberate because the wordmark occupies only a
    # small part of this generously padded cover box.
    present = bright_frac >= 0.50 and (dark_frac >= 0.012 or very_dark_frac >= 0.006)
    if progress_cb:
        progress_cb(
            f"   Fallback badge check at {sample_t:.2f}s: "
            f"dark={dark_frac:.3f}, bright={bright_frac:.3f}"
        )
    return (WM_TOP_X, WM_TOP_Y, WM_TOP_W, WM_TOP_H) if present else None


def _detect_top_watermark_end(path, max_scan=120.0, badge_box=None):
    """Estimate when the opening top-centre badge disappears.

    The old implementation compared the average colour of the whole box.
    That can miss a small black wordmark disappearing from an otherwise white
    background. We now track the pixels that are dark in the reference badge
    itself, while retaining the old full-region comparison as a fallback.
    """
    try:
        src_w, src_h = _probe_resolution(path)
    except Exception:
        src_w, src_h = 1920, 1080
    sx = src_w / 1920; sy = src_h / 1080
    if badge_box:
        bb_x, bb_y, bb_w, bb_h = badge_box
        rx = max(0, int(bb_x * sx)); ry = max(0, int(bb_y * sy))
        rw = max(1, int(bb_w * sx)); rh = max(1, int(bb_h * sy))
    else:
        rx = max(0, int(WM_TOP_X * sx)); ry = max(0, int(WM_TOP_Y * sy))
        rw = max(1, int(WM_TOP_W * sx)); rh = max(1, int(WM_TOP_H * sy))

    def _grab_region(t):
        fd, tf = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
        try:
            subprocess.run(["ffmpeg","-y","-ss",f"{t:.2f}","-i",str(path),
                             "-vframes","1",tf], capture_output=True, timeout=8)
            img = Image.open(tf).convert("RGB")
            return np.array(img)[ry:ry+rh, rx:rx+rw].astype(float)
        except Exception:
            return None
        finally:
            try: os.unlink(tf)
            except OSError: pass

    # 0.5 s is more reliable than frame 0 for title cards that fade in.
    ref_t = 0.50
    ref = _grab_region(ref_t)
    if ref is None or ref.size == 0:
        ref_t = 0.0
        ref = _grab_region(ref_t)
    if ref is None or ref.size == 0:
        return 0.0

    ref_gray = ref.mean(axis=2)
    ref_dark = ref_gray < 185
    dark_frac = float(ref_dark.mean())
    bright_frac = float((ref_gray > 210).mean())
    use_dark_tracking = dark_frac >= 0.008 and bright_frac >= 0.40

    # Preserve the historical safety check for non-title-card regions when
    # the reference does not contain a usable dark wordmark.
    if not use_dark_tracking and (ref > 200).mean() < 0.60:
        return 0.0

    total = _probe_duration(path)
    scan_end = min(max_scan, max(ref_t, total - 2.0))
    step = 0.5
    t = ref_t + step
    last_present = ref_t
    absent_run = 0

    while t <= scan_end:
        frame = _grab_region(t)
        if frame is not None and frame.size > 0:
            if use_dark_tracking:
                frame_gray = frame.mean(axis=2)
                # How many pixels that formed the original wordmark are still
                # dark in this frame? This is much more sensitive than a mean
                # difference across the entire white rectangle.
                retained = float((frame_gray[ref_dark] < 205).mean()) if ref_dark.any() else 0.0
                current_dark = float((frame_gray < 185).mean())
                present = retained >= 0.50 and current_dark >= dark_frac * 0.30
            else:
                diff = float(np.abs(frame - ref).mean())
                present = diff < 12

            if present:
                last_present = t
                absent_run = 0
            else:
                absent_run += 1
                # Require two consecutive misses to avoid ending the cover on
                # a single transition/fade frame.
                if absent_run >= 2:
                    return min(last_present + step, max_scan)
        t += step

    return min(last_present + step, max_scan)




def _load_gemini_notebook_template():
    """Load the dedicated Gemini Notebook wordmark template as grayscale."""
    if not CV2_AVAILABLE or not GEMINI_NOTEBOOK_TEMPLATE.exists():
        return None
    tmpl = cv2.imread(str(GEMINI_NOTEBOOK_TEMPLATE), cv2.IMREAD_GRAYSCALE)
    if tmpl is None or tmpl.size == 0:
        return None
    # Trim any near-white border so template matching focuses on the wordmark.
    mask = tmpl < 245
    ys, xs = np.where(mask)
    if len(xs) and len(ys):
        x0, x1 = max(0, int(xs.min()) - 3), min(tmpl.shape[1], int(xs.max()) + 4)
        y0, y1 = max(0, int(ys.min()) - 3), min(tmpl.shape[0], int(ys.max()) + 4)
        tmpl = tmpl[y0:y1, x0:x1]
    return tmpl


def _grab_cv_gray_frame(path, t):
    """Extract one frame with FFmpeg and return it as an OpenCV grayscale image."""
    fd, tf = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(path),
            "-vframes", "1", "-q:v", "2", tf,
        ], capture_output=True, timeout=10)
        frame = cv2.imread(tf, cv2.IMREAD_GRAYSCALE) if CV2_AVAILABLE else None
        return frame
    except Exception:
        return None
    finally:
        try: os.unlink(tf)
        except OSError: pass


def _find_gemini_notebook_wordmark(path, progress_cb=None):
    """Find the newer ``Gemini Notebook`` wordmark on the opening slide.

    This uses a dedicated template for the current wordmark rather than trying
    to reuse the old bottom-right NotebookLM watermark as a template.
    Returns a dict with the matched 1920x1080 box, confidence, reference time,
    and matched template size; otherwise returns None.
    """
    tmpl = _load_gemini_notebook_template()
    if tmpl is None:
        if progress_cb:
            progress_cb("   Gemini Notebook template unavailable")
        return None

    try:
        total = _probe_duration(str(path))
    except Exception:
        total = 10.0

    best = None
    sample_times = (0.15, 0.35, 0.60, 0.90, 1.20, 1.60, 2.00, 2.50, 3.00)
    for t in sample_times:
        if t >= total:
            break
        frame = _grab_cv_gray_frame(path, t)
        if frame is None:
            continue
        fh, fw = frame.shape[:2]

        # The opening wordmark is top-centre. Restricting the search reduces
        # false matches against the large title text underneath it.
        sx0, sx1 = int(fw * 0.25), int(fw * 0.75)
        sy0, sy1 = 0, int(fh * 0.24)
        roi = frame[sy0:sy1, sx0:sx1]
        if roi.size == 0:
            continue

        for scale in np.arange(0.65, 1.81, 0.05):
            tw = max(8, int(tmpl.shape[1] * scale))
            th = max(6, int(tmpl.shape[0] * scale))
            if tw >= roi.shape[1] or th >= roi.shape[0]:
                continue
            rt = cv2.resize(tmpl, (tw, th), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
            res = cv2.matchTemplate(roi, rt, cv2.TM_CCOEFF_NORMED)
            _, val, _, loc = cv2.minMaxLoc(res)
            if best is None or val > best[0]:
                x = sx0 + loc[0]
                y = sy0 + loc[1]
                best = (float(val), t, x, y, tw, th, fw, fh)

    # This threshold is intentionally lower than the old NotebookLM matcher:
    # the template is taken from a browser-rendered example and may be scaled
    # or antialiased slightly differently in raw NotebookLM exports.
    if best is None or best[0] < 0.43:
        if progress_cb and best is not None:
            progress_cb(f"   Gemini wordmark template best confidence={best[0]:.2f} — fallback cover will be used")
        return None

    val, t, x, y, w, h, fw, fh = best
    to1920x = 1920.0 / fw
    to1080y = 1080.0 / fh
    box = (
        int(x * to1920x), int(y * to1080y),
        max(1, int(w * to1920x)), max(1, int(h * to1080y)),
    )
    if progress_cb:
        progress_cb(
            f"   ✅ Gemini Notebook detected at {t:.2f}s "
            f"conf={val:.2f} box={box[0]},{box[1]} {box[2]}x{box[3]}"
        )
    return {
        "box": box,
        "confidence": val,
        "time": t,
        "template_size": (w, h),
        "frame_size": (fw, fh),
    }


def _track_gemini_notebook_end(path, match, progress_cb=None, max_scan=60.0):
    """Track the dedicated Gemini Notebook wordmark until it disappears."""
    if not CV2_AVAILABLE or not match:
        return None
    tmpl = _load_gemini_notebook_template()
    if tmpl is None:
        return None

    try:
        total = _probe_duration(str(path))
    except Exception:
        return None

    fw0, fh0 = match["frame_size"]
    mw, mh = match["template_size"]
    # Resize the source template to the exact size that produced the best hit.
    rt = cv2.resize(tmpl, (mw, mh), interpolation=cv2.INTER_CUBIC)

    bx, by, bw, bh = match["box"]
    # Convert the 1920x1080 match back to the extraction frame coordinate space.
    x = int(bx * fw0 / 1920.0)
    y = int(by * fh0 / 1080.0)
    w = max(1, int(bw * fw0 / 1920.0))
    h = max(1, int(bh * fh0 / 1080.0))

    step = 0.40
    t = max(0.0, match["time"])
    scan_end = min(max_scan, total)
    last_present = t
    absent_run = 0
    seen = False
    threshold = max(0.34, min(0.48, match["confidence"] * 0.58))

    while t <= scan_end:
        frame = _grab_cv_gray_frame(path, t)
        if frame is not None:
            fh, fw = frame.shape[:2]
            # Recalculate from normalized coordinates in case extraction reports
            # a slightly different frame size.
            cx = int(bx * fw / 1920.0)
            cy = int(by * fh / 1080.0)
            cw = max(1, int(bw * fw / 1920.0))
            ch = max(1, int(bh * fh / 1080.0))
            pad_x = max(30, int(cw * 0.35))
            pad_y = max(18, int(ch * 0.90))
            x0, y0 = max(0, cx-pad_x), max(0, cy-pad_y)
            x1, y1 = min(fw, cx+cw+pad_x), min(fh, cy+ch+pad_y)
            roi = frame[y0:y1, x0:x1]

            val = 0.0
            if roi.shape[0] >= rt.shape[0] and roi.shape[1] >= rt.shape[1]:
                res = cv2.matchTemplate(roi, rt, cv2.TM_CCOEFF_NORMED)
                _, val, _, _ = cv2.minMaxLoc(res)

            present = val >= threshold
            if present:
                seen = True
                last_present = t
                absent_run = 0
            elif seen:
                absent_run += 1
                # Require three consecutive misses so fades do not expose the
                # wordmark for a few frames.
                if absent_run >= 3:
                    end_t = min(total, last_present + step * 1.5)
                    if progress_cb:
                        progress_cb(f"   Gemini Notebook disappears at ~{end_t:.1f}s")
                    return end_t
        t += step

    if seen:
        if progress_cb:
            progress_cb(f"   Gemini Notebook remains visible — covering through {scan_end:.1f}s")
        return min(scan_end, total)
    return None

def _detect_fixed_badge_end_guaranteed(path, progress_cb=None, max_scan=60.0):
    """Track the first-page Gemini/Notebook wordmark in its known top-centre area.

    This deliberately does *not* depend on the bottom-right watermark or on
    OpenCV finding the same logo elsewhere.  We take the actual pixels from
    the known top-centre wordmark area on an early frame and track those dark
    strokes until they disappear.  If tracking is uncertain, a conservative
    fallback duration is returned so the branding is still covered.
    """
    try:
        total = _probe_duration(str(path))
    except Exception:
        total = 12.0

    try:
        src_w, src_h = _probe_resolution(str(path))
    except Exception:
        src_w, src_h = 1920, 1080

    sx = src_w / 1920.0
    sy = src_h / 1080.0

    # The cover is intentionally generous.  Detection uses a slightly tighter
    # inner region so large nearby title text cannot influence the tracker.
    cx = max(0, int(WM_TOP_X * sx))
    cy = max(0, int(WM_TOP_Y * sy))
    cw = max(1, int(WM_TOP_W * sx))
    ch = max(1, int(WM_TOP_H * sy))

    inset_x = int(45 * sx)
    inset_y = int(10 * sy)
    rx = cx + inset_x
    ry = cy + inset_y
    rw = max(1, cw - 2 * inset_x)
    rh = max(1, ch - 2 * inset_y)

    def _grab_gray(t):
        fd, tf = tempfile.mkstemp(suffix='.jpg'); os.close(fd)
        try:
            subprocess.run([
                'ffmpeg', '-y', '-ss', f'{t:.2f}', '-i', str(path),
                '-vframes', '1', '-q:v', '2', tf
            ], capture_output=True, timeout=10)
            img = Image.open(tf).convert('L')
            arr = np.asarray(img)
            crop = arr[ry:ry+rh, rx:rx+rw]
            return crop.astype(np.float32) if crop.size else None
        except Exception:
            return None
        finally:
            try: os.unlink(tf)
            except OSError: pass

    # Pick the early frame containing the strongest black wordmark.  This is
    # robust to a short fade-in at the beginning of NotebookLM exports.
    best = None
    for t in (0.20, 0.40, 0.60, 0.80, 1.00, 1.25):
        if t >= total:
            break
        g = _grab_gray(t)
        if g is None:
            continue
        dark = g < 165
        very_dark = g < 105
        score = float(dark.mean()) + 0.75 * float(very_dark.mean())
        if best is None or score > best[0]:
            best = (score, t, g)

    # Guaranteed behaviour: if frame analysis fails, still cover the opening
    # for a sensible period rather than silently leaving the brand visible.
    fallback_end = min(max(2.0, total * 0.12), 12.0, total)
    if best is None:
        if progress_cb:
            progress_cb(f'   Top-logo tracking unavailable — forcing cover for {fallback_end:.1f}s')
        return fallback_end

    _, ref_t, ref = best
    # Focus only on truly dark strokes.  The NotebookLM grid/background is
    # much lighter, so it does not become part of the tracking mask.
    ref_mask = ref < 165
    ref_very_dark = ref < 105
    ink_frac = float(ref_mask.mean())
    very_dark_frac = float(ref_very_dark.mean())

    if progress_cb:
        progress_cb(
            f'   Fixed top-logo reference at {ref_t:.2f}s '
            f'(dark={ink_frac:.3f}, very-dark={very_dark_frac:.3f})'
        )

    if ink_frac < 0.0025 and very_dark_frac < 0.0010:
        if progress_cb:
            progress_cb(f'   Wordmark pixels were faint — forcing cover for {fallback_end:.1f}s')
        return fallback_end

    scan_end = min(max_scan, total)
    step = 0.40
    t = ref_t + step
    last_present = ref_t
    absent_run = 0

    while t <= scan_end:
        g = _grab_gray(t)
        if g is not None and g.shape == ref.shape:
            # Percentage of pixels that formed the original black wordmark and
            # are still dark now.  This directly tracks the actual opening logo.
            retained = float((g[ref_mask] < 195).mean()) if ref_mask.any() else 0.0
            retained_vdark = float((g[ref_very_dark] < 165).mean()) if ref_very_dark.any() else retained
            current_ink = float((g < 175).mean())

            present = (
                (retained >= 0.42 or retained_vdark >= 0.48)
                and current_ink >= max(0.0015, ink_frac * 0.18)
            )

            if present:
                last_present = t
                absent_run = 0
            else:
                absent_run += 1
                # Three misses (~1.2 s) prevents a fade/transition frame from
                # ending the cover too early.
                if absent_run >= 3:
                    end_t = min(last_present + step, total)
                    # Never return a sub-second cover for a recognised badge.
                    end_t = max(end_t, min(2.0, total))
                    if progress_cb:
                        progress_cb(f'   Fixed top logo disappears at ~{end_t:.1f}s')
                    return end_t
        t += step

    # If the logo remains detectable throughout the scan, keep the cover for
    # that entire period. This is safer than exposing NotebookLM branding.
    end_t = min(scan_end, total)
    if progress_cb:
        progress_cb(f'   Fixed top logo remains visible — covering through {end_t:.1f}s')
    return end_t


def _find_gemini_notebook_ocr_box(path, progress_cb=None, max_scan=12.0):
    """Hybrid Gemini Notebook detector.

    Uses OCR with image enhancement because the new watermark is small,
    grey and anti-aliased. Also searches for the icon/text combination area.
    Returns coordinates in 1920x1080 space.
    """
    if not OCR_AVAILABLE or not CV2_AVAILABLE:
        return None

    keywords = ("gemini notebook", "notebooklm", "gemini")
    duration = min(_probe_duration(str(path)), max_scan)

    for t in np.arange(0, duration, 0.5):
        frame = _grab_cv_gray_frame(path, float(t))
        if frame is None:
            continue

        try:
            # Improve faint grey watermark visibility
            enlarged = cv2.resize(frame, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
            enhanced = clahe.apply(enlarged)
            thresh = cv2.threshold(enhanced, 180, 255, cv2.THRESH_BINARY_INV)[1]

            variants = [enhanced, thresh]
            fh, fw = frame.shape[:2]

            for img in variants:
                data = pytesseract.image_to_data(
                    img,
                    config='--psm 11',
                    output_type=pytesseract.Output.DICT
                )

                words = []
                for i, txt in enumerate(data.get('text', [])):
                    clean = re.sub(r'[^a-z ]', '', txt.lower()).strip()
                    if clean:
                        words.append(clean)

                joined = ' '.join(words)
                if any(k in joined for k in keywords):
                    xs=[]; ys=[]; x2=[]; y2=[]
                    for i, txt in enumerate(data.get('text', [])):
                        clean = re.sub(r'[^a-z ]', '', txt.lower()).strip()
                        if clean and any(part in clean for part in ('gemini','notebook','notebooklm')):
                            x=int(data['left'][i]/2)
                            y=int(data['top'][i]/2)
                            w=int(data['width'][i]/2)
                            h=int(data['height'][i]/2)
                            xs.append(x); ys.append(y); x2.append(x+w); y2.append(y+h)

                    if xs:
                        x=min(xs); y=min(ys)
                        w=max(x2)-x; h=max(y2)-y
                    else:
                        continue

                    box=(
                        int(x*1920/fw),
                        int(y*1080/fh),
                        max(40,int(w*1920/fw)),
                        max(20,int(h*1080/fh))
                    )

                    if progress_cb:
                        progress_cb(f"   Gemini OCR detected {box}")
                    return {'box':box,'time':float(t)}

            # Fallback: known Gemini watermark area, detect dark pixels
            # bottom area even if OCR misses the text.
            crop = frame[int(fh*0.75):fh, int(fw*0.55):fw]
            if crop.size:
                dark = (crop < 190).mean()
                if dark > 0.01:
                    box=(
                        int(fw*0.55*1920/fw)-20,
                        int(fh*0.75*1080/fh)-15,
                        int(fw*0.35*1920/fw)+40,
                        int(fh*0.15*1080/fh)+30
                    )
                    if progress_cb:
                        progress_cb(f"   Gemini visual fallback detected {box}")
                    return {'box':box,'time':float(t)}

        except Exception as e:
            if progress_cb:
                progress_cb(f"   OCR error: {e}")

    return None


def remove_notebooklm_watermark(inp, out, src_resolution, tmp, progress_cb=None):
    inp_str, out_str = str(inp), str(out)
    if progress_cb: progress_cb("Detecting end-card start time…")
    ecs = _detect_end_card_start(inp_str, progress_cb=progress_cb)
    duration = _probe_duration(inp_str)
    # The detector returns `duration` when no end card is found.
    # Any value less than that means a genuine end card was detected.
    trim_at = None
    if ecs < duration - 0.3:
        trim_at = ecs
        if progress_cb: progress_cb(f"✂️ Trimming end card at {trim_at:.1f}s  ({duration - trim_at:.1f}s removed)")
    else:
        if progress_cb: progress_cb("   No end card to trim")
    use_logo = SLC_LOGO.exists() and SLC_LOGO.stat().st_size > 500

    # --- Persistent bottom-right "Gemini Notebook" badge cover ────────────
    # This badge is present for the whole video, so it always gets covered
    # (with the SLC logo composited into the same box, if available).
    br_box = (WM_BR_X, WM_BR_Y, WM_BR_W, WM_BR_H)
    br_png = tmp / "wm_br.png"
    if use_logo:
        br_png = _make_logo_composite(logo_path=SLC_LOGO, box=br_box, logo_h=56)
    else:
        _make_box_png([(*br_box, BOX_RADIUS)], br_png, colour=(249, 249, 249, 255))

    # --- Optional separate opening-title wordmark cover ────────────────────
    # Some exports also show a larger "Gemini Notebook" wordmark on the
    # opening title card, in a different spot from the persistent badge
    # above. When present, we cover that too, but ONLY for the seconds it is
    # actually on screen — never permanently, since that area holds real
    # video content for the rest of the clip.
    if progress_cb:
        progress_cb("Detecting a separate opening-title Gemini Notebook wordmark…")
    gemini_match = _find_gemini_notebook_ocr_box(inp_str, progress_cb=progress_cb)
    if not gemini_match and progress_cb:
        progress_cb("   No separate opening wordmark found — covering only the persistent badge")

    opening_box = None
    top_end = 0.0
    if gemini_match:
        gx, gy, gw, gh = gemini_match["box"]
        # Generous padding removes the icon, all lettering, and antialiased edge
        # pixels while avoiding the large title below.
        pad_x, pad_y = 28, 18
        ox = max(0, gx - pad_x)
        oy = max(0, gy - pad_y)
        ow = min(1920 - ox, gw + pad_x * 2)
        oh = min(1080 - oy, gh + pad_y * 2)

        # If the "opening" match actually falls inside/on top of the
        # persistent bottom-right box, it's the same badge — don't cover it
        # twice (that duplicate cover, drawn only for a few seconds, is what
        # previously blanked out the SLC logo instead of showing it).
        bx, by, bw, bh = br_box
        overlaps_br = not (ox + ow <= bx or bx + bw <= ox or oy + oh <= by or by + bh <= oy)
        if overlaps_br:
            if progress_cb:
                progress_cb("   Detected wordmark overlaps the persistent badge — using single cover only")
        else:
            opening_box = (ox, oy, ow, oh)
            top_end = _track_gemini_notebook_end(
                inp_str, gemini_match, progress_cb=progress_cb, max_scan=60.0
            )
            if top_end is None or top_end <= 0:
                top_end = _detect_fixed_badge_end_guaranteed(
                    inp_str, progress_cb=progress_cb, max_scan=60.0
                )
            if top_end is None or top_end <= 0:
                top_end = min(15.0, duration)
            if progress_cb:
                progress_cb(
                    f"   ✅ Separate opening cover active to ~{top_end:.1f}s "
                    f"at ({ox},{oy}) {ow}x{oh}"
                )

    cmd = ["ffmpeg", "-y", "-i", inp_str, "-i", str(br_png)]
    if opening_box:
        open_png = tmp / "wm_open.png"
        if use_logo:
            open_png = _make_logo_composite(logo_path=SLC_LOGO, box=opening_box, logo_h=44)
        else:
            _make_box_png([(*opening_box, BOX_RADIUS)], open_png, colour=(249, 249, 249, 255))
        en_top = f"between(t\\,0\\,{top_end:.2f})"
        fc = ("[1:v]format=rgba[br];[0:v][br]overlay=x=0:y=0[vbr];"
              "[2:v]format=rgba[op];"
              f"[vbr][op]overlay=x=0:y=0:enable='{en_top}'[vout]")
        cmd += ["-i", str(open_png)]
    else:
        fc = "[1:v]format=rgba[br];[0:v][br]overlay=x=0:y=0[vout]"
    if trim_at is not None:
        cmd += ["-filter_complex",fc,"-map","[vout]","-map","0:a",
                "-t",f"{trim_at:.2f}","-c:v","libx264","-preset","ultrafast","-crf","23",
                "-c:a","aac","-b:a","128k","-ar","48000","-ac","2","-r","30","-pix_fmt","yuv420p",out_str]
    else:
        cmd += ["-filter_complex",fc,"-map","[vout]","-map","0:a",
                "-c:v","libx264","-preset","ultrafast","-crf","23",
                "-c:a","aac","-b:a","128k","-ar","48000","-ac","2","-r","30","-pix_fmt","yuv420p","-shortest",out_str]
    _ff(cmd, timeout=max(900, int(duration*25)))
    return Path(out)


def _find_keyframe_at_or_after(path, target_t, window=30.0):
    """Return the timestamp (seconds) of the first video keyframe at or
    after target_t, scanning only a `window`-second slice of the file
    (fast — decodes keyframes only, not the whole video). Returns None if
    no keyframe is found in that slice.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-skip_frame", "nokey",
             "-show_entries", "frame=pts_time",
             "-read_intervals", f"{target_t:.3f}%+{window:.0f}",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = float(line)
            except ValueError:
                continue
            if t >= target_t - 0.001:
                return t
    except Exception:
        pass
    return None


def add_notebooklm_transition(intro, main, out, tmp, duration=1.0, direction="left"):
    tm = {"left":"wipeleft","right":"wiperight","up":"wipeup","down":"wipedown"}
    wipe = tm.get(direction,"wipeleft"); intro_d = _probe_duration(intro)
    half = max(0.25, min(duration/2, intro_d-0.05))
    cc = "color=c=black:s=1920x1080:r=30"
    main_d = _probe_duration(main)

    # The transition filter below only ever blends in the first `half`
    # seconds of `main` — everything after that plays back completely
    # unchanged. Re-encoding the *entire* main video just to add that
    # first fraction of a second of blending wastes huge amounts of time
    # on longer videos. When main is long enough, split it at a keyframe
    # just past the point the transition actually needs, re-encode only
    # that short head alongside the intro, and stream-copy (no re-encode,
    # near-instant) the much larger remainder before concatenating.
    needed = half + 1.0
    split_t = None
    if main_d > needed + 3.0:
        candidate = _find_keyframe_at_or_after(main, needed)
        if candidate is not None and candidate < main_d - 0.5:
            split_t = candidate

    tail = None
    if split_t is None:
        main_input = ["-i", str(main)]
        trans_timeout = max(600, int(main_d * 20))
        trans_out = out
    else:
        main_input = ["-t", f"{split_t:.3f}", "-i", str(main)]
        trans_timeout = 120  # only ~split_t seconds of main are encoded now
        trans_out = tmp / "transition_head.mp4"
        tail = tmp / "main_tail.mp4"
        # A tiny epsilon nudges past the keyframe's exact timestamp. Without
        # it, floating-point rounding can make ffmpeg treat `-ss` as landing
        # a hair *before* the keyframe and seek back to the previous one
        # instead (often frame 0 — i.e. no trim at all). The epsilon is far
        # smaller than the gap to the next keyframe, so it can't overshoot.
        tail_ss = split_t + 0.02
        _ff(["ffmpeg", "-y", "-ss", f"{tail_ss:.3f}", "-i", str(main),
             "-c", "copy", "-avoid_negative_ts", "make_zero", str(tail)],
            timeout=120)

    _ff(["ffmpeg","-y","-i",str(intro)] + main_input +
        ["-f","lavfi","-t",f"{duration}","-i",cc,
         "-f","lavfi","-t",f"{duration}","-i","anullsrc=r=48000:cl=stereo",
         "-filter_complex",
         "[0:v]fps=30,format=yuv420p,settb=AVTB[v0];"
         "[1:v]fps=30,format=yuv420p,settb=AVTB[v1];"
         "[2:v]fps=30,format=yuv420p,settb=AVTB[vc];"
         f"[v0][vc]xfade=transition={wipe}:duration={half}:offset={max(intro_d-half,0):.3f}[vx];"
         f"[vx][v1]xfade=transition={wipe}:duration={half}:offset={intro_d:.3f}[vout];"
         f"[0:a][3:a]acrossfade=d={half}:c1=tri:c2=tri[ax];"
         f"[ax][1:a]acrossfade=d={half}:c1=tri:c2=tri[aout]",
         "-map","[vout]","-map","[aout]",
         "-c:v","libx264","-preset","ultrafast","-crf","23",
         "-c:a","aac","-b:a","128k","-ar","48000","-ac","2",
         "-r","30","-pix_fmt","yuv420p",str(trans_out)], timeout=trans_timeout)

    if tail is None:
        return Path(out)
    return concat([trans_out, tail], out, tmp)


def concat(parts, out, tmp):
    lst = tmp/"list.txt"
    with open(lst,"w") as f:
        for p in parts: f.write(f"file '{Path(p).resolve()}'\n")
    try:
        _ff(["ffmpeg","-y","-f","concat","-safe","0","-i",str(lst),"-c","copy",str(out)])
    except RuntimeError:
        _ff(["ffmpeg","-y","-f","concat","-safe","0","-i",str(lst),
             "-c:v","libx264","-preset","ultrafast","-crf","23",
             "-c:a","aac","-b:a","128k","-pix_fmt","yuv420p",str(out)])
    return Path(out)


def ensure_assets():
    """Make sure the SLC logo and intro template are present and valid.
    The logo ships as a real file in assets/slc_logo.png. The embedded
    base64 string below is kept only as a last-resort fallback and is
    validated with Pillow before being trusted, since a corrupt fallback
    should fail loudly at startup rather than break mid-job."""
    if not SLC_LOGO.exists() or SLC_LOGO.stat().st_size < 100:
        SLC_LOGO.parent.mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode(_SLC_LOGO_B64)
        SLC_LOGO.write_bytes(raw)
        try:
            img = Image.open(SLC_LOGO)
            img.load()
        except Exception as e:
            raise RuntimeError(
                f"assets/slc_logo.png is missing and the embedded fallback "
                f"logo is corrupt ({e}). Add a real slc_logo.png to assets/."
            )
    if not INTRO_TPL.exists():
        raise FileNotFoundError(f"Missing intro template: {INTRO_TPL}")
    if INTRO_TPL.stat().st_size < 10000:
        raise RuntimeError("Intro template appears corrupt.")


def process_video(course_name: str, unit_number: str, video_bytes: bytes,
                   tmp: Path, progress_cb=None, chapter_number: str = "",
                   awarding_body: str = "") -> tuple[bytes, str]:
    """Run the full merge pipeline on raw video bytes. Returns
    (output_bytes, output_filename). This is the same sequence the
    original Streamlit queue used: normalise -> intro/outro -> remove
    watermark -> transition -> concat with outro."""
    def _p(msg):
        if progress_cb:
            progress_cb(msg)

    course_name = _clean_text_field(course_name)
    unit_number = _clean_text_field(unit_number, max_len=40)
    chapter_number = _clean_text_field(chapter_number, max_len=40)
    awarding_body = _clean_text_field(awarding_body)

    raw = tmp / "raw.mp4"
    raw.write_bytes(video_bytes)
    src_res = _probe_resolution(str(raw))

    _p("1/4 Building intro, outro, normalising…")
    results, errors = {}, {}

    def _job(name, fn, *args):
        try:
            results[name] = fn(*args)
        except Exception as e:
            errors[name] = e

    # All three of these are independent of each other, so they run as
    # three concurrent tasks instead of "norm in the background while
    # intro+outro block the main thread one after another" — that serial
    # intro/outro build was needless extra wall-clock time per job.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_job, "norm", normalise, raw, tmp / "norm.mp4"),
            pool.submit(_job, "intro", make_intro, course_name, unit_number, chapter_number, awarding_body, tmp),
            pool.submit(_job, "outro", make_outro, tmp),
        ]
        for f in futures:
            f.result()

    if errors:
        raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()))

    _p(f"2/4 Replacing watermarks ({src_res[0]}x{src_res[1]})…")
    norm_clean = remove_notebooklm_watermark(
        results["norm"], tmp / "norm_clean.mp4", src_res, tmp,
        progress_cb=lambda s: _p(f"2/4 {s}"))

    _p("3/4 Adding transition…")
    with_trans = add_notebooklm_transition(
        results["intro"], norm_clean, tmp / "intro_and_main.mp4", tmp)

    _p("4/4 Merging final segments…")
    final = concat([with_trans, results["outro"]], tmp / "final.mp4", tmp)

    data = final.read_bytes()
    name_parts = [course_name[:30], unit_number]
    if chapter_number:
        name_parts.append(chapter_number)
    fn = _safe_filename("SLC_Video_" + "_".join(name_parts) + ".mp4")
    return data, fn


def preview_frame(course, unit_num, chapter_number, awarding_body=""):
    if not INTRO_TPL.exists(): raise FileNotFoundError(f"Missing: {INTRO_TPL}")
    fd, tp = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        subprocess.run(["ffmpeg","-y","-i",str(INTRO_TPL),"-ss","3","-vframes","1",tp],
                       capture_output=True, timeout=10)
        bg = Image.open(tp).convert("RGBA"); bg.load()
    finally:
        try: os.unlink(tp)
        except: pass
    comp = Image.alpha_composite(bg, render_intro_overlay(course,unit_num,chapter_number,awarding_body)).convert("RGB")
    buf = BytesIO(); comp.save(buf,"JPEG",quality=90); buf.seek(0)
    return buf
