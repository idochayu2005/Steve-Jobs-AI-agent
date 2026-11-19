"""Jobs requiring a degree or 7+ years never reach Telegram or the sheet."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from integrations.sheets_sync import PendingSheetRows, SheetRowPreparer
from scrapers import orchestrator as scan
from storage.queue import PendingAlertQueue

JOB = {"id": "acme_1", "title": "System Administrator", "company_name": "Acme",
       "location": "Tel Aviv", "job_url": "https://acme/jobs/1", "content": "..."}


class RequirementsGateTests(unittest.TestCase):
    """Verify the gate runs before the Telegram queue."""

    def _run(self, details):
        with tempfile.TemporaryDirectory() as directory:
            queue = PendingAlertQueue(Path(directory) / "q.json")
            rows = PendingSheetRows(Path(directory) / "r.json")
            preparer = SheetRowPreparer(
                fetch_text=lambda url, fallback: "text",
                extract=lambda title, text: details,
                store=rows, today=lambda: date(2026, 11, 19))
            producer = scan.JobProducer(
                queue=queue, location_filter=MagicMock(), history_store=MagicMock(),
                health_store=MagicMock(), reporter=MagicMock(), sheet_rows=preparer)
            with patch("scrapers.orchestrator.analyze_job", return_value={}) as analyze:
                producer._analyze_and_enqueue([JOB])
            return queue.ids(), set(rows.load()), analyze.call_count

    def test_degree_required_is_not_sent(self) -> None:
        """No Telegram alert, no sheet row, no AI analysis cost."""

        queued, rows, analyzed = self._run({"degree": "required", "years_required": 3})
        self.assertEqual((queued, rows, analyzed), (set(), set(), 0))

    def test_seven_years_is_not_sent(self) -> None:
        """7+ years is skipped too."""

        queued, _, _ = self._run({"degree": "not mentioned", "years_required": 7})
        self.assertEqual(queued, set())

    def test_degree_or_equivalent_is_sent(self) -> None:
        """'Or equivalent experience' still goes through."""

        queued, rows, _ = self._run({"degree": "or-equivalent", "years_required": 3})
        self.assertEqual((queued, rows), ({"acme_1"}, {"acme_1"}))


if __name__ == "__main__":
    unittest.main()
