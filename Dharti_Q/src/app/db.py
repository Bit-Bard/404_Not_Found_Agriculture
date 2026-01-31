from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import MetaData, Table, Column
from sqlalchemy import String, DateTime, func, create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings


@dataclass(frozen=True)
class DbHandles:
    engine: Engine
    sessions: Table
    farmers: Table
    images: Table


def build_mysql_url(settings: Settings) -> URL:
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
    url = build_mysql_url(settings)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


def define_sessions_table(metadata: MetaData, name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("chat_id", String(64), primary_key=True),
        Column("state_json", LONGTEXT, nullable=False),
        Column("created_at", DateTime(timezone=False), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=False), nullable=False, server_default=func.now(), onupdate=func.now()),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def define_farmers_table(metadata: MetaData, name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("chat_id", String(64), primary_key=True),
        Column("farmer_name", String(120), nullable=True),
        Column("crop", String(80), nullable=True),
        Column("land_size", String(32), nullable=True),  # keep as string to avoid driver decimal quirks
        Column("land_unit", String(32), nullable=True),
        Column("location_text", String(255), nullable=True),
        Column("lat", String(32), nullable=True),
        Column("lon", String(32), nullable=True),
        Column("created_at", DateTime(timezone=False), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=False), nullable=False, server_default=func.now(), onupdate=func.now()),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def define_images_table(metadata: MetaData, name: str) -> Table:
    # Use BIGINT autoincrement via MySQL dialect by declaring Integer + autoincrement in raw SQL usually,
    # but SQLAlchemy will generate correct DDL for MySQL with autoincrement on primary key integer.
    from sqlalchemy import BigInteger

    return Table(
        name,
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("chat_id", String(64), nullable=False),
        Column("telegram_file_id", String(256), nullable=True),
        Column("file_path", String(512), nullable=False),
        Column("caption", String(512), nullable=True),
        Column("created_at", DateTime(timezone=False), nullable=False, server_default=func.now()),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def init_db(settings: Settings) -> DbHandles:
    engine = make_engine(settings)
    md = MetaData()

    sessions = define_sessions_table(md, settings.mysql_sessions_table)
    farmers = define_farmers_table(md, settings.mysql_farmers_table)
    images = define_images_table(md, settings.mysql_images_table)

    try:
        md.create_all(engine)
    except SQLAlchemyError as e:
        raise RuntimeError(
            "DB init failed. Ensure XAMPP MySQL is running and the database exists. Verify MYSQL_* env vars."
        ) from e

    return DbHandles(engine=engine, sessions=sessions, farmers=farmers, images=images)
