"""Tests for the Google Sheets row mapping, filters and webhook sync."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from integrations.sheets_sync import (
    PendingSheetRows,
    SheetRowPreparer,
    build_row,
    city_name,
    classify_type,
    skip_reason,
    sync_sent_jobs,
)

JOB = {
    "id": "acme_1",
    "company_name": "Acme",
    "title": "System Administrator",
    "location": "Petach Tikva | Center District | Israel",
    "job_url": "https://example.test/jobs/1",
    "content": "",
}
DETAILS = {
    "years_required": 3,
    "degree": "not mentioned",
    "key_tech": "Active Directory, Azure, PowerShell",
    "fit_score": 82,
    "missing": "AWS, Okta",
    "description_found": True,
}
ENV = {"SHEETS_WEBHOOK_URL": "https://hook.test/exec", "SHEETS_WEBHOOK_SECRET": "s"}


class SheetRowMappingTests(unittest.TestCase):
    """Verify the 13-column row and the type/city helpers."""

    def test_build_row_matches_sheet_columns(self) -> None:
        """Produce the A-M column values in the sheet's order and labels."""

        row = build_row(JOB, DETAILS, date(2026, 11, 6))

        self.assertEqual(
            row,
            [
                "Acme", "System Administrator", "IT / Sysadmin", "Petah Tikva",
                "3", "Not mentioned", "Active Directory, Azure, PowerShell",
                "82", "AWS, Okta", "https://example.test/jobs/1",
                "2026-11-06", "New", "", "", "", "Company site",
            ],
        )

    def test_build_row_uses_job_source(self) -> None:
        """Write the job's source into column P."""

        row = build_row({**JOB, "source": "LinkedIn"}, DETAILS, date(2026, 11, 6))

        self.assertEqual(len(row), 16)
        self.assertEqual(row[15], "LinkedIn")

    def test_missing_description_marks_degree_unknown(self) -> None:
        """Leave analysis columns blank when no description was found."""

        row = build_row(JOB, {"description_found": False}, date(2026, 11, 6))

        self.assertEqual(row[5], "Unknown")
        self.assertEqual(row[6:9], ["", "", ""])

    def test_classify_type(self) -> None:
        """Map titles to the sheet's Type labels."""

        cases = {
            "DevOps Engineer": "DevOps / SRE",
            "Site Reliability Engineer": "DevOps / SRE",
            "IT Systems Engineer": "IT / Sysadmin",
            "Cloud Security Engineer - Microsoft 365": "Security Ops",
            "Cloud Operations Engineer": "Cloud",
            "System Engineer": "System Engineer",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(classify_type(title), expected)

    def test_city_name(self) -> None:
        """Shorten central locations and fall back to Israel."""

        self.assertEqual(city_name("Tel Aviv-Yafo, Tel Aviv District"), "Tel Aviv")
        self.assertEqual(city_name("Herzeliya | Herzliya"), "Herzliya")
        self.assertEqual(city_name("IL"), "Israel")


class SheetFilterTests(unittest.TestCase):
    """Verify the Bachelor's-degree and 7+ years filters."""

    def test_degree_required_is_skipped(self) -> None:
        """Skip jobs that require a degree."""

        self.assertIsNotNone(skip_reason({**DETAILS, "degree": "required"}))

    def test_seven_plus_years_is_skipped(self) -> None:
        """Skip jobs asking for 7 or more years."""

        self.assertIsNotNone(skip_reason({**DETAILS, "years_required": 7}))
        self.assertIsNone(skip_reason({**DETAILS, "years_required": 6}))

    def test_equivalent_or_advantage_degree_is_kept(self) -> None:
        """Keep jobs where a degree is optional."""

        for degree in ("or-equivalent", "advantage", "not mentioned"):
            with self.subTest(degree=degree):
                self.assertIsNone(skip_reason({**DETAILS, "degree": degree}))

    def test_preparer_stores_only_kept_jobs(self) -> None:
        """Store rows for kept jobs and none for filtered jobs."""

        with tempfile.TemporaryDirectory() as directory:
            store = PendingSheetRows(Path(directory) / "rows.json")
            details = iter([DETAILS, {**DETAILS, "degree": "required"}])
            preparer = SheetRowPreparer(
                fetch_text=lambda url, fallback: "text",
                extract=lambda title, text: next(details),
                store=store,
                today=lambda: date(2026, 11, 6),
            )
            preparer.prepare(JOB)
            preparer.prepare({**JOB, "id": "acme_2"})

            self.assertEqual(list(store.load()), ["acme_1"])


class SheetSyncTests(unittest.TestCase):
    """Verify the webhook upload and its failure handling."""

    def _store(self, directory: str) -> PendingSheetRows:
        store = PendingSheetRows(Path(directory) / "rows.json")
        store.put("acme_1", ["row1"])
        store.put("acme_2", ["row2"])
        return store

    def test_posts_only_sent_rows_and_clears_them(self) -> None:
        """Upload rows of sent jobs only, then drop them from pending."""

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            response = MagicMock()
            response.json.return_value = {"ok": True, "added": 1, "skipped": 0}
            post = MagicMock(return_value=response)

            sync_sent_jobs(["acme_1"], store=store, env=ENV, post=post)

            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload, {"secret": "s", "rows": [["row1"]]})
            self.assertEqual(list(store.load()), ["acme_2"])

    def test_failure_keeps_rows_pending(self) -> None:
        """Keep rows for the next run when the webhook fails."""

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            post = MagicMock(side_effect=OSError("network down"))

            sync_sent_jobs(["acme_1"], store=store, env=ENV, post=post)

            self.assertEqual(sorted(store.load()), ["acme_1", "acme_2"])

    def test_missing_config_skips_without_posting(self) -> None:
        """Do nothing when the webhook is not configured."""

        post = MagicMock()
        sync_sent_jobs(["acme_1"], env={}, post=post)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
