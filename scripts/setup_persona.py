"""人物画像から Anam アバターを作り、音声を選び、見た目の説明を生成して persona.yaml に書き込む。

使い方:
    python -m scripts.setup_persona persona.yaml
    python -m scripts.setup_persona persona.yaml --voice-id <id>   # 音声を指定して対話選択を省く
    python -m scripts.setup_persona persona.yaml --skip-avatar     # 設定済みのアバターを使い回す
    python -m scripts.setup_persona persona.yaml --provider openai # 見た目の説明を OpenAI で生成する
"""

import argparse
import asyncio
import base64
import io
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

from app import anam  # noqa: E402
from app.llm import PROVIDERS, REFUSAL_REPLY, Part, Provider, Turn, default_provider, get_llm  # noqa: E402

MAX_UPLOAD_BYTES = 4_500_000  # Anam の上限


class LiteralStr(str):
    pass


yaml.SafeDumper.add_representer(
    LiteralStr, lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
)


def prepare_image(path: Path) -> Path:
    """Anam の上限を超える画像は JPEG に縮小して一時ファイルに書き出す。"""
    if path.stat().st_size <= MAX_UPLOAD_BYTES and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return path
    img = Image.open(path).convert("RGB")
    img.thumbnail((1536, 1536))
    out = path.with_name(f"{path.stem}.upload.jpg")
    img.save(out, "JPEG", quality=90)
    print(f"画像を縮小しました: {out}")
    return out


async def describe_appearance(provider: Provider, image_path: Path, name: str, age: int, gender: str) -> str:
    """LLM に画像を見せ、外見の説明を作る（システムプロンプトの「見た目」に入れる）。"""
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)

    prompt = (
        f"この画像は架空の人物「{name}」（{age}歳・{gender}）の AI 生成ポートレートです。"
        "この人物を演じる会話AIのプロフィールに載せるため、髪型・髪色・服装・雰囲気など"
        "外見の特徴を日本語で2〜3文、客観的に書いてください。説明文だけを出力してください。"
    )
    turn = Turn("user", [Part("image", base64.standard_b64encode(buf.getvalue()).decode()), Part("text", prompt)])
    llm = get_llm(provider)
    print(f"見た目の説明を生成中…（{llm.name}）")
    chunks = [text async for text in llm.stream("あなたは人物画像の外見を簡潔に説明するアシスタントです。", [turn], "setup")]
    return "".join(chunks).strip()


def choose_voice(gender: str) -> str:
    voices = anam.list_voices()
    # 自分で取り込んだ・複製した声は国や性別が未設定のことが多いので、絞り込まずに必ず先頭に出す
    own = [v for v in voices if v.get("createdByOrganizationId")]
    stock = [v for v in voices if not v.get("createdByOrganizationId")]
    want = {"女性": "FEMALE", "男性": "MALE"}.get(gender)
    japanese = [v for v in stock if v.get("country") == "JP"]
    stock_candidates = japanese or stock
    if want:
        stock_candidates = [v for v in stock_candidates if v.get("gender") in (want, None)] or stock_candidates
    if not japanese:
        print("日本アクセントの音声が見つからなかったため全音声を表示します（多言語対応の音声を選んでください）。")
    candidates = own + stock_candidates

    for i, v in enumerate(candidates):
        mark = "★自作 " if v in own else ""
        print(
            f"[{i:2}] {mark}{v['displayName']}  ({v['provider']}/{v.get('providerModelId')}, "
            f"{v.get('gender')}, {v.get('country')})\n     試聴: {v.get('sampleUrl')}"
        )
    while True:
        choice = input("使う音声の番号: ").strip()
        if choice.isdigit() and int(choice) < len(candidates):
            return candidates[int(choice)]["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("persona_file", type=Path, nargs="?", default=Path("persona.yaml"))
    parser.add_argument("--voice-id")
    parser.add_argument("--skip-avatar", action="store_true")
    parser.add_argument("--provider", choices=PROVIDERS, help="見た目の説明に使う LLM（省略時は LLM_PROVIDER）")
    args = parser.parse_args()

    if not args.persona_file.exists():
        sys.exit(f"{args.persona_file} がありません。persona.example.yaml をコピーして編集してください。")
    data = yaml.safe_load(args.persona_file.read_text(encoding="utf-8"))
    image_path = (args.persona_file.parent / data["image"]).resolve()
    if not image_path.exists():
        sys.exit(f"画像が見つかりません: {image_path}")

    # 途中で失敗しても、それまでに決まった値（作成済みアバター・選んだ声）は失わないよう段階ごとに保存する
    if not (args.skip_avatar and data.get("avatar_id")):
        print("アバターを作成中…（1〜2分かかることがあります）")
        avatar = anam.create_avatar(data["name"][:50], prepare_image(image_path))
        data["avatar_id"] = avatar["id"]
        print(f"avatar_id: {avatar['id']}")
        save(args.persona_file, data)

    if not data.get("voice_id") or args.voice_id:
        data["voice_id"] = args.voice_id or choose_voice(data["gender"])
        save(args.persona_file, data)

    provider = args.provider or default_provider()
    try:
        appearance = asyncio.run(describe_appearance(provider, image_path, data["name"], data["age"], data["gender"]))
    except Exception as e:  # noqa: BLE001 - 認証エラー等は案内を出して終える（ここまでの設定は保存済み）
        sys.exit(
            f"見た目の説明の生成に失敗しました（{provider}）: {e}\n"
            f".env の {provider} の API キーとモデル名を確認するか、--provider で別の LLM を指定して再実行してください。"
        )
    if appearance in ("", REFUSAL_REPLY):
        print("見た目の説明は生成できませんでした。persona.yaml の appearance を手で書いてください。")
        appearance = ""
    data["appearance"] = appearance
    print(appearance)
    save(args.persona_file, data)
    print(f"{args.persona_file} を更新しました。")


def save(path: Path, data: dict) -> None:
    out = {k: LiteralStr(v) if isinstance(v, str) and "\n" in v else v for k, v in data.items()}
    path.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
