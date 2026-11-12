"""Support-desk titles are filtered out; infra and cloud-support titles stay."""

from __future__ import annotations

import unittest

from scrapers.orchestrator import TitleRelevanceFilter


class SupportExclusionTests(unittest.TestCase):
    """Verify helpdesk / IT-support titles are excluded."""

    def setUp(self) -> None:
        """Use the default keyword lists."""

        self.filter = TitleRelevanceFilter()

    def _passes(self, title: str) -> bool:
        """Return whether the title filter keeps ``title``."""

        return self.filter.evaluate(title).allowed

    def test_support_titles_are_excluded(self) -> None:
        """Drop helpdesk, IT support, NOC and Hebrew support titles."""

        for title in ("IT Support / IT Engineer", "Helpdesk & System Administrator",
                      "Service Desk Engineer", "NOC Engineer", "Technical Support Engineer",
                      "תמיכה טכנית ומערכות מידע", "טכנאי מחשבים ותשתיות",
                      "איש/אשת IT ותמיכה במשתמשים"):
            self.assertFalse(self._passes(title), title)

    def test_infra_titles_still_pass(self) -> None:
        """Keep sysadmin, DevOps and cloud-support titles."""

        for title in ("System Administrator", "DevOps Engineer",
                      "Cloud Support Engineer", "מנהל/ת תשתיות ענן"):
            self.assertTrue(self._passes(title), title)


if __name__ == "__main__":
    unittest.main()
