"""LinkedIn's 2026-11 alert format adds badge lines around each job card."""

from __future__ import annotations

import unittest

from integrations.gmail_sync import parse_linkedin_alert
from scrapers.orchestrator import is_relevant_job

BODY = """Your job alert for Infrastructure Engineer, on-site or hybrid in Tel Aviv-Yafo
DevOps Engineer
Priority Software
Rosh HaAyin

This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/4400000001/?trk=a

---------------------------------------------------------

System Administrator
Kaltura
Ramat Gan

This company is actively hiring
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/4400000002/?trk=a

---------------------------------------------------------

IT Systems & Network Engineer
Toka
Tel Aviv-Yafo

8 company alumni
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/4400000003/?trk=a

---------------------------------------------------------

Microsoft System Engineer
COMDA
Tel Aviv District, Israel

1 company alum
View job: https://www.linkedin.com/comm/jobs/view/4400000004/?trk=a
"""


class NewAlertFormatTests(unittest.TestCase):
    """Badge lines must not shift title / company / location."""

    def test_cards_with_badges_parse_correctly(self) -> None:
        """Read every card's three lines despite the extra badges."""

        jobs = parse_linkedin_alert(BODY)

        self.assertEqual(
            [(j["title"], j["company"], j["location"]) for j in jobs],
            [("DevOps Engineer", "Priority Software", "Rosh HaAyin"),
             ("System Administrator", "Kaltura", "Ramat Gan"),
             ("IT Systems & Network Engineer", "Toka", "Tel Aviv-Yafo"),
             ("Microsoft System Engineer", "COMDA", "Tel Aviv District, Israel")],
        )

    def test_it_systems_title_is_relevant(self) -> None:
        """'IT Systems ...' titles are infra roles."""

        self.assertTrue(is_relevant_job("IT Systems & Network Engineer").allowed)


if __name__ == "__main__":
    unittest.main()
