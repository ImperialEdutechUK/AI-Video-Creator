import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rules  # noqa: E402
from rules import SheetRow, decide  # noqa: E402


def row(**kw):
    base = dict(row=10, date="11/9/2026", awarding_body="Focus Awards",
                course_raw="Level 5 Diploma in Leadership", unit_raw=15, chapter_raw=5,
                url="https://notebooklm.link.google/OwYjk2tkmv7Q", done_by="", added="")
    base.update(kw)
    return SheetRow(**base)


class CourseName(unittest.TestCase):
    def test_strips_awarding_body(self):
        self.assertEqual(
            rules.strip_awarding_body("Qualifi", "Qualifi Level 03 Extended Diploma in Health and Social Care"),
            "Level 03 Extended Diploma in Health and Social Care")

    def test_case_and_separators(self):
        self.assertEqual(rules.strip_awarding_body("QUALIFI", "qualifi - Level 3 Diploma"), "Level 3 Diploma")
        self.assertEqual(rules.strip_awarding_body("TQUK", "TQUK: Level 3 Diploma in Healthcare Support Work (RQF)"),
                         "Level 3 Diploma in Healthcare Support Work (RQF)")
        self.assertEqual(rules.strip_awarding_body("Focus Awards", "Focus  Awards Level 5 Diploma"),
                         "Level 5 Diploma")

    def test_only_whole_word_prefix(self):
        self.assertEqual(rules.strip_awarding_body("Qualifi", "Qualifications in Care Level 3"),
                         "Qualifications in Care Level 3")
        self.assertEqual(rules.strip_awarding_body("Qualifi", "Level 3 Qualifi Diploma"), "Level 3 Qualifi Diploma")

    def test_no_prefix_unchanged(self):
        self.assertEqual(rules.strip_awarding_body("Focus Awards", "Level 5 Diploma in Leadership"),
                         "Level 5 Diploma in Leadership")

    def test_aliases_and_first_word(self):
        self.assertEqual(rules.strip_awarding_body("OTHM", "OTHM Qualifications Level 7 Diploma"), "Level 7 Diploma")
        self.assertEqual(rules.strip_awarding_body("Highfield Qualifications", "Highfield Level 2 Award"),
                         "Level 2 Award")

    def test_never_empty(self):
        self.assertEqual(rules.strip_awarding_body("Qualifi", "Qualifi"), "Qualifi")


class Numbers(unittest.TestCase):
    def test_parse(self):
        for v, want in [(14, "14"), (14.0, "14"), ("14", "14"), ("014", "14"), ("Unit 14", "14"), ("U14", "14"),
                        ("Chapter 4", "4"), ("4.0", "4"), ("", ""), (None, ""), ("abc", ""), (0, ""), (4.5, ""),
                        ("1, 2", "")]:
            self.assertEqual(rules.parse_number(v), want, v)

    def test_labels_match_web_app(self):
        self.assertEqual(rules.unit_label("14"), "Unit 14")
        self.assertEqual(rules.chapter_label("4"), "Chapter 4")


class Titles(unittest.TestCase):
    def test_match(self):
        self.assertTrue(rules.title_matches("U22C6", "22", "6"))
        self.assertTrue(rules.title_matches("u22c06", "22", "6"))
        self.assertTrue(rules.title_matches("U14C4- Focus Awards", "14", "4"))
        self.assertTrue(rules.title_matches("U14C4-_Focus", "14", "4"))
        self.assertTrue(rules.title_matches("Unit 14 Chapter 4", "14", "4"))
        self.assertTrue(rules.title_matches("U 14 C 4", "14", "4"))

    def test_no_false_match(self):
        self.assertFalse(rules.title_matches("U22C6", "2", "6"))
        self.assertFalse(rules.title_matches("U22C61", "22", "6"))
        self.assertFalse(rules.title_matches("U2C16", "21", "6"))
        self.assertFalse(rules.title_matches("Intro U22C6", "22", "6"))


