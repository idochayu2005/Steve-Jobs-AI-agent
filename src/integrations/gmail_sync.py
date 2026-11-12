"""Read job emails from Gmail (via the sheet's Apps Script webhook).

Two jobs per run:
1. LinkedIn job-alert emails (Gmail label "Job Alerts") become candidate jobs
   that go through the same filters, analysis, Telegram queue and Google
   Sheet rows as company-site jobs, with Source = "LinkedIn".
2. Company replies to applications ("thanks for applying", "we decided to
   move forward with other candidates", interview invitations) update the
   matching row's Status in the sheet.

Gmail is only read. Run: ``python -m integrations.gmail_sync``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests
from dotenv import load_dotenv

from paths import DATA_DIR

LOGGER = logging.getLogger(__name__)

PROCESSED_FILE = DATA_DIR / "processed_emails.json"
ALERT_DAYS = 7
REPLY_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 300  # Gmail searches over 30 days can take minutes
LINKEDIN_SOURCE = "LinkedIn"

JOB_URL_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)[^\s<>\])]*",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"(?i)^(view job|apply|easy apply|promoted|actively recruiting|be an early|"
    r"see all jobs|see more jobs|unsubscribe|help|manage|your job alert|"
    r"\d+\+? new jobs?|.*\bconnections?\b.*|.*\balumni\b.*|.*\bapplicants?\b.*|"
    r".*\bago$|.*school.*|this company.*|new$|-+|=+|\W*)$"
)


# --------------------------------------------------------------------------
# Apps Script webhook
# --------------------------------------------------------------------------
class AppsScriptClient:
    """Call actions on the sheet's Apps Script web app."""

    def __init__(
        self,
        url: str,
        secret: str,
        post: Callable[..., Any] = requests.post,
    ) -> None:
        """Store the web app URL, the shared secret and the HTTP poster."""

        self._url = url
        self._secret = secret
        self._post = post

    @classmethod
    def from_environment(cls) -> "AppsScriptClient | None":
        """Build a client from SHEETS_WEBHOOK_URL/SECRET, or None if unset."""

        url = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
        secret = os.getenv("SHEETS_WEBHOOK_SECRET", "").strip()
        return cls(url, secret) if url and secret else None

    def call(self, action: str, **payload: Any) -> dict[str, Any]:
        """Run one action and return its JSON reply; raise on failure."""

        response = self._post(
            self._url,
            json={"secret": self._secret, "action": action, **payload},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        reply = response.json()
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error", "webhook error")))
        return reply


# --------------------------------------------------------------------------
# Processed message ids
# --------------------------------------------------------------------------
class ProcessedEmails:
    """Remember which Gmail message ids were already handled."""

    def __init__(self, path: Path = PROCESSED_FILE) -> None:
        """Use ``path`` as the JSON backing file."""

        self._path = Path(path)

    def load(self) -> set[str]:
        """Return processed ids (empty on a missing or corrupt file)."""

        try:
            return set(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()

    def add_many(self, ids: Iterable[str]) -> None:
        """Add ids and write the file atomically."""

        merged = sorted(self.load() | set(ids))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, delete=False,
            suffix=".tmp",
        ) as handle:
            json.dump(merged, handle, indent=1)
        os.replace(handle.name, self._path)


TRASH_LEDGER_FILE = DATA_DIR / "alert_emails_to_trash.json"


class AlertTrashLedger:
    """Alert email id -> job ids it contained, kept until the email is trashed."""

    def __init__(self, path: Path = TRASH_LEDGER_FILE) -> None:
        """Use ``path`` as the JSON backing file."""

        self._path = Path(path)

    def load(self) -> dict[str, list[str]]:
        """Return the ledger ({} on a missing or corrupt file)."""

        try:
            return dict(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def save(self, ledger: Mapping[str, list[str]]) -> None:
        """Write the ledger atomically."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, delete=False,
            suffix=".tmp",
        ) as handle:
            json.dump(dict(ledger), handle, indent=1)
        os.replace(handle.name, self._path)


# --------------------------------------------------------------------------
# LinkedIn alert parsing
# --------------------------------------------------------------------------
def parse_linkedin_alert(body: str) -> list[dict[str, str]]:
    """Extract jobs from a LinkedIn job-alert email's plain-text body.

    LinkedIn lists each job as title / company / location lines followed by
    a ``.../jobs/view/<id>`` link. Returns dicts with job_id, title,
    company and location; jobs whose lines cannot be read are returned with
    empty fields so the caller can fall back to the LLM.
    """

    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    previous_end = 0
    for match in JOB_URL_RE.finditer(body or ""):
        job_id = match.group(1)
        block = body[previous_end : match.start()]
        previous_end = match.end()
        if job_id in seen:
            continue
        seen.add(job_id)
        lines = [
            line.strip(" \t•·|-:")
            for line in block.replace("\r", "").split("\n")
        ]
        lines = [line for line in lines if line and not NOISE_RE.match(line)]
        title, company, location = "", "", ""
        if len(lines) >= 3:
            title, company, location = lines[-3], lines[-2], lines[-1]
        elif len(lines) == 2:
            title, company = lines[-2], lines[-1]
        if company and " · " in company and not location:
            company, location = company.split(" · ", 1)
        jobs.append({
            "job_id": job_id,
            "title": title,
            "company": company,
            "location": location,
        })
    return jobs


LLM_ALERT_PROMPT = """This is a LinkedIn job alert email. For each of these LinkedIn job ids: {ids}
return the job's title, company and location exactly as written in the email.
Return JSON only: {{"jobs": [{{"job_id": "...", "title": "...", "company": "...", "location": "..."}}]}}.
Use "" for anything not in the email. Do not invent jobs or ids.

Email:
{body}"""


def fill_missing_with_llm(
    jobs: list[dict[str, str]],
    body: str,
    complete: Callable[[str], str],
) -> list[dict[str, str]]:
    """Use the LLM to fill jobs whose title/company could not be parsed."""

    missing = [job["job_id"] for job in jobs if not (job["title"] and job["company"])]
    if not missing:
        return jobs
    try:
        reply = json.loads(complete(
            LLM_ALERT_PROMPT.format(ids=", ".join(missing), body=body[:12000])
        ))
        found = {str(j.get("job_id")): j for j in reply.get("jobs", [])}
    except Exception as error:  # noqa: BLE001 - keep what was parsed
        LOGGER.warning("Alert LLM fallback failed: %s", type(error).__name__)
        return jobs
    for job in jobs:
        extra = found.get(job["job_id"])
        if extra and not (job["title"] and job["company"]):
            for key in ("title", "company", "location"):
                job[key] = job[key] or str(extra.get(key, "")).strip()
    return jobs


def to_candidate_job(parsed: Mapping[str, str]) -> dict[str, Any]:
    """Convert a parsed alert job to the producer's job dict."""

    job_id = parsed["job_id"]
    return {
        "id": f"linkedin_{job_id}",
        "title": parsed.get("title", ""),
        "company_name": parsed.get("company", ""),
        "location": parsed.get("location", "") or "Israel",
        "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "content": "",
        "source": LINKEDIN_SOURCE,
    }


# --------------------------------------------------------------------------
# Company replies -> sheet status
# --------------------------------------------------------------------------
REPLY_PROMPT = """You read emails a job seeker received. Classify this email.
Return JSON only:
{{"kind": "applied" | "rejected" | "interview" | "other",
  "company": "<hiring company name or "">",
  "job_title": "<job title or "">"}}
- "applied": confirms an application was received/sent (e.g. "thanks for applying", "your application was sent to X").
- "rejected": the company will not proceed with the candidate (e.g. "decided to move forward with other candidates", "not to proceed"). If the email says the role/program is not relevant for the candidate or the candidate declined, use "other".
- "interview": invites to an interview/call or a home assignment.
- "other": anything else (newsletters, job alerts, marketing, recruiters' generic offers).
For LinkedIn "application was sent" emails, company is the hiring company, not LinkedIn.

From: {sender}
Subject: {subject}
Body:
{body}"""

TRANSITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    # kind: (new status, statuses it may replace)
    "applied": ("Applied", ("", "New")),
    "interview": ("Interview", ("", "New", "Applied")),
    "rejected": ("Rejected", ("", "New", "Applied", "Interview")),
}

# Kinds of email that add a new sheet row when the company isn't listed yet.
NEW_ROW_STATUS = {"applied": "Applied", "interview": "Interview"}
LINKEDIN_VIEW_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)


