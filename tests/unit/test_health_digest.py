"""The scraper-health message lists only sites failing 3+ scans, once a day."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from scrapers.orchestrator import OperationalReporter

STATE = {
    "orbit": {"company_name": "Orbit", "consecutive_failures": 3, "last_error_type": "HTTP_404"},
    "flip": {"company_name": "Flipper", "consecutive_failures": 1, "last_error_type": "TIMEOUT"},
    "ok": {"company_name": "Healthy Co", "consecutive_failures": 0},
}


class HealthDigestTests(unittest.TestCase):
    """Verify the daily failing-sites summary."""

    def test_lists_only_three_plus_failures(self) -> None:
        """Ignore single flips and healthy sites."""

        message = OperationalReporter.build_health_digest({}, STATE)

        self.assertIn("Orbit: 3 failed scans in a row (HTTP_404)", message)
        self.assertNotIn("Flipper", message)
        self.assertNotIn("Healthy", message)

    def test_nothing_when_no_site_reaches_threshold(self) -> None:
        """Send nothing when every site failed fewer than 3 times."""

        self.assertIsNone(OperationalReporter.build_health_digest(
            {}, {"a": {"company_name": "A", "consecutive_failures": 2}}))

    def test_sent_at_most_once_per_day(self) -> None:
        """Second run on the same day is silent; next day sends again."""

        notifier = MagicMock()
        day = {"value": "2026-11-18"}
        with tempfile.TemporaryDirectory() as directory:
            reporter = OperationalReporter(
                notifier, digest_file=Path(directory) / "d.txt",
                today=lambda: day["value"])
            reporter.send_health_digest({}, STATE)
            reporter.send_health_digest({}, STATE)
            day["value"] = "2026-11-19"
            reporter.send_health_digest({}, STATE)

        self.assertEqual(notifier.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
