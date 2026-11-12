"""Tests for LinkedIn alert parsing and reply-to-status matching."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from integrations.gmail_sync import (
    GmailSync,
    ProcessedEmails,
    new_application_row,
    normalize_company,
    parse_linkedin_alert,
    plan_status_update,
    to_candidate_job,
)

ALERT_BODY = """Your job alert for system administrator in Israel
30+ new jobs match your preferences.

System Administrator
Kaltura
Bnei Brak, Tel Aviv District, Israel
Actively recruiting
View job: https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc%3D&refId=x

IT Systems Engineer
Nexxen
Tel Aviv-Yafo, Tel Aviv District, Israel
3 connections
View job: https://www.linkedin.com/comm/jobs/view/4087654321/?trackingId=def

See all jobs: https://www.linkedin.com/comm/jobs/search/?keywords=x
"""

ROWS = [
    {"row": 5, "company": "Moon Active", "title": "IT Infrastructure & Cloud Engineer", "status": "New"},
    {"row": 17, "company": "Nexxen", "title": "IT Systems Engineer", "status": "Applied"},
    {"row": 18, "company": "Cato Networks", "title": "IT System Engineer", "status": "New"},
    {"row": 19, "company": "Cato Networks", "title": "MDR Security Analyst (Threat Hunting)", "status": "New"},
]


class LinkedInAlertParserTests(unittest.TestCase):
    """Verify the plain-text LinkedIn alert parser."""

    def test_parses_title_company_location_and_id(self) -> None:
        """Read each job card above its link."""

        jobs = parse_linkedin_alert(ALERT_BODY)

        self.assertEqual(
            jobs,
            [
                {"job_id": "4012345678", "title": "System Administrator",
                 "company": "Kaltura",
                 "location": "Bnei Brak, Tel Aviv District, Israel"},
                {"job_id": "4087654321", "title": "IT Systems Engineer",
                 "company": "Nexxen",
                 "location": "Tel Aviv-Yafo, Tel Aviv District, Israel"},
            ],
        )

    def test_candidate_job_uses_clean_link_and_source(self) -> None:
        """Strip tracking parameters and mark the source."""

        job = to_candidate_job(parse_linkedin_alert(ALERT_BODY)[0])

        self.assertEqual(job["job_url"], "https://www.linkedin.com/jobs/view/4012345678/")
        self.assertEqual(job["id"], "linkedin_4012345678")
        self.assertEqual(job["source"], "LinkedIn")


class StatusMatchingTests(unittest.TestCase):
    """Verify how classified emails map to sheet status changes."""

    def test_applied_confirmation_updates_new_row(self) -> None:
        """Move a New row to Applied."""

        update, _ = plan_status_update(
            {"kind": "applied", "company": "Moon Active Ltd", "job_title": ""},
            ROWS, "2026-11-08",
        )

        self.assertEqual((update.row, update.status, update.expect), (5, "Applied", "New"))

    def test_rejection_updates_applied_row(self) -> None:
        """Move an Applied row to Rejected."""

        update, _ = plan_status_update(
            {"kind": "rejected", "company": "Nexxen", "job_title": "IT Systems Engineer"},
            ROWS, "2026-11-08",
        )

        self.assertEqual((update.row, update.status), (17, "Rejected"))

    def test_applied_never_downgrades(self) -> None:
        """Do not set Applied on a row that is already further along."""

        rows = [{**ROWS[1], "status": "Interview"}]
        update, reason = plan_status_update(
            {"kind": "applied", "company": "Nexxen"}, rows, "2026-11-08"
        )

        self.assertIsNone(update)
        self.assertIn("Interview", reason)

    def test_same_company_uses_title_to_pick_row(self) -> None:
        """Pick the row whose title matches when a company has several."""

        update, _ = plan_status_update(
            {"kind": "applied", "company": "Cato Networks", "job_title": "IT System Engineer"},
            ROWS, "2026-11-08",
        )

        self.assertEqual(update.row, 18)

    def test_ambiguous_company_is_skipped(self) -> None:
        """Skip when the title cannot tell the company's rows apart."""

        update, reason = plan_status_update(
            {"kind": "applied", "company": "Cato Networks", "job_title": ""},
            ROWS, "2026-11-08",
        )

        self.assertIsNone(update)
        self.assertIn("several rows", reason)

    def test_unknown_company_and_other_kind_are_skipped(self) -> None:
        """Ignore companies not in the sheet and non-reply emails."""

        self.assertIsNone(plan_status_update({"kind": "applied", "company": "Unknown Co"}, ROWS, "")[0])
        self.assertIsNone(plan_status_update({"kind": "other", "company": "Nexxen"}, ROWS, "")[0])

    def test_normalize_company(self) -> None:
        """Drop suffixes and punctuation."""

        self.assertEqual(normalize_company("Cato Networks Ltd."), "cato")


