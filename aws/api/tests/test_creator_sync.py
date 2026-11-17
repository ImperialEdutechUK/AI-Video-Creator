"""creator_sync: claiming units from the Doc Formatting tab and writing finished
units into the video log (moto DynamoDB, fake Graph and WordPress)."""
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "automation"))
for k, v in {"JOBS_TABLE": "creator-test-jobs", "MEDIA_BUCKET": "m", "WORKER_CLUSTER": "c", "WORKER_TASK_FAMILY": "f",
             "AWS_REGION": "eu-west-2", "AWS_DEFAULT_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "testing",
             "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing", "CREATOR_ENABLED": "true"}.items():
    os.environ[k] = v
os.environ.pop("AWS_PROFILE", None)

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

from rules import SheetRow  # noqa: E402

HEADER = ["Course Name ", "Course Link", "Unit Received", "Done by", "Podcasts", "Status", "Study Guides", "Status",
          "Chapter-wise Videos ", "Status", "VImeo Links", "Status"]
LINK = "https://southlondoncollege.org/course/support-work/"


class FakeGraph:
    def __init__(self, doc_rows, log_rows):
        self.doc = [HEADER] + doc_rows
        self.log = log_rows          # list of 9-col lists, row 1 = header
        self.writes = []

    def open_tab(self, wb, name):
        from excel import Workbook
        return Workbook("d", "i", "wb", "doc-ws", name)

    def read_grid(self, wb):
        return 1, [list(r) for r in self.doc]

    def _grid(self, wb):
        return self.doc if wb.worksheet_id == "doc-ws" else self.log

    def read_range(self, wb, address):
        import re
        a, b = (address.split(":") + [address])[:2]
        c1, r1 = re.match(r"([A-Z]+)(\d+)", a).groups()
        c2, r2 = re.match(r"([A-Z]+)(\d+)", b).groups()
        grid = self._grid(wb)
        out = []
        for r in range(int(r1), int(r2) + 1):
            row = grid[r - 1] if r - 1 < len(grid) else []
            row = list(row) + [""] * 12
            out.append(row[ord(c1) - 65: ord(c2) - 64])
        return out

    def write_cell(self, wb, col, row, value):
        self.writes.append((wb.worksheet_id, f"{col}{row}", value))
        grid = self._grid(wb)
        while len(grid) < row:
            grid.append([""] * 12)
        r = list(grid[row - 1]) + [""] * 12
        r[ord(col) - 65] = value
        grid[row - 1] = r

    def write_range(self, wb, address, values):
        import re
        start = int(re.match(r"[A-Z]+(\d+)", address).group(1))
        for i, vals in enumerate(values):
            for j, v in enumerate(vals):
                self.write_cell(wb, chr(65 + j), start + i, v)

    def last_used_row(self, wb):
        grid = self._grid(wb)
        return max((i + 1 for i, r in enumerate(grid) if any(str(x).strip() for x in r)), default=1)

    def read_rows(self, wb):
        return [SheetRow(row=i + 1, date=r[0], awarding_body=r[1], course_raw=r[2], unit_raw=r[3], chapter_raw=r[4],
                         url=r[6], done_by=r[7], added=r[8]) for i, r in enumerate(self.log) if i > 0]


class FakeWP:
    def __init__(self, chapters=3, error=None):
        self.chapters, self.error = chapters, error

    def unit(self, link, unit):
        from wordpress import Chapter, CourseUnit, WordPressError
        if self.error:
            raise WordPressError(self.error)
        return CourseUnit("181076", "Level 2 Award", "NCFE CACHE", unit, f"Unit {unit} - X",
                          [Chapter(n, f"Chapter {n} - t", str(n), [f"https://x/{n}.pdf"]) for n in range(1, self.chapters + 1)])


