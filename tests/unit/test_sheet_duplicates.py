"""Tests for skipping jobs already in the sheet by company + title."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from integrations.gmail_sync import GmailSync, ProcessedEmails
from integrations.sheets_sync import (
    PendingSheetRows,
    drop_duplicates,
    find_duplicate,
    same_company,
    sync_sent_jobs,
)

SHEET = [
    {"row": 114, "company": "Pentera", "title": "System Administrator",
     "status": "New", "link": ""},
    {"row": 106, "company": "Top Soft (agency)", "title": "(role not stated)",
     "status": "Applied", "link": ""},
]


def _row(company: str, title: str, link: str = "") -> list[str]:
    """Build a 16-column sheet row."""

    row = [""] * 16
    row[0], row[1], row[9] = company, title, link
    return row


class DuplicateMatchTests(unittest.TestCase):
    """Verify loose company and normalized title matching."""

    def test_loose_company_match(self) -> None:
        """Match a company name with an extra suffix word."""

        self.assertTrue(same_company("Pentera Security", "Pentera"))
        self.assertTrue(same_company("Cato Networks Ltd.", "cato"))
        self.assertTrue(same_company("Top Soft", "Top Soft (agency)"))
        self.assertFalse(same_company("Pentera", "Penta"))
        self.assertFalse(same_company("", "Pentera"))

    def test_same_company_and_title_is_duplicate(self) -> None:
        """Ignore case, whitespace and punctuation in titles."""

        self.assertIsNotNone(find_duplicate(
            "Pentera Ltd", "  system  administrator. ", "https://x/jobs/1", SHEET))
        self.assertIsNone(find_duplicate("Pentera", "DevOps Engineer", "", SHEET))

    def test_link_match_still_counts(self) -> None:
        """Keep the link check, ignoring tracking and trailing slash."""

        rows = [{"company": "A", "title": "B", "link": "https://www.linkedin.com/jobs/view/1/"}]
        self.assertIsNotNone(find_duplicate(
            "Other", "Other", "https://linkedin.com/jobs/view/1?trk=x", rows))

    def test_drop_duplicates_against_sheet_and_batch(self) -> None:
        """Skip rows already in the sheet and repeats inside one batch."""

        fresh, dupes = drop_duplicates(
            [_row("Pentera", "System Administrator", "https://p/1"),
             _row("Wiz", "DevOps Engineer", "https://w/1"),
             _row("Wiz Inc", "devops engineer", "https://w/2")],
            SHEET,
        )

        self.assertEqual([r[0] for r in fresh], ["Wiz"])
        self.assertEqual(len(dupes), 2)


class SyncSkipsDuplicatesTests(unittest.TestCase):
    """Verify the send step reads the sheet and skips existing jobs."""

    def test_existing_job_is_not_posted(self) -> None:
        """Post only the new row; clear both from pending."""

        def post(url, json, timeout):
            response = MagicMock()
            if json.get("action") == "rows":
                response.json.return_value = {"ok": True, "rows": SHEET}
            else:
                response.json.return_value = {"ok": True, "added": len(json["rows"]), "skipped": 0}
            post.calls.append(json)
            return response

        post.calls = []
        with tempfile.TemporaryDirectory() as directory:
            store = PendingSheetRows(Path(directory) / "rows.json")
            store.put("p", _row("Pentera", "System Administrator", "https://p/1"))
            store.put("w", _row("Wiz", "DevOps Engineer", "https://w/1"))

            sync_sent_jobs(["p", "w"], store=store,
                           env={"SHEETS_WEBHOOK_URL": "u", "SHEETS_WEBHOOK_SECRET": "s"},
                           post=post)
            left = store.load()

        appended = [c for c in post.calls if "rows" in c and c.get("action") != "rows"]
        self.assertEqual([r[0] for r in appended[0]["rows"]], ["Wiz"])
        self.assertEqual(left, {})


class GmailSkipsLooseDuplicateTests(unittest.TestCase):
    """Verify the Gmail step doesn't add a row for a loosely matching company."""

    def test_loose_company_updates_existing_row_not_added(self) -> None:
        """'Pentera Security' email updates the 'Pentera' row, no new row."""

        client = MagicMock()

        def call(action, **payload):
            if action == "alerts":
                return {"messages": []}
            if action == "replies":
                return {"messages": [{"id": "r1", "date": "2026-11-11", "from": "a@x",
                                      "subject": "Thanks", "body": ""}]}
            if action == "rows":
                return {"rows": [dict(r) for r in SHEET]}
            return {"ok": True}

        client.call.side_effect = call
        complete = MagicMock(return_value=json.dumps(
            {"kind": "applied", "company": "Pentera Security", "job_title": "System Administrator"}))
        with tempfile.TemporaryDirectory() as directory:
            sync = GmailSync(client, complete,
                             ProcessedEmails(Path(directory) / "p.json"), MagicMock())
            summary = sync.run()

        appends = [c for c in client.call.call_args_list if c.args[0] == "append"]
        self.assertEqual(appends, [])
        self.assertEqual(summary["added_rows"], [])
        set_calls = [c for c in client.call.call_args_list if c.args[0] == "set_status"]
        self.assertEqual(set_calls[0].kwargs["updates"][0]["row"], 114)


if __name__ == "__main__":
    unittest.main()
