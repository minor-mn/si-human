"""話題・要約・コメントの準備データをJSONファイルに保存する。"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.comments import ThreadComments
from app.llm import Provider
from app.news import NewsBriefing

log = logging.getLogger(__name__)
ID_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"


class StoredPreparation(BaseModel):
    version: Literal[1] = 1
    preparation_id: str = Field(pattern=ID_PATTERN)
    prepared_at: datetime
    provider: Provider
    news: NewsBriefing | None
    threads: list[ThreadComments] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    used: bool = False


class PreparationStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def save(self, preparation: StoredPreparation) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{preparation.preparation_id}.json"
        temporary = None
        try:
            # 同じディレクトリで書き終えてから置き換え、途中のJSONを公開しない。
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory,
                prefix=f".{preparation.preparation_id}-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(preparation.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, preparation_id: str) -> StoredPreparation | None:
        if not re.fullmatch(ID_PATTERN, preparation_id):
            return None
        path = self.directory / f"{preparation_id}.json"
        try:
            with path.open("rb") as handle:
                data = handle.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("準備ファイルが大きすぎます")
            preparation = StoredPreparation.model_validate_json(data)
            if preparation.preparation_id != preparation_id:
                raise ValueError("準備IDが一致しません")
            return preparation
        except FileNotFoundError:
            return None
        except (ValidationError, ValueError) as exc:
            # 壊れたファイルは残し、内容をログに丸ごと出さない。
            log.warning("準備ファイルを読み込めません（%s、%s）", path.name, type(exc).__name__)
            return None
