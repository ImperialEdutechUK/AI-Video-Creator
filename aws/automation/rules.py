"""
Pure decision rules for the sheet -> NotebookLM -> Drive automation.

No I/O here, so every rule is unit-tested (tests/test_rules.py) and shared
by the API Lambda (sheet scanning, write-back) and the NotebookLM container
(title matching).
"""
from __future__ import annotations

import colorsys
import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field

MAX_ATTEMPTS = 3

# Sheet values the bot writes (column H / column I).
BOT_NAME = "auto"
ADDED_YES = "Yes"
ADDED_NO = "NO"

# AUTO item states (DynamoDB). "active" states are still being worked on.
ACTIVE_STATES = {"pending", "downloading", "failing", "processing", "saving", "verifying", "writing"}
TERMINAL_STATES = {"done", "failed", "abandoned"}

# Known awarding-body spellings that differ between the "Awarding Body"
# column and the start of the course name. Keys are lower-case.
AWARDING_BODY_ALIASES: dict[str, list[str]] = {
    "othm": ["OTHM Qualifications"],
    "othm qualifications": ["OTHM"],
    "tquk": ["Training Qualifications UK"],
    "training qualifications uk": ["TQUK"],
    "qualifi": ["Qualifi Ltd"],
    "focus awards": ["Focus Award"],
}
_STOPWORDS = {"the", "a", "an", "of", "and", "for", "in", "uk"}


# ── text clean-up ────────────────────────────────────────────────────────

