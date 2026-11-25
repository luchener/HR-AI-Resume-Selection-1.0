"""Transactional announcement adapter; no legacy configuration or filesystem I/O."""
from datetime import datetime, timedelta, timezone
import math
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from db_announcement_state import announcements, dismissals
from db_audit import append_audit
from db_models import User


class AnnouncementError(Exception):
    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.detail, self.status = detail, status


def _uuid(value):
    try:
        identity = UUID(str(value))
        if not identity.int:
            raise ValueError
        return identity
    except (ValueError, TypeError, AttributeError):
        raise AnnouncementError('公告不存在。', 404) from None


def parse_time(value):
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return datetime.fromtimestamp(value, timezone.utc)
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00').replace('z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        raise AnnouncementError('公告时间无效。') from None


def _text(value, name, maximum):
    if not isinstance(value, str):
        raise AnnouncementError(f'{name}必须为文本。')
    result = value.strip()
    if not result:
        raise AnnouncementError(f'{name}不能为空。')
    if len(result) > maximum:
        raise AnnouncementError(f'{name}不能超过 {maximum} 字。')
    if '\x00' in result or any(0xD800 <= ord(char) <= 0xDFFF for char in result):
        raise AnnouncementError(f'{name}含无效字符。')
    return result


def schedule(payload, *, now):
    units = {'minute': 1, 'hour': 60, 'day': 1440}
    mode = payload.get('start_mode') or 'now'
    if mode not in ('now', 'at', 'delay'):
        raise AnnouncementError('投放时间模式无效。')
    def minutes(value, unit, *, delay=False):
        if not isinstance(unit, str) or unit not in units:
            raise AnnouncementError('延迟时间单位无效。' if delay else '有效期单位无效。')
        try:
            if isinstance(value, bool):
                raise ValueError
            return max(0 if delay else 1, int(value or 0)) * units[unit]
        except (TypeError, ValueError, OverflowError):
            raise AnnouncementError('延迟时间必须为数字。' if delay else '有效期必须为数字。') from None
    try:
        if mode == 'at':
            start = max(now, parse_time(payload.get('start_at')))
        elif mode == 'delay':
            start = now + timedelta(minutes=minutes(payload.get('delay_value'), payload.get('delay_unit') or 'minute', delay=True))
        else:
            start = now
        end = start + timedelta(minutes=minutes(payload.get('duration_value'), payload.get('duration_unit') or 'day'))
    except (OverflowError, TypeError):
        raise AnnouncementError('公告时间超出支持范围。') from None
    return mode, start, end


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _public(row, now, *, user=False):
    result = {key: row[key] for key in ('title', 'content')}
    result.update(announcement_id=str(row['id']), created_at=_iso(row['created_at']),
                  start_at=_iso(row['start_at']), end_at=_iso(row['end_at']))
    if not user:
        status = ('cancelled' if row['status'] == 'cancelled' else
                  'scheduled' if now < row['start_at'] else 'expired' if now >= row['end_at'] else 'active')
        result.update(start_mode=row['start_mode'], status=status,
                      created_by_id=str(row['created_by_id']), created_by_name=row['created_by_name'])
        if row['cancelled_at'] is not None:
            result['cancelled_at'] = _iso(row['cancelled_at'])
    return result


def _pagination(value, default, *, size=False):
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1, min(100, parsed)) if size else max(1, parsed)


