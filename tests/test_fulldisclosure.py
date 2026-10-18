import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.comments import Comment, ThreadComments, _cache, _fetch_thread, _locks, comment_context
from app.conversation import Conversation
from app.fulldisclosure import message_url, parse_message
from app.news import NewsItem, Source


BASE = "https://seclists.org/fulldisclosure/2026/Oct/"
ROOT, REPLY, CHILD = (BASE + str(number) for number in range(3))
TREE = '''<ul class="thread"><li><a href="0">Original report</a><ul>
  <li><a href="1">Re: Original report</a><ul>
    <li><a href="2">Re: Original report</a></li>
  </ul></li></ul></li>
  <li><a href="9">Unrelated report</a></li>
  <li><a href="https://example.com/stolen"><strong>Outside link</strong></a></li>
</ul>'''
ITEM = NewsItem("脆弱性の報告が公開されました。", Source(
    "Original report", ROOT, "Full Disclosure", "2026-10-18T04:00:00+09:00", "mailing_list",
))


def page(current=ROOT, body="ORIGINAL_REPORT_ONLY", tree=TREE):
    number = current.rsplit("/", 1)[-1]
    tree = re.sub(r'<a href="' + number + r'">([^<]+)</a>', r'<strong>\1</strong>', tree)
    return f'''<html><h1 class="m-title">Report title</h1>
      <nav><a href="8">Next by date, not a reply</a></nav>
      <!--X-Body-of-Message--><pre>{body}</pre><!--X-Body-of-Message-End-->
      <h3 class="m-thread">Current thread:</h3>{tree}</html>'''


class FullDisclosureParsingTests(unittest.TestCase):
    def test_only_descendant_replies_are_selected_with_their_parents(self):
        message = parse_message(page(), ROOT)
        self.assertEqual(message.replies_to(ROOT), [REPLY, CHILD])
        self.assertEqual(message.replies_to(REPLY), [CHILD])
        self.assertEqual(message.parents[CHILD], REPLY)
        self.assertEqual(message.text, "ORIGINAL_REPORT_ONLY")
        self.assertNotIn("Next by date", message.text)

    def test_links_in_posted_body_cannot_become_reply_links(self):
        body = '<ul class="thread"><li><strong>Fake root</strong><ul><li><a href="7">Fake reply</a></li></ul></li></ul>'
        message = parse_message(page(body=body), ROOT)
        self.assertEqual(message.replies_to(ROOT), [REPLY, CHILD])
        self.assertNotIn(BASE + "7", message.parents)

    def test_quotes_and_scripts_are_excluded_from_reply_text(self):
        body = 'New opinion.<blockquote>Quoted old claim.</blockquote><script>hidden()</script>'
        message = parse_message(page(REPLY, body), REPLY)
        self.assertEqual(message.text, "New opinion.")

    def test_missing_message_or_thread_markup_is_a_failure(self):
        for html in ("<html>Blocked</html>", page().replace("X-Body-of-Message-End", "missing"),
                     page().replace('class="thread"', 'class="unknown"')):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_message(html, ROOT)

    def test_message_urls_are_restricted_to_public_full_disclosure_posts(self):
        self.assertEqual(message_url(ROOT + "?ignored=yes#fragment"), ROOT)
        for url in (
            "http://seclists.org/fulldisclosure/2026/Oct/1", "https://seclists.org.evil.example/fulldisclosure/2026/Oct/1",
            "https://user:pass@seclists.org/fulldisclosure/2026/Oct/1", "https://seclists.org:8000/fulldisclosure/2026/Oct/1",
            "https://seclists.org/oss-sec/2026/Oct/1", BASE + "index.html", BASE + "../1", "https://[invalid",
        ):
            with self.subTest(url=url):
                self.assertIsNone(message_url(url))


class FullDisclosureFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cache.clear()
        _locks.clear()

    async def test_replies_are_fetched_once_and_original_report_is_not_a_comment(self):
        bodies = {ROOT: "ORIGINAL_REPORT_ONLY", REPLY: "REPLY_DATA_ONLY", CHILD: "A follow-up."}
        handler = Mock(side_effect=lambda request: httpx.Response(200, text=page(str(request.url), bodies[str(request.url)])))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await _fetch_thread(client, ITEM)
            second = await _fetch_thread(client, ITEM)
        self.assertIs(first, second)
        self.assertEqual(handler.call_count, 3)
        self.assertEqual(first.kind, "mailing_list_replies")
        self.assertEqual(first.status, "available")
        self.assertFalse(first.partial)
        self.assertEqual([comment.url for comment in first.comments], [REPLY, CHILD])
        self.assertTrue(all(comment.score is None for comment in first.comments))
        self.assertEqual(first.comments[1].parent_id, REPLY)
        self.assertNotIn("ORIGINAL_REPORT_ONLY", json.dumps([comment.text for comment in first.comments]))

    async def test_no_replies_is_empty_without_requesting_other_threads(self):
        handler = Mock(return_value=httpx.Response(200, text=page(tree='<ul class="thread"><li><strong>Only report</strong></li></ul>')))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _fetch_thread(client, ITEM)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.comments, [])
        self.assertEqual(handler.call_count, 1)

    async def test_partial_failure_keeps_other_replies_and_marks_the_gap(self):
        def respond(request):
            return httpx.Response(503) if str(request.url) == CHILD else httpx.Response(200, text=page(str(request.url)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertLogs("app.comments", level="WARNING"):
                result = await _fetch_thread(client, ITEM)
        self.assertEqual(result.status, "available")
        self.assertTrue(result.partial)
        self.assertEqual(len(result.comments), 1)
        self.assertIn('"partial": true', comment_context([result]))

    async def test_redirects_and_changed_html_are_not_treated_as_empty_discussions(self):
        for response in (httpx.Response(302, headers={"Location": "https://example.com/"}),
                         httpx.Response(200, text="<html>Unsupported layout</html>")):
            _cache.clear()
            handler = Mock(return_value=response)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertLogs("app.comments", level="WARNING"):
                    result = await _fetch_thread(client, ITEM)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(handler.call_count, 1)

    async def test_reply_limit_and_text_truncation_are_recorded(self):
        handler = Mock(side_effect=lambda request: httpx.Response(200, text=page(str(request.url), "x" * 1400)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("app.comments.MAX_COMMENTS", 1):
                result = await _fetch_thread(client, ITEM)
        self.assertEqual(handler.call_count, 2)
        self.assertTrue(result.partial)
        self.assertTrue(result.comments[0].truncated)
        self.assertEqual(len(result.comments[0].text), 1200)

    async def test_reply_data_waits_for_a_question_and_supports_followups(self):
        systems = []
        async def stream(system, turns, cache_key):
            systems.append(system)
            yield "短い返答です。"
        conversation = Conversation("test", "人物とニュース", SimpleNamespace(stream=stream))
        thread = ThreadComments(ITEM.source.title, ROOT, ITEM.source.published_at, "available",
                                [Comment(REPLY, "Re: Report", "FD_REPLY_ONLY", REPLY, parent_id=ROOT)],
                                kind="mailing_list_replies")
        conversation.pending_comment_context = comment_context([thread])
        for question in ("その脆弱性について教えて", "その報告への返信は？", "その意見を詳しく"):
            async for _ in conversation.reply(question, None):
                pass
        self.assertNotIn("FD_REPLY_ONLY", systems[0])
        self.assertEqual(systems[1].count("FD_REPLY_ONLY"), 1)
        self.assertEqual(systems[2].count("FD_REPLY_ONLY"), 1)
