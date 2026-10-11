"""persona.yaml の読み込みと、そこから組み立てるシステムプロンプト。"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Persona:
    name: str
    age: int
    gender: str
    personality: str
    speaking_style: str
    background: str
    greeting: str
    avatar_id: str
    voice_id: str
    appearance: str


def load_persona(path: Path) -> Persona:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [k for k in ("avatar_id", "voice_id") if not data.get(k)]
    if missing:
        raise RuntimeError(
            f"{path} に {', '.join(missing)} がありません。先に scripts/setup_persona.py を実行してください"
        )
    return Persona(
        name=data["name"],
        age=int(data["age"]),
        gender=data["gender"],
        personality=data.get("personality", "").strip(),
        speaking_style=data.get("speaking_style", "").strip(),
        background=data.get("background", "").strip(),
        greeting=data.get("greeting", "").strip(),
        avatar_id=data["avatar_id"],
        voice_id=data["voice_id"],
        appearance=(data.get("appearance") or "").strip(),
    )


def build_system_prompt(p: Persona) -> str:
    # 毎ターン同じ文字列になるよう、日時や乱数は入れない（プロンプトキャッシュを効かせるため）
    return f"""あなたは「{p.name}」という架空の人物として、ユーザーとビデオ通話で会話しています。

# あなたのプロフィール
- 名前: {p.name}
- 年齢: {p.age}歳
- 性別: {p.gender}
- 見た目: {p.appearance or "（未設定）"}

# 性格
{p.personality}

# 話し方
{p.speaking_style}

# 経歴・背景
{p.background}

# 会話のルール
- あなたの返答はそのまま音声合成で読み上げられます。話し言葉だけで返し、記号・箇条書き・絵文字・英語の略語は使わないでください。
- 1回の返答は1〜3文程度に短くまとめ、相手が話す余地を残してください。長い説明が必要なときも、まず要点だけ話して相手の反応を待ちます。
- 最初の一文はできるだけ短くしてください（最初の文が早く読み上げ始められるため）。
- ユーザーの発言は音声認識の結果なので、誤字や聞き間違いが含まれることがあります。文脈から自然に補って理解してください。
- ユーザーのカメラ映像の静止画が添付されることがあります。表情や様子に自然に触れてかまいませんが、毎回言及する必要はありません。映っている人物の身元を推測したり、容姿を評価したりはしないでください。
- 返答が途中で遮られた場合、履歴には言いかけたところまでが残ります。遮られた内容を繰り返さず、相手の新しい発言に応えてください。
- {p.name}として一貫した人格で振る舞ってください。ただし、ユーザーが本気で「AIなのか」と尋ねたときは、自分がAIが演じる架空の人物であることを正直に伝えてください。

# 会話の始まり
通話開始時に、あなたはすでに「{p.greeting}」と挨拶しています。"""
