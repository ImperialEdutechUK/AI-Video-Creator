"""
Pure logic for the chapter-video creator (no AWS, no browser):

- reading the "Doc Formatting Completed Units" tab,
- deciding which units still need chapter videos,
- awarding body / course name split,
- names used in NotebookLM and the row written to the "Chapter-wise AI video log".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass

from rules import norm_space

BOT_NAME = "auto"
STATUS_DONE = "Done"     # column J when every chapter of the unit is in the video log

# "Doc Formatting Completed Units" columns (the team's layout).
DOC_COLS = {"course": "A", "link": "B", "unit": "C", "done_by": "D", "videos_by": "I", "videos_status": "J"}
DOC_HEADERS = {"A": "course name", "B": "course link", "C": "unit", "I": "chapter-wise video"}

# Awarding bodies as written in the log's column B (left: how they appear in
# course names, lower case, longest first so "ncfe cache" wins over "ncfe").
AWARDING_BODIES = [
    ("ncfe cache", "NCFE CACHE"), ("ncfecache", "NCFE CACHE"), ("ncfc cache", "NCFE CACHE"),
    ("focus awards", "Focus Awards"), ("focus award", "Focus Awards"),
    ("pearson btec", "Pearson BTEC"),
    ("qualifi", "Qualifi"), ("qualify", "Qualifi"),
    ("ncfe", "NCFE"), ("ncfc", "NCFE"),
    ("athe", "ATHE"), ("tquk", "TQUK"), ("ncc", "NCC"), ("othm", "OTHM"),
]

# Max chapters NotebookLM works on at the same time, and units in progress.
MAX_ATTEMPTS = 2


@dataclass
class DocRow:
    row: int
    course_raw: str
    link: str
    unit_raw: str
    done_by: str
    videos_by: str

    @property
    def unit(self) -> int | None:
        m = re.search(r"\d+", self.unit_raw or "")
        return int(m.group()) if m and int(m.group()) > 0 else None


def doc_rows(first_row: int, grid: list[list[str]]) -> list[DocRow]:
    """Rows of the Doc Formatting tab from a usedRange text grid starting at column A."""
    out = []
    for i, r in enumerate(grid):
        r = list(r) + [""] * (12 - len(r))
        out.append(DocRow(first_row + i, norm_space(r[0]), norm_space(r[1]), norm_space(r[2]),
                          norm_space(r[3]), norm_space(r[8])))
    return out


def check_doc_header(header: list[str]) -> str | None:
    """None if the header looks like the team's layout, else what's wrong."""
    h = [norm_space(x).lower() for x in list(header) + [""] * 12]
    idx = {"A": 0, "B": 1, "C": 2, "I": 8}
    for col, want in DOC_HEADERS.items():
        if want not in h[idx[col]]:
            return f"column {col} should be '{want}' but is '{h[idx[col]]}'"
    return None


def canonical_awarding_body(text: str) -> str:
    t = norm_space(text).lower().replace(" ", " ")
    for key, name in AWARDING_BODIES:
        if t == key:
            return name
    return norm_space(text)


def split_course_name(name: str) -> tuple[str, str]:
    """'NCFE CACHE Level 2 Award in X' -> ('NCFE CACHE', 'Level 2 Award in X').
    Returns ('', name) when the name doesn't start with an awarding body."""
    clean = norm_space(name.replace(" ", " "))
    low = clean.lower()
    for key, canon in AWARDING_BODIES:
        if low.startswith(key) and (len(low) == len(key) or not low[len(key)].isalnum()):
            rest = clean[len(key):].lstrip(" -:–—")
            if rest:
                return canon, norm_space(rest)
    return "", clean


def course_key(course: str) -> str:
    """Comparable course name: awarding body and punctuation dropped, from 'Level' on."""
    _, c = split_course_name(course)
    m = re.search(r"\blevel\b", c, re.I)
    if m:
        c = c[m.start():]
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", c.lower()).split())


def unit_key(course_link: str, unit: int) -> str:
    link = course_link.strip().rstrip("/").lower()
    return "CUNIT#" + hashlib.sha1(f"{link}|{unit}".encode()).hexdigest()


def logged_units(log_rows) -> set[tuple[str, int]]:
    """(course key, unit) pairs that already have rows in the video log."""
    out = set()
    for r in log_rows:
        if r.course_raw and r.unit:
            try:
                out.add((course_key(r.course_raw), int(r.unit)))
            except (TypeError, ValueError):
                continue
    return out


@dataclass
class UnitDecision:
    action: str   # "claim" | "skip"
    reason: str = ""


