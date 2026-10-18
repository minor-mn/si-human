"""ブラウザ（Anam SDK）と LLM（Claude / OpenAI）を仲介する API サーバー。

起動: uvicorn app.server:app --port 8000
"""

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import anam  # noqa: E402
from app.comments import ThreadComments, comment_context  # noqa: E402
from app.conversation import Conversation  # noqa: E402
from app.llm import PROVIDERS, Provider, default_provider, get_llm  # noqa: E402
from app.news import NewsBriefing  # noqa: E402
from app.vuln_watch import BriefingUnavailable, fetch_briefing  # noqa: E402
from app.persona import build_system_prompt, load_persona  # noqa: E402

logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).resolve().parent.parent
persona = load_persona(ROOT / os.environ.get("PERSONA_FILE", "persona.yaml"))
system_prompt = build_system_prompt(persona)
max_session_seconds = int(os.environ.get("ANAM_MAX_SESSION_SECONDS", "600"))
news_on_connect = os.environ.get("NEWS_ON_CONNECT", "true").lower() in {"true", "1", "yes"}

# ローカルで 1 人が使う前提なのでメモリ上に置く（サーバー再起動で履歴は消える）
conversations: dict[str, Conversation] = {}


@dataclass
class PreparedConversation:
    conversation: Conversation
    provider: Provider
    greeting: str
    news: NewsBriefing | None
    warnings: list[str]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


preparations: dict[str, PreparedConversation] = {}

app = FastAPI()


class SessionRequest(BaseModel):
    preparation_id: str | None = None
    # 再接続時は既存の会話 ID を渡すと、履歴を引き継いだまま新しい Anam セッションを張る
    conversation_id: str | None = None
    # 新規接続は準備時のLLM。再接続時に指定すると、同じ履歴のまま別のLLMで続けられる
    provider: Provider | None = None


class PreparationRequest(BaseModel):
    provider: Provider | None = None


class PreparationResponse(BaseModel):
    preparation_id: str
    persona_name: str
    provider: Provider
    llm: str
    news: NewsBriefing | None
    warnings: list[str]


class SessionResponse(BaseModel):
    session_token: str
    conversation_id: str
    persona_name: str
    greeting: str | None
    llm: str
    news: NewsBriefing | None = None


@app.get("/api/config")
async def get_config() -> dict:
    return {"providers": PROVIDERS, "default_provider": default_provider()}


class ChatRequest(BaseModel):
    conversation_id: str
    text: str
    image_b64: str | None = None  # カメラ映像の JPEG（base64、data: プレフィックスなし）


def _make_prepared(provider: Provider, briefing: NewsBriefing | None, threads: list[ThreadComments], warnings: list[str]) -> PreparedConversation:
    llm = get_llm(provider)
    conversation = Conversation(id=secrets.token_urlsafe(16), system_prompt=system_prompt, llm=llm)
    greeting = persona.greeting
    if briefing:
        greeting = "\n".join(part for part in (persona.greeting, briefing.text) if part)
        conversation.system_prompt += briefing.conversation_context()
    if threads:
        conversation.pending_comment_context = comment_context(threads)
    return PreparedConversation(conversation, provider, greeting, briefing, warnings)


def _preparation_response(preparation_id: str, prepared: PreparedConversation) -> PreparationResponse:
    return PreparationResponse(
        preparation_id=preparation_id,
        persona_name=persona.name,
        provider=prepared.provider,
        llm=prepared.conversation.llm.name,
        news=prepared.news,
        warnings=prepared.warnings,
    )


def _load_prepared(preparation_id: str | None, missing_status: int = 409) -> PreparedConversation:
    if not preparation_id:
        raise HTTPException(missing_status, "接続準備が必要です。「接続準備」を押してください。")
    prepared = preparations.get(preparation_id)
    if prepared is None:
        raise HTTPException(missing_status, "準備データが見つかりません。「接続準備」を押してください。")
    return prepared


@app.get("/api/preparations/{preparation_id}")
async def get_preparation(preparation_id: str) -> PreparationResponse:
    return _preparation_response(preparation_id, _load_prepared(preparation_id, missing_status=404))


@app.post("/api/prepare")
async def prepare_connection(req: PreparationRequest) -> PreparationResponse:
    provider = req.provider or default_provider()
    briefing, threads, warnings = None, [], []
    if news_on_connect:
        try:
            result = await fetch_briefing()
        except BriefingUnavailable as exc:
            logging.getLogger(__name__).warning("vuln-watchの読み込みに失敗しました: %s", exc)
            raise HTTPException(503, "vuln-watchから話題を読み込めませんでした。接続先を確認して準備し直してください。") from exc
        briefing, threads, warnings = result.news, result.threads, result.warnings
    preparation_id = secrets.token_urlsafe(16)
    prepared = _make_prepared(provider, briefing, threads, warnings)
    preparations[preparation_id] = prepared
    return _preparation_response(preparation_id, prepared)


async def _session_token() -> str:
    return await anam.create_session_token(
        name=persona.name,
        avatar_id=persona.avatar_id,
        voice_id=persona.voice_id,
        max_session_seconds=max_session_seconds,
    )


@app.post("/api/session")
async def create_session(req: SessionRequest) -> SessionResponse:
    if req.conversation_id and req.preparation_id:
        raise HTTPException(400, "準備データと再接続する会話を同時に指定できません。")

    if req.conversation_id:
        conversation = conversations.get(req.conversation_id)
        if conversation is None:
            raise HTTPException(409, "会話が見つかりません。接続準備からやり直してください。")
        llm = get_llm(req.provider) if req.provider else conversation.llm
        token = await _session_token()
        conversation.llm = llm
        greeting, briefing = None, None
    else:
        prepared = _load_prepared(req.preparation_id)
        async with prepared.lock:
            if req.preparation_id not in preparations:
                raise HTTPException(409, "この準備データはすでに接続に使用されています。")
            if req.provider is not None and req.provider != prepared.provider:
                raise HTTPException(409, "LLMが変更されています。接続準備からやり直してください。")
            # この段階では外部記事の取得やLLMによる要約は行わない。
            token = await _session_token()
            conversation = prepared.conversation
            llm = conversation.llm
            greeting, briefing = prepared.greeting, prepared.news
            conversations[conversation.id] = conversation
            del preparations[req.preparation_id]

    return SessionResponse(
        session_token=token,
        conversation_id=conversation.id,
        persona_name=persona.name,
        greeting=greeting,
        llm=llm.name,
        news=briefing,
    )


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    conversation = conversations.get(req.conversation_id)
    if conversation is None:
        raise HTTPException(404, "unknown conversation_id")
    # 改行区切り JSON ではなく素のテキストを流す（ブラウザ側は受け取った断片をそのまま Anam に渡すだけ）
    return StreamingResponse(
        conversation.reply(req.text, req.image_b64),
        media_type="text/plain; charset=utf-8",
    )


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
