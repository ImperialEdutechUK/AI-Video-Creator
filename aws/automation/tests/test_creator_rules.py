import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import creator_rules as cr  # noqa: E402
from rules import SheetRow  # noqa: E402


def doc(row, course, link="https://southlondoncollege.org/course/x/", unit="3", videos_by=""):
    return cr.DocRow(row, course, link, unit, "Anjani", videos_by)


def logrow(course, unit, chapter="1"):
    return SheetRow(row=9, date="", awarding_body="", course_raw=course, unit_raw=unit, chapter_raw=chapter,
                    url="https://notebooklm.link.google/x", done_by="", added="")


class Split(unittest.TestCase):
    def test_awarding_bodies(self):
        cases = {
            "NCFE CACHE Level 2 Certificate In Understanding Autism": ("NCFE CACHE", "Level 2 Certificate In Understanding Autism"),
            "QUALIFI Level 4 Diploma in Early Learning and Childcare": ("Qualifi", "Level 4 Diploma in Early Learning and Childcare"),
            "Qualify Level 3 Diploma": ("Qualifi", "Level 3 Diploma"),
            "Focus Award Level 3 Certificate in Leisure": ("Focus Awards", "Level 3 Certificate in Leisure"),
            "ATHE - Level 3 Diploma in Accounting (RQF)": ("ATHE", "Level 3 Diploma in Accounting (RQF)"),
            "NCFE Level 3 Certificate in Travel and Tourism: Aviation (RQF)": ("NCFE", "Level 3 Certificate in Travel and Tourism: Aviation (RQF)"),
            "TQUK Level 3 Diploma for Residential Childcare": ("TQUK", "Level 3 Diploma for Residential Childcare"),
            "Level 5 Extended Diploma in IT-Networking (RQF)": ("", "Level 5 Extended Diploma in IT-Networking (RQF)"),
            "NCFE CACHE Level 2 Certificate in  Understanding Working": ("NCFE CACHE", "Level 2 Certificate in Understanding Working"),
        }
        for raw, want in cases.items():
            self.assertEqual(cr.split_course_name(raw), want, raw)

    def test_word_boundary(self):
        # "Athena" is not "ATHE"
        self.assertEqual(cr.split_course_name("Athena Level 3 X"), ("", "Athena Level 3 X"))

    def test_course_key_ignores_awarding_body_and_punctuation(self):
        self.assertEqual(cr.course_key("QUALIFI Level 4 Diploma in Cyber Security (RQF)"),
                         cr.course_key("Level 4 Diploma in Cyber Security (RQF)"))


class Decide(unittest.TestCase):
    def setUp(self):
        self.logged = cr.logged_units([logrow("Level 2 Certificate In Understanding Autism", "3")])

    def test_claims_a_new_unit(self):
        self.assertEqual(cr.decide_unit(doc(10, "NCFE CACHE Level 2 Award in X", unit="1"), self.logged, None, 0).action, "claim")

    def test_skips_person_and_logged_and_missing(self):
        self.assertEqual(cr.decide_unit(doc(1, "NCFE Level 2 X", videos_by="piyumi"), set(), None, 0).reason,
                         "chapter videos done by a person")
        self.assertEqual(cr.decide_unit(doc(2, "NCFE CACHE Level 2 Certificate In Understanding Autism"), self.logged, None, 0).reason,
                         "already in the video log")
        self.assertEqual(cr.decide_unit(doc(3, "NCFE Level 2 X", link=""), set(), None, 0).reason, "no course link")
        self.assertEqual(cr.decide_unit(doc(4, "NCFE Level 2 X", unit=""), set(), None, 0).reason, "no unit number")
        self.assertEqual(cr.decide_unit(doc(5, "test"), set(), None, 0).reason, "no course name")

    def test_in_progress_and_skipped_recheck(self):
        self.assertEqual(cr.decide_unit(doc(6, "NCFE Level 2 X", videos_by="auto"), set(), {"cstate": "chapters"}, 0).action, "skip")
        skipped = {"cstate": "skipped", "retry_at": 100}
        self.assertEqual(cr.decide_unit(doc(7, "NCFE Level 2 X"), set(), skipped, 50).action, "skip")
        self.assertEqual(cr.decide_unit(doc(7, "NCFE Level 2 X"), set(), skipped, 200).action, "claim")
        # "auto" in the sheet but the bot has no record: leave it for a person
        self.assertEqual(cr.decide_unit(doc(8, "NCFE Level 2 X", videos_by="auto"), set(), None, 0).action, "skip")

    def test_unit_numbers(self):
        self.assertEqual(doc(1, "x", unit="Unit 18").unit, 18)
        self.assertEqual(doc(1, "x", unit="0").unit, None)

    def test_order_newest_first(self):
        rows = [doc(5, "a"), doc(534, "b"), doc(100, "c")]
        self.assertEqual([r.row for r in cr.order_units(rows)], [534, 100, 5])


