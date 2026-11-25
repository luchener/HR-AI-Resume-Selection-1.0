"""Explicit opt-in PostgreSQL session factory; importing never opens connections."""
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from db_security import normalize_url


def build_runtime(database_url: str):
    url = make_url(normalize_url(database_url))
    if url.query:
        raise ValueError('Runtime URL query overrides are forbidden')
    if url.username != 'resume_app':
        raise ValueError('Runtime must use the dedicated resume_app role')
    engine = create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2,
                           pool_timeout=10, pool_recycle=1800, hide_parameters=True,
                           connect_args={'connect_timeout': 10, 'options': '-c statement_timeout=15000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=30000'})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


def verify_runtime(engine):
    """Explicit startup probe; no import-time I/O or privileged ledger access."""
    from sqlalchemy import text
    with engine.connect() as conn:
        role = conn.execute(text('SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user')).one()
        if role[0] != 'resume_app' or any(role[1:]):
            raise ValueError('Unsafe runtime role')
        if conn.scalar(text('SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user))')):
            raise ValueError('Runtime role must have no role memberships')
        if conn.scalar(text("SELECT has_schema_privilege(current_user,'public','CREATE') OR has_database_privilege(current_user,current_database(),'TEMP')")):
            raise ValueError('Runtime role has forbidden DDL privileges')
        for table, privileges in {'legacy_records':'SELECT', 'alembic_version':'SELECT', 'audit_logs':'UPDATE,DELETE,TRUNCATE'}.items():
            if conn.scalar(text('SELECT has_table_privilege(current_user,:table,:privileges)'), {'table':table, 'privileges':privileges}):
                raise ValueError('Runtime role has forbidden table privileges')
        from db_check_schema import metadata
        from sqlalchemy import select
        for name, table in metadata().tables.items():
            if name not in {'legacy_records'}:
                conn.execute(select(table).limit(0))
    return True


@contextmanager
def transaction(factory):
    with factory() as session, session.begin():
        yield session
