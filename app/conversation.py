"""会話履歴の保持と、LLM へのストリーミング問い合わせ。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.comments import asks_for_comments
from app.llm import LLM, Part, Turn

log = logging.getLogger(__name__)

ERROR_REPLY = "ごめんなさい、ちょっと調子が悪いみたいです。もう一度話しかけてもらえますか？"


@dataclass
class Conversation:
    """1 人のユーザーとの会話。Anam のセッションが切れて再接続しても引き継ぐ。

    履歴は追記のみ（過去のメッセージを書き換えない）にして、プロンプトキャッシュを毎ターン効かせる。
    """

    id: str
    system_prompt: str
    llm: LLM
    turns: list[Turn] = field(default_factory=list)
    # 前のターンが一言も返さずに遮られた場合、そのユーザー発話を次のターンに繰り越す
    pending_user_parts: list[Part] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 接続準備で取得して保管するだけにし、質問されるまでLLMには渡さない。
    pending_comment_context: str = ""

    async def reply(self, user_text: str, image_b64: str | None) -> AsyncIterator[str]:
        """ユーザー発話に対する返答をテキスト断片として流す。

        ブラウザ側が中断（ユーザーの割り込み）すると、この非同期ジェネレータはキャンセルされる。
        その場合も言いかけた部分までを履歴に残す。
        """
        async with self.lock:
            if self.pending_comment_context and asks_for_comments(user_text):
                self.system_prompt += self.pending_comment_context
                self.pending_comment_context = ""
            parts = self.pending_user_parts
            self.pending_user_parts = []
            if image_b64:
                parts.append(Part("image", image_b64))
            parts.append(Part("text", user_text))
            self.turns.append(Turn("user", parts))

            spoken: list[str] = []
            try:
                async for text in self.llm.stream(self.system_prompt, self.turns, cache_key=self.id):
                    spoken.append(text)
                    yield text
            except Exception as e:  # noqa: BLE001 - API エラーで黙り込まないよう、一言返して原因をログに出す
                log.error("LLM の呼び出しに失敗しました（%s）: %s", self.llm.name, e)
                # 謝罪は読み上げるだけで履歴には残さない（spoken に入れない）
                yield ERROR_REPLY
            finally:
                said = "".join(spoken).strip()
                if said:
                    self.turns.append(Turn("assistant", [Part("text", said)]))
                else:
                    # user / assistant の交互を崩さないよう、返答ゼロのターンは履歴から外して繰り越す
                    self.pending_user_parts = self.turns.pop().parts