class CourseInTitle(unittest.TestCase):
    # Real titles from a shared notebook (row 7 of the TEST log failed on these)
    TITLES = ["U2C4 -Level 5 Certificate in Human Nutrition",
              "U2C4 - Level 3 Diploma in Specialist Support for Teaching and Learning in Schools",
              "U2C4 - Level 3 Certificate in Supporting Teaching & Learning in Schools"]

    def test_picks_the_rows_course(self):
        keep, _ = rules.pick_by_course(
            self.TITLES, "Level 3 Certificate in Supporting Teaching & Learning in Schools (RQF)", "TQUK")
        self.assertEqual(keep, [2])
        keep, _ = rules.pick_by_course(
            self.TITLES, "Level 3 Diploma in Specialist Support for Teaching and Learning in Schools (RQF)", "TQUK")
        self.assertEqual(keep, [1])

    def test_cut_short_titles_still_match(self):
        keep, _ = rules.pick_by_course(
            ["U2C4 - Level 3 Certificate in Supporting Tea", "U2C4 - Level 3 Diploma in Specialist Support"],
            "Level 3 Certificate in Supporting Teaching & Learning in Schools (RQF)")
        self.assertEqual(keep, [0])

    def test_awarding_body_in_title(self):
        keep, _ = rules.pick_by_course(
            ["U14C4 - Focus Awards Level 5 Diploma in Leadership", "U14C4 - Level 3 Award in Something"],
            "Level 5 Diploma in Leadership for Health and Social Care", "Focus Awards")
        self.assertEqual(keep, [0])

    def test_plain_titles_untouched(self):
        self.assertEqual(rules.pick_by_course(["U22C6", "U22C6"], "Any course")[0], [0, 1])

    def test_no_course_match_rejected(self):
        keep, note = rules.pick_by_course(
            ["U2C4 -Level 5 Certificate in Human Nutrition", "U2C4 - Level 3 Diploma in Specialist Support"],
            "Level 2 Award in Food Safety")
        self.assertEqual(keep, [])
        self.assertIn("no video title", note)

    def test_lone_video_for_other_course_rejected(self):
        keep, note = rules.pick_by_course(["U2C5 -Level 5 Certificate in Human Nutrition"],
                                          "Level 3 Certificate in Supporting Teaching & Learning in Schools")
        self.assertEqual(keep, [])
        self.assertIn("another course", note)
        keep, _ = rules.pick_by_course(["U2C5 - Level 3 Certificate in Supporting Tea"],
                                       "Level 3 Certificate in Supporting Teaching & Learning in Schools")
        self.assertEqual(keep, [0])

    def test_equal_matches_stay_ambiguous(self):
        keep, _ = rules.pick_by_course(["U2C4 - Level 3 Certificate in Care", "U2C4 - Level 3 Certificate in Care"],
                                       "Level 3 Certificate in Care")
        self.assertEqual(keep, [0, 1])  # both kept -> the downloader reports "a person must choose"


class Order(unittest.TestCase):
    def test_start_row_first_then_above_nearest_first(self):
        rows = [row(row=n) for n in (6640, 6650, 6646, 10, 6645, 7000)]
        self.assertEqual([r.row for r in rules.order_rows(rows, 6646)], [6646, 6650, 7000, 6645, 6640, 10])

    def test_no_start_row_top_to_bottom(self):
        rows = [row(row=n) for n in (30, 4, 12)]
        self.assertEqual([r.row for r in rules.order_rows(rows, 0)], [4, 12, 30])


class Colours(unittest.TestCase):
    def test_classify(self):
        for hexc, want in [("#FF0000", "red"), ("#F4CCCC", "red"), ("#E06666", "red"), ("#FFFF00", "yellow"),
                           ("#FFF2CC", "yellow"), ("#00FF00", "green"), ("#D9EAD3", "green"),
                           ("#9900FF", "purple"), ("#0563C1", "blue"), ("#FFFFFF", None), ("#000000", None),
                           ("#F3F3F3", None), (None, None), ("", None), ("FFFF0000", "red")]:
            self.assertEqual(rules.classify_color(hexc), want, hexc)

    def test_skip(self):
        self.assertEqual(rules.skip_for_color(["green", "red"], {"red", "yellow"}), "red")
        self.assertIsNone(rules.skip_for_color(["green"], {"red", "yellow"}))