def new_application_row(
    classification: Mapping[str, str],
    message: Mapping[str, Any],
) -> list[str]:
    """Build a 16-column sheet row for an application found only in Gmail.

    The Link column is the LinkedIn job when the email has one, otherwise a
    Gmail link to the email itself (the sheet script needs a unique link).
    """

    body = str(message.get("body", ""))
    sender = str(message.get("from", "")).lower()
    match = LINKEDIN_VIEW_RE.search(body)
    if match:
        link = f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
        source = "LinkedIn"
    else:
        link = f"https://mail.google.com/mail/u/0/#all/{message.get('id', '')}"
        source = "LinkedIn" if "linkedin" in sender else "Email"
    date_text = str(message.get("date", ""))[:10]
    kind = str(classification.get("kind", "applied")).lower()
    raw_title = str(classification.get("job_title", "")).strip()
    title = raw_title or "(role not stated)"
    job_type = ""
    if raw_title:
        from integrations.sheets_sync import classify_type

        job_type = classify_type(raw_title)
    return [
        str(classification.get("company", "")).strip(),
        title,
        job_type, "", "", "", "", "", "",
        link,
        date_text,
        NEW_ROW_STATUS.get(kind, "Applied"),
        f"Auto: added from {kind} email {date_text}",
        "", "",
        source,
    ]


