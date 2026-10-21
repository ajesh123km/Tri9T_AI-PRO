from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    # Keep the developer SQLite database usable after adding extracted table
    # cells; production deployments should use a migration tool.
    columns = {column["name"] for column in inspect(engine).get_columns("version_nodes")}
    if "table_rows" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE version_nodes ADD COLUMN table_rows TEXT DEFAULT '[]'"))

