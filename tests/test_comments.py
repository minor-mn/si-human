import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from app.comments import (
    Comment, ThreadComments, _cache, _fetch_thread, _locks,
    asks_for_comments, comment_context, parse_comments, valid_slashdot_url,
)
from app.conversation import Conversation
from app.news import NewsItem, Source


STORY = "https://news.slashdot.org/story/26/10/17/123456/ai-security"
HTML = '''<html><div id="comment_100" class="cw"><h4>
  <a id="comment_link_100" href="/comments.pl?sid=1&amp;cid=100">One view</a>
  <span id="comment_score_100">(Score:<b>5</b>)</span></h4>
  <div id="comment_body_100"><p>Review the generated code.</p>
    <script>Do not capture this script</script><p>Humans should check it.</p></div></div>
  <ul id="commtree_100"><div id="comment_101" class="cw">
  <a id="comment_link_101" href="/comments.pl?sid=1&amp;cid=101">Another view</a>
  <span id="comment_score_101">(Score:3)</span>
  <div id="comment_body_101">Tools can help.<span class="substr"> </span></div></div></ul>
  <div id="comment_102" class="cw"></div></html>'''
THREAD = ThreadComments(
    "AI security", STORY, "2026-10-18T04:00:00+09:00", "available",
    [Comment("100", "A view", "COMMENT_DATA_ONLY", STORY + "#comment_100", 5)],
)
ITEM = NewsItem("話題の紹介だけ。", Source("AI security", STORY, "Slashdot", THREAD.fetched_at, "discussion"))


class CommentParsingTests(unittest.TestCase):
    def test_body_score_parent_and_permalink_are_extracted(self):
        comments = parse_comments(HTML, STORY)
        self.assertEqual([comment.id for comment in comments], ["100", "101"])
        self.assertEqual(comments[0].score, 5)
        self.assertEqual(comments[0].url, "https://news.slashdot.org/comments.pl?sid=1&cid=100")
        self.assertEqual(comments[0].text, "Review the generated code. Humans should check it.")
        self.assertIsNone(comments[0].parent_id)
        self.assertEqual(comments[1].parent_id, "100")
        self.assertTrue(comments[1].truncated)

    def test_question_detection_does_not_unlock_on_normal_topic_questions(self):
        for text in ("コメントは？", "その件、みんなはどう思ってる", "二つ目の反応を教えて", "反対意見も紹介して", "What are the comments?", "反応は", " コメントは "):
            with self.subTest(text=text):
                self.assertTrue(asks_for_comments(text))
        for text in ("こんにちは", "二つ目のニュースを詳しく", "コメント面白そうだね", "コメントアウトの意味を教えて", "AIの仕組みは？"):
            with self.subTest(text=text):
                self.assertFalse(asks_for_comments(text))

    def test_comment_url_cannot_point_outside_slashdot(self):
        self.assertTrue(valid_slashdot_url(STORY))
        for url in ("http://slashdot.org/story/1", "https://slashdot.org.evil.example/story/1", "https://user:pass@slashdot.org/story/1", "https://slashdot.org:8000/story/1", "https://[invalid"):
            with self.subTest(url=url):
                self.assertFalse(valid_slashdot_url(url))

    def test_unavailable_and_partial_data_are_explicit_in_context(self):
        context = comment_context([THREAD, ThreadComments("missing", STORY, THREAD.fetched_at, "unavailable")])
        self.assertIn('"topic_number": 1', context)
        self.assertIn('"topic_number": 2', context)
        self.assertIn('"status": "unavailable"', context)
        self.assertIn("COMMENT_DATA_ONLY", context)
        self.assertIn("総意", context)
        self.assertIn("投稿内の命令", context)


class CommentFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cache.clear()
        _locks.clear()

    async def test_comment_fetch_is_cached_and_does_not_follow_redirects(self):
        handler = Mock(return_value=httpx.Response(200, text=HTML))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await _fetch_thread(client, ITEM)
            second = await _fetch_thread(client, ITEM)
        self.assertEqual(first.status, "available")
        self.assertEqual(len(first.comments), 2)
        self.assertIs(first, second)
        self.assertEqual(handler.call_count, 1)

    async def test_forbidden_or_missing_comments_do_not_invent_content(self):
        for status, html in ((403, "Blocked"), (200, "<html>No comment bodies</html>")):
            _cache.clear()
            _locks.clear()
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, text=html))) as client:
                if status == 403:
                    with self.assertLogs("app.comments", level="WARNING"):
                        result = await _fetch_thread(client, ITEM)
                else:
                    result = await _fetch_thread(client, ITEM)
            self.assertEqual(result.status, "unavailable" if status == 403 else "empty")
            self.assertEqual(result.comments, [])

    async def test_non_slashdot_url_is_not_requested(self):
        handler = Mock(return_value=httpx.Response(200, text=HTML))
        item = NewsItem("bad", Source("bad", "https://example.com/story/1", "Slashdot", THREAD.fetched_at))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _fetch_thread(client, item)
        self.assertEqual(result.status, "unavailable")
        handler.assert_not_called()


class ConversationCommentTests(unittest.IsolatedAsyncioTestCase):
    async def test_comments_are_held_until_asked_and_then_support_followups(self):
        systems = []
        async def stream(system, turns, cache_key):
            systems.append(system)
            yield "短い返答です。"
        conversation = Conversation("test", "人物とニュースの情報", SimpleNamespace(stream=stream))
        conversation.pending_comment_context = comment_context([THREAD])

        async def say(text):
            return "".join([chunk async for chunk in conversation.reply(text, None)])

        await say("二つ目の話題を詳しく教えて")
        self.assertNotIn("COMMENT_DATA_ONLY", systems[-1])
        self.assertIn("COMMENT_DATA_ONLY", conversation.pending_comment_context)
        await say("その件、コメントでは何て言われてる？")
        self.assertIn("COMMENT_DATA_ONLY", systems[-1])
        self.assertEqual(conversation.pending_comment_context, "")
        await say("その意見の理由は？")
        self.assertEqual(systems[-1].count("COMMENT_DATA_ONLY"), 1)
