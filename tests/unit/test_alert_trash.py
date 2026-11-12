"""Processed alert emails go to Trash only once their jobs are finished."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from integrations.gmail_sync import AlertTrashLedger, GmailSync, ProcessedEmails

ALERT = """System Administrator
Kaltura
Tel Aviv, Israel
View job: https://www.linkedin.com/comm/jobs/view/4012345678/?x=1
"""


def _client(trash_reply=None, trash_error=None):
    """Fake webhook: one alert email, no replies."""

    client = MagicMock()

    def call(action, **payload):
        if action == "alerts":
            return {"messages": [{"id": "a1", "body": ALERT}]}
        if action == "replies":
            return {"messages": []}
        if action == "trash":
            if trash_error:
                raise trash_error
            return trash_reply or {"ok": True, "trashed": len(payload["ids"]), "skipped": 0}
        return {"ok": True}

    client.call.side_effect = call
    return client


def _trash_calls(client):
    """Return the ids lists sent to the trash action."""

    return [c.kwargs["ids"] for c in client.call.call_args_list if c.args[0] == "trash"]


class AlertTrashTests(unittest.TestCase):
    """Verify when alert emails are trashed."""

    def _sync(self, client, directory, pending):
        """Build a GmailSync whose pending check returns ``pending``."""

        return GmailSync(
            client, MagicMock(return_value="{}"),
            ProcessedEmails(Path(directory) / "p.json"), MagicMock(return_value=1),
            still_pending=lambda ids: pending["value"],
            trash_ledger=AlertTrashLedger(Path(directory) / "t.json"),
        )

    def test_kept_while_job_pending_then_trashed(self) -> None:
        """Keep the email until its job is delivered and in the sheet."""

        client = _client()
        pending = {"value": True}
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(client, directory, pending)
            first = sync.run()
            pending["value"] = False
            second = sync.run()

        self.assertEqual(first["trashed"], 0)
        self.assertEqual(_trash_calls(client), [["a1"]])
        self.assertEqual(second["trashed"], 1)

    def test_filtered_out_jobs_trash_right_away(self) -> None:
        """Trash in the same run when nothing from the email is queued."""

        client = _client()
        with tempfile.TemporaryDirectory() as directory:
            summary = self._sync(client, directory, {"value": False}).run()

        self.assertEqual(_trash_calls(client), [["a1"]])
        self.assertEqual(summary["trashed"], 1)

    def test_failure_keeps_email_for_next_run(self) -> None:
        """An old webhook without "trash" doesn't crash and keeps the id."""

        client = _client(trash_error=RuntimeError("unknown action: trash"))
        with tempfile.TemporaryDirectory() as directory:
            summary = self._sync(client, directory, {"value": False}).run()
            left = json.loads((Path(directory) / "t.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["trashed"], 0)
        self.assertIn("a1", left)

    def test_no_pending_check_means_no_trash(self) -> None:
        """Without a pending check nothing is ever trashed."""

        client = _client()
        with tempfile.TemporaryDirectory() as directory:
            GmailSync(client, MagicMock(return_value="{}"),
                      ProcessedEmails(Path(directory) / "p.json"),
                      MagicMock(return_value=1)).run()

        self.assertEqual(_trash_calls(client), [])


if __name__ == "__main__":
    unittest.main()
