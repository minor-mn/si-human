"""Anam REST API の薄いラッパー（アバター作成・音声一覧・セッショントークン発行）。"""

import os
from pathlib import Path

import httpx

ANAM_BASE_URL = "https://api.anam.ai/v1"

# Anam 側の LLM を無効化し、応答テキストはブラウザ経由でこちらから流し込む
CUSTOM_LLM_ID = "CUSTOMER_CLIENT_V1"
AVATAR_MODEL = "cara-4"


def _headers() -> dict[str, str]:
    api_key = os.environ.get("ANAM_API_KEY")
    if not api_key:
        raise RuntimeError("ANAM_API_KEY が設定されていません（.env を確認してください）")
    return {"Authorization": f"Bearer {api_key}"}


def create_avatar(display_name: str, image_path: Path) -> dict:
    """1 枚の人物画像から Cara-4 アバターを作成する（生成には 1〜2 分かかることがある）。"""
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    content_type = mime.get(image_path.suffix.lower())
    if content_type is None:
        raise ValueError(f"対応していない画像形式です: {image_path.suffix}（png/jpg/webp のみ）")
    with image_path.open("rb") as f:
        resp = httpx.post(
            f"{ANAM_BASE_URL}/avatars",
            headers=_headers(),
            data={"displayName": display_name, "avatarModel": AVATAR_MODEL},
            files={"imageFile": (image_path.name, f, content_type)},
            timeout=300,
        )
    resp.raise_for_status()
    return resp.json()


def list_voices() -> list[dict]:
    """利用できる音声をすべて取得する（ページングを辿る）。"""
    voices: list[dict] = []
    page = 1
    while True:
        resp = httpx.get(
            f"{ANAM_BASE_URL}/voices",
            headers=_headers(),
            params={"page": page, "perPage": 100},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        voices.extend(body["data"])
        if page >= body["meta"]["lastPage"]:
            return voices
        page += 1


async def create_session_token(*, name: str, avatar_id: str, voice_id: str, max_session_seconds: int) -> str:
    persona_config = {
        "name": name,
        "avatarId": avatar_id,
        "avatarModel": AVATAR_MODEL,
        "voiceId": voice_id,
        "llmId": CUSTOM_LLM_ID,
        "languageCode": "ja",  # 音声認識を日本語に固定（精度と遅延の両方に効く）
        "maxSessionLengthSeconds": max_session_seconds,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{ANAM_BASE_URL}/auth/session-token",
            headers=_headers(),
            json={"personaConfig": persona_config},
        )
    resp.raise_for_status()
    return resp.json()["sessionToken"]
