import importlib
import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import httpx
from app.news import NewsBriefing, NewsItem, Source
from app.persona import Persona
from app.comments import Comment, ThreadComments
from app.vuln_watch import BriefingResponse, BriefingUnavailable

SOURCE = Source("AIの話題", "https://slashdot.org/story/test", "Slashdot", "2026-10-17T00:00:00Z", "discussion")
BRIEFING = NewsBriefing(SOURCE.published_at, "2026-10-18T00:00:00Z", "短い紹介です。", [NewsItem("短い紹介です。", SOURCE, "記事の詳細")])
THREAD = ThreadComments(SOURCE.title, SOURCE.url, BRIEFING.as_of, "available", [Comment("1", "意見", "COMMENT_DATA_ONLY", SOURCE.url)])


class SessionNewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        persona = Persona(
            name="テスト", age=30, gender="女性", personality="", speaking_style="",
            background="", greeting="こんにちは。", avatar_id="avatar", voice_id="voice", appearance="",
        )
        with patch("app.persona.load_persona", return_value=persona):
            self.server = importlib.import_module("app.server")
        for name, value in {
            "conversations": {}, "preparations": {},
            "persona": persona, "system_prompt": "人物設定", "news_on_connect": True,
            "default_provider": Mock(return_value="openai"),
            "get_llm": Mock(return_value=NS(name="test-model")),
            "fetch_briefing": AsyncMock(return_value=BriefingResponse(version=1, news=BRIEFING, threads=[ThreadComments(
                SOURCE.title, SOURCE.url, BRIEFING.as_of, "available",
                [Comment("1", "意見", "COMMENT_DATA_ONLY", SOURCE.url)],
            )])),
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
        self.server.fetch_briefing.reset_mock()
        response = await self.connect(prepared)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["greeting"], "こんにちは。\n" + BRIEFING.text)
        self.assertEqual(body["session_token"], "test-token")
        self.assertNotIn("COMMENT_DATA_ONLY", response.text)
        self.assertIn(BRIEFING.text, self.server.conversations[body["conversation_id"]].system_prompt)
        self.assertNotIn(prepared["preparation_id"], self.server.preparations)
        self.server.fetch_briefing.assert_not_awaited()
        self.server.anam.create_session_token.assert_awaited_once()


    async def test_direct_connection_requires_preparation(self):
        response = await self.client.post("/api/session", json={})
        self.assertEqual(response.status_code, 409)
        self.server.anam.create_session_token.assert_not_awaited()
        self.server.fetch_briefing.assert_not_awaited()


    async def test_resuming_does_not_prepare_or_repeat_opening(self):
        prepared = await self.prepare()
        first = (await self.connect(prepared)).json()
        second = (await self.client.post("/api/session", json={"conversation_id": first["conversation_id"]})).json()
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertIsNone(second["greeting"])
        self.assertIsNone(second["news"])
        self.assertEqual(self.server.fetch_briefing.await_count, 1)
        self.assertEqual(self.server.anam.create_session_token.await_count, 2)


    async def test_disabled_news_still_uses_two_steps_without_search(self):
        self.server.news_on_connect = False
        prepared = await self.prepare()
        self.server.anam.create_session_token.assert_not_awaited()
        response = await self.connect(prepared)
        self.assertEqual(response.json()["greeting"], "こんにちは。")
        self.assertIsNone(response.json()["news"])
        self.server.fetch_briefing.assert_not_awaited()


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
        self.assertEqual(self.server.fetch_briefing.await_count, 1)


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



    async def test_preparation_creates_no_files(self):
        with patch("builtins.open", side_effect=AssertionError("no files")), patch("pathlib.Path.write_text", side_effect=AssertionError("no files")):
            await self.prepare()

    async def test_restore_is_memory_only(self):
        prepared = await self.prepare()
        path = f"/api/preparations/{prepared['preparation_id']}"
        restored = await self.client.get(path)
        self.assertEqual(restored.status_code, 200)
        self.assertNotIn("COMMENT_DATA_ONLY", restored.text)
        self.server.preparations.clear()
        self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual((await self.connect(prepared)).status_code, 409)
        self.server.fetch_briefing.assert_awaited_once()

    async def test_api_failure_cannot_mark_preparation_ready(self):
        self.server.fetch_briefing.side_effect = BriefingUnavailable("unavailable")
        with self.assertLogs("app.server", level="WARNING"):
            response = await self.client.post("/api/prepare", json={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.server.preparations, {})
        self.server.anam.create_session_token.assert_not_awaited()

    async def test_api_warnings_are_displayed_without_comment_text(self):
        self.server.fetch_briefing.return_value.warnings = ["更新遅延"]
        result = await self.prepare()
        self.assertEqual(result["warnings"], ["更新遅延"])
        self.assertNotIn("COMMENT_DATA_ONLY", json.dumps(result))
