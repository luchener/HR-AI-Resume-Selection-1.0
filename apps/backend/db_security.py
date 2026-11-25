"""Shared database boundaries. No import-time connection and no application cutover."""
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def normalize_url(value: str) -> str:
    url = make_url(value)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("PostgreSQL with psycopg is required")
    if not url.database or not url.username:
        raise ValueError("Database and explicit username are required")
    return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def migration_engine(value: str):
    return create_engine(normalize_url(value), pool_pre_ping=True, hide_parameters=True,
                         connect_args={"connect_timeout": 10, "options": "-c statement_timeout=60000 -c lock_timeout=5000"})


def bounded_page(page: int, size: int) -> tuple[int, int]:
    if isinstance(page, bool) or isinstance(size, bool) or not isinstance(page, int) or not isinstance(size, int):
        raise ValueError("Pagination requires integers")
    if page < 1 or not 1 <= size <= 100 or (page - 1) * size > 100000:
        raise ValueError("Pagination outside supported range")
    return (page - 1) * size, size


def literal_like(value: str) -> str:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("Search text exceeds limit")
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
