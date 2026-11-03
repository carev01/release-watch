import os

from alembic import context
from sqlalchemy import create_engine

from release_watch import models  # noqa: F401  (registers tables)
from release_watch.db import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ["RW_DATABASE_URL"]


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
