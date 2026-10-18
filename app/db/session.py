import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./faculty.db")


def make_engine(url: str | None = None):
    url = url or DATABASE_URL
    # Railway gives "postgres://", SQLAlchemy wants "postgresql://"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # FastAPI runs requests on several threads; SQLite needs this to allow it.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, connect_args=connect_args)


def add_missing_columns(engine) -> list[str]:
    """Add columns that exist in the models but not yet in the database.

    create_all() makes missing TABLES but never changes existing ones, so a
    faculty.db from an older version would lack new columns. Only nullable
    columns are added this way (safe: old rows just get NULL).
    """
    added = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or not col.nullable:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}'))
                added.append(f"{table.name}.{col.name}")
    return added


def init_db(url: str | None = None) -> sessionmaker[Session]:
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)