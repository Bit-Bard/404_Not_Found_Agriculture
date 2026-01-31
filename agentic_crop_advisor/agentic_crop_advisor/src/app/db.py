from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import MetaData, Table, Column
from sqlalchemy import String, DateTime, func, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine.url import URL

from .config import Settings


@dataclass(frozen=True)
class DbHandles:
    engine: Engine
    table: Table


def build_mysql_url(settings: Settings) -> URL:
    """
    SQLAlchemy URL using mysql-connector-python driver.
    """
    return URL.create(
        drivername="mysql+mysqlconnector",
        username=settings.mysql_user,
        password=settings.mysql_password or None,
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_database,
        query={"charset": "utf8mb4"},
    )


def make_engine(settings: Settings) -> Engine:
    """
    Create SQLAlchemy engine. Keep it simple and reliable for XAMPP.
    """
    url = build_mysql_url(settings)
    # pool_pre_ping helps recover from stale MySQL connections
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


def define_sessions_table(metadata: MetaData, table_name: str) -> Table:
    """
    One table to persist GraphState JSON per Telegram chat_id.
    """
    return Table(
        table_name,
        metadata,
        Column("chat_id", String(64), primary_key=True),
        Column("state_json", LONGTEXT, nullable=False),
        Column("created_at", DateTime(timezone=False), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=False), nullable=False, server_default=func.now(), onupdate=func.now()),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def init_db(settings: Settings) -> DbHandles:
    """
    Creates engine + ensures sessions table exists.
    Returns engine + table handle.
    """
    engine = make_engine(settings)
    metadata = MetaData()
    table = define_sessions_table(metadata, settings.mysql_table)

    try:
        metadata.create_all(engine)
    except SQLAlchemyError as e:
        raise RuntimeError(
            "DB init failed. Ensure XAMPP MySQL is running and the database exists. "
            "Also verify MYSQL_* env vars."
        ) from e

    return DbHandles(engine=engine, table=table)
