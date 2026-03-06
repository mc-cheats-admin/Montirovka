"""Alembic environment configuration for the AutoEdit backend.

This file wires Alembic to the project's runtime configuration and ORM models.

Key responsibilities:
- load the database URL from ``app.core.config.get_settings()`` instead of
  relying on the placeholder value in ``alembic.ini``;
- discover SQLAlchemy metadata from the current backend model module;
- support both offline and online migration modes;
- remain robust while the project structure evolves during staged generation.

The project currently exposes ORM models through ``app.db.models``. Depending on
how that module is implemented, metadata may be available via one of several
common patterns:

- ``Base.metadata``
- module-level ``metadata``
- one or more declarative model classes with ``__table__`` definitions

To keep migrations resilient, this file attempts metadata discovery in a
defensive way and raises a clear error if nothing usable is found.
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path
import sys
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import make_url
from sqlalchemy.schema import MetaData

# ---------------------------------------------------------------------------
# Ensure backend project root is importable.
# ---------------------------------------------------------------------------
#
# Alembic executes this file from the backend directory context. Still, to make
# imports deterministic across Windows local runs, Docker, and CI-like runners,
# we explicitly prepend the backend root directory to sys.path.
#
CURRENT_FILE = Path(__file__).resolve()
BACKEND_ROOT = CURRENT_FILE.parent.parent

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402


config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically according to the values in alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _normalize_database_url(url: str) -> str:
    """Normalize the SQLAlchemy database URL for Alembic.

    Args:
        url: Raw database URL from application settings.

    Returns:
        A normalized string form of the SQLAlchemy URL.

    Notes:
        - Validation is intentionally light but still helpful.
        - ``make_url`` ensures malformed URLs fail early with a clear traceback.
    """
    normalized = str(make_url(url))
    return normalized


def _discover_target_metadata() -> MetaData:
    """Discover SQLAlchemy metadata from ``app.db.models``.

    Returns:
        The project's target ``MetaData`` object used by Alembic autogeneration.

    Raises:
        RuntimeError: If metadata cannot be discovered from the models module.
    """
    import app.db.models as models_module  # imported lazily after sys.path setup

    # 1. Common pattern: module exports Base with Base.metadata.
    base = getattr(models_module, "Base", None)
    if base is not None:
        metadata = getattr(base, "metadata", None)
        if isinstance(metadata, MetaData):
            return metadata

    # 2. Another common pattern: module exports metadata directly.
    module_metadata = getattr(models_module, "metadata", None)
    if isinstance(module_metadata, MetaData):
        return module_metadata

    # 3. Fallback: scan module attributes for declarative model classes.
    discovered_metadata: MetaData | None = None

    for attribute_name in dir(models_module):
        attribute = getattr(models_module, attribute_name, None)
        table = getattr(attribute, "__table__", None)
        metadata = getattr(table, "metadata", None)
        if isinstance(metadata, MetaData):
            discovered_metadata = metadata
            break

    if discovered_metadata is not None:
        return discovered_metadata

    raise RuntimeError(
        "Unable to discover SQLAlchemy metadata from app.db.models. "
        "Expected Base.metadata, module-level metadata, or declarative models."
    )


settings = get_settings()
database_url = _normalize_database_url(settings.database_url)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = _discover_target_metadata()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    In offline mode Alembic does not create a DBAPI connection. Instead, it
    emits SQL statements directly using only the configured URL.
    """
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def _build_engine_config() -> dict[str, Any]:
    """Build SQLAlchemy engine configuration for online migrations.

    Returns:
        A dictionary compatible with ``engine_from_config``.
    """
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url
    return section


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In online mode Alembic creates an Engine and binds a live Connection to the
    migration context.
    """
    configuration = _build_engine_config()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        _configure_and_run_online(connection)


def _configure_and_run_online(connection: Connection) -> None:
    """Configure Alembic context for an active connection and run migrations.

    Args:
        connection: Active SQLAlchemy connection.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()