"""
Create chapter videos in NotebookLM (the "PCS-Nandika" notebook):

  upload the chapter PDF as a source (named "U1C1 - <course>.pdf")
  -> Studio > Video Overview > customise: Explainer, Classic, only that source,
     the team's focus prompt -> Generate now
  -> when it's ready: rename it "U1C1 - <course>" and copy its share link.

Only touches items the bot made itself (matched by their source name). Never
deletes anything.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

log = logging.getLogger("creator")

VIDEO_PROMPT = (
    "Clean academic slide presentation style suitable for UK vocational and professional online courses that "
    "utilises a modern, vibrant, and visually engaging aesthetic. Use high-quality stock photos, professional "
    "icons, and relevant graphics on every slide to create a polished, colourful appearance that resembles a "
    "high-end digital textbook. Use Montserrat for headings and Open Sans for body text (or Calibri if "
    "unavailable), ensuring high contrast and clear visibility. Focus strictly on the uploaded chapter document "
    "to explain all key concepts, definitions, principles, processes, models, and legislation through a detailed "
    "narration script, while the on-screen text remains strictly minimalist. Use keywords or anchor terms only, "
    "with a maximum of five words per bullet point and no more than five bullet points per slide to ensure the "
    "video remains visually clean. If a concept is complex, split the information across multiple slides rather "
    "than increasing the word count on a single page. The video narration must perform the heavy lifting by "
    "providing full professional explanations, while the slides serve as visual reinforcements that highlight "
    "essential terminology and assessment-relevant content using a subtle accent colour. Ensure the structure "
    "begins with a colourful overview of the chapter purpose, followed by a step-by-step logical progression of "
    "the main points, and concludes with a concise recap. You must use correct and complete British English "
    "spelling throughout, such as ‘organisation’ and ‘behaviour’, and you are strictly "
    "prohibited from using any abbreviated or shortened word forms. Always write words like ‘management’, "
    "‘department’, ‘introduction’, and ‘organisation’ in full, and rephrase any "
    "labels or bullet points that are too long rather than truncating them. Where appropriate, integrate simple "
    "labelled diagrams, flowcharts, arrows, or basic shapes to explain relationships or processes visually "
    "alongside the imagery. Maintain a professional and supportive tone tailored for adult learners, ensuring the "
    "video clarifies the written material without adding external information or creative examples not found in "
    "the source document. Please avoid mentioning the course name in either the video visuals or the generated "
    "audio."
)

FORMAT = "Explainer"
VISUAL_STYLE = "Classic"


class CreatorError(Exception):
    """A problem creating this chapter's video (retried on a later run)."""


class QuotaReached(Exception):
    """NotebookLM says the account can't make more videos right now."""


class NotebookUnavailable(Exception):
    """The PCS notebook can't be opened (moved, deleted or no access)."""


@dataclass
class StudioItem:
    index: int
    title: str
    meta: str          # e.g. "7:54 · Explainer · 1 source · 1d ago" (empty while generating)
    is_video: bool
    generating: bool


_QUOTA_RE = re.compile(r"(?i)(daily limit|limit reached|reached your limit|reached the limit|try again tomorrow|"
                       r"out of .*(credits|usage)|usage limit)")


def safe_name(text: str, limit: int = 150) -> str:
    text = re.sub(r'[\\/:*?"<>|‘’“”]+', " ", text)
    return " ".join(text.split())[:limit].strip(" .")


def video_title(unit: int, chapter: int, course: str) -> str:
    return f"U{unit}C{chapter} - {course.strip()}"


def source_name(unit: int, chapter: int, course: str, pdf_hash: str = "") -> str:
    """Source file name in NotebookLM. The short content hash keeps a corrected
    PDF from ever reusing an old, wrong upload with the same chapter name."""
    tag = f" [{pdf_hash[:8]}]" if pdf_hash else ""
    return safe_name(video_title(unit, chapter, course)) + tag + ".pdf"


def text_source_name(unit: int, chapter: int, course: str, pdf_hash: str = "") -> str:
    """Name of the pasted-text copy of a chapter PDF (used when NotebookLM
    can't make a video from the PDF itself). Ends in .txt so the video's
    'View prompt and sources' list shows which source it was made from."""
    tag = f" [{pdf_hash[:8]}]" if pdf_hash else ""
    return safe_name(video_title(unit, chapter, course)) + tag + ".txt"