class MarkRepair(unittest.TestCase):
    L = "https://southlondoncollege.org/course/a/"

    def grid(self, rows):
        head = ["Course Name", "Course Link", "Unit Received", "Done by", "", "", "", "", "Chapter-wise Videos", "Status"]
        return [head] + [list(r) + [""] * (12 - len(r)) for r in rows]

    def test_marks_follow_the_unit_not_the_row(self):
        unit = {"cstate": "done", "course_raw": "NCFE Level 2 Award X", "course_link": self.L, "unit": 1,
                "chapters": [{"state": "ready"}, {"state": "ready"}]}
        g = self.grid([
            ["Other course", self.L + "o", "4", "A", "", "", "", "", "auto", "Done"],   # stray marks left behind
            ["NCFE Level 2 Award X", self.L, "1", "A", "", "", "", "", "", ""],         # the bot's unit, marks lost
            ["Third", self.L + "t", "2", "A", "", "", "", "", "Rukaiya", "Done"],      # a person's row
        ])
        fixes = cr.plan_mark_fixes(cr.doc_rows(1, g)[1:], g, [unit])
        got = sorted((f.row, f.col, f.value) for f in fixes)
        self.assertEqual(got, [(2, "I", ""), (2, "J", ""), (3, "I", "auto"), (3, "J", "Done")])

    def test_person_name_and_unfinished_units(self):
        units = [{"cstate": "done", "course_raw": "A", "course_link": self.L, "unit": 1, "chapters": [{"state": "ready"}]},
                 {"cstate": "done", "course_raw": "B", "course_link": self.L, "unit": 2,
                  "chapters": [{"state": "ready"}, {"state": "failed"}]},
                 {"cstate": "chapters", "course_raw": "C", "course_link": self.L, "unit": 3, "chapters": [{"state": "queued"}]},
                 {"cstate": "skipped", "course_raw": "D", "course_link": self.L, "unit": 4}]
        g = self.grid([["A", self.L, "1", "x", "", "", "", "", "Piyumi", ""],   # person took it: untouched
                       ["B", self.L, "2", "x", "", "", "", "", "auto", ""],     # a chapter failed: no Done
                       ["C", self.L, "3", "x", "", "", "", "", "", ""],         # in progress: auto only
                       ["D", self.L, "4", "x", "", "", "", "", "", ""]])        # skipped by the bot: nothing
        fixes = cr.plan_mark_fixes(cr.doc_rows(1, g)[1:], g, units)
        self.assertEqual([(f.row, f.col, f.value) for f in fixes], [(4, "I", "auto")])

    def test_already_right_means_no_writes(self):
        unit = {"cstate": "done", "course_raw": "A", "course_link": self.L, "unit": 1, "chapters": [{"state": "ready"}]}
        g = self.grid([["A", self.L, "1", "x", "", "", "", "", "auto", "Done"]])
        self.assertEqual(cr.plan_mark_fixes(cr.doc_rows(1, g)[1:], g, [unit]), [])


class LogRow(unittest.TestCase):
    def test_layout(self):
        self.assertEqual(cr.log_row(dt.date(2026, 11, 12), "NCFE CACHE", "Level 2 Award in X", 1, 3, "https://l"),
                         ["11/12/2026", "NCFE CACHE", "Level 2 Award in X", "1", "3", "auto", "https://l"])

    def test_header_check(self):
        good = ["Course Name ", "Course Link", "Unit Received", "Done by", "Podcasts", "Status", "Study Guides",
                "Status", "Chapter-wise Videos ", "Status", "VImeo Links", "Status"]
        self.assertIsNone(cr.check_doc_header(good))
        self.assertIn("column I", cr.check_doc_header(good[:8] + ["Something else"]))


if __name__ == "__main__":
    unittest.main()
