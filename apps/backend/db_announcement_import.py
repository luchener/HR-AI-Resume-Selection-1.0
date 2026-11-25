"""Explicit, offline-only projection of one frozen announcement inventory.

The general importer retains original JSON in protected legacy_records. This
separate projection runs only as resume_migrator against empty business tables;
it neither opens a connection nor rereads files or activates production routes.
"""
import hashlib
import json
from pathlib import PurePosixPath

import sqlalchemy as sa

from db_announcement_state import announcements, dismissals
from db_announcements import AnnouncementError, _uuid, _text, parse_time
from db_migrate_json import validate_inventory
from db_models import LegacyRecord, User


def project_announcement_snapshot(report):
    validate_inventory(report)
    known = {_uuid(value) for value in report['known_ids']['users']}
    records, closed, read_files = {}, [], set()
    try:
        for root, key, wrapped in report['legacy_records']:
            if root not in {'announcements', 'announcement_reads'}:
                continue
            path = PurePosixPath(key)
            if (len(path.parts) != 2 or path.parts[0] != root or path.suffix != '.json'
                    or key not in report['manifest'] or type(wrapped) is not dict
                    or type(wrapped.get('value')) is not dict):
                raise ValueError
            row = wrapped['value']
            identity = _uuid(path.stem)
            if root == 'announcement_reads':
                if identity not in known or identity in read_files or type(row.get('dismissed')) is not list:
                    raise ValueError
                read_files.add(identity)
                ids = {_uuid(value) for value in row['dismissed']}
                if ids:
                    # Legacy files only retain their last update timestamp,
                    # not a separate timestamp for every dismissed ID.
                    updated = parse_time(row.get('updated_at'))
                    closed.extend(dict(user_id=identity, announcement_id=aid, dismissed_at=updated)
                                  for aid in sorted(ids, key=str))
                continue
            if identity in records or _uuid(row.get('announcement_id')) != identity:
                raise ValueError
            creator = _uuid(row.get('created_by_id'))
            if creator not in known:
                raise ValueError
            start, end = parse_time(row.get('start_at')), parse_time(row.get('end_at'))
            status, mode = row.get('status'), row.get('start_mode')
            cancelled = parse_time(row['cancelled_at']) if row.get('cancelled_at') else None
            if (end <= start or status not in ('active', 'cancelled') or mode not in ('now', 'at', 'delay')
                    or (status == 'cancelled') != (cancelled is not None)):
                raise ValueError
            name = row.get('created_by_name')
            if not isinstance(name, str) or len(name) > 64 or '\x00' in name:
                raise ValueError
            records[identity] = dict(id=identity, title=_text(row.get('title'), '公告标题', 100),
                content=_text(row.get('content'), '公告内容', 5000), start_mode=mode, start_at=start, end_at=end,
                status=status, created_by_id=creator, created_by_name=name,
                created_at=parse_time(row.get('created_at')), cancelled_at=cancelled,
                deleted_at=parse_time(row['deleted_at']) if row.get('deleted_at') else None)
        if any(item['announcement_id'] not in records for item in closed):
            raise ValueError
    except (AnnouncementError, KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('Announcement snapshot projection blocked') from None
    # The digest binds normalized values and source manifest, without printing
    # content or account identities in conversion diagnostics.
    rows = sorted(records.values(), key=lambda row: str(row['id']))
    closed.sort(key=lambda row: (str(row['user_id']), str(row['announcement_id'])))
    body = dict(announcements=rows, dismissals=closed, inventory_sha256=report['inventory_sha256'])
    digest = hashlib.sha256(json.dumps(body, default=str, sort_keys=True, ensure_ascii=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return body | {'projection_sha256': digest}


def apply_announcement_snapshot(session, report):
    if not session.in_transaction():
        raise ValueError('Announcement projection requires caller-owned transaction')
    plan = project_announcement_snapshot(report)
    if session.scalar(sa.text('SELECT current_user')) != 'resume_migrator':
        raise ValueError('Announcement projection requires the migration role')
    session.execute(sa.text('SELECT pg_advisory_xact_lock(71616001)'))
    ledger = session.execute(sa.select(LegacyRecord.payload).where(
        LegacyRecord.root_name == '__announcement_migration__', LegacyRecord.record_key == 'projection')).scalar_one_or_none()
    counts = {'announcements': len(plan['announcements']), 'dismissals': len(plan['dismissals'])}
    if ledger is not None:
        if ledger.get('projection_sha256') != plan['projection_sha256']:
            raise ValueError('Conflicting announcement snapshot already projected')
        return counts | {'replayed': True}
    users = {row['created_by_id'] for row in plan['announcements']} | {row['user_id'] for row in plan['dismissals']}
    if users:
        found = session.execute(sa.select(User.id).where(User.id.in_(users)).order_by(User.id).with_for_update()).scalars().all()
        if set(found) != users:
            raise ValueError('Announcement snapshot users are missing from target')
    session.execute(sa.text('LOCK TABLE announcements, announcement_dismissals IN SHARE ROW EXCLUSIVE MODE'))
    if (session.scalar(sa.select(sa.func.count()).select_from(announcements))
            or session.scalar(sa.select(sa.func.count()).select_from(dismissals))):
        raise ValueError('Announcement projection requires empty target tables')
    if plan['announcements']:
        session.execute(announcements.insert(), plan['announcements'])
    if plan['dismissals']:
        session.execute(dismissals.insert(), plan['dismissals'])
    session.execute(sa.insert(LegacyRecord).values(root_name='__announcement_migration__', record_key='projection',
        payload=dict(projection_sha256=plan['projection_sha256'], inventory_sha256=plan['inventory_sha256'], **counts)))
    return counts | {'replayed': False}
