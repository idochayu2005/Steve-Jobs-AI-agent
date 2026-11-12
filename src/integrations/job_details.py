"""Fetch a job's description and extract structured requirements.

Used only by the Google Sheets sync: the Telegram analysis stays unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from paths import PROMPTS_DIR

LOGGER = logging.getLogger(__name__)

MIN_DESCRIPTION_CHARS = 400
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

EXTRACTION_PROMPT = """Candidate profile and scoring rules:
{profile}

Read the job and return JSON only with:
years_required: minimum years of experience required as a number (range 3-5 -> 3, "5+" -> 5), or null if not stated;
degree: one of "required", "advantage", "or-equivalent", "not mentioned" ("or-equivalent" when a degree OR equivalent experience is accepted; "advantage" when only preferred);
key_tech: 3 to 5 key required technologies, short, comma separated, English;
fit_score: 0-100 per the scoring rules above;
missing: the 2-4 most important REQUIRED skills/experience the candidate does NOT show, short English, comma separated, max ~12 words; "Nothing major" if none;
description_found: true if the text is a real job description, false otherwise.

Job title: {title}
Job description text:
{text}"""


def html_to_text(html: str) -> str:
    """Return the visible text of an HTML page, whitespace-collapsed."""

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


class JobTextFetcher:
    """Fetch job page text with requests, falling back to headless Chromium.

    The browser is started lazily on the first page that needs it and must
    be released with :meth:`close`.
    """

    def __init__(self) -> None:
        """Prepare a lazy Playwright session."""

        self._playwright: Any = None
        self._browser: Any = None

    def fetch(self, url: str, fallback_text: str = "") -> str:
        """Return the best available description text for ``url``."""

        if len(fallback_text) >= MIN_DESCRIPTION_CHARS:
            return fallback_text
        text = self._fetch_static(url)
        if len(text) < MIN_DESCRIPTION_CHARS:
            text = self._fetch_rendered(url) or text
        return text or fallback_text

    @staticmethod
    def _fetch_static(url: str) -> str:
        """Fetch server-rendered HTML; return '' on any failure."""

        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            LOGGER.debug("Static fetch failed for %s: %s", url, error)
            return ""
        return html_to_text(response.text)

    def _fetch_rendered(self, url: str) -> str:
        """Render the page in headless Chromium; return '' on any failure."""

        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch()
            page = self._browser.new_page()
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                return re.sub(r"\s+", " ", page.inner_text("body")).strip()
            finally:
                page.close()
        except Exception as error:  # noqa: BLE001 - never break the scan
            LOGGER.debug("Rendered fetch failed for %s: %s", url, error)
            return ""

    def close(self) -> None:
        """Release the browser if it was started."""

        try:
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._playwright = None


def load_profile() -> str:
    """Return the candidate profile used for scoring."""

    path = PROMPTS_DIR / "user_profile.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def make_openai_completion() -> Callable[[str], str]:
    """Return a function that sends one prompt to gpt-4o-mini as JSON mode."""

    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def complete(prompt: str) -> str:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or "{}"

    return complete


def extract_requirements(
    title: str,
    text: str,
    profile: str,
    complete: Callable[[str], str],
) -> dict[str, Any]:
    """Ask the model for structured requirements; return {} on failure."""

    prompt = EXTRACTION_PROMPT.format(
        profile=profile, title=title, text=text[:9000]
    )
    try:
        return json.loads(complete(prompt))
    except Exception as error:  # noqa: BLE001 - never break the scan
        LOGGER.warning(
            "Requirement extraction failed for %r: %s",
            title,
            type(error).__name__,
        )
        return {}
