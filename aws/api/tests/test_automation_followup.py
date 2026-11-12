"""
State-machine test for automation._advance() with AWS replaced by fakes:
processing -> saving -> verifying -> writing -> done, and the failure paths.
"""
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                    # aws/api
sys.path.insert(0, str(HERE.parents[2] / "automation"))     # aws/automation
os.environ.setdefault("JOBS_TABLE", "test-jobs")
os.environ.setdefault("MEDIA_BUCKET", "test-media")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")

import automation  # noqa: E402


class Fake:
    def __init__(self, job=None, write_outcome="written", verify_problem=None):
        self.jobs = {"j1": job} if job else {}
        self.writes, self.cleaned, self.saves, self.transitions = [], [], [], []
        self.write_outcome = write_outcome
        self.verify_problem = verify_problem

    def install(self, test):
        orig = {n: getattr(automation, n) for n in
                ("_set", "_get_job", "_write_result", "_cleanup_s3", "_start_drive_save", "verify_drive")}
        test.addCleanup(lambda: [setattr(automation, n, f) for n, f in orig.items()])

        def _set(pk, from_state, expr, values=None, names=None):
            new = None
            for key in (":s", ":v", ":w", ":d", ":f", ":p"):
                if values and key in values and "qstate" in expr:
                    new = values[key]
                    break
            self.transitions.append((from_state, new))
            return True

        automation._set = _set
        automation._get_job = lambda job_id: self.jobs.get(job_id) if job_id else None
        automation._write_result = lambda item, value: (self.writes.append(value), self.write_outcome)[1]
        automation._cleanup_s3 = lambda job: self.cleaned.append(bool(job))
        automation._start_drive_save = lambda job: self.saves.append(job["job_id"]) or True
        automation.verify_drive = lambda drive, job: self.verify_problem


def item(state, **kw):
    base = {"pk": "AUTO#x", "qstate": state, "job_id": "j1", "row": 7, "attempts": 1}
    base.update(kw)
    return base


class FollowUp(unittest.TestCase):
    def run_step(self, fake, it, now=1_000_000.0):
        fake.install(self)
        automation._advance(it, now, lambda: object())
        return it

    def test_happy_path(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "idle"})
        self.run_step(f, item("processing"))
        self.assertEqual(f.saves, ["j1"])
        self.assertEqual(f.transitions, [("processing", "saving")])

        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "saved",
                  "drive_file_id": "F", "drive_link": "L"})
        self.run_step(f, item("saving"))
        self.assertEqual(f.transitions, [("saving", "verifying"), ("verifying", "writing"), ("writing", "done")])
        self.assertEqual(f.writes, ["Yes"])
        self.assertEqual(f.cleaned, [True])  # S3 copies removed once the video is safe in Drive

    def test_still_processing_waits(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "processing", "drive_status": "idle"})
        self.run_step(f, item("processing"))
        self.assertEqual(f.transitions, [])

    def test_processing_failure_marks_no(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "failed", "error": "bad video"})
        it = self.run_step(f, item("processing"))
        self.assertEqual(f.transitions[0], ("processing", "failing"))
        self.assertIn("bad video", it["error"])
        f2 = Fake(f.jobs["j1"])
        self.run_step(f2, it)
        self.assertEqual(f2.writes, ["NO"])
        self.assertEqual(f2.transitions[-1], ("failing", "failed"))

    def test_drive_failure_marks_no(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "failed",
                  "drive_error": "quota"})
        it = self.run_step(f, item("saving"))
        self.assertEqual(it["qstate"], "failing")
        self.assertIn("quota", it["error"])

    def test_verification_failure_marks_no(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "saved"},
                 verify_problem="checksum mismatch")
        it = self.run_step(f, item("verifying"))
        self.assertEqual(it["qstate"], "failing")
        self.assertEqual(f.writes, [])  # never "Yes" for an unverified file

    def test_person_took_over(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "saved"},
                 write_outcome="taken_over")
        self.run_step(f, item("writing"))
        self.assertEqual(f.transitions, [("writing", "abandoned")])

    def test_stuck_download_is_requeued(self):
        f = Fake()
        self.run_step(f, item("downloading", heartbeat=0, attempts=1, job_id=None))
        self.assertEqual(f.transitions, [("downloading", "pending")])

    def test_stuck_download_gives_up(self):
        f = Fake()
        it = self.run_step(f, item("downloading", heartbeat=0, attempts=3, job_id=None))
        self.assertEqual(it["qstate"], "failing")

    def test_pending_too_long_gives_no(self):
        f = Fake()
        it = self.run_step(f, item("pending", updated_at=0, job_id=None))
        self.assertEqual(it["qstate"], "failing")
        self.assertIn("2 hours", it["error"])

    def test_pending_recent_waits(self):
        f = Fake()
        self.run_step(f, item("pending", updated_at=1_000_000.0 - 600, job_id=None))
        self.assertEqual(f.transitions, [])

    def test_processing_too_long_gives_no(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "queued"})
        it = self.run_step(f, item("processing", updated_at=0))
        self.assertEqual(it["qstate"], "failing")
        self.assertIn("90 minutes", it["error"])

    def test_verifying_too_long_gives_no(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "saved"})
        it = self.run_step(f, item("verifying", updated_at=0))
        self.assertEqual(it["qstate"], "failing")
        self.assertEqual(f.writes, [])

    def test_sheet_write_stuck_alerts_but_keeps_trying(self):
        sent = []
        orig = automation.alert
        automation.alert = lambda kind, subject, message, *a, **k: sent.append(kind) or True
        self.addCleanup(lambda: setattr(automation, "alert", orig))
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "saved"})
        self.run_step(f, item("writing", updated_at=0))
        self.assertEqual(sent, ["sheet_write_stuck"])
        self.assertEqual(f.writes, ["Yes"])

    def test_drive_save_timeout(self):
        f = Fake({"pk": "JOB#j1", "job_id": "j1", "status": "done", "drive_status": "saving"})
        it = self.run_step(f, item("saving", saving_since=0))
        self.assertEqual(it["qstate"], "failing")


if __name__ == "__main__":
    unittest.main()