@unittest.skipIf(mock_aws is None, "moto not installed")
class CreatorSync(unittest.TestCase):
    def setUp(self):
        os.environ["JOBS_TABLE"] = "creator-test-jobs"
        self.m = mock_aws()
        self.m.start()
        self.addCleanup(self.m.stop)
        import boto3
        boto3.resource("dynamodb", region_name="eu-west-2").create_table(
            TableName="creator-test-jobs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "active", "AttributeType": "S"},
                                  {"AttributeName": "created_at", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "active", "KeySchema": [
                {"AttributeName": "active", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["qstate", "sid"]}}])
        for mod in ("automation", "creator_sync"):
            sys.modules.pop(mod, None)
        import automation
        import creator_sync
        from excel import Workbook
        self.A, self.C = automation, creator_sync
        self.C.ENABLED = True
        self.C.notebook_url = lambda: "https://notebook.google.com/notebook/nb"
        self.alerts = []
        self.A.alert = lambda kind, *a, **k: self.alerts.append(kind) or True
        self.wb = Workbook("d", "i", "wb", "log-ws", "Chapter-wise AI video log")

    def graph(self, doc_rows, log_rows=None):
        g = FakeGraph(doc_rows, log_rows or [["Date", "AB", "Course", "Unit", "Chapter", "By", "Link", "Done by", "Added"]])
        self.A.open_sheet = lambda: (g, self.wb)
        return g

    def scan(self, g, wp=None):
        self.C.wordpress = lambda: wp or FakeWP()
        return self.C.scan(g, self.wb, g.read_rows(self.wb), dry_run=False)

    def test_claims_newest_unit_and_marks_auto(self):
        g = self.graph([
            ["NCFE CACHE Level 2 Award in Support Work in Schools and Colleges", LINK, "1", "Anjani", "", "", "", "", "piyumi"],
            ["NCFE CACHE Level 2 Award in Support Work in Schools and Colleges", LINK, "2", "Anjani", "", "", "", "", ""],
            ["NCFE CACHE Level 2 Award in Support Work in Schools and Colleges", LINK, "3", "Anjani", "", "", "", "", ""],
        ])
        rep = self.scan(g)
        self.assertEqual([c["row"] for c in rep["claimed"]], [4])          # newest first, one per run
        self.assertEqual(rep["claimed"][0]["awarding_body"], "NCFE CACHE")
        self.assertIn(("doc-ws", "I4", "auto"), g.writes)
        self.assertEqual(len(g.writes), 1)                                  # nothing else touched
        units = self.C.active_units()
        self.assertEqual(units[0]["course_name"], "Level 2 Award in Support Work in Schools and Colleges")
        self.assertEqual([c["state"] for c in units[0]["chapters"]], ["queued"] * 3)
        self.assertTrue(self.C.needs_task(units))

    def test_skips_units_already_in_the_log(self):
        g = self.graph([["QUALIFI Level 4 Diploma in Cyber Security (RQF)", LINK, "2", "A", "", "", "", "", ""]],
                       [["h"] * 9, ["11/12/2026", "Qualifi", "Level 4 Diploma in Cyber Security (RQF)", "2", "1",
                                    "Piyumi", "", "", ""]])
        rep = self.scan(g)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(rep["skipped"].get("already in the video log"), 1)
        self.assertEqual(g.writes, [])

    def test_wordpress_problem_skips_to_next_unit_without_touching_sheet(self):
        g = self.graph([["NCC Level 4 Diploma in Computing", LINK, "3", "y", "", "", "", "", ""],
                        ["NCC Level 4 Diploma in Computing", LINK + "x", "2", "y", "", "", "", "", ""]])
        rep = self.scan(g, FakeWP(error="Unit 3 has no 'Chapter ...' items"))
        self.assertEqual(len(rep["problems"]), 2)
        self.assertEqual(g.writes, [])
        # recorded so it isn't looked up again on every run
        rep2 = self.scan(g, FakeWP())
        self.assertEqual(rep2["claimed"], [])

    def test_person_takes_the_row_just_before_claim(self):
        g = self.graph([["NCFE Level 2 X", LINK, "1", "A", "", "", "", "", ""]])
        orig = g.read_range

        def racing(wb, address):
            if address.startswith("A2:I2"):
                g.doc[1][8] = "Chathurika"
            return orig(wb, address)
        g.read_range = racing
        rep = self.scan(g)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(g.writes, [])

    def test_logging_finished_unit(self):
        g = self.graph([["NCFE CACHE Level 2 Award in Support Work in Schools and Colleges", LINK, "1", "A", "", "", "", "", ""]],
                       [["h"] * 9, ["11/11/2026", "ATHE", "Other course", "1", "1", "Amana", "https://old", "Amana", "Yes"]])
        self.scan(g)
        u = self.C.active_units()[0]
        chapters = [{**c, "state": "ready", "link": f"https://notebook.google.com/notebook/nb/artifact/a{c['n']}"}
                    for c in u["chapters"]]
        chapters[1] = {**chapters[1], "state": "failed", "error": "Not made: wrong PDF"}
        self.A.table.update_item(Key={"pk": u["pk"]}, UpdateExpression="SET chapters = :c, cstate = :l",
                                 ExpressionAttributeValues={":c": chapters, ":l": "logging"})
        self.C.follow_up()
        self.assertEqual(g.log[2][:9], [g.log[2][0], "NCFE CACHE", "Level 2 Award in Support Work in Schools and Colleges",
                                        "1", "1", "auto", "https://notebook.google.com/notebook/nb/artifact/a1", "", ""])
        self.assertEqual(g.log[3][4], "3")                                  # chapter 2 failed: not logged
        self.assertEqual(len(g.log), 4)
        done = self.A.table.get_item(Key={"pk": u["pk"]})["Item"]
        self.assertEqual(done["cstate"], "done")
        self.assertNotIn("active", done)
        self.assertEqual(len(self.alerts), 1)                               # the failed chapter is reported
        self.C.follow_up()                                                  # nothing written twice
        self.assertEqual(len(g.log), 4)
        self.assertEqual(g.doc[1][9], "")                                   # a chapter failed: Status not Done

    def test_old_unit_is_closed_not_left_hanging(self):
        import time
        from decimal import Decimal
        g = self.graph([["NCFE CACHE Level 2 Award in X", LINK, "1", "A", "", "", "", "", ""]])
        self.scan(g)
        u = self.C.active_units()[0]
        chapters = [dict(c) for c in u["chapters"]]
        chapters[0].update(state="ready", link="https://notebook.google.com/notebook/nb/artifact/a1")
        chapters[1].update(state="generating")
        old = Decimal(str(time.time() - 25 * 3600))
        self.A.table.update_item(Key={"pk": u["pk"]}, UpdateExpression="SET chapters = :c, created_at = :o, updated_at = :o",
                                 ExpressionAttributeValues={":c": chapters, ":o": old})
        # while waiting for a Google sign-in it is NOT closed (only an email)
        self.A.table.put_item(Item={"pk": "AUTOCFG#google", "signin_required": True})
        self.C.follow_up()
        self.assertEqual(self.A.table.get_item(Key={"pk": u["pk"]})["Item"]["cstate"], "chapters")
        self.assertTrue(any(k.startswith("creator_stuck") for k in self.alerts))
        self.A.table.delete_item(Key={"pk": "AUTOCFG#google"})
        self.C.follow_up()                                                  # closes it ...
        self.C.follow_up()                                                  # ... and logs what's ready
        done = self.A.table.get_item(Key={"pk": u["pk"]})["Item"]
        self.assertEqual(done["cstate"], "done")
        self.assertEqual(len(g.log), 2)                                     # chapter 1 only
        self.assertIn("Not finished within 24 hours", done["chapters"][1]["error"])

    def test_all_chapters_logged_sets_status_done(self):
        g = self.graph([["NCFE CACHE Level 2 Award in X", LINK, "1", "A", "", "", "", "", ""]])
        self.scan(g)
        u = self.C.active_units()[0]
        chapters = [{**c, "state": "ready", "link": f"https://notebook.google.com/notebook/nb/artifact/a{c['n']}"}
                    for c in u["chapters"]]
        g.doc.insert(1, ["Someone else's row", LINK, "1", "A", "", "", "", "", ""])   # rows moved meanwhile
        self.A.table.update_item(Key={"pk": u["pk"]}, UpdateExpression="SET chapters = :c, cstate = :l",
                                 ExpressionAttributeValues={":c": chapters, ":l": "logging"})
        self.C.follow_up()
        self.assertEqual(g.doc[2][8:10], ["auto", "Done"])
        self.assertEqual(g.doc[1][9] if len(g.doc[1]) > 9 else "", "")

    def test_unit_with_nothing_made_clears_auto(self):
        g = self.graph([["NCFE Level 2 X", LINK, "1", "A", "", "", "", "", ""]])
        self.scan(g)
        u = self.C.active_units()[0]
        chapters = [{**c, "state": "failed", "error": "x"} for c in u["chapters"]]
        self.A.table.update_item(Key={"pk": u["pk"]}, UpdateExpression="SET chapters = :c, cstate = :l",
                                 ExpressionAttributeValues={":c": chapters, ":l": "logging"})
        self.C.follow_up()
        self.assertEqual(g.doc[1][8], "")                                   # "auto" removed again
        self.assertEqual(len(g.log), 1)                                     # no log rows


if __name__ == "__main__":
    unittest.main()
