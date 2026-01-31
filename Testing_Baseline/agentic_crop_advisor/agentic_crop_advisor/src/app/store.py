from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import orjson
from sqlalchemy import select, insert, update
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import DbHandles, init_db
from .models import GraphState


log = logging.getLogger("store")


def _orjson_dumps(obj: object) -> str:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")


def _orjson_loads(s: str) -> dict:
    return orjson.loads(s)


@dataclass
class StateStore:
    """
    Minimal persistence layer.

    - mysql backend: one row per chat_id with state_json
    - json backend: one file (STORE_FILE) with dict {chat_id: state_dict}

    Interface:
      - load(chat_id) -> GraphState
      - save(state) -> None
    """

    settings: Settings
    backend: str
    db: Optional[DbHandles] = None
    json_path: Optional[Path] = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "StateStore":
        backend = settings.store_backend.lower()

        if backend == "mysql":
            db = init_db(settings)
            return cls(settings=settings, backend="mysql", db=db)

        # json fallback
        p = settings.store_file
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("{}", encoding="utf-8")
        return cls(settings=settings, backend="json", json_path=p)

    def load(self, chat_id: str) -> GraphState:
        chat_id = str(chat_id)

        if self.backend == "mysql":
            assert self.db is not None
            return self._load_mysql(chat_id)

        assert self.json_path is not None
        return self._load_json(chat_id)

    def save(self, state: GraphState) -> None:
        if self.backend == "mysql":
            assert self.db is not None
            self._save_mysql(state)
            return

        assert self.json_path is not None
        self._save_json(state)

    # ---------------- MySQL ----------------

    def _load_mysql(self, chat_id: str) -> GraphState:
        assert self.db is not None
        t = self.db.table

        try:
            with self.db.engine.connect() as conn:
                row = conn.execute(select(t.c.state_json).where(t.c.chat_id == chat_id)).fetchone()
        except SQLAlchemyError as e:
            raise RuntimeError("DB load failed. Check MySQL connectivity.") from e

        if not row:
            return GraphState(chat_id=chat_id)

        state_json = row[0]
        try:
            data = _orjson_loads(state_json)
            return GraphState.model_validate(data)
        except Exception as e:
            # If corrupted state exists, start fresh rather than crashing the bot
            log.exception("State parse failed for chat_id=%s. Resetting.", chat_id)
            return GraphState(chat_id=chat_id)

    def _save_mysql(self, state: GraphState) -> None:
        assert self.db is not None
        t = self.db.table

        payload = state.model_dump(mode="json")
        state_json = _orjson_dumps(payload)

        try:
            with self.db.engine.begin() as conn:
                exists = conn.execute(select(t.c.chat_id).where(t.c.chat_id == state.chat_id)).fetchone()
                if exists:
                    conn.execute(
                        update(t)
                        .where(t.c.chat_id == state.chat_id)
                        .values(state_json=state_json)
                    )
                else:
                    conn.execute(
                        insert(t).values(chat_id=state.chat_id, state_json=state_json)
                    )
        except SQLAlchemyError as e:
            raise RuntimeError("DB save failed. Check MySQL permissions and table.") from e

    # ---------------- JSON (fallback) ----------------

    def _read_all_json(self) -> dict:
        assert self.json_path is not None
        try:
            raw = self.json_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            log.exception("Failed reading JSON store. Resetting file.")
            self.json_path.write_text("{}", encoding="utf-8")
            return {}

    def _write_all_json(self, data: dict) -> None:
        assert self.json_path is not None
        self.json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_json(self, chat_id: str) -> GraphState:
        all_data = self._read_all_json()
        entry = all_data.get(chat_id)
        if not entry:
            return GraphState(chat_id=chat_id)
        try:
            return GraphState.model_validate(entry)
        except Exception:
            log.exception("JSON state parse failed for chat_id=%s. Resetting.", chat_id)
            return GraphState(chat_id=chat_id)

    def _save_json(self, state: GraphState) -> None:
        all_data = self._read_all_json()
        all_data[state.chat_id] = state.model_dump(mode="json")
        self._write_all_json(all_data)
