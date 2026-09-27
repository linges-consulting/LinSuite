"""Alembic runs as the schema owner (DATABASE_URL_MIGRATE), never as the app role."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Every model module must be imported here, or its table is missing from the metadata
# that autogenerate compares the database against.
from auth import models as _auth_models  # noqa: F401
from core import models as _core_models  # noqa: F401
from core.config import get_settings
from core.db import Base
from customers import models as _customers_models  # noqa: F401
from forms import models as _forms_models  # noqa: F401
from notes import models as _notes_models  # noqa: F401
from scheduling import models as _scheduling_models  # noqa: F401
from settings import models as _settings_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    # Never silence the application's loggers when migrations run in-process (tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url_migrate,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url_migrate)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