COMPANY_NOISE = re.compile(
    r"\b(ltd|inc|llc|corp|corporation|limited|israel|group|technologies|"
    r"technology|networks|software|the|team|careers|recruiting|talent)\b"
)


def normalize_company(name: str) -> str:
    """Lower-case a company name and drop legal/common suffix words."""

    text = re.sub(r"[^\w\s]", " ", (name or "").casefold())
    text = COMPANY_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_overlap(a: str, b: str) -> int:
    """Count shared words between two job titles."""

    words = lambda s: set(re.findall(r"\w+", (s or "").casefold())) - {"and", "the"}
    return len(words(a) & words(b))


@dataclass(frozen=True)
class StatusUpdate:
    """One planned change to a sheet row's Status."""

    row: int
    status: str
    expect: str
    note: str
    company: str
    title: str


def plan_status_update(
    classification: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
    date_text: str,
) -> tuple[StatusUpdate | None, str]:
    """Match a classified email to one sheet row; return (update, reason)."""

    kind = str(classification.get("kind", "other")).lower()
    if kind not in TRANSITIONS:
        return None, "not an application reply"
    company = normalize_company(str(classification.get("company", "")))
    if not company:
        return None, "company not found in email"
    candidates = [
        row for row in rows
        if (c := normalize_company(str(row.get("company", ""))))
        and (c == company or c in company or company in c)
    ]
    if not candidates:
        return None, "company not in the sheet"
    if len(candidates) > 1:
        title = str(classification.get("job_title", ""))
        scored = sorted(
            candidates, key=lambda r: title_overlap(title, str(r.get("title"))),
            reverse=True,
        )
        best = title_overlap(title, str(scored[0].get("title")))
        if best == 0 or best == title_overlap(title, str(scored[1].get("title"))):
            return None, "several rows for this company; could not tell which job"
        candidates = scored[:1]
    row = candidates[0]
    new_status, allowed_from = TRANSITIONS[kind]
    current = str(row.get("status", ""))
    if current == new_status:
        return None, f"already {new_status}"
    if current not in allowed_from:
        return None, f"status is {current!r}; not changing it to {new_status}"
    return (
        StatusUpdate(
            row=int(row["row"]),
            status=new_status,
            expect=current,
            note=f"Auto: {kind} email {date_text}",
            company=str(row.get("company", "")),
            title=str(row.get("title", "")),
        ),
        "matched",
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
class GmailSync:
    """Run the LinkedIn-alert and reply-status steps once."""

    def __init__(
        self,
        client: AppsScriptClient,
        complete: Callable[[str], str],
        processed: ProcessedEmails,
        enqueue_jobs: Callable[[list[dict[str, Any]]], int],
        notify: Callable[[str], Any] | None = None,
        still_pending: Callable[[set[str]], bool] | None = None,
        trash_ledger: AlertTrashLedger | None = None,
    ) -> None:
        """Inject the webhook client, LLM, processed store and job sink.

        ``still_pending(job_ids)`` says whether any of those jobs is still
        waiting for Telegram delivery or the sheet upload. Without it, alert
        emails are never trashed.
        """

        self._client = client
        self._complete = complete
        self._processed = processed
        self._enqueue_jobs = enqueue_jobs
        self._notify = notify
        self._still_pending = still_pending
        self._ledger = trash_ledger or (
            AlertTrashLedger() if still_pending else None
        )

    def _trash_finished_alerts(self, new: Mapping[str, list[str]]) -> int:
        """Trash alert emails whose jobs are all delivered and in the sheet.

        Jobs that were filtered out never enter the queues, so they count as
        finished. Emails with a job still waiting stay for a later run. Only
        "Job Alerts" emails are listed here (the webhook re-checks the
        label); company replies are never trashed. Any failure (e.g. a
        webhook without the "trash" action yet) keeps the emails.
        """

        if self._still_pending is None or self._ledger is None:
            return 0
        ledger = self._ledger.load()
        ledger.update(new)
        finished = [mid for mid, ids in ledger.items()
                    if not self._still_pending(set(ids))]
        trashed = 0
        if finished:
            try:
                reply = self._client.call("trash", ids=finished)
                trashed = int(reply.get("trashed", 0) or 0)
                for mid in finished:
                    ledger.pop(mid, None)
            except Exception as error:  # noqa: BLE001 - never break the run
                LOGGER.warning(
                    "Could not trash alert emails (kept for next run): %s",
                    type(error).__name__,
                )
        self._ledger.save(ledger)
        return trashed

    def run(self, alert_days: int = ALERT_DAYS, reply_days: int = REPLY_DAYS) -> dict[str, Any]:
        """Process new alert emails and replies; return a summary."""

        done = self._processed.load()
        summary: dict[str, Any] = {"alert_emails": 0, "alert_jobs": 0,
                                   "queued": 0, "status_changes": [],
                                   "unmatched": []}

        alerts = [m for m in self._client.call("alerts", days=alert_days)["messages"]
                  if m["id"] not in done]
        jobs: dict[str, dict[str, Any]] = {}
        email_jobs: dict[str, list[str]] = {}
        for message in alerts:
            parsed = parse_linkedin_alert(message.get("body", ""))
            parsed = fill_missing_with_llm(parsed, message.get("body", ""), self._complete)
            email_jobs[message["id"]] = []
            for item in parsed:
                if item["title"] and item["company"]:
                    job = to_candidate_job(item)
                    jobs[job["id"]] = job
                    email_jobs[message["id"]].append(job["id"])
        summary["alert_emails"] = len(alerts)
        summary["alert_jobs"] = len(jobs)
        summary["queued"] = self._enqueue_jobs(list(jobs.values())) if jobs else 0
        self._processed.add_many(m["id"] for m in alerts)

        replies = [m for m in self._client.call("replies", days=reply_days)["messages"]
                   if m["id"] not in done]
        if replies:
            rows = self._client.call("rows")["rows"]
            updates: list[StatusUpdate] = []
            new_rows: list[list[str]] = []
            for message in replies:
                classification = self._classify(message)
                update, reason = plan_status_update(
                    classification, rows, str(message.get("date", ""))[:10]
                )
                if update:
                    updates.append(update)
                    # Reflect it locally so a second email sees the new status.
                    for row in rows:
                        if row["row"] == update.row:
                            row["status"] = update.status
                elif (
                    reason == "company not in the sheet"
                    and classification.get("kind") in NEW_ROW_STATUS
                ):
                    from integrations.sheets_sync import find_duplicate

                    row = new_application_row(classification, message)
                    existing = find_duplicate(
                        row[0], row[1], row[9], rows, unknown_title_matches=True
                    )
                    if existing and existing.get("row") == -1:
                        continue  # a second email about a row added this run
                    if existing:
                        # Loose company match (e.g. "Pentera Security" vs a
                        # hand-added "Pentera" row): don't add a duplicate,
                        # ask a human to check the status instead.
                        summary["unmatched"].append(
                            f"{row[0]} ({classification.get('kind')}): may be the "
                            f"existing row {existing.get('company')} - "
                            f"{existing.get('title')}; not added"
                        )
                        continue
                    new_rows.append(row)
                    # Reflect it locally so a second email (e.g. LinkedIn
                    # plus the company's own confirmation) doesn't add twice.
                    rows.append({"row": -1, "company": row[0],
                                 "title": row[1], "status": row[11]})
                elif classification.get("kind") in TRANSITIONS and not (
                    reason.startswith("already") or reason.startswith("status is")
                ):
                    # Only report emails that need a human: unknown company,
                    # ambiguous row or missing company. Rows already at (or
                    # past) the status are fine and not worth a message.
                    summary["unmatched"].append(
                        f"{classification.get('kind')}: {classification.get('company')} "
                        f"({message.get('subject', '')[:60]}) - {reason}"
                    )
            if updates:
                self._client.call("set_status", updates=[
                    {"row": u.row, "status": u.status, "expect": u.expect, "note": u.note}
                    for u in updates
                ])
            summary["status_changes"] = [
                f"{u.company} - {u.title}: {u.expect or 'empty'} -> {u.status}"
                for u in updates
            ]
            if new_rows:
                self._client.call("append", rows=new_rows)
            summary["added_rows"] = [f"{r[0]} - {r[1]} ({r[11]})" for r in new_rows]
            self._processed.add_many(m["id"] for m in replies)

        summary["trashed"] = self._trash_finished_alerts(email_jobs)

        added = summary.get("added_rows", [])
        if self._notify and (summary["status_changes"] or summary["unmatched"] or added):
            lines = ["📬 עדכון סטטוס מהמייל:"] + summary["status_changes"]
            if added:
                lines += ["", "נוספו לגיליון (הגשות שלא היו בו):"] + added
            if summary["unmatched"]:
                lines += ["", "לא הותאמו לשורה בגיליון:"] + summary["unmatched"]
            try:
                self._notify("\n".join(lines))
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("Status summary not sent: %s", type(error).__name__)
        return summary

    def _classify(self, message: Mapping[str, Any]) -> dict[str, str]:
        """Ask the LLM what kind of reply this email is."""

        try:
            return json.loads(self._complete(REPLY_PROMPT.format(
                sender=message.get("from", ""),
                subject=message.get("subject", ""),
                body=str(message.get("body", ""))[:4000],
            )))
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Reply classification failed: %s", type(error).__name__)
            return {"kind": "other"}


def build_job_enqueuer() -> Callable[[list[dict[str, Any]]], int]:
    """Return a function that filters and queues LinkedIn jobs like the scan."""

    from analysis.filters.location import LocationFilter
    from integrations.job_details import (
        extract_requirements,
        load_profile,
        make_openai_completion,
    )
    from integrations.sheets_sync import PendingSheetRows, SheetRowPreparer
    from scrapers import orchestrator as scan
    from storage.history import JobHistoryStore
    from storage.queue import PendingAlertQueue

    queue = PendingAlertQueue()
    history = JobHistoryStore()
    location_filter = LocationFilter(strict_mode=False)
    profile = load_profile()
    complete = make_openai_completion()
    # LinkedIn pages are not fetched (LinkedIn forbids scraping), so the
    # sheet row is built from the alert email's fields only.
    sheet_rows = SheetRowPreparer(
        fetch_text=lambda url, fallback: fallback,
        extract=lambda title, text: extract_requirements(title, text, profile, complete),
        store=PendingSheetRows(),
    )

    def enqueue(jobs: list[dict[str, Any]]) -> int:
        queued_ids = {alert.job_id for alert in queue.load()}
        kept: list[dict[str, Any]] = []
        for job in jobs:
            if history.contains(job["id"]) or job["id"] in queued_ids:
                continue
            if not scan.is_relevant_job(job["title"]).allowed:
                continue
            if not location_filter.evaluate(job["title"], job["job_url"], job["location"]).allowed:
                continue
            if not scan.CENTRAL_AREA_FILTER.evaluate(job["location"]).allowed:
                continue
            kept.append(job)
        before = len(queue.load())
        if kept:
            producer = scan.JobProducer(
                queue=queue,
                location_filter=location_filter,
                history_store=history,
                health_store=scan.ScraperHealthStore(scan.HEALTH_FILE),
                reporter=scan.OperationalReporter(None),
                sheet_rows=sheet_rows,
            )
            producer._analyze_and_enqueue(kept)  # same analysis + alert format
        return len(queue.load()) - before

    return enqueue


def build_pending_check() -> Callable[[set[str]], bool]:
    """Return a check: is any of these jobs still waiting for Telegram or the sheet?"""

    from integrations.sheets_sync import PendingSheetRows
    from storage.queue import PendingAlertQueue

    def still_pending(job_ids: set[str]) -> bool:
        waiting = PendingAlertQueue().ids() | set(PendingSheetRows().load())
        return bool(job_ids & waiting)

    return still_pending


def main() -> int:
    """Run one Gmail sync; never fail the whole agent run."""

    load_dotenv()
    from logging_config import configure_logging

    configure_logging()
    client = AppsScriptClient.from_environment()
    if client is None:
        LOGGER.info("📧 Gmail sync skipped: SHEETS_WEBHOOK_URL/SECRET not set.")
        return 0
    from integrations.job_details import make_openai_completion
    from notifications.telegram.bot import TelegramNotifier

    notifier = None
    try:
        notifier = TelegramNotifier.from_environment()
    except ValueError:
        pass
    try:
        summary = GmailSync(
            client=client,
            complete=make_openai_completion(),
            processed=ProcessedEmails(),
            enqueue_jobs=build_job_enqueuer(),
            notify=notifier.send if notifier else None,
            still_pending=build_pending_check(),
        ).run()
    except Exception as error:  # noqa: BLE001
        LOGGER.error("❌ Gmail sync failed: %s: %s", type(error).__name__, error)
        return 1
    finally:
        if notifier is not None:
            notifier.close()
    LOGGER.info("📧 Gmail sync: %s", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