def pdf_text(pdf: Path) -> str:
    """The chapter's text, as a person would copy it out of the PDF: page
    footers ('1 | Page ...') and table-of-contents dot leaders left out."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(pdf))
        raw = [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:
        raise CreatorError(f"The chapter PDF could not be read for its text ({e.__class__.__name__}).") from None
    lines = []
    for page_text in raw:
        for line in page_text.splitlines():
            s = " ".join(line.split())
            # page footers ("1 | Page ..." or "... Ltd ... 2 | Page") and contents dot leaders
            if not s or re.search(r"(?i)\b\d+\s*\|\s*page\b", s) or re.search(r"\.{8,}", s):
                continue
            lines.append(s)
    text = "\n".join(lines)
    if len(text.split()) < 100:
        raise CreatorError("The chapter PDF has almost no text to copy (scanned pages?).")
    return text


def pdf_matches(pdf: Path, course: str, unit: int) -> str | None:
    """None if the PDF's first pages name this course and unit, else the problem.
    (Catches a chapter page in WordPress that links another course's PDF.)"""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(pdf))
        text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(3, len(reader.pages))))
    except Exception as e:
        return f"the PDF could not be read ({e.__class__.__name__})"
    flat = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
    if not flat:
        return None  # scanned/image-only PDF: nothing to compare, allow
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", course.lower()).split()
             if w not in {"in", "and", "the", "for", "of", "to", "rqf", "a", "an", "with"}]
    found = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", flat))
    level = re.search(r"(?i)\blevel\s*0*(\d+)", course)
    if level and not re.search(rf"\blevel\s*0*{level.group(1)}\b", flat):
        head = " ".join(text.split())[:160]
        return f"the PDF linked in WordPress is not for a Level {level.group(1)} course (it starts: \"{head}\")"
    if words and found / len(words) < 0.75:
        head = " ".join(text.split())[:160]
        return f"the PDF linked in WordPress seems to be for another course (it starts: \"{head}\")"
    # Many chapter PDFs don't say "Unit N" at all; only reject one that names a
    # different unit.
    units = {int(u) for u in re.findall(r"\bunit\s*0*(\d{1,3})\b", flat)}
    if units and unit not in units:
        head = " ".join(text.split())[:160]
        return (f"the PDF linked in WordPress is for Unit {', '.join(map(str, sorted(units)))}, not Unit {unit} "
                f"(it starts: \"{head}\")")
    return None


# ── notebook & sources ───────────────────────────────────────────────────

