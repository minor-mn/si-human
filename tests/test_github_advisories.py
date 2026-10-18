import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app import github_advisories as github
from app.comments import _fetch_thread, comment_context
from app.news import JST, Article, NewsItem, _github_articles, fetch_articles, parse_selection


NOW = datetime(2026, 10, 18, 12, tzinfo=JST)


def advisory(number=1, *, published=None, **changes):
    identifier = f"GHSA-abcd-1234-{number:04d}"
    row = {
        "ghsa_id": identifier, "cve_id": f"CVE-2026-{10000 + number}",
        "html_url": f"https://github.com/advisories/{identifier}",
        "summary": f"Package-{number} vulnerability", "description": "Detailed security information.",
        "type": "reviewed", "severity": "high", "withdrawn_at": None,
        "published_at": (published or NOW).isoformat(), "updated_at": NOW.isoformat(),
        "vulnerabilities": [{"package": {"ecosystem": "pip", "name": "sample-package"},
                             "vulnerable_version_range": "<1.2.3", "first_patched_version": "1.2.3"}],
        "references": ["https://example.com/security"],
    }
    return {**row, **changes}


async def fetch(client, now=NOW):
    return await github.fetch_advisories(client, now, minimum=5, cache_seconds=1800)


class AdvisoryParsingTests(unittest.TestCase):
    def test_publication_date_identifiers_severity_and_versions_are_preserved(self):
        old = NOW - timedelta(days=10)
        row = advisory(published=old)
        parsed = github.parse_advisory(row, NOW)
        self.assertEqual(parsed.published_at, old)
        self.assertEqual(parsed.details["updated_at"], NOW.isoformat())
        self.assertEqual(parsed.details["ghsa_id"], row["ghsa_id"])
        self.assertEqual(parsed.details["cve_id"], row["cve_id"])
        self.assertEqual(parsed.details["severity"], "high")
        package = parsed.details["vulnerabilities"][0]
        self.assertEqual(package["vulnerable_version_range"], "<1.2.3")
        self.assertEqual(package["first_patched_version"], "1.2.3")

    def test_unreviewed_withdrawn_future_or_malformed_records_are_not_used(self):
        for row in (
            advisory(type="unreviewed"), advisory(type="malware"), advisory(withdrawn_at=NOW.isoformat()),
            advisory(published=NOW + timedelta(seconds=1)), advisory(published_at="2026-10-18T12:00:00"),
            advisory(published_at="unknown"), advisory(summary=None), advisory(ghsa_id="wrong"),
            advisory(html_url="https://evil.example/advisories/GHSA-abcd-1234-0001"), {}, None,
        ):
            with self.subTest(row=row):
                self.assertIsNone(github.parse_advisory(row, NOW))

    def test_advisory_urls_only_allow_canonical_public_github_advisory_pages(self):
        url = advisory()["html_url"]
        self.assertEqual(github.advisory_url(url + "#details"), url)
        for value in (url.replace("https:", "http:"), url.replace("github.com", "github.com.evil.example"),
                      url.replace("github.com", "user:pass@github.com"), url.replace("github.com", "github.com:8000"),
                      "https://github.com/org/repo/issues/1", "https://[invalid"):
            with self.subTest(url=value):
                self.assertIsNone(github.advisory_url(value))

    def test_selected_summary_retains_structured_advisory_data(self):
        row = advisory()
        article = Article(row["summary"], row["description"], row["html_url"], github.PUBLISHER, NOW,
                          "advisory", github.parse_advisory(row, NOW).details)
        items = parse_selection('{"items":[{"id":1,"summary":"パッケージに脆弱性が報告されました。"}]}', [article])
        self.assertEqual(items[0].advisory, article.advisory)


class AdvisoryFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        github._cache = None
        github._lock = asyncio.Lock()
        patcher = patch.dict("os.environ", {"GITHUB_ADVISORY_TOKEN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_pages_cover_all_recent_records_beyond_one_hundred(self):
        first = [advisory(i, published=NOW - timedelta(minutes=i)) for i in range(100)]
        second = [advisory(100 + i, published=NOW - timedelta(hours=2, minutes=i)) for i in range(7)]
        second.append(advisory(200, published=NOW - timedelta(days=3)))
        next_url = github.API_URL + "?type=reviewed&sort=published&direction=desc&per_page=100&after=next-page"
        def respond(request):
            if request.url.params.get("after"):
                return httpx.Response(200, json=second, headers={"Link": f'<{github.API_URL}?after=unneeded>; rel="next"'})
            self.assertEqual(request.url.params["type"], "reviewed")
            self.assertEqual(request.url.params["is_withdrawn"], "false")
            self.assertEqual(request.url.params["sort"], "published")
            self.assertEqual(request.headers["Accept"], "application/vnd.github+json")
            return httpx.Response(200, json=first, headers={"Link": f'<{next_url}>; rel="next"'})
        handler = Mock(side_effect=respond)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            articles = await _github_articles(client, NOW)
        self.assertEqual(handler.call_count, 2)
        self.assertEqual(len(articles), 107)
        self.assertTrue(all(article.publisher == github.PUBLISHER for article in articles))
        self.assertTrue(all(article.published_at >= NOW - timedelta(hours=24) for article in articles))

    async def test_sparse_source_uses_latest_five_and_cache_rechecks_publication_dates(self):
        rows = [advisory(i, published=NOW - timedelta(days=3, minutes=i)) for i in range(9)]
        handler = Mock(return_value=httpx.Response(200, json=rows))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await _github_articles(client, NOW)
            second = await _github_articles(client, NOW + timedelta(minutes=2))
        self.assertEqual(handler.call_count, 1)
        self.assertEqual([article.url for article in first], [row["html_url"] for row in rows[:5]])
        self.assertEqual(first, second)

    async def test_rate_limit_reset_is_respected_without_immediate_retries(self):
        handler = Mock(return_value=httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("app.github_advisories.time.time", return_value=1000), patch("app.github_advisories.time.monotonic", return_value=10):
                with self.assertLogs("app.github_advisories", level="WARNING"):
                    self.assertEqual(await fetch(client), [])
            with patch("app.github_advisories.time.monotonic", return_value=1811):
                self.assertEqual(await fetch(client), [])
        self.assertEqual(handler.call_count, 1)

    async def test_malformed_page_does_not_silently_return_a_partial_day(self):
        first = [advisory(i) for i in range(5)]
        def respond(request):
            return httpx.Response(200, json={"unexpected": "object"}) if request.url.params.get("after") else httpx.Response(
                200, json=first, headers={"Link": f'<{github.API_URL}?after=next>; rel="next"'},
            )
        handler = Mock(side_effect=respond)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertLogs("app.github_advisories", level="WARNING"):
                self.assertEqual(await fetch(client), [])
        self.assertEqual(handler.call_count, 2)

    async def test_tokens_and_redirects_never_leave_the_official_api(self):
        for external in ("https://example.com/advisories", "https://api.github.com.evil.example/advisories", "http://api.github.com/advisories"):
            github._cache = None
            handler = Mock(return_value=httpx.Response(200, json=[advisory()], headers={"Link": f'<{external}>; rel="next"'}))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with patch.dict("os.environ", {"GITHUB_ADVISORY_TOKEN": "test-token-only"}), self.assertLogs("app.github_advisories", level="WARNING"):
                    self.assertEqual(await fetch(client), [])
            self.assertEqual(handler.call_count, 1)
            self.assertEqual(handler.call_args.args[0].headers["Authorization"], "Bearer test-token-only")
        github._cache = None
        handler = Mock(return_value=httpx.Response(302, headers={"Location": "https://example.com/"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with self.assertLogs("app.github_advisories", level="WARNING"):
                self.assertEqual(await fetch(client), [])
        self.assertEqual(handler.call_count, 1)

    async def test_all_three_sources_are_merged_and_api_failure_keeps_rss_topics(self):
        slashdot = [Article("AI", "説明", "https://slashdot.org/story/1", "Slashdot", NOW)]
        disclosure = [Article("Report", "説明", "https://seclists.org/fulldisclosure/2026/Oct/1", "Full Disclosure", NOW)]
        for status in (200, 503):
            github._cache = None
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=[advisory()])))
            with patch("app.news.httpx.AsyncClient", return_value=client), patch("app.news._fetch_feed", AsyncMock(side_effect=[slashdot, disclosure])):
                articles = await fetch_articles(NOW)
            self.assertEqual({article.publisher for article in articles}, {"Slashdot", "Full Disclosure", github.PUBLISHER} if status == 200 else {"Slashdot", "Full Disclosure"})

    async def test_advisories_have_no_comment_fetch_failure_or_fabricated_discussion(self):
        row = advisory()
        article = Article(row["summary"], row["description"], row["html_url"], github.PUBLISHER, NOW, "advisory")
        handler = Mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            thread = await _fetch_thread(client, NewsItem("脆弱性が報告されました。", article.source()))
        handler.assert_not_called()
        self.assertEqual(thread.status, "not_applicable")
        self.assertFalse(thread.partial)
        self.assertEqual(thread.comments, [])
        self.assertIn("コメントを取得対象にしていません", comment_context([thread]))
