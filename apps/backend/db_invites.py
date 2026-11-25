"""Explicit invite workflow on the shared public-auth transaction boundary.

No environment/legacy/provider imports. Mail callbacks run outside locks and
must enforce network timeouts. Only the committed current invitation is returned.
"""
from datetime import timedelta
import re
import secrets
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from db_audit import append_audit
from db_invite_cipher import InviteCipher
from db_invite_state import requests
from db_models import User
from db_public_auth import PublicAuthError, PublicAuthService, _email
from db_public_auth_state import invites, challenges


def _text(value, minimum, maximum):
    if (type(value) is not str or not minimum <= len(value.strip()) <= maximum or
            re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]', value)):
        raise PublicAuthError('文本长度或格式不正确。')
    return value.strip()


def _identity(value):
    try:
        result = UUID(str(value))
        if not result.int:
            raise ValueError
        return result
    except (TypeError, ValueError, AttributeError):
        raise PublicAuthError('申请单不存在。', 404) from None


def _mask(email):
    if not email:
        return ''
    local, domain = email.split('@', 1)
    return (local[0] if len(local) > 1 else '') + '***@' + domain


class InviteService:
    def __init__(self, public_auth, *, cipher, send_mail, ttl_hours=24, max_rejections=3, per_ip_day=10):
        if (not isinstance(public_auth, PublicAuthService) or not isinstance(cipher, InviteCipher) or
                not callable(send_mail) or type(ttl_hours) is not int or not 1 <= ttl_hours <= 720 or
                type(max_rejections) is not int or not 1 <= max_rejections <= 20 or
                type(per_ip_day) is not int or not 1 <= per_ip_day <= 100):
            raise ValueError('Explicit invitation composition and bounded policy required')
        self.auth, self.cipher, self.send_mail = public_auth, cipher, send_mail
        self.ttl_hours, self.max_rejections, self.per_ip_day = ttl_hours, max_rejections, per_ip_day

    def _actor(self, session, identity, version):
        row = session.execute(sa.select(User.__table__).where(User.id == _identity(identity))
                              .with_for_update()).mappings().one_or_none()
        if (row is None or row['status'] != 'active' or row['deleted_at'] is not None or
                type(version) is not int or row['password_version'] != version):
            return PublicAuthError('Invalid or revoked token', 401)
        if (row['email'] or '').lower() not in self.auth.admin_emails:
            return PublicAuthError('Superadministrator required', 403)
        return row

    def _audit(self, session, actor, action, target, request_id):
        append_audit(session, actor_user_id=actor['id'], action='invite.' + action,
                     target_type='invite', target_id=str(target), request_id=request_id)

    def _request(self, session, identity):
        row = session.execute(sa.select(requests).where(requests.c.id == identity)
                              .with_for_update()).mappings().one_or_none()
        return row if row else PublicAuthError('申请单不存在。', 404)

    def _rejected(self, session, email_hash):
        return session.scalar(sa.select(sa.func.count()).select_from(requests).where(
            requests.c.email_hash == email_hash, requests.c.status == 'rejected'))

    def submit(self, email, note, *, ip, request_id):
        email, note = _email(email), _text(note, 2, 200)
        if type(request_id) is not str or not 1 <= len(request_id) <= 128:
            raise PublicAuthError('Invalid request ID')
        if email in self.auth.admin_emails:
            raise PublicAuthError('该邮箱不可申请公开注册。')
        def create(session, now):
            for namespace, subject, count, seconds in [('invite_apply_ip', ip, self.per_ip_day, 86400),
                                                       ('invite_apply_email', email, 1, 60)]:
                if error := self.auth._take(session, now, namespace, subject, count, seconds):
                    return error
            email_hash = self.auth.digest('email', email)
            if self._rejected(session, email_hash) >= self.max_rejections:
                return PublicAuthError('该邮箱的申请已多次未通过，请联系管理员。', 429)
            if session.scalar(sa.select(sa.exists().where(requests.c.email_hash == email_hash,
                                                          requests.c.status == 'pending'))):
                return PublicAuthError('该邮箱已有申请在审批中，请耐心等待。', 409)
            if session.scalar(sa.select(sa.func.count()).select_from(requests)) >= self.auth.max_keys:
                return PublicAuthError('申请容量已满，请联系管理员。', 503)
            identity = uuid4()
            sealed = self.cipher.encrypt('request', identity, dict(email=email, note=note, reject_reason=''))
            session.execute(requests.insert().values(id=identity, email_hash=email_hash, sealed=sealed, created_at=now))
            # Anonymous submission is not an authenticated user operation.
            from db_check_schema import metadata
            session.execute(metadata().tables['audit_logs'].insert().values(actor_identity='anonymous',
                action='invite.submit', target_type='invite', target_id=str(identity), request_id=request_id,
                metadata={'outcome':'success'}))
            return dict(message='申请已提交，管理员审批通过后邀请码将发送至你的邮箱。', email=email)
        return self.auth._transaction(create)

    def list_requests(self, actor_id, *, password_version, status='pending', page=1, size=200):
        if status not in {'pending', 'approved', 'rejected'}:
            raise PublicAuthError('申请状态不正确。')
        self._page(page, size)
        def read(session, now):
            actor = self._actor(session, actor_id, password_version)
            if isinstance(actor, PublicAuthError):
                return actor
            query = sa.select(requests).where(requests.c.status == status)
            total = session.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
            items = []
            for row in session.execute(query.order_by(requests.c.created_at.desc(), requests.c.id)
                                       .offset((page-1)*size).limit(size)).mappings():
                data = self.cipher.decrypt('request', row['id'], row['sealed'])
                items.append(dict(request_id=str(row['id']), email=_mask(data['email']), note=data['note'],
                    status=row['status'], created_at=row['created_at'].isoformat(),
                    reviewed_at=row['reviewed_at'].isoformat() if row['reviewed_at'] else None,
                    reject_reason=data['reject_reason'], code_sent=row['code_sent'],
                    rejected_count=self._rejected(session, row['email_hash'])))
            return dict(items=items, total=total)
        return self.auth._transaction(read)

    @staticmethod
    def _page(page, size):
        if type(page) is not int or not 1 <= page <= 10000 or type(size) is not int or not 1 <= size <= 200:
            raise PublicAuthError('分页参数不正确。')

    def _allocate(self, session, now, actor, email, note, *, request_identity=None, pending=False):
        if session.scalar(sa.select(sa.func.count()).select_from(invites)) >= self.auth.max_keys:
            return PublicAuthError('邀请码容量已满，请联系管理员。', 503)
        for _ in range(8):
            code = ''.join(secrets.choice('ABCDEFGHJKMNPQRSTUVWXYZ23456789') for _ in range(self.auth.invite_length))
            key = self.auth.digest('invite', code)
            details = self.cipher.encrypt('invite', key, dict(email=email, note=note))
            result = session.execute(insert(invites).values(code_hash=key, email_hash=self.auth.digest('email', email),
                created_by=actor['id'], created_at=now, expires_at=now+timedelta(hours=self.ttl_hours),
                details=details, request_id=request_identity, delivery_pending=pending)
                .on_conflict_do_nothing().returning(invites.c.code_hash)).scalar_one_or_none()
            if result:
                return key, code
        return PublicAuthError('邀请码生成失败，请重试。', 503)

    def generate(self, actor_id, *, password_version, request_id, count=1, note='', email=''):
        if type(count) is not int or not 1 <= count <= 20:
            raise PublicAuthError('数量必须为1至20的整数。')
        if type(email) is not str:
            raise PublicAuthError('邮箱格式不正确。')
        note, email = _text(note, 0, 200), _email(email) if email else ''
        if email in self.auth.admin_emails:
            raise PublicAuthError('该邮箱不可公开注册。')
        def generate(session, now):
            actor = self._actor(session, actor_id, password_version)
            if isinstance(actor, PublicAuthError):
                return actor
            if session.scalar(sa.select(sa.func.count()).select_from(invites)) + count > self.auth.max_keys:
                return PublicAuthError('邀请码容量已满，请联系管理员。', 503)
            codes = []
            # Batch allocation has a savepoint: a later collision cannot commit a
            # partially allocated batch when business counters commit normally.
            with session.begin_nested() as batch:
                for _ in range(count):
                    allocated = self._allocate(session, now, actor, email, note)
                    if isinstance(allocated, PublicAuthError):
                        batch.rollback()
                        return allocated
                    key, code = allocated
                    self._audit(session, actor, 'generate', key, request_id)
                    codes.append(code)
            return dict(codes=codes, message='已生成邀请码，请立即复制保存（服务器不保存明文）。')
        return self.auth._transaction(generate)

    def _invalidate(self, session, now, key):
        session.execute(invites.update().where(invites.c.code_hash == key).values(revoked_at=now, delivery_pending=False))
        session.execute(challenges.update().where(challenges.c.invite_hash == key,
            challenges.c.status.in_(['pending','sent'])).values(status='failed'))
        session.execute(requests.update().where(requests.c.invite_hash == key).values(code_sent=False))

    def review(self, actor_id, identity, *, password_version, request_id, action, reason=''):
        identity, reason = _identity(identity), _text(reason, 0, 500)
        if action not in {'approve', 'reject', 'resend'}:
            raise PublicAuthError('审批操作不正确。')
        def reserve(session, now):
            actor = self._actor(session, actor_id, password_version)
            if isinstance(actor, PublicAuthError):
                return actor
            row = self._request(session, identity)
            if isinstance(row, PublicAuthError):
                return row
            if row['status'] != ('approved' if action == 'resend' else 'pending'):
                return PublicAuthError('申请状态已变化，请刷新列表。', 409)
            data = self.cipher.decrypt('request', identity, row['sealed'])
            if action == 'reject':
                data['reject_reason'] = reason
                session.execute(requests.update().where(requests.c.id == identity).values(status='rejected',
                    reviewed_at=now, reviewed_by=actor['id'], sealed=self.cipher.encrypt('request', identity, data)))
                self._audit(session, actor, 'reject', identity, request_id)
                return dict(email=data['email'], reason=reason, rejected_total=self._rejected(session, row['email_hash']))
            if action == 'resend':
                old = session.execute(sa.select(invites).where(invites.c.code_hash == row['invite_hash'])
                                      .with_for_update()).mappings().one_or_none()
                if (old is None or old['used_by'] is not None or old['revoked_at'] is not None or
                        (old['delivery_pending'] and (now-old['created_at']).total_seconds() < 60)):
                    return PublicAuthError('邀请码已使用、撤销或仍在发送中，不能补发。', 409)
                if error := self.auth._take(session, now, 'invite_resend', str(identity), 1, 60):
                    return error
            allocated = self._allocate(session, now, actor, data['email'], data['note'], request_identity=identity, pending=True)
            if isinstance(allocated, PublicAuthError):
                return allocated
            key, code = allocated
            if action == 'resend':
                self._invalidate(session, now, row['invite_hash'])
            session.execute(requests.update().where(requests.c.id == identity).values(status='approved',
                reviewed_by=actor['id'], reviewed_at=now, invite_hash=key, code_sent=False))
            self._audit(session, actor, action, identity, request_id)
            return dict(email=data['email'], code=code, key=key)
        pending = self.auth._transaction(reserve)
        delivered = False
        if action == 'reject':
            if reason:
                try:
                    self.send_mail(purpose='invite_reject', email=pending['email'], reason=reason)
                except Exception:
                    pass
            # No sensitive response, but do not report success to a revoked actor.
            def confirm(session, now):
                actor = self._actor(session, actor_id, password_version)
                return actor if isinstance(actor, PublicAuthError) else dict(message='已拒绝该申请。',
                    rejected_total=pending['rejected_total'], max_rejections=self.max_rejections)
            return self.auth._transaction(confirm)
        try:
            self.send_mail(purpose='invite', email=pending['email'], code=pending['code'], expires_hours=self.ttl_hours)
            delivered = True
        except Exception:
            pass  # Provider errors and credentials never leave this boundary.
        def finish(session, now):
            actor = self._actor(session, actor_id, password_version)
            row = self._request(session, identity)
            invite = session.execute(sa.select(invites).where(invites.c.code_hash == pending['key'])
                                     .with_for_update()).mappings().one_or_none()
            if isinstance(actor, PublicAuthError):
                if invite is not None and invite['used_by'] is None:
                    self._invalidate(session, now, pending['key'])
                return actor
            if (isinstance(row, PublicAuthError) or row['invite_hash'] != pending['key'] or invite is None or
                    invite['revoked_at'] is not None or not invite['delivery_pending'] or invite['expires_at'] <= now):
                if invite is not None and invite['used_by'] is None:
                    self._invalidate(session, now, pending['key'])
                return PublicAuthError('邀请码状态已变化，请刷新列表。', 409)
            session.execute(invites.update().where(invites.c.code_hash == pending['key']).values(delivery_pending=False))
            session.execute(requests.update().where(requests.c.id == identity).values(code_sent=delivered))
            self._audit(session, actor, 'delivery', identity, request_id)
            return dict(message='审批已通过，邀请码已发送至申请邮箱。' if delivered else
                '邮件发送失败，请复制邀请码人工转达，或使用补发。', request_id_info=str(identity),
                invite_code=pending['code'], code_sent=delivered)
        return self.auth._transaction(finish)

    def list_codes(self, actor_id, *, password_version, status='all', page=1, size=200):
        if status not in {'all', 'active', 'used', 'expired'}:
            raise PublicAuthError('邀请码状态不正确。')
        self._page(page, size)
        def read(session, now):
            actor = self._actor(session, actor_id, password_version)
            if isinstance(actor, PublicAuthError):
                return actor
            state = sa.case((invites.c.used_by.is_not(None), 'used'),
                (sa.or_(invites.c.expires_at <= now, invites.c.revoked_at.is_not(None)), 'expired'), else_='active')
            query = sa.select(invites, User.username.label('used_by_username'), state.label('state')).outerjoin(
                User, User.id == invites.c.used_by)
            if status != 'all':
                query = query.where(state == status)
            total = session.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
            items = []
            for row in session.execute(query.order_by(invites.c.created_at.desc(), invites.c.code_hash)
                                       .offset((page-1)*size).limit(size)).mappings():
                details = self.cipher.decrypt('invite', row['code_hash'], row['details']) if row['details'] else {}
                email = details.get('email', '')
                items.append(dict(code_hash=row['code_hash'], status=row['state'],
                    bound_email=_mask(email) if email else ('已绑定' if row['email_hash'] != self.auth.digest('email','') else ''),
                    created_at=row['created_at'].isoformat(), expires_at=row['expires_at'].isoformat(),
                    used_by_username=row['used_by_username'], note=details.get('note',''), revoked=row['revoked_at'] is not None,
                    delivery_pending=row['delivery_pending']))
            return dict(items=items, total=total)
        return self.auth._transaction(read)

    def revoke(self, actor_id, code_or_hash, *, password_version, request_id):
        if type(code_or_hash) is not str or len(code_or_hash) > 128:
            raise PublicAuthError('邀请码不存在。', 404)
        key = code_or_hash.lower() if re.fullmatch('[0-9a-fA-F]{64}', code_or_hash) else self.auth.digest(
            'invite', re.sub(r'[\s_-]', '', code_or_hash).upper())
        def revoke(session, now):
            actor = self._actor(session, actor_id, password_version)
            if isinstance(actor, PublicAuthError):
                return actor
            row = session.execute(sa.select(invites).where(invites.c.code_hash == key)
                                  .with_for_update()).mappings().one_or_none()
            if row is None or row['used_by'] is not None:
                return PublicAuthError('邀请码不存在或已使用。', 404)
            if row['revoked_at'] is None:
                self._invalidate(session, now, key)
                self._audit(session, actor, 'revoke', key, request_id)
            return dict(message='邀请码已作废。')
        return self.auth._transaction(revoke)
