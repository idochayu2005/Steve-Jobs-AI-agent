"""Applied-status rules and the Sent-mail application check."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from integrations.gmail_sync import (
    GmailSync,
    ProcessedEmails,
    company_from_domain,
    plan_status_update,
)

ROWS = [
    {"row": 7, "company": "Orbit", "title": "System Administrator", "status": "Not applied", "link": ""},
    {"row": 8, "company": "Wiz", "title": "DevOps Engineer", "status": "Interview", "link": ""},
]


class AppliedStatusTests(unittest.TestCase):
    """An application email sets Applied unless the row is further along."""

    def test_not_applied_row_becomes_applied(self) -> None:
        """'Not applied' is no longer skipped."""

        update, _ = plan_status_update(
            {"kind": "applied", "company": "Orbit", "job_title": ""}, ROWS, "2026-11-18")
        self.assertEqual((update.row, update.status), (7, "Applied"))

    def test_interview_row_is_kept(self) -> None:
        """Never move Interview back to Applied."""

        update, reason = plan_status_update(
            {"kind": "applied", "company": "Wiz", "job_title": ""}, ROWS, "")
        self.assertIsNone(update)
        self.assertIn("Interview", reason)


class CompanyFromDomainTests(unittest.TestCase):
    """Guess the company from the recipient's domain."""

    def test_domains(self) -> None:
        """Skip free-mail, handle .co.il and subdomains."""

        self.assertEqual(company_from_domain("HR <jobs@orbit-cs.com>"), "Orbit Cs")
        self.assertEqual(company_from_domain("a@gmail.com, b@matrix.co.il"), "Matrix")
        self.assertEqual(company_from_domain("x@gmail.com"), "")


def _client(sent, rows):
    """Fake webhook with no alerts or replies and the given sent mail."""

    client = MagicMock()

    def call(action, **payload):
        if action in ("alerts", "replies"):
            return {"messages": []}
        if action == "sent":
            return {"messages": sent}
        if action == "rows":
            return {"rows": [dict(r) for r in rows]}
        return {"ok": True}

    client.call.side_effect = call
    return client


SENT = [{"id": "s1", "threadId": "t1", "date": "2026-11-17T09:00:00Z",
         "to": "Noa <noa@nishapro.co.il>", "subject": "קורות חיים - איש סיסטם",
         "pdf": True, "body": "מצרף קורות חיים"}]


class SentMailTests(unittest.TestCase):
    """Verify the Sent-mail step."""

    def _run(self, client, answer):
        with tempfile.TemporaryDirectory() as directory:
            sync = GmailSync(client, MagicMock(return_value=json.dumps(answer)),
                             ProcessedEmails(Path(directory) / "p.json"), MagicMock())
            first = sync.run()
            second = sync.run()
        return first, second

    def test_new_application_is_added_once(self) -> None:
        """Add a row with the thread link, Applied and the Sent-mail note."""

        client = _client(SENT, ROWS)
        first, _ = self._run(client, {"is_application": True, "company": "",
                                      "job_title": "System Administrator"})

        appends = [c.kwargs["rows"] for c in client.call.call_args_list if c.args[0] == "append"]
        self.assertEqual(len(appends), 1)
        row = appends[0][0]
        self.assertEqual(row[0], "Nishapro")
        self.assertEqual(row[9], "https://mail.google.com/mail/u/0/#all/t1")
        self.assertEqual(row[11], "Applied")
        self.assertTrue(row[12].startswith("Auto: found in Sent mail"))

    def test_matching_row_is_set_to_applied(self) -> None:
        """Update the existing row instead of adding one."""

        client = _client(SENT, ROWS)
        self._run(client, {"is_application": True, "company": "Orbit",
                           "job_title": "System Administrator"})

        sets = [c.kwargs["updates"] for c in client.call.call_args_list if c.args[0] == "set_status"]
        self.assertEqual(sets[0][0]["row"], 7)
        self.assertFalse([c for c in client.call.call_args_list if c.args[0] == "append"])

    def test_non_application_is_ignored(self) -> None:
        """Personal mail changes nothing."""

        client = _client(SENT, ROWS)
        self._run(client, {"is_application": False})

        self.assertFalse([c for c in client.call.call_args_list
                          if c.args[0] in ("append", "set_status")])

    def test_old_webhook_without_sent_is_tolerated(self) -> None:
        """A webhook that lacks "sent" doesn't break the run."""

        client = MagicMock()

        def call(action, **payload):
            if action == "sent":
                raise RuntimeError("unknown action: sent")
            return {"messages": []} if action in ("alerts", "replies") else {"rows": []}

        client.call.side_effect = call
        first, _ = self._run(client, {})
        self.assertEqual(first.get("added_rows", []), [])


if __name__ == "__main__":
    unittest.main()
