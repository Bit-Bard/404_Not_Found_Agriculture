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

    def save_image_record(self, chat_id: str, file_path: str, caption: Optional[str], telegram_file_id: Optional[str]) -> None:
        """
        Store uploaded crop image metadata in DB (mysql) or ignore (json).
        """
        if self.backend != "mysql":
            return
        assert self.db is not None
        t = self.db.images
        try:
            with self.db.engine.begin() as conn:
                conn.execute(
                    insert(t).values(
                        chat_id=str(chat_id),
                        telegram_file_id=telegram_file_id,
                        file_path=file_path,
                        caption=caption,
                    )
                )
        except SQLAlchemyError:
            log.exception("Failed to insert image record for chat_id=%s", chat_id)

    # ---------------- MySQL ----------------

    def _load_mysql(self, chat_id: str) -> GraphState:
        assert self.db is not None
        t = self.db.sessions

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
        except Exception:
            log.exception("State parse failed for chat_id=%s. Resetting.", chat_id)
            return GraphState(chat_id=chat_id)

    def _save_mysql(self, state: GraphState) -> None:
        assert self.db is not None
        sessions = self.db.sessions
        farmers = self.db.farmers

        payload = state.model_dump(mode="json")
        state_json = _orjson_dumps(payload)

        ctx = state.context
        farmer_row = {
            "chat_id": state.chat_id,
            "farmer_name": (ctx.farmer_name or None),
            "crop": (ctx.crop or None),
            "land_size": (str(ctx.land_size) if ctx.land_size is not None else None),
            "land_unit": (ctx.land_unit or None),
            "location_text": (ctx.location_text or None),
            "lat": (str(ctx.lat) if ctx.lat is not None else None),
            "lon": (str(ctx.lon) if ctx.lon is not None else None),
        }

        try:
            with self.db.engine.begin() as conn:
                # sessions upsert
                exists = conn.execute(select(sessions.c.chat_id).where(sessions.c.chat_id == state.chat_id)).fetchone()
                if exists:
                    conn.execute(
                        update(sessions)
                        .where(sessions.c.chat_id == state.chat_id)
                        .values(state_json=state_json)
                    )
                else:
                    conn.execute(insert(sessions).values(chat_id=state.chat_id, state_json=state_json))

                # farmers upsert (profile snapshot)
                f_exists = conn.execute(select(farmers.c.chat_id).where(farmers.c.chat_id == state.chat_id)).fetchone()
                if f_exists:
                    conn.execute(
                        update(farmers)
                        .where(farmers.c.chat_id == state.chat_id)
                        .values(**farmer_row)
                    )
                else:
                    conn.execute(insert(farmers).values(**farmer_row))

        except SQLAlchemyError as e:
            raise RuntimeError("DB save failed. Check MySQL permissions and tables.") from e

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
