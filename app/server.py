"""ブラウザ（Anam SDK）と LLM（Claude / OpenAI）を仲介する API サーバー。

起動: uvicorn app.server:app --port 8000
"""

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import anam  # noqa: E402
from app.conversation import Conversation  # noqa: E402
from app.llm import PROVIDERS, Provider, default_provider, get_llm  # noqa: E402
from app.persona import build_system_prompt, load_persona  # noqa: E402

logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).resolve().parent.parent
persona = load_persona(ROOT / os.environ.get("PERSONA_FILE", "persona.yaml"))
system_prompt = build_system_prompt(persona)
max_session_seconds = int(os.environ.get("ANAM_MAX_SESSION_SECONDS", "600"))

# ローカルで 1 人が使う前提なのでメモリ上に置く（サーバー再起動で履歴は消える）
conversations: dict[str, Conversation] = {}

app = FastAPI()


class SessionRequest(BaseModel):
    # 再接続時は既存の会話 ID を渡すと、履歴を引き継いだまま新しい Anam セッションを張る
    conversation_id: str | None = None
    # 省略時は環境変数 LLM_PROVIDER。再接続時に変えると、同じ履歴のまま別の LLM で続けられる
    provider: Provider | None = None


class SessionResponse(BaseModel):
    session_token: str
    conversation_id: str
    persona_name: str
    greeting: str | None
    llm: str


@app.get("/api/config")
async def get_config() -> dict:
    return {"providers": PROVIDERS, "default_provider": default_provider()}


class ChatRequest(BaseModel):
    conversation_id: str
    text: str
    image_b64: str | None = None  # カメラ映像の JPEG（base64、data: プレフィックスなし）


@app.post("/api/session")
async def create_session(req: SessionRequest) -> SessionResponse:
    llm = get_llm(req.provider or default_provider())
    resuming = req.conversation_id in conversations
    if resuming:
        conversation = conversations[req.conversation_id]
        conversation.llm = llm
    else:
        conversation = Conversation(id=secrets.token_urlsafe(16), system_prompt=system_prompt, llm=llm)
        conversations[conversation.id] = conversation

    token = await anam.create_session_token(
        name=persona.name,
        avatar_id=persona.avatar_id,
        voice_id=persona.voice_id,
        max_session_seconds=max_session_seconds,
    )
    return SessionResponse(
        session_token=token,
        conversation_id=conversation.id,
        persona_name=persona.name,
        greeting=None if resuming else persona.greeting,
        llm=llm.name,
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