def norm_space(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _prefix_pattern(name: str) -> re.Pattern:
    words = [re.escape(w) for w in name.split()]
    # The awarding body must be a whole word at the start, followed by a
    # separator or whitespace, so "Qualifi" never eats "Qualifications".
    return re.compile(r"^\s*" + r"\s+".join(words) + r"(?:\s*[-–—:|,]\s*|\s+)", re.I)


def strip_awarding_body(awarding_body: str, course_name: str) -> str:
    """Remove the awarding body from the start of the course name.

    Qualifi + "Qualifi Level 03 Extended Diploma in Health and Social Care"
      -> "Level 03 Extended Diploma in Health and Social Care"

    Returns the course name unchanged when it doesn't start with the
    awarding body, or when removing it would leave nothing.
    """
    course = norm_space(course_name)
    body = norm_space(awarding_body)
    if not course or not body:
        return course
    names = [body] + AWARDING_BODY_ALIASES.get(body.lower(), [])
    words = body.split()
    if len(words) >= 2 and len(words[0]) >= 3 and words[0].lower() not in _STOPWORDS:
        names.append(words[0])  # "Highfield Qualifications" -> also try "Highfield"
    for name in sorted(set(names), key=len, reverse=True):
        m = _prefix_pattern(name).match(course)
        if m and course[m.end():].strip():
            return course[m.end():].strip()
    return course


def parse_number(value) -> str:
    """'14', 14, 14.0, 'Unit 14', 'U14', '014' -> '14'. Anything else -> ''."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return ""
        return str(int(value)) if int(value) > 0 else ""
    text = norm_space(value)
    m = re.fullmatch(r"(?i)(?:unit|chapter|ch|u|c)?\s*\.?\s*0*(\d{1,4})(?:\.0+)?", text)
    if not m or int(m.group(1)) == 0:
        return ""
    return str(int(m.group(1)))


def unit_label(unit: str) -> str:
    """Same format the web app sends: "Unit 14"."""
    return f"Unit {unit}"


def chapter_label(chapter: str) -> str:
    return f"Chapter {chapter}"


# ── NotebookLM title matching ────────────────────────────────────────────

_TITLE_RE = re.compile(
    r"^\s*U(?:nit)?[\s._-]*0*(\d{1,4})[\s._-]*C(?:h(?:apter)?)?[\s._-]*0*(\d{1,4})(?!\d)",
    re.I,
)


def title_unit_chapter(title: str) -> tuple[str, str] | None:
    """'U22C6' -> ('22', '6'); 'U14C4- Focus...' -> ('14', '4'); else None."""
    m = _TITLE_RE.match(title or "")
    if not m:
        return None
    return str(int(m.group(1))), str(int(m.group(2)))


def title_matches(title: str, unit: str, chapter: str) -> bool:
    found = title_unit_chapter(title)
    return found is not None and found == (str(int(unit)), str(int(chapter)))


def _norm_words(text: str) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"\(.*?\)", " ", text)            # "(RQF)" etc.
    return " ".join(re.findall(r"[a-z0-9]+", text))


def title_course(title: str, awarding_body: str = "") -> str:
    """Course part of a Studio title: "U2C4 - Level 3 Certificate in ..." ->
    "level 3 certificate in ..." (awarding body removed, normalised)."""
    m = _TITLE_RE.match(title or "")
    rest = (title or "")[m.end():] if m else ""
    rest = re.sub(r"^[\s\-_.:|–—]+", "", rest)
    return _norm_words(strip_awarding_body(awarding_body, rest))


def course_match(title: str, course: str, awarding_body: str = "") -> float | None:
    """How well a Studio title's course part fits the row's course, 0..1.
    None when the title has no course part (e.g. just "U22C6").
    Titles are often cut short, so a title that is the start of the course
    name (at least 3 words) counts as a full match."""
    import difflib

    t = title_course(title, awarding_body)
    if not t:
        return None
    c = _norm_words(strip_awarding_body(awarding_body, course))
    if not c:
        return None
    if len(t.split()) >= 3 and (c.startswith(t) or t.startswith(c)):
        return 1.0
    # Compare whole words: shared filler like "level ... certificate in" must
    # not make two different courses look alike.
    return difflib.SequenceMatcher(None, t.split(), c.split()).ratio()


COURSE_MATCH_MIN = 0.8      # below this a titled video is for a different course
COURSE_MISMATCH_MAX = 0.5   # a lone candidate scoring below this is rejected


def pick_by_course(titles: list[str], course: str, awarding_body: str = "") -> tuple[list[int], str]:
    """Narrow same-unit/chapter Studio items down by the course name in their titles.

    Returns (indexes to keep, note). Titles without a course part are kept as
    they are. If several titles carry course names, only the best match(es)
    at or above COURSE_MATCH_MIN are kept; a lone titled item is dropped only
    when it is clearly for another course."""
    scores = [course_match(t, course, awarding_body) for t in titles]
    titled = [i for i, s in enumerate(scores) if s is not None]
    untitled = [i for i, s in enumerate(scores) if s is None]
    if not titled:
        return list(range(len(titles))), ""
    if len(titles) == 1:
        s = scores[0]
        if s < COURSE_MISMATCH_MAX:
            return [], f"the only video with that number is for another course ({titles[0][:80]!r})"
        return [0], ""
    good = [i for i in titled if scores[i] >= COURSE_MATCH_MIN]
    if not good:
        return untitled, "no video title matches this course name"
    best = max(scores[i] for i in good)
    keep = [i for i in good if scores[i] == best]
    return keep + untitled, ""


# ── sheet colours ────────────────────────────────────────────────────────

def classify_color(hex_color: str | None) -> str | None:
    """Map a cell colour to red / orange / yellow / green / blue / purple, or
    None for white, grey, black and "no fill". Pastel fills count too."""
    if not hex_color:
        return None
    h = hex_color.strip().lstrip("#")
    if len(h) == 8:  # AARRGGBB
        h = h[2:]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        return None
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
    if sat < 0.08 or val < 0.15:
        return None
    deg = hue * 360
    if deg < 15 or deg >= 340:
        return "red"
    if deg < 42:
        return "orange"
    if deg < 72:
        return "yellow"
    if deg < 170:
        return "green"
    if deg < 255:
        return "blue"
    return "purple"


# ── rows ─────────────────────────────────────────────────────────────────

@dataclass
class SheetRow:
    row: int                 # 1-based Excel row number
    date: str
    awarding_body: str
    course_raw: str
    unit_raw: object
    chapter_raw: object
    url: str
    done_by: str             # column H
    added: str               # column I
    colors: list[str] = field(default_factory=list)
    date_value: object = None  # raw cell value (Excel date serial when it is a real date)

    @property
    def unit(self) -> str:
        return parse_number(self.unit_raw)

    @property
    def chapter(self) -> str:
        return parse_number(self.chapter_raw)

    @property
    def course(self) -> str:
        return strip_awarding_body(self.awarding_body, self.course_raw)


def row_date(row: SheetRow) -> dt.date | None:
    """The row's date from column A: an Excel serial number, or text such as
    10/23/2026 (US order, as the sheet displays it) or 2026-10-23."""
    v = row.date_value
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 20000 < v < 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    text = norm_space(row.date if v in (None, "") else v)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def row_key(workbook_id: str, url: str, unit: str, chapter: str, course: str = "") -> str:
    """Stable identity for a sheet row: inserting or sorting rows doesn't
    change it, and the test copy and the real sheet never share keys.
    The course is part of it because one shared notebook link is used by
    several courses with the same unit/chapter numbers."""
    raw = f"{workbook_id}|{norm_space(url)}|{unit}|{chapter}|{_norm_words(course)}"
    return "AUTO#" + hashlib.sha1(raw.encode()).hexdigest()[:24]


def is_notebook_link(url: str) -> bool:
    # NotebookLM is now also served as "Gemini Notebook" on notebook.google.com.
    return bool(re.match(r"(?i)^https?://(notebook(?:lm)?\.link\.google|notebook(?:lm)?\.google\.com|goo\.gle)/",
                         norm_space(url)))


@dataclass
class Decision:
    action: str      # "new" | "retry" | "skip"
    reason: str = ""
    reset_attempts: bool = False


def decide(row: SheetRow, item: dict | None, max_attempts: int = MAX_ATTEMPTS) -> Decision:
    """What the bot should do with one sheet row, given its DynamoDB record.

    The bot only ever touches rows where column H is empty or "auto":
    anything a person has put their name on is left alone.
    """
    done_by = norm_space(row.done_by).lower()
    added = norm_space(row.added).lower()

    if not norm_space(row.url):
        return Decision("skip", "no URL")
    if done_by and done_by != BOT_NAME:
        return Decision("skip", f"done by {row.done_by}")
    if added == "yes":
        return Decision("skip", "already added")
    if added and added != "no":
        return Decision("skip", f"column I says {row.added!r}")
    if added == "no" and done_by != BOT_NAME:
        return Decision("skip", "marked NO by a person")
    if not is_notebook_link(row.url):
        return Decision("skip", "URL is not a NotebookLM link")
    if not row.unit or not row.chapter:
        return Decision("skip", "unit or chapter number missing")
    if not norm_space(row.course_raw):
        return Decision("skip", "course name missing")

    if item is None:
        return Decision("new")
    state = item.get("qstate", "")
    attempts = int(item.get("attempts", 0) or 0)
    if state in ACTIVE_STATES:
        return Decision("skip", f"in progress ({state})")
    if not added:
        # H is empty/auto and I is blank although we finished before: a
        # person cleared the cell to ask for a fresh try.
        return Decision("new", "cleared in the sheet", reset_attempts=True)
    # added == "no" and done_by == auto
    if state == "failed" and attempts < max_attempts:
        return Decision("retry", f"attempt {attempts + 1} of {max_attempts}")
    return Decision("skip", f"gave up after {attempts} attempts")


def order_rows(rows: list[SheetRow], start_row: int = 0) -> list[SheetRow]:
    """Processing order. With a start row: that row and everything below it
    first (top to bottom), then the older rows above it, nearest first."""
    if not start_row:
        return sorted(rows, key=lambda r: r.row)
    return sorted(rows, key=lambda r: (0, r.row) if r.row >= start_row else (1, -r.row))


def skip_for_color(colors: list[str], skip_colors: set[str]) -> str | None:
    for c in colors:
        if c in skip_colors:
            return c
    return None