class Decide(unittest.TestCase):
    def test_new_row(self):
        self.assertEqual(decide(row(), None).action, "new")

    def test_person_rows_untouched(self):
        self.assertEqual(decide(row(done_by="nandika"), None).action, "skip")
        self.assertEqual(decide(row(done_by="nandika", added="Yes"), None).action, "skip")
        self.assertEqual(decide(row(added="NO"), None).action, "skip")  # NO written by a person

    def test_done_rows(self):
        self.assertEqual(decide(row(done_by="auto", added="Yes"), {"qstate": "done"}).action, "skip")
        self.assertEqual(decide(row(added="yes"), None).action, "skip")

    def test_bad_rows(self):
        self.assertEqual(decide(row(url=""), None).action, "skip")
        self.assertEqual(decide(row(url="https://example.com/x"), None).action, "skip")
        self.assertEqual(decide(row(url="https://notebook.google.com/notebook/286f6392-efc7"), None).action, "new")
        self.assertEqual(decide(row(url="https://notebooklm.google.com/notebook/abc"), None).action, "new")
        self.assertEqual(decide(row(unit_raw=""), None).action, "skip")
        self.assertEqual(decide(row(course_raw=""), None).action, "skip")

    def test_in_progress(self):
        for s in ("pending", "downloading", "processing", "saving", "verifying", "writing", "failing"):
            self.assertEqual(decide(row(done_by="auto"), {"qstate": s}).action, "skip", s)

    def test_retry_until_limit(self):
        d = decide(row(done_by="auto", added="NO"), {"qstate": "failed", "attempts": 1})
        self.assertEqual(d.action, "retry")
        d = decide(row(done_by="auto", added="NO"), {"qstate": "failed", "attempts": 3})
        self.assertEqual(d.action, "skip")

    def test_cleared_cell_restarts(self):
        d = decide(row(done_by="auto", added=""), {"qstate": "failed", "attempts": 3})
        self.assertEqual((d.action, d.reset_attempts), ("new", True))
        d = decide(row(done_by="", added=""), {"qstate": "done", "attempts": 1})
        self.assertEqual((d.action, d.reset_attempts), ("new", True))

    def test_claimed_but_lost(self):
        # H already "auto" but no record (e.g. the save after writing H failed)
        self.assertEqual(decide(row(done_by="Auto"), None).action, "new")

    def test_keys(self):
        a = rules.row_key("wb1", "https://x", "15", "5")
        self.assertEqual(a, rules.row_key("wb1", " https://x ", "15", "5"))
        self.assertNotEqual(a, rules.row_key("wb2", "https://x", "15", "5"))
        self.assertNotEqual(a, rules.row_key("wb1", "https://x", "15", "4"))
        # one shared notebook link, same unit/chapter, different courses -> different rows
        b = rules.row_key("wb1", "https://x", "2", "4", "Level 3 Certificate in Supporting Teaching")
        self.assertNotEqual(b, rules.row_key("wb1", "https://x", "2", "4", "Level 5 Certificate in Human Nutrition"))
        self.assertEqual(b, rules.row_key("wb1", "https://x", "2", "4", "level 3 certificate in  supporting teaching"))
        self.assertTrue(a.startswith("AUTO#"))

    def test_dates(self):
        import datetime as dt
        self.assertEqual(rules.row_date(row(date_value=46335)), dt.date(2026, 11, 9))
        self.assertEqual(rules.row_date(row(date_value="", date="11/9/2026")), dt.date(2026, 11, 9))
        self.assertIsNone(rules.row_date(row(date_value="", date="")))


if __name__ == "__main__":
    unittest.main()
