"""Migration-only target snapshot certification; never a post-cutover merge tool.

Digests cover every column and row, including server defaults and encrypted
values. They certify target stability since import, not independent correctness
of the source-to-target converter. No row values leave this module.
"""
import hashlib
from sqlalchemy import select, text, and_, not_
from db_check_schema import metadata

ATTESTATION_KEY = 'target_attestation_v1'


def lock_target(session):
    # Names come solely from reviewed static metadata, never request/source data.
    preparer = session.bind.dialect.identifier_preparer
    names = ', '.join(preparer.quote(name) for name in sorted(metadata().tables))
    session.execute(text(f'LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE'))


def target_attestation(session):
    session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    session.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
    result = {}
    for name, table in sorted(metadata().tables.items()):
        statement = select(table)
        if name == 'legacy_records':
            statement = statement.where(not_(and_(table.c.root_name == '__migration__',
                                                  table.c.record_key == ATTESTATION_KEY)))
        # PostgreSQL JSONB emits canonical key order and preserves JSON types.
        # Each row is hashed separately; ordering by digest is independent of IDs.
        subquery = statement.subquery('attested_row')
        from sqlalchemy import func, cast, String
        from sqlalchemy.dialects.postgresql import JSONB
        encoded = select(cast(cast(func.row_to_json(subquery.table_valued()), JSONB), String))
        hashes = []
        for (value,) in session.execute(encoded):
            hashes.append(hashlib.sha256(value.encode('utf-8')).digest())
        digest = hashlib.sha256(b''.join(sorted(hashes))).hexdigest()
        result[name] = {'count': len(hashes), 'sha256': digest}
    return {'version': 1, 'tables': result}


def verify_attestation(session, expected):
    if expected is None or expected != target_attestation(session):
        raise ValueError('Target rows differ from imported snapshot; replay certification blocked')