def sheet_skip(row: DocRow, logged: set[tuple[str, int]]) -> UnitDecision | None:
    """The skip the sheet alone decides, whatever the bot's record says (None = the
    record matters). Lets the scan read records only for the rows that need them."""
    if not row.course_raw or row.course_raw.lower() in ("test", "testing", "course name"):
        return UnitDecision("skip", "no course name")
    who = row.videos_by.lower()
    if who and who != BOT_NAME:
        return UnitDecision("skip", "chapter videos done by a person")
    if row.unit is None:
        return UnitDecision("skip", "no unit number")
    if not row.link.startswith("https://southlondoncollege.org/"):
        return UnitDecision("skip", "no course link")
    if (course_key(row.course_raw), row.unit) in logged:
        return UnitDecision("skip", "already in the video log")
    return None


def decide_unit(row: DocRow, logged: set[tuple[str, int]], item: dict | None, now: float) -> UnitDecision:
    skip = sheet_skip(row, logged)
    if skip:
        return skip
    who = row.videos_by.lower()
    if item:
        state = item.get("cstate")
        if state == "skipped" and float(item.get("retry_at", 0)) <= now and who != BOT_NAME:
            return UnitDecision("claim", "re-check after an earlier problem")
        return UnitDecision("skip", f"bot: {state}")
    if who == BOT_NAME:
        return UnitDecision("skip", "marked auto but unknown to the bot")
    return UnitDecision("claim")


@dataclass
class MarkFix:
    row: int
    col: str          # "I" or "J"
    value: str        # "" clears
    course: str
    unit: int | None
    reason: str


def unit_identity(course_raw: str, link: str, unit) -> tuple[str, str, int]:
    return (norm_space(course_raw), norm_space(link).rstrip("/").lower(), int(unit))


def plan_mark_fixes(rows: list[DocRow], grid: list[list[str]], units: list[dict]) -> list[MarkFix]:
    """Keep the bot's marks in "Chapter-wise Videos" (I) / "Status" (J) on the right
    rows, whatever people do to the row order. A row is identified by its course
    name, course link and unit - never by its row number.

    - a unit the bot made (or is making): I = auto, and J = Done once every
      chapter is in the video log (only filled in when empty)
    - "auto" on a row the bot has no record of (left behind when rows were
      moved or partly sorted): cleared, together with a "Done" next to it
    - a person's name in I is never touched."""
    expected: dict[tuple, dict] = {}
    for u in units:
        if u.get("cstate") in ("chapters", "logging", "done"):
            try:
                expected[unit_identity(u["course_raw"], u["course_link"], u["unit"])] = u
            except (KeyError, TypeError, ValueError):
                continue
    fixes = []
    for r in rows:
        if r.unit is None:
            ident = None
        else:
            ident = unit_identity(r.course_raw, r.link, r.unit)
        cells = list(grid[r.row - 1]) + [""] * 12 if r.row - 1 < len(grid) else [""] * 12
        i_val, j_val = norm_space(r.videos_by), norm_space(cells[9])
        u = expected.get(ident) if ident else None
        if u is not None:
            if i_val and i_val.lower() != BOT_NAME:
                continue  # a person's name: theirs to keep
            if not i_val:
                fixes.append(MarkFix(r.row, "I", BOT_NAME, r.course_raw, r.unit, "bot's unit had lost its 'auto'"))
            complete = u.get("cstate") == "done" and all(c.get("state") == "ready" for c in u.get("chapters", []))
            if complete and not j_val:
                fixes.append(MarkFix(r.row, "J", STATUS_DONE, r.course_raw, r.unit, "bot's finished unit had lost 'Done'"))
        elif i_val.lower() == BOT_NAME:
            fixes.append(MarkFix(r.row, "I", "", r.course_raw, r.unit, "'auto' on a unit the bot never took"))
            if j_val.lower() == STATUS_DONE.lower():
                fixes.append(MarkFix(r.row, "J", "", r.course_raw, r.unit, "'Done' next to that stray 'auto'"))
    return fixes


def order_units(rows: list[DocRow]) -> list[DocRow]:
    """Newest first (bottom of the sheet upwards)."""
    return sorted(rows, key=lambda r: -r.row)


def log_date(today: dt.date) -> str:
    """Column A as the team writes it (e.g. 11/12/2026)."""
    return f"{today.month}/{today.day}/{today.year}"


def log_row(today: dt.date, awarding_body: str, course: str, unit: int, chapter: int, link: str) -> list[str]:
    """Columns A..G of a new video-log row. H and I stay empty so the download
    bot picks it up and takes it through rebranding to Drive."""
    return [log_date(today), awarding_body, course, str(unit), str(chapter), BOT_NAME, link]
