"""
Pure logic for the unit podcasts and study guides (no AWS, no browser).

Both come from the "Doc Formatting Completed Units" tab, one unit per row:
  podcast:     column E "Podcasts"      ("auto" while the bot makes it) -> F "Status" = Done
  study guide: column G "Study Guides"  ("auto" while the bot makes it) -> H "Status" = Done
A person's name in E / G means that person does it: the bot leaves the row alone.

Course names inside the materials never show the awarding body ("Focus Awards",
"NCFE CACHE", "Qualifi"...); Drive folders keep the sheet's full name.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import creator_rules as cr
from rules import norm_space

BOT_NAME = cr.BOT_NAME
STATUS_DONE = cr.STATUS_DONE


@dataclass(frozen=True)
class Kind:
    name: str          # "podcast" | "studyguide"
    label: str         # for people: "podcast" | "study guide"
    prefix: str        # DynamoDB key prefix
    item_kind: str     # DynamoDB "kind"
    active: str        # value of the 'active' index while in progress
    who_col: str
    status_col: str
    header: str        # expected text in the who column's header

    @property
    def who_idx(self) -> int:
        return ord(self.who_col) - ord("A")

    @property
    def status_idx(self) -> int:
        return ord(self.status_col) - ord("A")


PODCAST = Kind("podcast", "podcast", "PUNIT", "punit", "P", "E", "F", "podcast")
STUDYGUIDE = Kind("studyguide", "study guide", "SUNIT", "sunit", "S", "G", "H", "study guide")
KINDS = (PODCAST, STUDYGUIDE)

# Bot states. Runner (NotebookLM / StudyGen browser) states, then the API's.
RUNNER_STATES = ("queued", "audio", "making")
UPLOAD_STATE = "upload"          # the file is in S3: the API puts it in Drive
FINAL_STATES = ("done", "failed", "skipped")
MAX_ATTEMPTS = 2


def unit_key(kind: Kind, course_link: str, unit: int) -> str:
    link = course_link.strip().rstrip("/").lower()
    return f"{kind.prefix}#" + hashlib.sha1(f"{link}|{unit}".encode()).hexdigest()


def check_headers(header: list[str]) -> str | None:
    h = [norm_space(x).lower() for x in list(header) + [""] * 12]
    for k in KINDS:
        if k.header not in h[k.who_idx]:
            return f"column {k.who_col} should be '{k.header}…' but is '{h[k.who_idx]}'"
        if "status" not in h[k.status_idx]:
            return f"column {k.status_col} should be 'Status' but is '{h[k.status_idx]}'"
    return None


def cell(grid_row: list[str], idx: int) -> str:
    row = list(grid_row) + [""] * 12
    return norm_space(row[idx])


def course_title(course_raw: str) -> str:
    """The course name as it appears in the materials: no awarding body."""
    return cr.split_course_name(course_raw)[1]


@dataclass
class Decision:
    action: str      # "claim" | "skip"
    reason: str = ""


def decide(kind: Kind, row: cr.DocRow, grid_row: list[str], item: dict | None, now: float) -> Decision:
    who = cell(grid_row, kind.who_idx).lower()
    if not row.course_raw or row.course_raw.lower() in ("test", "testing", "course name"):
        return Decision("skip", "no course name")
    if who and who != BOT_NAME:
        return Decision("skip", f"{kind.label} done by a person")
    if row.unit is None:
        return Decision("skip", "no unit number")
    if not row.link.startswith("https://southlondoncollege.org/"):
        return Decision("skip", "no course link")
    if item:
        state = item.get("state")
        if state == "skipped" and float(item.get("retry_at", 0)) <= now and who != BOT_NAME:
            return Decision("claim", "re-check after an earlier problem")
        return Decision("skip", f"bot: {state}")
    if who == BOT_NAME:
        return Decision("skip", "marked auto but unknown to the bot")
    if cell(grid_row, kind.status_idx).lower() == STATUS_DONE.lower():
        return Decision("skip", "already marked Done")
    return Decision("claim")


def plan_mark_fixes(kind: Kind, rows: list[cr.DocRow], grid: list[list[str]], items: list[dict]) -> list[cr.MarkFix]:
    """Same as the chapter videos' mark repair, for this kind's two columns:
    the bot's units keep 'auto' (+ 'Done' when finished) whatever people do to
    the row order; a stray 'auto' the bot never took is cleared; a person's name
    is never touched."""
    expected: dict[tuple, dict] = {}
    for it in items:
        if it.get("state") not in ("skipped", "failed", None):
            try:
                expected[cr.unit_identity(it["course_raw"], it["course_link"], it["unit"])] = it
            except (KeyError, TypeError, ValueError):
                continue
    fixes = []
    for r in rows:
        ident = cr.unit_identity(r.course_raw, r.link, r.unit) if r.unit is not None else None
        g = grid[r.row - 1] if r.row - 1 < len(grid) else []
        who, status = cell(g, kind.who_idx), cell(g, kind.status_idx)
        it = expected.get(ident) if ident else None
        if it is not None:
            if who and who.lower() != BOT_NAME:
                continue
            if not who:
                fixes.append(cr.MarkFix(r.row, kind.who_col, BOT_NAME, r.course_raw, r.unit,
                                        f"bot's {kind.label} had lost its 'auto'"))
            if it.get("state") == "done" and not status:
                fixes.append(cr.MarkFix(r.row, kind.status_col, STATUS_DONE, r.course_raw, r.unit,
                                        f"bot's finished {kind.label} had lost 'Done'"))
        elif who.lower() == BOT_NAME:
            fixes.append(cr.MarkFix(r.row, kind.who_col, "", r.course_raw, r.unit,
                                    f"'auto' on a {kind.label} the bot never took"))
            if status.lower() == STATUS_DONE.lower():
                fixes.append(cr.MarkFix(r.row, kind.status_col, "", r.course_raw, r.unit,
                                        f"'Done' next to that stray 'auto'"))
    return fixes


# ── names and texts ──────────────────────────────────────────────────────

PODCAST_PROMPT = (
    "This is a guide created as an additional reference material for learners.\n"
    "This is the unit {unit} of the course {course}\n"
    " \n"
    "Do not use irrelevant information. Stick to content. \n"
    "A brief host introduction outlining the topic of this course unit.\n"
    "Clear explanations of the main concepts or key points covered in the source material\n"
    "A short summary or key takeaway message. \n"
    "A brief host sign-off."
)


def two_digits(unit: int) -> str:
    return f"{int(unit):02d}"


def podcast_prompt(unit: int, course_raw: str) -> str:
    return PODCAST_PROMPT.format(unit=two_digits(unit), course=course_title(course_raw))


def podcast_audio_title(unit: int, course_raw: str) -> str:
    """Name of the audio in NotebookLM Studio."""
    return f"Podcast U{unit} - {course_title(course_raw)}"


def podcast_file_name(unit_title: str) -> str:
    """The Podcast Creator's own naming: 'Unit 2 - Health and Safety' ->
    'Unit_2_-_Health_and_Safety.mp4'."""
    value = re.sub(r"[^A-Za-z0-9._ -]+", "-", unit_title).strip()
    value = re.sub(r"\s+", "_", value)
    return (value or "podcast_episode")[:80] + ".mp4"


def studyguide_file_name(unit: int) -> str:
    return f"unit-{two_digits(unit)}.pdf"
