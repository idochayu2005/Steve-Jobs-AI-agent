"""Sync newly sent jobs to the "Jobs" Google Sheet via an Apps Script webhook.

Flow:
1. Producer (scan): for each queued job, ``SheetRowPreparer`` fetches the
   description, extracts requirements, applies the sheet filters and stores
   the finished row in ``data/pending_sheet_rows.json``.
2. Consumer (send): after Telegram delivery, ``sync_sent_jobs`` POSTs the
   rows of the jobs actually sent in that run to ``SHEETS_WEBHOOK_URL``.
   Rows stay pending if the POST fails and are retried on the next run.

Missing configuration is logged and skipped; it never fails a run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

from analysis.filters.location import CentralAreaFilter
from paths import DATA_DIR

LOGGER = logging.getLogger(__name__)

PENDING_ROWS_FILE = DATA_DIR / "pending_sheet_rows.json"
WEBHOOK_TIMEOUT_SECONDS = 30
MAX_YEARS_REQUIRED = 7  # jobs asking for this many years or more are skipped
DEFAULT_SOURCE = "Company site"

# Title keyword -> sheet "Type" label, checked in order.
TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Security Ops", (
        "security", "soc", "incident", "threat", "cyber", "iam",
        "identity and access",
    )),
    ("IT / Sysadmin", (
        "system administrator", "systems administrator", "sysadmin",
        "it engineer", "it administrator", "it system", "it infrastructure",
        "network engineer", "infrastructure administrator",
        "system & infrastructure", "systems & infrastructure",
        "infrastructure specialist", "it specialist",
    )),
    ("DevOps / SRE", (
        "devops", "sre", "site reliability", "platform", "infrastructure",
    )),
    ("Cloud", ("cloud",)),
    ("System Engineer", ("system engineer", "systems engineer")),
)

DEGREE_LABELS = {
    "required": "Required",
    "advantage": "Advantage only",
    "or-equivalent": "Or equivalent exp.",
    "not mentioned": "Not mentioned",
}

# Central-area spelling -> city name shown in the sheet.
CITY_NAMES: tuple[tuple[str, str], ...] = (
    ("Tel Aviv", "Tel Aviv"), ("Tel-Aviv", "Tel Aviv"), ("TLV", "Tel Aviv"),
    ("Jaffa", "Tel Aviv"), ("Ramat Hahayal", "Tel Aviv"),
    ("תל אביב", "Tel Aviv"), ("Ramat Gan", "Ramat Gan"),
    ("רמת גן", "Ramat Gan"), ("Givatayim", "Givatayim"),
    ("Bnei Brak", "Bnei Brak"), ("Herzliya", "Herzliya"),
    ("Herzliyya", "Herzliya"), ("Herzelia", "Herzliya"),
    ("Hertzliya", "Herzliya"), ("הרצליה", "Herzliya"),
    ("Petah Tikva", "Petah Tikva"), ("Petach Tikva", "Petah Tikva"),
    ("Petah Tiqva", "Petah Tikva"), ("Petach Tikvah", "Petah Tikva"),
    ("פתח תקווה", "Petah Tikva"), ("Ra'anana", "Ra'anana"),
    ("Raanana", "Ra'anana"), ("רעננה", "Ra'anana"),
    ("Kfar Saba", "Kfar Saba"), ("Hod HaSharon", "Hod HaSharon"),
    ("Hod Hasharon", "Hod HaSharon"), ("Rosh HaAyin", "Rosh HaAyin"),
    ("Rosh Ha'ayin", "Rosh HaAyin"), ("Holon", "Holon"),
    ("Bat Yam", "Bat Yam"), ("Rishon LeZion", "Rishon LeZion"),
    ("Or Yehuda", "Or Yehuda"), ("Yehud", "Yehud"),
    ("Airport City", "Airport City"), ("Kiryat Ono", "Kiryat Ono"),
)


def classify_type(title: str) -> str:
    """Return the sheet "Type" label for a job title."""

    lowered = title.casefold()
    for label, keywords in TYPE_RULES:
        if any(keyword in lowered for keyword in keywords):
            return label
    return "DevOps / SRE"


def city_name(location: str) -> str:
    """Return a short central city name, or "Israel" when none is named."""

    central = CentralAreaFilter(allowed_locations=[c for c, _ in CITY_NAMES])
    decision = central.evaluate(location)
    if decision.matched_location:
        return dict(CITY_NAMES)[decision.matched_location]
    return "Israel"


def parse_years(value: Any) -> float | None:
    """Return years required as a number, or None when unknown."""

    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def skip_reason(details: Mapping[str, Any]) -> str | None:
    """Return why a job is filtered out of the sheet, or None to keep it."""

    if str(details.get("degree", "")).lower() == "required":
        return "requires a Bachelor's degree"
    years = parse_years(details.get("years_required"))
    if years is not None and years >= MAX_YEARS_REQUIRED:
        return f"requires {years:g}+ years"
    return None


def build_row(
    job: Mapping[str, Any],
    details: Mapping[str, Any],
    found: date,
) -> list[str]:
    """Map one job and its details to the sheet's columns A-P.

    N (Tailored resume) and O (Personal note) are left empty; P is the
    job's source (``job["source"]``, default "Company site").
    """

    found_description = bool(details.get("description_found", True))
    years = parse_years(details.get("years_required"))
    degree = str(details.get("degree", "not mentioned")).lower()
    fit = details.get("fit_score")
    return [
        str(job.get("company_name", "")),
        str(job.get("title", "")),
        classify_type(str(job.get("title", ""))),
        city_name(str(job.get("location", ""))),
        "" if years is None else f"{years:g}",
        DEGREE_LABELS.get(degree, "Not mentioned") if found_description else "Unknown",
        str(details.get("key_tech", "")) if found_description else "",
        "" if fit in (None, "") or not found_description else str(fit),
        str(details.get("missing", "")) if found_description else "",
        str(job.get("job_url", "")),
        found.isoformat(),
        "New",
        "",
        "",
        "",
        str(job.get("source") or DEFAULT_SOURCE),
    ]


class PendingSheetRows:
    """Durable job_id -> sheet row store between the scan and send steps."""

    def __init__(self, path: Path = PENDING_ROWS_FILE) -> None:
        """Use ``path`` as the JSON backing file."""

        self._path = Path(path)

    def load(self) -> dict[str, list[str]]:
        """Return all pending rows, or {} when the file is missing/corrupt."""

        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, rows: Mapping[str, list[str]]) -> None:
        """Write rows atomically."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, delete=False,
            suffix=".tmp",
        ) as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, self._path)

    def put(self, job_id: str, row: list[str]) -> None:
        """Store or replace one job's row."""

        rows = self.load()
        rows[job_id] = row
        self.save(rows)

    def pop_many(self, job_ids: Iterable[str]) -> None:
        """Remove the given jobs' rows."""

        rows = self.load()
        for job_id in job_ids:
            rows.pop(job_id, None)
        self.save(rows)