def open_notebook(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    if "accounts.google.com" in page.url:
        from notebooklm import SignInRequired
        raise SignInRequired("Google asked to sign in while opening the PCS notebook")
    if "/accessrequest/" in page.url:
        raise NotebookUnavailable("The content team account has no access to the PCS notebook.")
    try:
        page.get_by_role("button", name="Add source").wait_for(timeout=60_000)
    except PWTimeout:
        raise NotebookUnavailable(f"The PCS notebook did not open ({page.url[:100]}).") from None
    page.wait_for_timeout(3_000)
    _dismiss_banners(page)


def _dismiss_banners(page: Page) -> None:
    for b in page.get_by_role("button", name="Dismiss banner").all():
        try:
            if b.is_visible():
                b.click(timeout=2_000)
        except Exception:
            pass


def source_names(page: Page) -> list[str]:
    """Names of the notebook's sources (from their 'Select <name>' checkboxes)."""
    labels = page.evaluate("""() => Array.from(document.querySelectorAll('input[type=checkbox][aria-label^="Select "]'))
        .map(e => e.getAttribute('aria-label').slice(7)).filter(n => n !== 'all sources')""")
    return labels


def upload_source(page: Page, pdf: Path, timeout_s: int = 300) -> None:
    """Upload a PDF as a source and wait until NotebookLM has read it."""
    if pdf.name in source_names(page):
        return
    page.get_by_role("button", name="Add source").click()
    dialog = page.locator("mat-dialog-container").last
    dialog.wait_for(timeout=20_000)
    with page.expect_file_chooser(timeout=20_000) as fc:
        dialog.get_by_role("button", name=re.compile("Upload files")).click()
    fc.value.set_files(str(pdf))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(3_000)
        _check_quota(page)
        box = page.locator(f'input[type=checkbox][aria-label="Select {pdf.name}"]')
        if box.count() and box.first.is_enabled():
            page.wait_for_timeout(2_000)
            return
    raise CreatorError(f"NotebookLM did not finish reading {pdf.name} within {timeout_s // 60} minutes.")


def add_text_source(page: Page, name: str, text: str, timeout_s: int = 180) -> None:
    """Add `text` as a 'Copied text' source and rename it `name` (NotebookLM
    titles pasted text from its first line, so the bot renames it to its own,
    unique name)."""
    if name in source_names(page):
        return
    before = source_names(page)
    page.get_by_role("button", name="Add source").click()
    dialog = page.locator("mat-dialog-container").last
    dialog.wait_for(timeout=20_000)
    dialog.get_by_role("button", name=re.compile(r"Copied text")).click()
    box = page.locator("mat-dialog-container textarea[formcontrolname=copiedText]").last
    try:
        box.wait_for(timeout=15_000)
    except PWTimeout:
        raise CreatorError("The 'Copied text' box did not open.") from None
    box.fill(text)
    page.locator("mat-dialog-container").last.get_by_role("button", name="Insert").click()
    deadline = time.time() + timeout_s
    index = -1
    while time.time() < deadline:
        page.wait_for_timeout(3_000)
        _check_quota(page)
        after = source_names(page)
        if len(after) > len(before):
            # the new source: first place where the list differs from before
            index = next((i for i, (a, b) in enumerate(zip(after, before)) if a != b), len(before))
            break
    if index < 0:
        raise CreatorError(f"NotebookLM did not add the pasted text within {timeout_s // 60} minutes.")
    _wait_source_ready(page, index, deadline)
    _rename_source(page, index, name)
    # Adding pasted text switches the Studio panel to show that text ("Studio >
    # Note"), hiding the Video Overview card: reload for the normal Studio view.
    open_notebook(page, page.url.split("?")[0])


def _source_rows(page: Page):
    return page.locator(".single-source-container")


def _wait_source_ready(page: Page, index: int, deadline: float) -> None:
    box = _source_rows(page).nth(index).locator('input[type=checkbox][aria-label^="Select "]')
    while time.time() < deadline:
        if box.count() and box.is_enabled():
            page.wait_for_timeout(2_000)
            return
        page.wait_for_timeout(3_000)
    raise CreatorError("NotebookLM did not finish reading the pasted text.")


def _rename_source(page: Page, index: int, name: str) -> None:
    btn = _source_rows(page).nth(index).locator("button.source-item-more-button")
    old = (btn.get_attribute("aria-description", timeout=10_000) or "") if btn.count() == 1 else ""
    names = source_names(page)
    if not old or index >= len(names) or names[index] != old:
        raise CreatorError("Could not find the pasted-text source to rename it.")
    btn.scroll_into_view_if_needed(timeout=10_000)
    btn.click()
    page.get_by_role("menuitem", name=re.compile(r"Rename", re.I)).click(timeout=10_000)
    field = page.locator("mat-dialog-container input[formcontrolname=title]").last
    try:
        field.wait_for(timeout=10_000)
    except PWTimeout:
        raise CreatorError("The source Rename box did not appear.") from None
    field.fill(name)
    page.locator("mat-dialog-container").last.get_by_role("button", name="Save").click()
    page.wait_for_timeout(2_500)
    if name not in source_names(page):
        raise CreatorError(f"Renaming the pasted text source to {name!r} did not stick.")


# ── video overview ───────────────────────────────────────────────────────

def _check_quota(page: Page) -> None:
    try:
        texts = page.evaluate("""() => Array.from(document.querySelectorAll(
            'simple-snack-bar, .mat-mdc-snack-bar-label, [role=alert], [role=status], mat-dialog-container'))
            .map(e => e.innerText).join(' | ')""")
    except Exception:
        return
    if texts and _QUOTA_RE.search(texts):
        raise QuotaReached(" ".join(texts.split())[:300])


def start_video(page: Page, source: str, prompt: str = VIDEO_PROMPT, dry_run: bool = False,
                screenshot: str = "") -> None:
    """Open Video Overview > customise, set Explainer / Classic / only `source` /
    the focus prompt, and press Generate now (dry_run: close instead)."""
    card = page.locator("[role=button][aria-label='Video Overview']")
    card.hover()
    card.locator(".option-icon").evaluate("e => e.click()")  # the ">" on the card opens the settings
    page.locator("mat-dialog-container").filter(has_text="Customise Video Overview").wait_for(timeout=20_000)
    # (not filtered by its title: the Sources picker replaces the title while open)
    dialog = page.locator("mat-dialog-container").last
    dialog.get_by_text(FORMAT, exact=True).click()
    page.wait_for_timeout(1_000)
    dialog.get_by_text(VISUAL_STYLE, exact=True).click()
    page.wait_for_timeout(500)
    _select_only_source(page, dialog, source)
    dialog.get_by_role("button", name="Customised topic").click()
    box = dialog.get_by_role("textbox", name="What should the video focus on?")
    box.fill(prompt)
    _verify_settings(dialog, source)
    if screenshot:
        page.screenshot(path=screenshot)
    if dry_run:
        dialog.get_by_role("button", name="Close dialogue").click()
        return
    dialog.get_by_role("button", name="Generate now").click()
    try:
        dialog.wait_for(state="detached", timeout=30_000)
    except PWTimeout:
        _check_quota(page)
        raise CreatorError("The Video Overview settings window did not close after 'Generate now'.") from None
    page.wait_for_timeout(3_000)
    _check_quota(page)


def _select_only_source(page: Page, dialog, source: str) -> None:
    _select_sources(page, dialog, [source])


def _select_sources(page: Page, dialog, sources: list[str]) -> None:
    """The settings window's Sources picker: a list of options (one per source,
    aria-selected) with a "Select all" box. Leave exactly `sources` selected."""
    wanted = set(sources)
    source = ", ".join(sources)
    dialog.get_by_role("button", name=re.compile(r"\d+\s+sources?")).first.click()
    options = dialog.locator("mat-list-option[role=option]")
    try:
        options.first.wait_for(timeout=15_000)
    except PWTimeout:
        raise CreatorError("The settings' source list did not open.") from None

    def state() -> list[tuple[str, bool]]:
        return options.evaluate_all("""els => els.map(e => [
            ((e.querySelector('.item-name') || e).innerText || '').trim(),
            e.getAttribute('aria-selected') === 'true'])""")

    current = state()
    names = [n for n, _ in current]
    for one in sources:
        if one not in names:
            raise CreatorError(f"The source {one} is not in the settings' source list.")
        if names.count(one) > 1:
            raise CreatorError(f"The notebook has {names.count(one)} sources named {one}.")
    # Untick "Select all" first when everything is ticked (one click instead of hundreds).
    if all(sel for _, sel in current) and len(current) > 1:
        dialog.locator("mat-checkbox.select-all-checkbox").click()
        page.wait_for_timeout(800)
        current = state()
    for i, (name, selected) in enumerate(current):
        if selected != (name in wanted):
            options.nth(i).click()
            page.wait_for_timeout(150)
    final = state()
    picked = [n for n, sel in final if sel]
    if sorted(picked) != sorted(sources):
        raise CreatorError(f"Could not select only {source} (selected: {picked[:5]}).")
    dialog.get_by_role("button", name="Confirm").click()
    page.wait_for_timeout(1_500)


def _verify_settings(dialog, source: str) -> None:
    text = dialog.inner_text()
    if not re.search(r"\b1 source\b", text):
        raise CreatorError(f"The Video Overview is not set to exactly one source ({source}).")
    checked = dialog.evaluate("""d => Array.from(d.querySelectorAll('input[type=radio]:checked'))
        .map(r => (r.closest('label, mat-radio-button, [role=radio], div') || r).innerText.trim().split('\\n')[0])""")
    log.debug("checked radios: %s", checked)


# ── studio list ──────────────────────────────────────────────────────────

_STUDIO_JS = r"""() => Array.from(document.querySelectorAll('.artifact-button-content')).map(n => {
  const labels = n.querySelector('.artifact-labels');
  const icon = n.querySelector('mat-icon.artifact-icon');
  const t = n.querySelector('.artifact-title');
  const d = n.querySelector('.artifact-details');
  return {
    id: labels ? (labels.id || '').replace('artifact-labels-', '') : '',
    title: t ? t.innerText.trim() : '',
    details: d ? d.innerText.replace(/\s+/g, ' ').trim() : '',
    icon: icon ? icon.innerText.trim() : '',
    kind: (n.querySelector('button.artifact-stretched-button') || {getAttribute: () => ''}).getAttribute('aria-description') || '',
  };
})"""


@dataclass
class Artifact:
    id: str
    title: str
    details: str
    icon: str
    kind: str

    @property
    def generating(self) -> bool:
        return self.icon == "progress_activity" or self.title.startswith("Generating")

    @property
    def failed(self) -> bool:
        return bool(re.search(r"(?i)\b(failed|couldn.t|could not|error)\b", self.title + " " + self.details))

    @property
    def ready_audio(self) -> bool:
        return (self.kind == "Audio Overview" and not self.generating and not self.failed
                and bool(re.search(r"\b\d{1,2}:\d{2}\b", self.details)))

    @property
    def ready_video(self) -> bool:
        return (self.kind == "Video Overview" and not self.generating and not self.failed
                and bool(re.search(r"\b\d{1,2}:\d{2}\b", self.details)))


def studio_items(page: Page) -> list[Artifact]:
    return [Artifact(**x) for x in page.evaluate(_STUDIO_JS) if x.get("id")]


def _item(page: Page, artifact_id: str):
    row = page.locator(f".artifact-button-content:has(#artifact-labels-{artifact_id})")
    if not row.count():
        raise CreatorError(f"Studio item {artifact_id} is no longer in the notebook.")
    return row.first


def _menu(page: Page, artifact_id: str, item: str) -> None:
    row = _item(page, artifact_id)
    row.scroll_into_view_if_needed(timeout=10_000)
    row.hover()
    row.get_by_role("button", name="More").click()
    page.get_by_role("menuitem", name=re.compile(item, re.I)).click(timeout=10_000)


def item_sources(page: Page, artifact_id: str) -> list[str]:
    """Source names the item was made from ("View prompt and sources")."""
    _menu(page, artifact_id, "View prompt and sources")
    dialog = page.locator("mat-dialog-container").filter(has_text="Prompt and sources").last
    dialog.wait_for(timeout=20_000)
    page.wait_for_timeout(1_000)
    names = dialog.evaluate("""d => Array.from(d.querySelectorAll('*')).filter(e => e.children.length === 0)
        .map(e => (e.innerText || '').trim()).filter(t => /\\.(pdf|docx?|txt|pptx?)$/i.test(t))""")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return names


def rename_item(page: Page, artifact_id: str, title: str, tries: int = 3) -> None:
    for attempt in range(tries):
        try:
            return _rename_item_once(page, artifact_id, title)
        except (CreatorError, PWTimeout):
            if attempt == tries - 1:
                raise
            page.keyboard.press("Escape")
            page.wait_for_timeout(3_000)


def _rename_item_once(page: Page, artifact_id: str, title: str) -> None:
    _menu(page, artifact_id, "Rename")
    # The title turns into an input in place (no dialog).
    box = _item(page, artifact_id).locator("input.artifact-title-input")
    try:
        box.wait_for(timeout=10_000)
    except PWTimeout:
        raise CreatorError("The Rename box did not appear.") from None
    box.fill(title)
    box.press("Enter")
    page.wait_for_timeout(2_500)
    now = next((a for a in studio_items(page) if a.id == artifact_id), None)
    if not now or now.title != title:
        raise CreatorError(f"Renaming the video to {title!r} did not stick (shows {now.title if now else '?'!r}).")


def share_link(page: Page, artifact_id: str) -> str:
    """Direct link to the video (the item's 'Open in new tab' link):
    https://notebook.google.com/notebook/<notebook>/artifact/<video>. It opens the
    same place as the short 'Copy link' links (whose button stays disabled in an
    automated browser), for anyone with access to the notebook."""
    row = _item(page, artifact_id)
    row.scroll_into_view_if_needed(timeout=10_000)
    row.hover()
    row.get_by_role("button", name="More").click()
    item = page.get_by_role("menuitem", name=re.compile("Open in new tab"))
    href = (item.get_attribute("href", timeout=10_000) or "").strip()
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if href.startswith("/"):
        href = "https://" + (re.match(r"https://([^/]+)", page.url).group(1)) + href
    if not re.match(rf"^https://notebook(lm)?\.google\.com/notebook/[\w-]+/artifact/{re.escape(artifact_id)}$", href):
        raise CreatorError(f"Could not read the video's link ({href[:100]!r}).")
    return href


def generate(page: Page, source: str, prompt: str = VIDEO_PROMPT) -> str:
    """Start a video for `source`; returns the new Studio item's id."""
    before = {a.id for a in studio_items(page)}
    start_video(page, source, prompt)
    for _ in range(30):
        new = [a for a in studio_items(page) if a.id not in before]
        if new:
            if len(new) > 1:
                log.warning("Several new Studio items appeared at once: %s", [a.id for a in new])
            return new[0].id
        page.wait_for_timeout(2_000)
    _check_quota(page)
    raise CreatorError("No new Video Overview appeared in Studio after 'Generate now'.")


# ── Audio Overview (unit podcasts) ───────────────────────────────────────

AUDIO_FORMAT = "Deep dive"
AUDIO_LENGTH = "Default"
AUDIO_FOCUS_BOX = "What should the AI hosts focus on in this episode?"
AUDIO_FORMATS = ("Deep dive", "Brief", "Critique", "Debate")


def audio_format(a: "Artifact") -> str | None:
    """The format a Studio audio item shows in its details ("12:40 · Deep dive · 2 sources")."""
    for f in AUDIO_FORMATS:
        if re.search(rf"(?:^|·)\s*{re.escape(f)}\s*(?:·|$)", a.details or "", re.I):
            return f
    return None


def _set_audio_format(page: Page, dialog) -> None:
    """Format AUDIO_FORMAT and Length AUDIO_LENGTH, then check both stuck."""
    radio = dialog.locator("mat-radio-button").filter(has_text=AUDIO_FORMAT)
    radio.get_by_text(AUDIO_FORMAT, exact=True).click()
    page.wait_for_timeout(800)
    if "mat-mdc-radio-checked" not in (radio.first.get_attribute("class") or ""):
        raise CreatorError(f"The Audio Overview format did not switch to '{AUDIO_FORMAT}'.")
    toggle = dialog.locator("mat-button-toggle").filter(
        has=page.get_by_text(AUDIO_LENGTH, exact=True))
    if toggle.count() != 1:
        raise CreatorError(f"The Audio Overview has no '{AUDIO_LENGTH}' length option.")
    chosen = lambda: "mat-button-toggle-checked" in (toggle.get_attribute("class") or "")
    if not chosen():
        toggle.locator("button").click()
        page.wait_for_timeout(500)
    if not chosen():
        raise CreatorError(f"The Audio Overview length did not switch to '{AUDIO_LENGTH}'.")


def start_audio(page: Page, sources: list[str], prompt: str, dry_run: bool = False, screenshot: str = "") -> None:
    """Studio > Audio Overview > customise: format Deep dive, length Default,
    exactly `sources` (all the unit's chapter PDFs), the team's prompt, Generate now."""
    card = page.locator("[role=button][aria-label='Audio Overview']")
    card.hover()
    card.locator(".option-icon").evaluate("e => e.click()")
    page.locator("mat-dialog-container").filter(has_text="Customise Audio Overview").wait_for(timeout=20_000)
    dialog = page.locator("mat-dialog-container").last
    _set_audio_format(page, dialog)
    _select_sources(page, dialog, sources)
    box = dialog.get_by_role("textbox", name=AUDIO_FOCUS_BOX)
    box.fill(prompt)
    text = dialog.inner_text()
    n = len(sources)
    if not re.search(rf"\b{n} sources?\b", text):
        raise CreatorError(f"The Audio Overview is not set to the unit's {n} source(s).")
    if box.input_value().strip() != prompt.strip():
        raise CreatorError("The podcast prompt did not go into the focus box.")
    if screenshot:
        page.screenshot(path=screenshot)
    if dry_run:
        dialog.get_by_role("button", name=re.compile("close", re.I)).first.click()
        return
    dialog.get_by_role("button", name="Generate now").click()
    try:
        dialog.wait_for(state="detached", timeout=30_000)
    except PWTimeout:
        _check_quota(page)
        raise CreatorError("The Audio Overview settings window did not close after 'Generate now'.") from None
    page.wait_for_timeout(3_000)
    _check_quota(page)


def generate_audio(page: Page, sources: list[str], prompt: str) -> str:
    """Start a Deep dive audio overview of `sources`; returns the new Studio item's id."""
    before = {a.id for a in studio_items(page)}
    start_audio(page, sources, prompt)
    for _ in range(30):
        new = [a for a in studio_items(page) if a.id not in before]
        if new:
            return new[0].id
        page.wait_for_timeout(2_000)
    _check_quota(page)
    raise CreatorError("No new Audio Overview appeared in Studio after 'Generate now'.")


def download_item(page: Page, artifact_id: str, dest_dir: Path, timeout_s: int = 300) -> Path:
    """The item's ⋮ > Download, saved in dest_dir."""
    with page.expect_download(timeout=timeout_s * 1000) as d:
        _menu(page, artifact_id, "Download")
    name = d.value.suggested_filename or f"{artifact_id}.m4a"
    out = Path(dest_dir) / safe_name(name, 120)
    d.value.save_as(str(out))
    if out.stat().st_size < 50_000:
        raise CreatorError(f"The downloaded audio is too small ({out.stat().st_size} bytes).")
    return out
