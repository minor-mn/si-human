import os
import unittest
from dataclasses import asdict
from unittest.mock import patch

import httpx

from app.vuln_watch import BriefingUnavailable, fetch_briefing
from test_news import BRIEFING, THREAD


class ApiReaderTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, response, **env):
        requests = []
        def handler(request):
            requests.append(request)
            return response
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        with patch.dict(os.environ, {"VULN_WATCH_API_URL": "http://watch.test/watch/api/briefing", **env}, clear=True), patch("app.vuln_watch.httpx.AsyncClient", return_value=client):
            data = await fetch_briefing()
        return data, requests

    def payload(self):
        return {"version": 1, "news": asdict(BRIEFING), "threads": [asdict(THREAD)], "warnings": ["更新遅延"]}

    async def test_reads_only_configured_api_and_preserves_comments(self):
        result, requests = await self.read(httpx.Response(200, json=self.payload()), VULN_WATCH_SOURCES="cve,slashdot")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/watch/api/briefing")
        self.assertEqual(requests[0].url.params["sources"], "cve,slashdot")
        self.assertEqual(result.threads[0].comments[0].text, "COMMENT_DATA_ONLY")
        self.assertEqual(result.warnings, ["更新遅延"])

    async def test_optional_hours_and_limit_are_sent_and_preserved_in_context(self):
        data = self.payload()
        data["news"].update(selection_policy="per_source_window", hours=48, limit=10)
        result, requests = await self.read(httpx.Response(200, json=data), VULN_WATCH_HOURS="48", VULN_WATCH_LIMIT="10")
        self.assertEqual(dict(requests[0].url.params), {"hours": "48", "limit": "10"})
        context = result.news.conversation_context()
        self.assertIn("過去48時間以内", context)
        self.assertIn("最大10件", context)
        self.assertNotIn("5件未満なら", context)

    async def test_default_request_omits_selection_parameters(self):
        _, requests = await self.read(httpx.Response(200, json=self.payload()))
        self.assertEqual(dict(requests[0].url.params), {})

    async def test_invalid_json_http_failure_redirect_and_unknown_version_fail(self):
        bad = self.payload()
        bad["version"] = 2
        for response in (httpx.Response(200, text="<!doctype html>"), httpx.Response(503), httpx.Response(302, headers={"Location": "https://external.test"}), httpx.Response(200, json=bad)):
            with self.subTest(status=response.status_code):
                with self.assertRaises(BriefingUnavailable):
                    await self.read(response)

    async def test_comment_mismatch_reply_as_topic_and_unsafe_urls_fail(self):
        for change in ("mismatch", "reply", "url"):
            data = self.payload()
            if change == "mismatch":
                data["threads"] = []
            elif change == "reply":
                data["news"]["items"][0]["source"]["kind"] = "mailing_list_reply"
            else:
                data["news"]["items"][0]["source"]["url"] = "javascript:alert(1)"
            with self.assertRaises(BriefingUnavailable):
                await self.read(httpx.Response(200, json=data))

    async def test_oversized_response_fails(self):
        with patch("app.vuln_watch.MAX_RESPONSE_BYTES", 10):
            with self.assertRaises(BriefingUnavailable):
                await self.read(httpx.Response(200, json=self.payload()))
