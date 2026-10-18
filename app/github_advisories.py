"""GitHubの公式APIからレビュー済みの公開脆弱性情報を読む。"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)
API_URL = "https://api.github.com/advisories"
PUBLISHER = "GitHub Advisory Database"
_cache: tuple[float, list[dict] | None] | None = None
_lock = asyncio.Lock()


@dataclass
class Advisory:
    title: str
    description: str
    url: str
    published_at: datetime
    details: dict


def advisory_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password
                or parsed.port not in {None, 443}
                or not re.fullmatch(r"/advisories/GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}", parsed.path)):
            return None
        return urlunsplit(("https", "github.com", parsed.path, "", ""))
    except ValueError:
        return None


def _valid_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "api.github.com" and parsed.path == "/advisories"
                and not parsed.username and not parsed.password and not parsed.fragment and parsed.port in {None, 443})
    except ValueError:
        return False


def parse_advisory(row: dict, now: datetime) -> Advisory | None:
    if not isinstance(row, dict) or row.get("type") != "reviewed" or row.get("withdrawn_at") is not None:
        return None
    try:
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        url = advisory_url(row["html_url"])
        title = row["summary"]
        if (published.tzinfo is None or published > now or not url or row.get("ghsa_id") != url.rsplit("/", 1)[-1]
                or not isinstance(title, str) or not title.strip()):
            return None
        description = row.get("description") or ""
        if not isinstance(description, str):
            return None
        details = {key: row.get(key) for key in (
            "ghsa_id", "cve_id", "severity", "updated_at", "github_reviewed_at", "vulnerabilities",
            "cvss", "cvss_severities", "cwes", "references",
        )}
        return Advisory(" ".join(title.split()), description, url, published, details)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _retry_seconds(response: httpx.Response, minimum: int) -> float:
    delay = float(minimum)
    retry = response.headers.get("retry-after")
    if retry:
        try:
            delay = max(delay, float(retry))
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(retry).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        try:
            delay = max(delay, float(response.headers["x-ratelimit-reset"]) - time.time())
        except (KeyError, ValueError):
            pass
    return delay


async def fetch_advisories(
    client: httpx.AsyncClient, now: datetime, *, minimum: int, cache_seconds: int,
) -> list[Advisory]:
    global _cache
    async with _lock:
        if _cache and time.monotonic() < _cache[0]:
            return [advisory for row in (_cache[1] or []) if (advisory := parse_advisory(row, now)) is not None]
        rows: list[dict] = []
        retry_seconds = float(cache_seconds)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        token = os.environ.get("GITHUB_ADVISORY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = API_URL
        params = {"type": "reviewed", "is_withdrawn": "false", "sort": "published", "direction": "desc", "per_page": 100}
        visited: set[str] = set()
        try:
            while url:
                if not _valid_api_url(url) or url in visited:
                    raise ValueError("GitHub Advisoryの次ページURLが不正です")
                visited.add(url)
                # 認証ヘッダーはこのAPIだけに送り、リダイレクト先へは送らない。
                async with client.stream("GET", url, params=params, headers=headers, follow_redirects=False) as response:
                    retry_seconds = _retry_seconds(response, cache_seconds)
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8_000_000:
                            raise ValueError("GitHub Advisoryの応答が大きすぎます")
                    payload = json.loads(data)
                    if not isinstance(payload, list):
                        raise ValueError("GitHub Advisoryの応答形式が不正です")
                    rows.extend(row for row in payload if isinstance(row, dict))
                    advisories = [advisory for row in rows if (advisory := parse_advisory(row, now)) is not None]
                    unique = {advisory.url: advisory for advisory in advisories}
                    # 公開日時の降順。24時間分の末尾と最新5件が揃うまでページを読む。
                    if len(unique) >= minimum and any(advisory.published_at < now - timedelta(hours=24) for advisory in unique.values()):
                        break
                    next_url = response.links.get("next", {}).get("url")
                    if not payload or not next_url:
                        break
                    url = urljoin(str(response.url), next_url)
                    params = None
            _cache = (time.monotonic() + cache_seconds, rows)
            return advisories
        except (httpx.HTTPError, ValueError) as exc:
            _cache = (time.monotonic() + retry_seconds, None)
            log.warning("GitHub Advisory Databaseの取得に失敗しました: %s", exc)
            return []