class SheetRowPreparer:
    """Build and store a sheet row for each newly queued job (scan step)."""

    def __init__(
        self,
        fetch_text: Callable[[str, str], str],
        extract: Callable[[str, str], Mapping[str, Any]],
        store: PendingSheetRows,
        today: Callable[[], date] = date.today,
    ) -> None:
        """Inject the text fetcher, the extractor and the pending store."""

        self._fetch_text = fetch_text
        self._extract = extract
        self._store = store
        self._today = today

    def prepare(self, job: Mapping[str, Any]) -> bool:
        """Store the job's row; return False when the job should be skipped.

        A job requiring a Bachelor's degree or 7+ years is skipped for both
        the sheet and Telegram. Errors keep the job (fail open), so a broken
        description fetch never hides a job.
        """

        try:
            text = self._fetch_text(
                str(job.get("job_url", "")), str(job.get("content", "") or "")
            )
            details = self._extract(str(job.get("title", "")), text)
            reason = skip_reason(details)
            if reason:
                LOGGER.info("📄 Skipping %s (%s).", job.get("id"), reason)
                return False
            self._store.put(str(job["id"]), build_row(job, details, self._today()))
        except Exception as error:  # noqa: BLE001 - never break the scan
            LOGGER.warning(
                "Sheet row preparation failed for %s: %s",
                job.get("id"),
                type(error).__name__,
            )
        return True


# --- Duplicate detection -------------------------------------------------
# The Apps Script webhook only de-duplicates by link (column J), but rows
# Ido adds by hand often have no link. Before appending, the agent also
# skips any job whose company + title is already in the sheet.

_COMPANY_NOISE = re.compile(
    r"\b(ltd|inc|llc|corp|corporation|limited|israel|group|technologies|"
    r"technology|networks|software|security|the|team|careers|recruiting|"
    r"talent|agency|co)\b"
)
UNKNOWN_TITLES = {"", "role not stated", "agency role not stated"}


def _words(text: str) -> list[str]:
    """Lower-case, drop punctuation and split into words."""

    return re.sub(r"[^\w\s]", " ", (text or "").casefold()).split()


def company_key(name: str) -> list[str]:
    """Company words without legal/common suffixes or bracketed notes."""

    name = re.sub(r"\(.*?\)", " ", name or "")  # "Top Soft (agency)" -> "Top Soft"
    name = name.split(" - ")[0]
    return _words(_COMPANY_NOISE.sub(" ", " ".join(_words(name))))


def same_company(a: str, b: str) -> bool:
    """Loose match: one company's words start the other's ("Pentera" ~ "Pentera Security")."""

    wa, wb = company_key(a), company_key(b)
    if not wa or not wb:
        return False
    short, long_ = sorted((wa, wb), key=len)
    return long_[: len(short)] == short


def title_key(title: str) -> str:
    """Normalized title for comparison (case, whitespace, punctuation)."""

    return " ".join(_words(re.sub(r"\(#?\d+\)", " ", title or "")))