class NewApplicationRowTests(unittest.TestCase):
    """Verify rows built for applications that are only in Gmail."""

    def test_linkedin_email_uses_job_link(self) -> None:
        """Link to the LinkedIn job when the email contains one."""

        row = new_application_row(
            {"kind": "applied", "company": "comblack",
             "job_title": "System & Infrastructure Engineer"},
            {"id": "m1", "date": "2026-11-10T12:57:00Z",
             "from": "LinkedIn <jobs-noreply@linkedin.com>",
             "body": "View job: https://www.linkedin.com/comm/jobs/view/4488870249/?x=1"},
        )

        self.assertEqual(len(row), 16)
        self.assertEqual(row[0], "comblack")
        self.assertEqual(row[2], "IT / Sysadmin")
        self.assertEqual(row[9], "https://www.linkedin.com/jobs/view/4488870249/")
        self.assertEqual(row[11], "Applied")
        self.assertEqual(row[15], "LinkedIn")

    def test_plain_email_links_to_the_email(self) -> None:
        """Fall back to a Gmail link and an empty type."""

        row = new_application_row(
            {"kind": "applied", "company": "Manpower", "job_title": ""},
            {"id": "abc123", "date": "2026-11-09", "from": "x@manpower.co.il", "body": "..."},
        )

        self.assertEqual(row[1], "(role not stated)")
        self.assertEqual(row[2], "")
        self.assertEqual(row[9], "https://mail.google.com/mail/u/0/#all/abc123")
        self.assertEqual(row[15], "Email")


class GmailSyncRunTests(unittest.TestCase):
    """Verify the end-to-end run with a fake webhook and LLM."""

    def test_run_queues_jobs_updates_status_and_remembers_ids(self) -> None:
        """Handle alerts and replies once, then skip them next time."""

        client = MagicMock()
        rows = [dict(r) for r in ROWS]

        def call(action, **payload):
            if action == "alerts":
                return {"messages": [{"id": "a1", "body": ALERT_BODY}]}
            if action == "replies":
                return {"messages": [{"id": "r1", "date": "2026-11-08T10:00:00Z",
                                      "from": "jobs@moonactive.com",
                                      "subject": "Thanks for applying", "body": "..."}]}
            if action == "rows":
                return {"rows": rows}
            return {"ok": True}

        client.call.side_effect = call
        complete = MagicMock(return_value=json.dumps(
            {"kind": "applied", "company": "Moon Active", "job_title": ""}))
        enqueue = MagicMock(return_value=2)
        with tempfile.TemporaryDirectory() as directory:
            processed = ProcessedEmails(Path(directory) / "p.json")
            sync = GmailSync(client, complete, processed, enqueue)

            summary = sync.run()
            second = sync.run()

        self.assertEqual(summary["alert_jobs"], 2)
        self.assertEqual(summary["queued"], 2)
        self.assertEqual(len(summary["status_changes"]), 1)
        set_calls = [c for c in client.call.call_args_list if c.args[0] == "set_status"]
        self.assertEqual(set_calls[0].kwargs["updates"][0]["row"], 5)
        self.assertEqual(second["alert_emails"], 0)
        self.assertEqual(len(set_calls), 1)

    def test_unknown_company_application_is_appended_once(self) -> None:
        """Add one row for a new company even if two emails mention it."""

        client = MagicMock()

        def call(action, **payload):
            if action == "alerts":
                return {"messages": []}
            if action == "replies":
                return {"messages": [
                    {"id": "r1", "date": "2026-11-10", "from": "a@x", "subject": "s", "body": ""},
                    {"id": "r2", "date": "2026-11-10", "from": "b@x", "subject": "s", "body": ""},
                ]}
            if action == "rows":
                return {"rows": [dict(r) for r in ROWS]}
            return {"ok": True}

        client.call.side_effect = call
        complete = MagicMock(return_value=json.dumps(
            {"kind": "applied", "company": "NewCo", "job_title": "System Administrator"}))
        with tempfile.TemporaryDirectory() as directory:
            sync = GmailSync(client, complete,
                             ProcessedEmails(Path(directory) / "p.json"), MagicMock())
            summary = sync.run()

        appends = [c for c in client.call.call_args_list if c.args[0] == "append"]
        self.assertEqual(len(appends), 1)
        self.assertEqual(len(appends[0].kwargs["rows"]), 1)
        self.assertEqual(appends[0].kwargs["rows"][0][0], "NewCo")
        self.assertEqual(summary["added_rows"], ["NewCo - System Administrator (Applied)"])


if __name__ == "__main__":
    unittest.main()
