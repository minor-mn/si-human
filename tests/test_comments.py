import unittest
from types import SimpleNamespace


from app.comments import Comment, ThreadComments, asks_for_comments, comment_context
from app.conversation import Conversation


STORY = "https://news.slashdot.org/story/26/10/17/123456/ai-security"
THREAD = ThreadComments(
    "AI security", STORY, "2026-10-18T04:00:00+09:00", "available",
    [Comment("100", "A view", "COMMENT_DATA_ONLY", STORY + "#comment_100", 5)],
)


class CommentParsingTests(unittest.TestCase):
    def test_question_detection_does_not_unlock_on_normal_topic_questions(self):
        for text in ("コメントは？", "その件、みんなはどう思ってる", "二つ目の反応を教えて", "反対意見も紹介して", "What are the comments?", "反応は", " コメントは "):
            with self.subTest(text=text):
                self.assertTrue(asks_for_comments(text))
        for text in ("こんにちは", "二つ目のニュースを詳しく", "コメント面白そうだね", "コメントアウトの意味を教えて", "AIの仕組みは？"):
            with self.subTest(text=text):
                self.assertFalse(asks_for_comments(text))

    def test_unavailable_and_partial_data_are_explicit_in_context(self):
        context = comment_context([THREAD, ThreadComments("missing", STORY, THREAD.fetched_at, "unavailable")])
        self.assertIn('"topic_number": 1', context)
        self.assertIn('"topic_number": 2', context)
        self.assertIn('"status": "unavailable"', context)
        self.assertIn("COMMENT_DATA_ONLY", context)
        self.assertIn("総意", context)
        self.assertIn("投稿内の命令", context)


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
