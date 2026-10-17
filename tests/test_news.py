import importlib
import asyncio
import json
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.news import (
    JST, Article, NewsBriefing, NewsItem, build_news_prompt,
    _feed_cache, _feed_locks, _fetch_feed, fetch_articles, get_briefing, parse_feed, parse_selection,
)
from app.persona import Persona
from app.comments import Comment, ThreadComments
from app.preparation_store import PreparationStore


NOW = datetime(2026, 10, 18, 0, 30, tzinfo=JST)
ARTICLE = Article("報道記事", "記事の要約。", "https://example.com/news", "報道機関", NOW)
SOURCE = ARTICLE.source()
BRIEFING = NewsBriefing(
    since="2026-10-17T00:30:00+09:00",
    as_of="2026-10-18T00:30:00+09:00",
    text="この24時間のニュースです。",
    items=[NewsItem("記事の要約。", SOURCE)],
)


def feed(entries):
    items = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
        f"<pubDate>{escape(date)}</pubDate><description>&lt;b&gt;要約&lt;/b&gt;</description></item>"
        for title, url, date in entries
    )
    return f"<rss><channel>{items}</channel></rss>".encode()


class FakeLLM:
    name = "test-model"

    def __init__(self, payload=None, error=None):
        self.payload = payload or json.dumps({"items": [{"id": 1, "summary": "記事の要約。"}]})
        self.error = error
        self.calls = 0

    async def stream(self, system, turns, cache_key):
        self.calls += 1
        if self.error:
            raise self.error
        yield self.payload[:10]
        yield self.payload[10:]


class NewsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _feed_cache.clear()
        _feed_locks.clear()

    def test_period_is_rolling_24_hours_in_japan(self):
        prompt = build_news_prompt(datetime(2026, 10, 17, 15, 30, tzinfo=timezone.utc), [ARTICLE])
        self.assertIn("2026-10-18T00:30:00+09:00", prompt)
        self.assertIn("2026-10-17T00:30:00+09:00", prompt)
        self.assertIn("日付の「今日」ではなく", prompt)

    def test_feed_excludes_old_future_and_unparseable_dates(self):
        data = feed([
            ("境界の24時間前", "https://example.com/edge", format_datetime(NOW - timedelta(hours=24))),
            ("日付変更前", "https://example.com/yesterday", format_datetime(NOW - timedelta(hours=1))),
            ("古い記事", "https://example.com/old", format_datetime(NOW - timedelta(hours=24, seconds=1))),
            ("未来の記事", "https://example.com/future", format_datetime(NOW + timedelta(seconds=1))),
            ("日付不明", "https://example.com/unknown", ""),
            ("時差不明", "https://example.com/naive", "Sun, 18 Oct 2026 00:00:00"),
        ])
        articles = parse_feed(data, "報道機関", NOW)
        self.assertEqual([article.title for article in articles], ["境界の24時間前", "日付変更前"])
        self.assertEqual(articles[0].summary, "要約")

    def test_feed_rejects_unsafe_links(self):
        data = feed([
            ("不正", "javascript:alert(1)", format_datetime(NOW)),
            ("不正", "https://user:password@example.com/", format_datetime(NOW)),
            ("不正", "https://[invalid", format_datetime(NOW)),
        ])
        self.assertEqual(parse_feed(data, "報道機関", NOW), [])

    def test_feed_rejects_dtd(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY x "test">]><rss/>', "報道機関", NOW)

    def test_atom_prefers_published_time_and_alternate_link(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>AI discussion</title>
          <link rel="self" href="https://example.com/feed"/>
          <link rel="alternate" href="https://example.com/posts/123/"/>
          <published>2026-10-17T12:00:00Z</published><updated>2026-10-17T15:00:00Z</updated>
          <content type="html">&lt;p&gt;Post summary&lt;/p&gt;</content>
        </entry><entry><title>Old post with recent activity</title>
          <link href="https://example.com/posts/old/"/>
          <published>2026-10-10T12:00:00Z</published><updated>2026-10-17T15:00:00Z</updated>
        </entry></feed>'''
        articles = parse_feed(data, "コミュニティ", NOW, "discussion")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://example.com/posts/123/")
        self.assertEqual(articles[0].summary, "Post summary")
        self.assertEqual(articles[0].source().kind, "discussion")

    def test_slashdot_atom_and_rdf_dates(self):
        feeds = (
            b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI</title>
              <link href="https://slashdot.org/story/1"/>
              <updated>2026-10-17T12:00:00Z</updated></entry></feed>''',
            b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
              xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
              <item><title>AI</title><link>https://slashdot.org/story/1</link>
              <dc:date>2026-10-17T12:00:00Z</dc:date></item></rdf:RDF>''',
        )
        for data in feeds:
            with self.subTest(data=data):
                articles = parse_feed(data, "Slashdot", NOW, "discussion")
                self.assertEqual(len(articles), 1)
                self.assertEqual(articles[0].published_at, datetime(2026, 10, 17, 21, tzinfo=JST))

    async def test_cached_feed_is_filtered_again_at_connection_time(self):
        data = feed([("記事", SOURCE.url, format_datetime(NOW - timedelta(hours=23, minutes=59)))])
        handler = Mock(return_value=httpx.Response(200, content=data))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await _fetch_feed(client, "test", "https://example.com/feed", NOW)
            second = await _fetch_feed(client, "test", "https://example.com/feed", NOW + timedelta(minutes=2))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(handler.call_count, 1)

    async def test_simultaneous_connections_do_not_repeat_feed_request(self):
        data = feed([("記事", SOURCE.url, format_datetime(NOW))])
        handler = Mock(return_value=httpx.Response(200, content=data))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results = await asyncio.gather(*(
                _fetch_feed(client, "test", "https://example.com/feed", NOW) for _ in range(3)
            ))
        self.assertEqual([len(result) for result in results], [1, 1, 1])
        self.assertEqual(handler.call_count, 1)

    async def test_rate_limit_respects_retry_after(self):
        handler = Mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("app.news.time.monotonic", return_value=100), self.assertLogs("app.news", level="WARNING"):
                self.assertEqual(await _fetch_feed(client, "Slashdot", "https://example.com/feed", NOW), [])
            with patch("app.news.time.monotonic", return_value=1901):
                self.assertEqual(await _fetch_feed(client, "Slashdot", "https://example.com/feed", NOW), [])
        self.assertEqual(handler.call_count, 1)

    async def test_unavailable_slashdot_returns_no_articles(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        with patch("app.news.httpx.AsyncClient", return_value=client), self.assertLogs("app.news", level="WARNING"):
            articles = await fetch_articles(NOW)
        self.assertEqual(articles, [])

    async def test_only_slashdot_is_requested(self):
        data = feed([("記事", "https://slashdot.org/story/test", format_datetime(NOW))])
        handler = Mock(return_value=httpx.Response(200, content=data))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("app.news.httpx.AsyncClient", return_value=client):
            articles = await fetch_articles(NOW)
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(handler.call_args.args[0].url.host, "rss.slashdot.org")
        self.assertEqual(articles[0].publisher, "Slashdot")
        self.assertEqual(articles[0].kind, "discussion")

    def test_model_cannot_choose_an_unknown_or_duplicate_source(self):
        for selections in (
            [{"id": 99, "summary": "捏造した出典"}],
            [{"id": True, "summary": "不正な番号"}],
            [{"id": 1, "summary": "一つ"}, {"id": 1, "summary": "重複"}],
            [{"id": 1, "summary": ""}],
            [],
        ):
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                parse_selection(json.dumps({"items": selections}), [ARTICLE])

    def test_selected_source_comes_from_feed_and_keeps_publication_date(self):
        selected = parse_selection('{"items":[{"id":1,"summary":"要約。","url":"https://fake.example"}]}', [ARTICLE])
        self.assertEqual(selected[0].source, SOURCE)
        self.assertEqual(selected[0].summary, "要約。")

    def test_four_trends_are_allowed_but_five_are_rejected(self):
        articles = [Article(f"記事{i}", "要約", f"https://example.com/{i}", "test", NOW) for i in range(5)]
        selections = [{"id": i, "summary": "話題です。"} for i in range(1, 6)]
        self.assertEqual(len(parse_selection(json.dumps({"items": selections[:4]}), articles)), 4)
        with self.assertRaises(ValueError):
            parse_selection(json.dumps({"items": selections}), articles)

    async def test_briefing_keeps_exact_period_and_combines_streamed_json(self):
        llm = FakeLLM()
        with patch("app.news.fetch_articles", AsyncMock(return_value=[ARTICLE])), patch("app.news.datetime") as clock:
            clock.now.return_value = NOW
            briefing = await get_briefing(llm)
        self.assertEqual(briefing.since, BRIEFING.since)
        self.assertEqual(briefing.as_of, BRIEFING.as_of)
        self.assertEqual(briefing.items[0].source, SOURCE)
        self.assertIn("記事の要約。", briefing.text)
        self.assertNotIn(SOURCE.url, briefing.text)

    async def test_missing_feed_does_not_call_llm(self):
        llm = FakeLLM()
        with patch("app.news.fetch_articles", AsyncMock(return_value=[])), self.assertLogs("app.news", level="WARNING"):
            self.assertIsNone(await get_briefing(llm))
        self.assertEqual(llm.calls, 0)

    async def test_generation_failure_or_timeout_allows_normal_greeting(self):
        for error in (RuntimeError("LLM unavailable"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                with patch("app.news.fetch_articles", AsyncMock(return_value=[ARTICLE])), self.assertLogs("app.news", level="WARNING"):
                    self.assertIsNone(await get_briefing(FakeLLM(error=error)))

    async def test_invalid_generated_json_is_not_spoken(self):
        with patch("app.news.fetch_articles", AsyncMock(return_value=[ARTICLE])), self.assertLogs("app.news", level="WARNING"):
            self.assertIsNone(await get_briefing(FakeLLM(payload="JSONではない回答")))

    async def test_each_new_briefing_fetches_again(self):
        fetch = AsyncMock(return_value=[ARTICLE])
        with patch("app.news.fetch_articles", fetch):
            await get_briefing(FakeLLM())
            await get_briefing(FakeLLM())
        self.assertEqual(fetch.await_count, 2)


class SessionNewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        persona = Persona(
            name="テスト", age=30, gender="女性", personality="", speaking_style="",
            background="", greeting="こんにちは。", avatar_id="avatar", voice_id="voice", appearance="",
        )
        with patch("app.persona.load_persona", return_value=persona):
            self.server = importlib.import_module("app.server")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for name, value in {
            "conversations": {}, "preparations": {},
            "preparation_store": PreparationStore(self.directory),
            "persona": persona, "system_prompt": "人物設定", "news_on_connect": True,
            "default_provider": Mock(return_value="openai"),
            "get_llm": Mock(return_value=NS(name="test-model")),
            "get_briefing": AsyncMock(return_value=BRIEFING),
            "fetch_threads": AsyncMock(return_value=[ThreadComments(
                SOURCE.title, SOURCE.url, BRIEFING.as_of, "available",
                [Comment("1", "意見", "COMMENT_DATA_ONLY", SOURCE.url)],
            )]),
        }.items():
            patcher = patch.object(self.server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.server.anam, "create_session_token", AsyncMock(return_value="test-token"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def prepare(self):
        response = await self.client.post("/api/prepare", json={"provider": "openai"})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def connect(self, preparation):
        return await self.client.post("/api/session", json={
            "preparation_id": preparation["preparation_id"], "provider": "openai",
        })

    async def test_preparation_stores_news_and_comments_without_connecting_avatar(self):
        body = await self.prepare()
        self.assertEqual(body["news"]["items"][0]["source"]["url"], SOURCE.url)
        self.assertNotIn("session_token", body)
        self.assertNotIn("COMMENT_DATA_ONLY", json.dumps(body))
        prepared = self.server.preparations[body["preparation_id"]]
        self.assertEqual(prepared.greeting, "こんにちは。\n" + BRIEFING.text)
        self.assertIn("COMMENT_DATA_ONLY", prepared.conversation.pending_comment_context)
        self.assertNotIn("COMMENT_DATA_ONLY", prepared.conversation.system_prompt)
        self.assertEqual(self.server.conversations, {})
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_connect_uses_prepared_data_without_fetch_or_summary(self):
        prepared = await self.prepare()
        self.server.get_briefing.reset_mock()
        self.server.fetch_threads.reset_mock()
        response = await self.connect(prepared)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["greeting"], "こんにちは。\n" + BRIEFING.text)
        self.assertEqual(body["session_token"], "test-token")
        self.assertNotIn("COMMENT_DATA_ONLY", response.text)
        self.assertIn(BRIEFING.text, self.server.conversations[body["conversation_id"]].system_prompt)
        self.assertNotIn(prepared["preparation_id"], self.server.preparations)
        self.server.get_briefing.assert_not_awaited()
        self.server.fetch_threads.assert_not_awaited()
        self.server.anam.create_session_token.assert_awaited_once()

    async def test_direct_connection_requires_preparation(self):
        response = await self.client.post("/api/session", json={})
        self.assertEqual(response.status_code, 409)
        self.server.anam.create_session_token.assert_not_awaited()
        self.server.get_briefing.assert_not_awaited()

    async def test_resuming_does_not_prepare_or_repeat_opening(self):
        prepared = await self.prepare()
        first = (await self.connect(prepared)).json()
        second = (await self.client.post("/api/session", json={"conversation_id": first["conversation_id"]})).json()
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertIsNone(second["greeting"])
        self.assertIsNone(second["news"])
        self.assertEqual(self.server.get_briefing.await_count, 1)
        self.assertEqual(self.server.fetch_threads.await_count, 1)
        self.assertEqual(self.server.anam.create_session_token.await_count, 2)

    async def test_missing_news_does_not_mark_preparation_ready(self):
        self.server.get_briefing.return_value = None
        response = await self.client.post("/api/prepare", json={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.server.preparations, {})
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_comment_failure_is_visible_without_connecting_avatar(self):
        self.server.fetch_threads.side_effect = RuntimeError("comments unavailable")
        with self.assertLogs("app.server", level="WARNING"):
            prepared = await self.prepare()
        self.assertTrue(prepared["warnings"])
        self.server.anam.create_session_token.assert_not_awaited()
        response = await self.connect(prepared)
        self.assertEqual(response.status_code, 200)
        self.assertIn(BRIEFING.text, response.json()["greeting"])

    async def test_unavailable_comment_thread_is_reported_in_preparation(self):
        self.server.fetch_threads.return_value = [ThreadComments(SOURCE.title, SOURCE.url, BRIEFING.as_of, "unavailable")]
        prepared = await self.prepare()
        self.assertTrue(prepared["warnings"])

    async def test_disabled_news_still_uses_two_steps_without_search(self):
        self.server.news_on_connect = False
        prepared = await self.prepare()
        self.server.anam.create_session_token.assert_not_awaited()
        response = await self.connect(prepared)
        self.assertEqual(response.json()["greeting"], "こんにちは。")
        self.assertIsNone(response.json()["news"])
        self.server.get_briefing.assert_not_awaited()
        self.server.fetch_threads.assert_not_awaited()

    async def test_failed_avatar_connection_can_retry_same_prepared_data(self):
        prepared = await self.prepare()
        self.server.anam.create_session_token.side_effect = RuntimeError("Anam unavailable")
        with self.assertRaises(RuntimeError):
            await self.connect(prepared)
        self.assertIn(prepared["preparation_id"], self.server.preparations)
        self.assertEqual(self.server.conversations, {})
        self.server.anam.create_session_token.side_effect = None
        response = await self.connect(prepared)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.get_briefing.await_count, 1)

    async def test_changed_provider_requires_new_preparation(self):
        prepared = await self.prepare()
        response = await self.client.post("/api/session", json={
            "preparation_id": prepared["preparation_id"], "provider": "claude",
        })
        self.assertEqual(response.status_code, 409)
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_used_preparation_cannot_create_another_avatar_session(self):
        prepared = await self.prepare()
        self.assertEqual((await self.connect(prepared)).status_code, 200)
        self.assertEqual((await self.connect(prepared)).status_code, 409)
        self.server.anam.create_session_token.assert_awaited_once()

    async def test_simultaneous_connect_clicks_create_only_one_avatar_session(self):
        prepared = await self.prepare()
        async def token(**kwargs):
            await asyncio.sleep(0)
            return "test-token"
        self.server.anam.create_session_token.side_effect = token
        responses = await asyncio.gather(self.connect(prepared), self.connect(prepared))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.server.anam.create_session_token.assert_awaited_once()

    async def test_missing_conversation_requires_preparation_again(self):
        response = await self.client.post("/api/session", json={"conversation_id": "missing"})
        self.assertEqual(response.status_code, 409)
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_preparation_is_saved_as_json_with_comment_data(self):
        prepared = await self.prepare()
        path = self.directory / f"{prepared['preparation_id']}.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["news"]["text"], BRIEFING.text)
        self.assertEqual(data["threads"][0]["comments"][0]["text"], "COMMENT_DATA_ONLY")
        self.assertFalse(data["used"])
        self.assertNotIn("session_token", data)

    async def test_ready_data_restores_after_server_memory_is_cleared(self):
        prepared = await self.prepare()
        self.server.preparations.clear()
        self.server.get_briefing.reset_mock()
        self.server.fetch_threads.reset_mock()
        response = await self.client.get(f"/api/preparations/{prepared['preparation_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "openai")
        self.assertEqual(response.json()["news"]["text"], BRIEFING.text)
        self.assertNotIn("COMMENT_DATA_ONLY", response.text)
        restored = self.server.preparations[prepared["preparation_id"]]
        self.assertIn("COMMENT_DATA_ONLY", restored.conversation.pending_comment_context)
        self.assertNotIn("COMMENT_DATA_ONLY", restored.conversation.system_prompt)
        self.server.get_briefing.assert_not_awaited()
        self.server.fetch_threads.assert_not_awaited()
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_connect_restores_json_without_running_preparation_again(self):
        prepared = await self.prepare()
        self.server.preparations.clear()
        self.server.get_briefing.reset_mock()
        self.server.fetch_threads.reset_mock()
        response = await self.connect(prepared)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["greeting"], "こんにちは。\n" + BRIEFING.text)
        self.server.get_briefing.assert_not_awaited()
        self.server.fetch_threads.assert_not_awaited()
        saved = self.server.preparation_store.load(prepared["preparation_id"])
        self.assertTrue(saved.used)
        self.assertEqual(saved.threads[0].comments[0].text, "COMMENT_DATA_ONLY")

    async def test_used_preparation_stays_consumed_after_memory_is_cleared(self):
        prepared = await self.prepare()
        await self.connect(prepared)
        self.server.preparations.clear()
        self.server.conversations.clear()
        self.assertEqual((await self.connect(prepared)).status_code, 409)
        response = await self.client.get(f"/api/preparations/{prepared['preparation_id']}")
        self.assertEqual(response.status_code, 409)
        self.server.anam.create_session_token.assert_awaited_once()

    async def test_save_failure_does_not_mark_data_ready(self):
        with patch.object(self.server.preparation_store, "save", side_effect=OSError("disk full")):
            with self.assertLogs("app.server", level="ERROR"):
                response = await self.client.post("/api/prepare", json={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.server.preparations, {})
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_corrupt_json_never_connects_or_refetches(self):
        prepared = await self.prepare()
        path = self.directory / f"{prepared['preparation_id']}.json"
        path.write_text("{broken")
        self.server.preparations.clear()
        self.server.get_briefing.reset_mock()
        with self.assertLogs("app.preparation_store", level="WARNING"):
            response = await self.connect(prepared)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(path.read_text(), "{broken")
        self.server.get_briefing.assert_not_awaited()
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_connection_state_save_failure_keeps_prepared_data_for_retry(self):
        prepared = await self.prepare()
        with patch.object(self.server.preparation_store, "save", side_effect=OSError("read only")):
            with self.assertLogs("app.server", level="ERROR"):
                response = await self.connect(prepared)
        self.assertEqual(response.status_code, 503)
        self.assertIn(prepared["preparation_id"], self.server.preparations)
        self.assertEqual(self.server.conversations, {})
        self.assertFalse(self.server.preparation_store.load(prepared["preparation_id"]).used)
        self.assertEqual((await self.connect(prepared)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
