"""Location validation for scraped job metadata and URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, unquote_plus, urlsplit

BLOCKED_LOCATIONS = frozenset({
    "Argentina",
    "Australia",
    "Barcelona",
    "Berlin",
    "Bogota",
    "Brazil",
    "Bucharest",
    "Budapest",
    "Buenos Aires",
    "Canada",
    "Chile",
    "China",
    "Copenhagen",
    "Dublin",
    "France",
    "Germany",
    "Hungary",
    "India",
    "Ireland",
    "Japan",
    "Krakow",
    "Lisbon",
    "London",
    "Madrid",
    "Manila",
    "Melbourne",
    "Mexico",
    "Mexico City",
    "Milan",
    "Munich",
    "Netherlands",
    "New York",
    "Paris",
    "PH",
    "Philippines",
    "Poland",
    "Prague",
    "Remote - US",
    "Romania",
    "San Francisco",
    "Santiago",
    "Sao Paulo",
    "Seattle",
    "Seoul",
    "Shanghai",
    "Singapore",
    "Spain",
    "Stockholm",
    "Sydney",
    "Taipei",
    "Tokyo",
    "Toronto",
    "UK",
    "United Kingdom",
    "United States",
    "US",
    "USA",
    "Vancouver",
    "Vienna",
    "Warsaw",
    "Zurich",
})

ALLOWED_LOCATIONS = frozenset({
    "Beer Sheva",
    "Beersheba",
    "Bnei Brak",
    "Caesarea",
    "Haifa",
    "Herzliya",
    "Hod Hasharon",
    "IL",
    "ISR",
    "Israel",
    "Jerusalem",
    "Kfar Saba",
    "Lod",
    "Modiin",
    "Netanya",
    "Ness Ziona",
    "Petah Tikva",
    "Ra'anana",
    "Raanana",
    "Ramat Hahayal",
    "Ramat Gan",
    "Rehovot",
    "Tel Aviv",
    "Tel-Aviv",
    "TLV",
    "Yavne",
    "Yokneam",
})


# Central-Israel area filter. Edit this list to change which cities are kept.
# Jobs whose location names only the country (e.g. "Israel", "Israel -
# Remote") are kept; jobs naming a city outside this list are dropped.
CENTRAL_AREA_LOCATIONS = (
    "Tel Aviv", "Tel-Aviv", "Tel Aviv-Yafo", "TLV", "Jaffa", "Yafo",
    "Ramat Hahayal", "Gush Dan", "תל אביב", "תל-אביב", "יפו",
    "Ramat Gan", "Ramat-Gan", "רמת גן",
    "Givatayim", "Givataim", "גבעתיים",
    "Bnei Brak", "Bnei Berak", "בני ברק",
    "Herzliya", "Herzliyya", "Herzelia", "Hertzliya", "הרצליה",
    "Petah Tikva", "Petach Tikva", "Petah Tiqva", "Petach Tikvah",
    "Petah Tikvah", "פתח תקווה", "פתח תקוה",
    "Ra'anana", "Raanana", "Ra’anana", "רעננה",
    "Kfar Saba", "Kfar-Saba", "כפר סבא",
    "Hod HaSharon", "Hod Hasharon", "הוד השרון",
    "Rosh HaAyin", "Rosh Ha'ayin", "Rosh Haayin", "Rosh Ha’ayin", "ראש העין",
    "Holon", "חולון",
    "Bat Yam", "בת ים",
    "Rishon LeZion", "Rishon Lezion", "Rishon Le Zion", "Rishon LeTsiyon",
    "ראשון לציון",
    "Or Yehuda", "אור יהודה",
    "Yehud", "יהוד",
    "Airport City", "Lod Airport City",
    "Kiryat Ono", "Kiryat-Ono", "קרית אונו", "קריית אונו",
)

# Words that do not name a city; a location made only of these is treated as
# country-level ("Israel", "Israel - Hybrid") and kept.
GENERIC_LOCATION_WORDS = frozenset({
    "israel", "il", "isr", "ישראל", "remote", "hybrid", "home", "office",
    "onsite", "on", "site", "from", "work", "wfh", "anywhere", "district",
    "center", "centre", "central", "region", "area", "country", "multiple",
    "locations", "location", "and", "or", "in", "the", "of", "&",
})


@dataclass(frozen=True)
class CentralAreaDecision:
    """Describe whether a job location is inside the central area."""

    allowed: bool
    reason: str = ""
    matched_location: str | None = None


class CentralAreaFilter:
    """Keep jobs in central Israel or with a country-level location only."""

    SEPARATOR_PATTERN = re.compile(r"[-_/.,:;|?&=+()\[\]{}'’\"]+")
    WHITESPACE_PATTERN = re.compile(r"\s+")

    def __init__(
        self,
        allowed_locations: Iterable[str] = CENTRAL_AREA_LOCATIONS,
        generic_words: Iterable[str] = GENERIC_LOCATION_WORDS,
    ) -> None:
        """Compile whole-phrase patterns for the allowed central locations."""

        self._generic_words = frozenset(
            self._normalize(word) for word in generic_words
        )
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for location in sorted(set(allowed_locations), key=len, reverse=True):
            normalized = self._normalize(location)
            if not normalized:
                continue
            escaped = re.escape(normalized).replace(r"\ ", r"\s+")
            self._patterns.append(
                (location, re.compile(rf"(?<!\w){escaped}(?!\w)"))
            )

    def _normalize(self, value: str) -> str:
        """Lower-case and collapse separators so phrases match whole words."""

        lowered = unquote_plus(value).casefold()
        separated = self.SEPARATOR_PATTERN.sub(" ", lowered)
        return self.WHITESPACE_PATTERN.sub(" ", separated).strip()

    def evaluate(self, job_location: str | None) -> CentralAreaDecision:
        """Return whether ``job_location`` is central or country-level only."""

        if not isinstance(job_location, str) or not job_location.strip():
            return CentralAreaDecision(
                allowed=True, reason="no location to check"
            )

        normalized = self._normalize(job_location)
        for location, pattern in self._patterns:
            if pattern.search(normalized):
                return CentralAreaDecision(
                    allowed=True,
                    reason="central area city",
                    matched_location=location,
                )

        leftover = [
            word for word in normalized.split()
            if word not in self._generic_words and not word.isdigit()
        ]
        if not leftover:
            return CentralAreaDecision(
                allowed=True, reason="country-level location"
            )
        return CentralAreaDecision(
            allowed=False, reason="city outside central area"
        )


@dataclass(frozen=True)
class LocationDecision:
    """Describe whether a job passed geographic validation."""

    allowed: bool
    reason: str = ""
    matched_location: str | None = None
    source: str | None = None


class LocationFilter:
    """Validate jobs using raw location, title, and URL location signals."""

    LOCATION_QUERY_KEYWORDS = (
        "country",
        "geo",
        "location",
        "office",
        "region",
    )
    SEPARATOR_PATTERN = re.compile(r"[-_/.,:;|?&=+()\[\]{}]+")
    WHITESPACE_PATTERN = re.compile(r"\s+")

    def __init__(
        self,
        blocked_locations: Iterable[str] = BLOCKED_LOCATIONS,
        allowed_locations: Iterable[str] = ALLOWED_LOCATIONS,
        strict_mode: bool = False,
    ) -> None:
        """Build reusable patterns and configure optional whitelist mode."""

        self.strict_mode = strict_mode
        self._blocked_patterns, self._blocked_acronyms = (
            self._compile_locations(blocked_locations)
        )
        self._allowed_patterns, self._allowed_acronyms = (
            self._compile_locations(allowed_locations)
        )

    def evaluate(
        self,
        job_title: str,
        job_url: str,
        job_location: str = "",
    ) -> LocationDecision:
        """Validate all job location signals and explain the decision."""

        sources = (
            ("title", job_title),
            ("url", job_url),
            ("location", job_location),
        )
        for source, value in sources:
            matched_location = self._find_match(
                value=value,
                source=source,
                patterns=self._blocked_patterns,
                acronyms=self._blocked_acronyms,
            )
            if matched_location is not None:
                return LocationDecision(
                    allowed=False,
                    reason="blocked foreign location",
                    matched_location=matched_location,
                    source=source,
                )

        allowed_sources = (
            ("location", job_location),
            ("title", job_title),
            ("url", job_url),
        )
        for source, value in allowed_sources:
            matched_location = self._find_match(
                value=value,
                source=source,
                patterns=self._allowed_patterns,
                acronyms=self._allowed_acronyms,
            )
            if matched_location is not None:
                return LocationDecision(
                    allowed=True,
                    matched_location=matched_location,
                    source=source,
                )

        if not self.strict_mode:
            return LocationDecision(allowed=True)

        return LocationDecision(
            allowed=False,
            reason="strict mode requires a known Israeli location",
        )

    def _compile_locations(
        self,
        locations: Iterable[str],
    ) -> tuple[tuple[tuple[str, re.Pattern[str]], ...], frozenset[str]]:
        """Compile phrases and isolate two-letter location acronyms."""

        patterns: list[tuple[str, re.Pattern[str]]] = []
        acronyms: set[str] = set()
        unique_locations = {
            location.strip()
            for location in locations
            if isinstance(location, str) and location.strip()
        }

        for location in sorted(
            unique_locations,
            key=lambda value: (-len(value), value),
        ):
            if len(location) == 2 and location.isupper():
                acronyms.add(location)
                continue

            normalized = self._normalize(location)
            escaped = re.escape(normalized).replace(r"\ ", r"\s+")
            patterns.append(
                (
                    location,
                    re.compile(rf"(?<!\w){escaped}(?!\w)"),
                )
            )

        return tuple(patterns), frozenset(acronyms)

    def _find_match(
        self,
        value: str,
        source: str,
        patterns: tuple[tuple[str, re.Pattern[str]], ...],
        acronyms: frozenset[str],
    ) -> str | None:
        """Return the first safely matched location in one source value."""

        if not isinstance(value, str) or not value:
            return None

        normalized = self._normalize(value)
        for location, pattern in patterns:
            if pattern.search(normalized):
                return location

        for acronym in acronyms:
            if self._matches_acronym(value, source, acronym):
                return acronym
        return None

    def _normalize(self, value: str) -> str:
        """Decode URLs and normalize separators for phrase matching."""

        decoded = unquote_plus(value).casefold()
        separated = self.SEPARATOR_PATTERN.sub(" ", decoded)
        return self.WHITESPACE_PATTERN.sub(" ", separated).strip()

    @classmethod
    def _matches_acronym(
        cls,
        value: str,
        source: str,
        acronym: str,
    ) -> bool:
        """Match short codes without treating ordinary words as locations."""

        decoded = unquote_plus(value)
        if source == "title":
            pattern = rf"(?<![A-Za-z]){re.escape(acronym)}(?![A-Za-z])"
            return re.search(pattern, decoded) is not None

        url_parts = urlsplit(value)
        path_segments = {
            unquote_plus(segment).casefold()
            for segment in url_parts.path.split("/")
            if segment
        }
        if acronym.casefold() in path_segments:
            return True

        for key, query_value in parse_qsl(
            url_parts.query,
            keep_blank_values=True,
        ):
            is_location_key = any(
                keyword in key.casefold()
                for keyword in cls.LOCATION_QUERY_KEYWORDS
            )
            if (
                is_location_key
                and query_value.strip().casefold() == acronym.casefold()
            ):
                return True
        return False
