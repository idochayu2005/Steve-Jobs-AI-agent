"""Senior titles are allowed; lead / manager titles are still dropped."""

from __future__ import annotations

import unittest

from scrapers.orchestrator import is_relevant_job


class SeniorTitleTests(unittest.TestCase):
    """Verify the seniority rules for infra titles."""

    def test_senior_and_sr_infra_titles_pass(self) -> None:
        """Ido wants above-junior roles; the AI step still drops 7+ years."""

        for title in ("Senior System Administrator", "Sr. DevOps Engineer",
                      "Senior Windows Endpoint Engineering & Security Engineer"):
            self.assertTrue(is_relevant_job(title).allowed, title)

    def test_lead_and_manager_titles_are_dropped(self) -> None:
        """Team leads, leads and managers stay out."""

        for title in ("SRE Team Lead", "DevOps Technical Lead",
                      "Product Manager", "HPC Support Team Lead"):
            self.assertFalse(is_relevant_job(title).allowed, title)


if __name__ == "__main__":
    unittest.main()


class ItManagerTitleTests(unittest.TestCase):
    """Hands-on IT manager titles pass; other manager titles don't."""

    def test_it_manager_titles_pass(self) -> None:
        """Allow IT / infrastructure / system manager titles."""

        for title in ("IT Manager", "IT & Infrastructure Manager",
                      "Infrastructure Manager", "System Manager", "Senior IT Manager"):
            self.assertTrue(is_relevant_job(title).allowed, title)

    def test_other_manager_titles_are_dropped(self) -> None:
        """Keep product / engineering / R&D / network managers out."""

        for title in ("Product Manager", "Engineering Manager", "R&D Manager",
                      "Windows Network Manager", "IT Team Lead"):
            self.assertFalse(is_relevant_job(title).allowed, title)
