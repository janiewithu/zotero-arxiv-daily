"""Weekly IEEE venue discovery and Zotero ingestion.

IEEE Xplore is used for authoritative venue metadata. OpenAlex is queried only
to locate a lawful open-access PDF. Non-OA records retain their DOI and IEEE
Xplore landing page and are never downloaded through an authenticated session.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
from loguru import logger
from omegaconf import OmegaConf
from pyzotero import zotero


IEEE_SEARCH_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
OPENALEX_WORK_URL = "https://api.openalex.org/works/https://doi.org/{}"
USER_AGENT = "zotero-arxiv-daily/weekly-venues (metadata and OA discovery)"


@dataclass(frozen=True)
class VenuePaper:
    venue_key: str
    venue_name: str
    title: str
    authors: list[str]
    abstract: str
    doi: str
    ieee_url: str
    article_number: str
    publication_date: str
    content_type: str
    volume: str = ""
    issue: str = ""
    start_page: str = ""
    end_page: str = ""


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def normalize_doi(value: str) -> str:
    value = (value or "").strip().lower()
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)


def parse_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, got {value!r}")


def matches_keywords(paper: VenuePaper, keywords: Iterable[str]) -> bool:
    terms = [term.strip().lower() for term in keywords if term.strip()]
    if not terms:
        return True
    haystack = f"{paper.title}\n{paper.abstract}".lower()
    return any(term in haystack for term in terms)


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    # Month-only and year-only values are not precise enough for a weekly
    # cutoff. Return None for them so deduplication, rather than an invented
    # first-of-period date, controls whether they are considered.
    for fmt in ("%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        return date(*(int(part) for part in match.groups()))
    return None


def is_within_lookback(paper: VenuePaper, lookback_days: int, today: date | None = None) -> bool:
    parsed = _parse_date(paper.publication_date)
    if parsed is None:
        # IEEE conference records sometimes expose only a year. Deduplication
        # and per-venue caps keep the first run bounded in that case.
        return True
    today = today or datetime.now(timezone.utc).date()
    return parsed >= today - timedelta(days=lookback_days)


class IeeeXploreClient:
    def __init__(self, api_key: str, timeout: int = 45):
        if not api_key:
            raise ValueError("IEEE_API_KEY is required for weekly venue retrieval")
        self.api_key = api_key
        self.timeout = timeout

    def search_venue(
        self,
        venue_key: str,
        publication_title: str,
        max_records: int,
        lookback_days: int,
    ) -> list[VenuePaper]:
        today = datetime.now(timezone.utc).date()
        params = {
            "apikey": self.api_key,
            "format": "json",
            "publication_title": publication_title,
            # IEEE defines start_date/end_date as insertion-date filters in
            # YYYYMMDD format, which makes this a true weekly delta query.
            "start_date": (today - timedelta(days=lookback_days)).strftime("%Y%m%d"),
            "end_date": today.strftime("%Y%m%d"),
            "max_records": min(max_records, 200),
            "sort_order": "desc",
            "sort_field": "article_number",
        }
        response = requests.get(
            IEEE_SEARCH_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        response.raise_for_status()
        records = response.json().get("articles", [])
        return [self._convert_record(venue_key, publication_title, record) for record in records]

    @staticmethod
    def _convert_record(venue_key: str, venue_name: str, record: dict) -> VenuePaper:
        author_rows = (record.get("authors") or {}).get("authors") or []
        authors = [row.get("full_name", "").strip() for row in author_rows if row.get("full_name")]
        article_number = str(record.get("article_number") or "")
        ieee_url = record.get("html_url") or (
            f"https://ieeexplore.ieee.org/document/{article_number}" if article_number else ""
        )
        return VenuePaper(
            venue_key=venue_key,
            venue_name=record.get("publication_title") or venue_name,
            title=(record.get("title") or "").strip(),
            authors=authors,
            abstract=(record.get("abstract") or "").strip(),
            doi=normalize_doi(record.get("doi") or ""),
            ieee_url=ieee_url,
            article_number=article_number,
            publication_date=str(record.get("insert_date") or record.get("publication_date") or record.get("publication_year") or ""),
            content_type=str(record.get("content_type") or ""),
            volume=str(record.get("volume") or ""),
            issue=str(record.get("issue") or ""),
            start_page=str(record.get("start_page") or ""),
            end_page=str(record.get("end_page") or ""),
        )


class OpenAccessLocator:
    def __init__(self, mailto: str = "", timeout: int = 30):
        self.mailto = mailto
        self.timeout = timeout

    def pdf_url(self, doi: str) -> str | None:
        if not doi:
            return None
        params = {"mailto": self.mailto} if self.mailto else None
        response = requests.get(
            OPENALEX_WORK_URL.format(quote(doi, safe="")),
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        work = response.json()
        if not (work.get("open_access") or {}).get("is_oa"):
            return None
        locations = [work.get("best_oa_location"), *(work.get("locations") or [])]
        for location in locations:
            if location and location.get("pdf_url"):
                return location["pdf_url"]
        return None

    def download_pdf(self, url: str, destination: Path) -> bool:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=90, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        first = b""
        with destination.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                if not first:
                    first = chunk[:5]
                handle.write(chunk)
        if "pdf" not in content_type and first != b"%PDF-":
            destination.unlink(missing_ok=True)
            return False
        return True


class ZoteroWeeklyInbox:
    def __init__(self, user_id: str, api_key: str, collection_path: str, dry_run: bool):
        self.client = zotero.Zotero(user_id, "user", api_key)
        self.collection_path = [part for part in collection_path.split("/") if part]
        self.dry_run = dry_run
        self.existing_dois: set[str] = set()
        self.existing_titles: set[str] = set()
        self.target_collection_key: str | None = None

    def prepare(self) -> None:
        items = self.client.everything(self.client.items(itemType="journalArticle || conferencePaper || preprint"))
        self.existing_dois = {normalize_doi(item["data"].get("DOI", "")) for item in items}
        self.existing_dois.discard("")
        self.existing_titles = {normalize_title(item["data"].get("title", "")) for item in items}
        self.existing_titles.discard("")
        self.target_collection_key = self._ensure_collection_path()

    def _ensure_collection_path(self) -> str | None:
        parent: str | bool = False
        collections = self.client.everything(self.client.collections())
        for name in self.collection_path:
            found = next(
                (
                    row for row in collections
                    if row["data"].get("name") == name
                    and row["data"].get("parentCollection", False) == parent
                ),
                None,
            )
            if found:
                parent = found["key"]
                continue
            if self.dry_run:
                logger.info(f"[dry-run] Would create Zotero collection: {'/'.join(self.collection_path)}")
                return None
            result = self.client.create_collections([{"name": name, "parentCollection": parent}])
            successful = result.get("successful", {})
            if not successful:
                raise RuntimeError(f"Failed to create Zotero collection {name}: {result}")
            created = next(iter(successful.values()))
            parent = created.get("key") or created.get("data", {}).get("key")
            if not parent:
                raise RuntimeError(f"Zotero did not return a key for collection {name}: {result}")
            collections.append({"key": parent, "data": {"name": name, "parentCollection": created.get("data", {}).get("parentCollection", False)}})
        return str(parent) if parent else None

    def contains(self, paper: VenuePaper) -> bool:
        return bool(
            (paper.doi and paper.doi in self.existing_dois)
            or normalize_title(paper.title) in self.existing_titles
        )

    def add(self, paper: VenuePaper, pdf_path: Path | None = None) -> str | None:
        if self.dry_run:
            logger.info(f"[dry-run] Would add [{paper.venue_key}] {paper.title}")
            return None
        item_type = "conferencePaper" if "conference" in paper.content_type.lower() or paper.venue_key in {
            "ISSCC", "VLSI", "CICC", "RFIC", "ESSCIRC"
        } else "journalArticle"
        item = self.client.item_template(item_type)
        item["title"] = paper.title
        item["abstractNote"] = paper.abstract
        item["creators"] = [self._creator(name) for name in paper.authors]
        item["date"] = paper.publication_date
        item["DOI"] = paper.doi
        item["url"] = paper.ieee_url
        item["volume"] = paper.volume
        item["issue"] = paper.issue
        if paper.start_page and paper.end_page:
            item["pages"] = f"{paper.start_page}-{paper.end_page}"
        elif paper.start_page:
            item["pages"] = paper.start_page
        if item_type == "conferencePaper":
            item["proceedingsTitle"] = paper.venue_name
        else:
            item["publicationTitle"] = paper.venue_name
        item["collections"] = [self.target_collection_key] if self.target_collection_key else []
        item["tags"] = [{"tag": "weekly-venue"}, {"tag": paper.venue_key}]
        self.client.check_items([item])
        result = self.client.create_items([item])
        successful = result.get("successful", {})
        if not successful:
            raise RuntimeError(f"Failed to create Zotero item {paper.title}: {result}")
        created = next(iter(successful.values()))
        item_key = created.get("key") or created.get("data", {}).get("key")
        if pdf_path and item_key:
            upload = self.client.attachment_simple([str(pdf_path)], parentid=item_key)
            logger.info(f"Uploaded OA PDF for {paper.title}: {upload}")
        self.existing_titles.add(normalize_title(paper.title))
        if paper.doi:
            self.existing_dois.add(paper.doi)
        return item_key

    @staticmethod
    def _creator(name: str) -> dict:
        parts = name.rsplit(" ", 1)
        if len(parts) == 2:
            return {"creatorType": "author", "firstName": parts[0], "lastName": parts[1]}
        return {"creatorType": "author", "name": name}


def load_config(path: str):
    config = OmegaConf.load(path)
    return OmegaConf.to_container(config, resolve=True)


def run(config: dict, dry_run_override: bool | None = None) -> dict:
    weekly = config["weekly_venues"]
    dry_run = parse_bool(weekly.get("dry_run"), default=True) if dry_run_override is None else dry_run_override
    ieee = IeeeXploreClient(os.environ.get("IEEE_API_KEY", ""))
    inbox = ZoteroWeeklyInbox(
        str(config["zotero"]["user_id"]),
        str(config["zotero"]["api_key"]),
        weekly.get("target_collection", "Journal-Conference/latest"),
        dry_run,
    )
    inbox.prepare()
    oa = OpenAccessLocator(weekly.get("openalex_mailto", ""))
    stats = {
        "queries_ok": 0,
        "retrieved": 0,
        "matched": 0,
        "duplicate": 0,
        "added": 0,
        "pdf": 0,
        "errors": 0,
    }
    max_records = int(weekly.get("max_records_per_venue", 100))
    max_additions = int(weekly.get("max_additions_per_run", 30))
    lookback_days = int(weekly.get("lookback_days", 14))
    keywords = weekly.get("keywords", [])
    venues = weekly.get("venues", {})
    with tempfile.TemporaryDirectory(prefix="weekly-venues-") as temp_dir:
        for venue_key, publication_titles in venues.items():
            titles = publication_titles if isinstance(publication_titles, list) else [publication_titles]
            for publication_title in titles:
                try:
                    papers = ieee.search_venue(
                        venue_key,
                        publication_title,
                        max_records,
                        lookback_days,
                    )
                except Exception as exc:
                    logger.error(f"Failed to retrieve {venue_key} ({publication_title}): {exc}")
                    stats["errors"] += 1
                    continue
                stats["queries_ok"] += 1
                stats["retrieved"] += len(papers)
                for paper in papers:
                    if stats["added"] >= max_additions:
                        break
                    if not is_within_lookback(paper, lookback_days) or not matches_keywords(paper, keywords):
                        continue
                    stats["matched"] += 1
                    if inbox.contains(paper):
                        stats["duplicate"] += 1
                        continue
                    pdf_path = None
                    try:
                        pdf_url = oa.pdf_url(paper.doi)
                        if pdf_url:
                            candidate = Path(temp_dir) / f"{paper.article_number or normalize_title(paper.title)[:40]}.pdf"
                            if oa.download_pdf(pdf_url, candidate):
                                pdf_path = candidate
                                stats["pdf"] += 1
                    except Exception as exc:
                        logger.warning(f"OA PDF lookup failed for {paper.title}: {exc}")
                    try:
                        inbox.add(paper, pdf_path)
                        stats["added"] += 1
                    except Exception as exc:
                        logger.error(f"Failed to add {paper.title}: {exc}")
                        stats["errors"] += 1
    if not stats["queries_ok"]:
        raise RuntimeError("Every IEEE venue query failed; verify IEEE_API_KEY and API access")
    logger.info(f"Weekly venue summary: {stats}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import weekly IEEE venue papers into Zotero")
    parser.add_argument("--config", default="config/weekly_venues.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Never modify Zotero")
    args = parser.parse_args()
    config = load_config(args.config)
    run(config, dry_run_override=True if args.dry_run else None)


if __name__ == "__main__":
    main()