class AnnouncementService:
    def __init__(self, session_factory, *, admin_emails=(), clock=None):
        if not callable(session_factory) or isinstance(admin_emails, (str, bytes)):
            raise TypeError('Explicit session factory and email policy required')
        self.sessions = session_factory
        self.admin_emails = frozenset(str(email).strip().lower() for email in admin_emails if str(email).strip())
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Announcement clock must return an aware datetime')
        return value.astimezone(timezone.utc)

    def _actor(self, session, actor_id, password_version, *, admin=False):
        try:
            actor = _uuid(actor_id)
        except AnnouncementError:
            raise AnnouncementError('Invalid or revoked token', 401) from None
        row = session.execute(sa.select(User.id, User.username, User.email, User.is_admin, User.status,
            User.deleted_at, User.password_version).where(User.id == actor).with_for_update()).mappings().one_or_none()
        if (row is None or row['status'] != 'active' or row['deleted_at'] is not None
                or type(password_version) is not int or row['password_version'] != password_version):
            raise AnnouncementError('Invalid or revoked token', 401)
        if admin and not (row['is_admin'] or (row['email'] or '').strip().lower() in self.admin_emails):
            raise AnnouncementError('Administrator required', 403)
        return row

    def _audit(self, session, actor, identity, action, request_id):
        append_audit(session, actor_user_id=actor['id'], action='announcement.' + action,
                     target_type='announcement', target_id=str(identity), request_id=request_id)

    def create(self, actor_id, payload, *, password_version, request_id):
        if type(payload) is not dict:
            raise AnnouncementError('请求体必须为对象。')
        title = _text(payload.get('title') or '', '公告标题', 100)
        content = _text(payload.get('content') or '', '公告内容', 5000)
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version, admin=True)
            now = self._now()
            mode, start, end = schedule(payload, now=now)
            identity = uuid4()
            session.execute(announcements.insert().values(id=identity, title=title, content=content,
                start_mode=mode, start_at=start, end_at=end, status='active', created_at=now,
                created_by_id=actor['id'], created_by_name=actor['username']))
            self._audit(session, actor, identity, 'create', request_id)
        return {'announcement_id': str(identity)}

    def list_admin(self, actor_id, *, password_version, page=1, size=10):
        page, size = _pagination(page, 1), _pagination(size, 10, size=True)
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version, admin=True)
            now = self._now()
            matches = sa.select(announcements).where(announcements.c.deleted_at.is_(None)).cte('announcement_matches')
            total = sa.select(sa.func.count().label('total')).select_from(matches).subquery('announcement_total')
            # Beyond the final page must still return the total. Avoid unbounded
            # OFFSET integers from turning a valid legacy pagination request into a DB error.
            offset = min((page - 1) * size, 2**63 - 1)
            selected = sa.select(matches).order_by(matches.c.start_at.desc(), matches.c.id.desc())\
                .offset(offset).limit(size).subquery('announcement_page')
            rows = session.execute(sa.select(total.c.total, *selected.c).select_from(
                total.outerjoin(selected, sa.true())).order_by(selected.c.start_at.desc(), selected.c.id.desc())).mappings().all()
            return {'total': int(rows[0]['total']), 'items': [_public(row, now) for row in rows if row['id'] is not None]}

    def active_for_user(self, actor_id, *, password_version):
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            now = self._now()
            already_closed = sa.exists(sa.select(1).select_from(dismissals).where(
                dismissals.c.user_id == actor['id'], dismissals.c.announcement_id == announcements.c.id))
            rows = session.execute(sa.select(announcements).where(announcements.c.deleted_at.is_(None),
                announcements.c.status == 'active', announcements.c.start_at <= now, announcements.c.end_at > now,
                ~already_closed).order_by(announcements.c.start_at.desc(), announcements.c.id.desc())).mappings().all()
            return {'items': [_public(row, now, user=True) for row in rows]}

    def _change(self, actor_id, identity, *, password_version, request_id, delete=False):
        identity = _uuid(identity)
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version, admin=True)
            row = session.execute(sa.select(announcements).where(announcements.c.id == identity).with_for_update()).mappings().one_or_none()
            if row is None:
                raise AnnouncementError('公告不存在。', 404)
            changed = row['deleted_at'] is None if delete else row['status'] != 'cancelled'
            if changed:
                values = {'deleted_at': self._now()} if delete else {'status': 'cancelled', 'cancelled_at': self._now()}
                session.execute(announcements.update().where(announcements.c.id == identity).values(**values))
                self._audit(session, actor, identity, 'delete' if delete else 'cancel', request_id)
        return {'message': '公告已删除（删除记录已保留）。' if delete else '公告已撤回，将不再弹出。'}

    def cancel(self, actor_id, announcement_id, **kwargs):
        return self._change(actor_id, announcement_id, **kwargs)

    def delete(self, actor_id, announcement_id, **kwargs):
        return self._change(actor_id, announcement_id, delete=True, **kwargs)

    def dismiss(self, actor_id, announcement_id, *, password_version, request_id):
        identity = _uuid(announcement_id)
        with self.sessions() as session, session.begin():
            actor = self._actor(session, actor_id, password_version)
            # Same order as management: actor first, announcement second. The
            # announcement lock also excludes concurrent cancel/soft deletion.
            exists = session.execute(sa.select(announcements.c.id).where(
                announcements.c.id == identity).with_for_update()).scalar_one_or_none()
            if exists is None:
                raise AnnouncementError('公告不存在。', 404)
            # The actor row is already locked, so repeated requests by the
            # same user serialize before this check. This makes the audit
            # event idempotent as well as the dismissal row.
            existing = session.execute(sa.select(dismissals.c.user_id).where(
                dismissals.c.user_id == actor['id'], dismissals.c.announcement_id == identity
            ).with_for_update()).scalar_one_or_none()
            if existing is None:
                session.execute(dismissals.insert().values(user_id=actor['id'],
                    announcement_id=identity, dismissed_at=self._now()))
                self._audit(session, actor, identity, 'dismiss', request_id)
        return {'message': '已关闭。'}