def link_key(link: str) -> str:
    """Link without scheme, query string or trailing slash."""

    link = (link or "").strip().split("?")[0].split("#")[0].rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", link.casefold())


def find_duplicate(
    company: str,
    title: str,
    link: str,
    rows: Iterable[Mapping[str, Any]],
    unknown_title_matches: bool = False,
) -> Mapping[str, Any] | None:
    """Return the existing sheet row this job duplicates, or None.

    A duplicate has the same link, or the same (loosely matched) company and
    the same normalized title. With ``unknown_title_matches`` a missing title
    on either side counts as the same title (used for Gmail-found rows).
    """

    wanted_link = link_key(link)
    wanted_title = title_key(title)
    for row in rows:
        if wanted_link and wanted_link == link_key(str(row.get("link", ""))):
            return row
        if not same_company(company, str(row.get("company", ""))):
            continue
        existing_title = title_key(str(row.get("title", "")))
        if existing_title == wanted_title and wanted_title not in UNKNOWN_TITLES:
            return row
        if unknown_title_matches and (
            wanted_title in UNKNOWN_TITLES or existing_title in UNKNOWN_TITLES
        ):
            return row
    return None


def drop_duplicates(
    rows: Sequence[list[str]],
    existing: Sequence[Mapping[str, Any]],
) -> tuple[list[list[str]], list[list[str]]]:
    """Split 16-column rows into (new, duplicates) against the sheet and each other."""

    seen = list(existing)
    fresh: list[list[str]] = []
    duplicates: list[list[str]] = []
    for row in rows:
        cell = lambda i: str(row[i]) if len(row) > i else ""
        if find_duplicate(cell(0), cell(1), cell(9), seen):
            duplicates.append(row)
            continue
        fresh.append(row)
        seen.append({"company": cell(0), "title": cell(1), "link": cell(9)})
    return fresh, duplicates


def fetch_sheet_rows(
    url: str,
    secret: str,
    post: Callable[..., Any] = requests.post,
) -> list[dict[str, Any]]:
    """Return the sheet's rows ({row, company, title, status, link})."""

    response = post(
        url, json={"secret": secret, "action": "rows"},
        timeout=WEBHOOK_TIMEOUT_SECONDS * 4,
    )
    response.raise_for_status()
    reply = response.json()
    if not reply.get("ok", False):
        raise RuntimeError(str(reply.get("error", "webhook refused the request")))
    return list(reply.get("rows", []))


def post_rows(
    url: str,
    secret: str,
    rows: Sequence[list[str]],
    post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    """POST rows to the Apps Script webhook and return its JSON reply."""

    response = post(
        url,
        json={"secret": secret, "rows": list(rows)},
        timeout=WEBHOOK_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    reply = response.json()
    if not reply.get("ok", False):
        raise RuntimeError(str(reply.get("error", "webhook refused the rows")))
    return reply


def sync_sent_jobs(
    sent_job_ids: Sequence[str],
    store: PendingSheetRows | None = None,
    env: Mapping[str, str] | None = None,
    post: Callable[..., Any] = requests.post,
) -> None:
    """Upload the rows of jobs sent in this run; keep them pending on failure."""

    env = os.environ if env is None else env
    url = env.get("SHEETS_WEBHOOK_URL", "").strip()
    secret = env.get("SHEETS_WEBHOOK_SECRET", "").strip()
    if not url or not secret:
        LOGGER.info("📄 Sheet sync skipped: SHEETS_WEBHOOK_URL/SECRET not set.")
        return

    store = store or PendingSheetRows()
    pending = store.load()
    ids = [job_id for job_id in sent_job_ids if job_id in pending]
    if not ids:
        LOGGER.info("📄 Sheet sync: no new rows to upload.")
        return
    try:
        existing = fetch_sheet_rows(url, secret, post=post)
    except Exception as error:  # noqa: BLE001 - fall back to the link check
        LOGGER.warning(
            "📄 Could not read the sheet for duplicates (%s); relying on the "
            "link check only.", type(error).__name__,
        )
        existing = []
    rows, duplicates = drop_duplicates([pending[i] for i in ids], existing)
    for row in duplicates:
        LOGGER.info("📄 Sheet: %s - %s is already in the sheet; skipped.", row[0], row[1])
    if not rows:
        store.pop_many(ids)
        LOGGER.info("📄 Sheet sync: all %s rows were already in the sheet.", len(ids))
        return
    try:
        reply = post_rows(url, secret, rows, post=post)
    except Exception as error:  # noqa: BLE001 - never break the run
        LOGGER.error(
            "❌ Sheet sync failed (%s); %s rows stay pending.",
            type(error).__name__,
            len(ids),
        )
        return
    store.pop_many(ids)
    LOGGER.info(
        "📄 Sheet sync: %s added, %s already there.",
        reply.get("added", "?"),
        int(reply.get("skipped", 0) or 0) + len(duplicates),
    )
