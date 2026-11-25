"""Explicit public-auth SQL rehearsal; no config, filesystem or provider imports.

All cooperating auth processes use one transaction advisory lock before rows.
Business refusals COMMIT counters/attempts; infrastructure failures roll back.
This conservative serialization is for correctness acceptance, not peak capacity.
Email is reserved pending, sent outside locks, then conditionally activated.
No automatic send retries: a delivered email cannot be rolled back by SQL.
"""
from datetime import timedelta
import hashlib
import hmac
import ipaddress
from math import ceil
import re
import secrets
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from db_audit import append_audit
from db_auth import AuthService, _EMAIL, _USERNAME, _password_error, hash_password, verify_password
from db_check_schema import metadata
from db_models import User
from db_public_auth_state import challenges, invites, limits, login_state

GENERIC_RESET = '如果该邮箱已注册，重置验证码已发送，请查收。'
INVALID_CODE = '邮箱验证码无效或已过期，请重新发送。'
INVALID_INVITE = '邀请码无效或不可用，请联系管理员获取新的邀请码。'


class PublicAuthError(Exception):
    def __init__(self, detail, status=422, *, retry_after=None, extra=None):
        super().__init__(detail)
        self.detail, self.status, self.retry_after = detail, status, retry_after
        self.extra = extra or {}


def _email(value):
    if (type(value) is not str or len(value) > 320 or not _EMAIL.fullmatch(value.strip()) or
            re.search(r'[\x00-\x20\x7f\ud800-\udfff]', value.strip())):
        raise PublicAuthError('邮箱格式不正确。')
    return value.strip().lower()


def _password(value):
    if type(value) is not str or len(value) > 1024:
        raise PublicAuthError('密码长度或格式不正确。')
    if error := _password_error(value):
        raise PublicAuthError(error)
    return value


class PublicAuthService(AuthService):
    def __init__(self, session_factory, *, secret, send_mail, admin_emails=(), captcha_enabled=True,
                 max_keys=10000, contact_email='', invite_length=4):
        super().__init__(session_factory, admin_emails=admin_emails)
        if type(secret) is not bytes or len(secret) < 32 or not callable(send_mail):
            raise ValueError('Explicit strong auth HMAC secret and mail callback required')
        if (type(captcha_enabled) is not bool or type(max_keys) is not int or not 10 <= max_keys <= 100000 or
                type(invite_length) is not int or not 4 <= invite_length <= 32):
            raise ValueError('Invalid public auth policy')
        self._secret, self.send_mail = secret, send_mail
        self.captcha_enabled, self.max_keys = captcha_enabled, max_keys
        self.contact_email = _email(contact_email) if contact_email else ''
        self.invite_length = invite_length

    @property
    def admin_emails(self):
        return self._admin_emails

    def digest(self, namespace, value):
        return hmac.new(self._secret, (namespace + '\0' + value).encode('utf8'), hashlib.sha256).hexdigest()

    def _transaction(self, action):
        with self._session_factory() as session, session.begin():
            session.execute(sa.text('SELECT pg_advisory_xact_lock(710413504)'))
            now = session.scalar(sa.select(sa.func.clock_timestamp()))
            session.execute(limits.delete().where(limits.c.expires_at <= now))
            session.execute(challenges.delete().where(challenges.c.expires_at <= now))
            # Managed history retains expiry/usage/revocation for the admin view.
            session.execute(invites.delete().where(invites.c.expires_at <= now, invites.c.details.is_(None),
                                                  invites.c.revoked_at.is_(None)))
            result = action(session, now)
        if isinstance(result, PublicAuthError):
            raise result
        return result

    def _take(self, session, now, namespace, subject, count, seconds):
        key = self.digest(namespace, subject)
        row = session.execute(sa.select(limits).where(limits.c.key == key)).mappings().one_or_none()
        if row is not None:
            if row['count'] >= count:
                return PublicAuthError('尝试次数过多，请稍后再试。', 429,
                                       retry_after=max(1, ceil((row['expires_at'] - now).total_seconds())))
            session.execute(limits.update().where(limits.c.key == key).values(count=row['count'] + 1))
        else:
            if session.scalar(sa.select(sa.func.count()).select_from(limits)) >= self.max_keys:
                return PublicAuthError('认证服务暂不可用。', 503)
            session.execute(limits.insert().values(key=key, count=1, expires_at=now + timedelta(seconds=seconds)))
        return None

    def check_request(self, endpoint, ip):
        # Only actual peer IP is used by HTTP; never trust arbitrary XFF.
        peer = str(ipaddress.ip_address(ip))
        return self._transaction(lambda s, n: self._take(s, n, 'http:' + endpoint, peer, 60, 600))

    @staticmethod
    def _user(session, *, email=None, username=None):
        query = sa.select(User.__table__)
        query = query.where(sa.func.lower(User.email) == email) if email is not None else query.where(
            sa.func.lower(User.username) == username.lower())
        return session.execute(query.with_for_update()).mappings().one_or_none()

    def _invite(self, session, now, code, email=None, *, accept_unbound=False):
        if type(code) is not str or len(code) > 128:
            return None
        clean = re.sub(r'[\s_-]', '', code).upper()
        if not re.fullmatch(r'[A-Z2-9]{4,32}', clean):
            return None
        row = session.execute(sa.select(invites).where(invites.c.code_hash == self.digest('invite', clean))
                              .with_for_update()).mappings().one_or_none()
        if (row is None or row['used_by'] is not None or row['expires_at'] <= now or
                row['revoked_at'] is not None or row['delivery_pending']):
            return None
        if email is not None and row['email_hash'] != self.digest('email', email):
            if not accept_unbound or row['email_hash'] != self.digest('email', ''):
                return None
        return row

    def issue_invite(self, actor_id, email, *, password_version, request_id, ttl_hours=24):
        email = _email(email)
        if email in self.admin_emails or type(ttl_hours) is not int or not 1 <= ttl_hours <= 720:
            raise PublicAuthError('该邮箱或邀请码有效期不可用。')
        actor_id = UUID(str(actor_id))
        def issue(session, now):
            actor = session.execute(sa.select(User.__table__).where(User.id == actor_id)
                                    .with_for_update()).mappings().one_or_none()
            if (actor is None or actor['status'] != 'active' or actor['deleted_at'] is not None or
                    type(password_version) is not int or actor['password_version'] != password_version):
                return PublicAuthError('Invalid or revoked token', 401)
            if (actor['email'] or '').lower() not in self.admin_emails:
                return PublicAuthError('Superadministrator required', 403)
            if session.scalar(sa.select(sa.func.count()).select_from(invites)) >= self.max_keys:
                return PublicAuthError('邀请码容量已满，请联系管理员。', 503)
            for _ in range(8):
                code = ''.join(secrets.choice('ABCDEFGHJKMNPQRSTUVWXYZ23456789') for _ in range(self.invite_length))
                key = self.digest('invite', code)
                written = session.execute(insert(invites).values(code_hash=key, email_hash=self.digest('email', email),
                    created_by=actor_id, expires_at=now + timedelta(hours=ttl_hours))
                    .on_conflict_do_nothing().returning(invites.c.code_hash)).scalar_one_or_none()
                if written:
                    append_audit(session, actor_user_id=actor_id, action='user.invite_create', target_type='user',
                                 target_id=str(actor_id), request_id=request_id)
                    return code
            return PublicAuthError('邀请码生成失败，请重试。', 503)
        return self._transaction(issue)

    def check_invite(self, code, *, ip):
        def check(session, now):
            if error := self._take(session, now, 'invite_check', ip, 10, 3600):
                return error
            return True if self._invite(session, now, code) else PublicAuthError(INVALID_INVITE)
        return self._transaction(check)

    def _challenge(self, session, now, purpose, subject, code):
        key = self.digest('subject:' + purpose, subject)
        row = session.execute(sa.select(challenges).where(challenges.c.purpose == purpose,
            challenges.c.subject == key).with_for_update()).mappings().one_or_none()
        if row is None or row['status'] != 'sent' or row['expires_at'] <= now or row['attempts'] >= 5:
            return None
        good = (type(code) is str and len(code) <= 32 and hmac.compare_digest(row['code_hash'],
            self.digest('code:' + purpose, subject + '\0' + code.strip().upper())))
        session.execute(challenges.update().where(challenges.c.id == row['id']).values(
            attempts=row['attempts'] + 1, status='used' if good or purpose == 'captcha' else
            ('failed' if row['attempts'] == 4 else 'sent')))
        return row if good else None

    def _new_challenge(self, session, now, purpose, subject, *, user=None, invite=None):
        key = self.digest('subject:' + purpose, subject)
        exists = session.scalar(sa.select(challenges.c.id).where(challenges.c.purpose == purpose,
                                                                challenges.c.subject == key))
        if exists is None and session.scalar(sa.select(sa.func.count()).select_from(challenges)) >= self.max_keys:
            return PublicAuthError('认证服务暂不可用。', 503)
        code = ''.join(secrets.choice('0123456789') for _ in range(4 if purpose == 'captcha' else 6))
        values = dict(purpose=purpose, subject=key, id=uuid4(), status='sent' if purpose == 'captcha' else 'pending',
            code_hash=self.digest('code:' + purpose, subject + '\0' + code), attempts=0,
            user_id=user['id'] if user else None, password_version=user['password_version'] if user else None,
            invite_hash=invite['code_hash'] if invite else None,
            expires_at=now + timedelta(seconds=300 if purpose == 'captcha' else 1800))
        session.execute(insert(challenges).values(**values).on_conflict_do_update(
            index_elements=[challenges.c.purpose, challenges.c.subject], set_=values))
        return values['id'], code

    def captcha(self, *, ip):
        def create(session, now):
            if error := self._take(session, now, 'captcha', ip, 60, 600):
                return error
            identity = str(uuid4())
            value = self._new_challenge(session, now, 'captcha', identity)
            return value if isinstance(value, PublicAuthError) else (identity, value[1])
        if not self.captcha_enabled:
            raise PublicAuthError('验证码功能未启用。', 404)
        return self._transaction(create)

    @staticmethod
    def _eligible(user, purpose):
        if user is None or user['deleted_at'] is not None:
            return False
        if purpose == 'unfreeze':
            return user['status'] == 'locked' and user['frozen_by'] == 'auto'
        return user['status'] == 'active' or (user['status'] == 'locked' and user['frozen_by'] == 'auto')

    def request_code(self, purpose, email, *, ip, invite_code='', username=''):
        if purpose not in {'register', 'reset', 'unfreeze'}:
            raise PublicAuthError('Invalid code purpose')
        email = _email(email)
        def reserve(session, now):
            if error := self._take(session, now, 'mail_ip', ip, 10, 600):
                return error
            user = self._user(session, email=email)
            if purpose == 'register' and (email in self.admin_emails or user is not None):
                return PublicAuthError(INVALID_INVITE)
            invite = self._invite(session, now, invite_code, email, accept_unbound=True) if purpose == 'register' else None
            if purpose == 'register' and invite is None:
                return PublicAuthError(INVALID_INVITE)
            for scope, maximum, window in [('mail_cooldown', 1, 60), ('mail_hour', 5, 3600)]:
                if error := self._take(session, now, scope, email, maximum, window):
                    return error
            if purpose != 'register' and (not self._eligible(user, purpose) or (purpose == 'unfreeze' and
                    (type(username) is not str or user['username'].lower() != username.strip().lower()))):
                return None if purpose == 'reset' else PublicAuthError('该账号不可自助解冻。', 409)
            pending = self._new_challenge(session, now, purpose, email, user=user, invite=invite)
            if invite is not None and not isinstance(pending, PublicAuthError):
                # Claim only after every business refusal, in the same transaction
                # as the pending challenge. Infrastructure failures roll both back.
                session.execute(invites.update().where(invites.c.code_hash == invite['code_hash'])
                                .values(email_hash=self.digest('email', email)))
            return pending
        pending = self._transaction(reserve)
        if pending is None:
            return
        identity, code = pending
        delivered = False
        try:
            self.send_mail(email=email, code=code, purpose=purpose)
            delivered = True
        except Exception:
            pass  # Never log provider exceptions, mailbox, code or credentials.
        def finish(session, now):
            user = self._user(session, email=email) if purpose != 'register' else None
            row = session.execute(sa.select(challenges).where(challenges.c.id == identity)
                                  .with_for_update()).mappings().one_or_none()
            if row is None or row['status'] != 'pending':
                return False
            valid = delivered and row['expires_at'] > now
            if purpose == 'register':
                valid = valid and self._invite(session, now, invite_code, email) is not None
            else:
                valid = valid and self._eligible(user, purpose) and user['id'] == row['user_id'] and (
                    user['password_version'] == row['password_version'])
            session.execute(challenges.update().where(challenges.c.id == identity).values(status='sent' if valid else 'failed'))
            return valid
        activated = self._transaction(finish)
        if not activated and purpose != 'reset':
            raise PublicAuthError('邮件发送失败或账号状态已变化，请稍后重试。', 502)

    def register(self, username, password, email, code, invite_code, *, ip, request_id):
        email, password = _email(email), _password(password)
        if type(username) is not str or not _USERNAME.fullmatch(username.strip()):
            raise PublicAuthError('用户名格式不正确。')
        username = username.strip()
        hashed, salt = hash_password(password)
        def create(session, now):
            if error := self._take(session, now, 'register', ip, 10, 3600):
                return error
            invite = self._invite(session, now, invite_code, email)
            if invite is None or email in self.admin_emails:
                return PublicAuthError(INVALID_INVITE)
            if session.scalar(sa.select(sa.exists().where(sa.or_(sa.func.lower(User.username) == username.lower(),
                                                               sa.func.lower(User.email) == email)))):
                return PublicAuthError('用户名或邮箱已被注册。', 409)
            challenge = self._challenge(session, now, 'register', email, code)
            if challenge is None or challenge['invite_hash'] != invite['code_hash']:
                return PublicAuthError(INVALID_CODE, 409)
            try:
                with session.begin_nested():
                    row = session.execute(User.__table__.insert().values(id=uuid4(), username=username, email=email,
                        password_hash=hashed, password_salt=salt, is_admin=False, is_super_admin=False,
                        status='active', created_at=now, updated_at=now).returning(*User.__table__.c)).mappings().one()
            except IntegrityError as exc:
                if getattr(getattr(exc.orig, 'diag', None), 'constraint_name', None) in {
                        'uq_users_username_lower', 'uq_users_email_lower'}:
                    # Restore the successfully validated code on a concurrent
                    # uniqueness loss; keep counters, and do not burn the invite.
                    session.execute(challenges.update().where(challenges.c.id == challenge['id']).values(
                        status='sent', attempts=challenge['attempts']))
                    return PublicAuthError('用户名或邮箱已被注册。', 409)
                raise
            session.execute(invites.update().where(invites.c.code_hash == invite['code_hash']).values(used_by=row['id']))
            append_audit(session, actor_user_id=row['id'], action='user.create', target_type='user',
                         target_id=str(row['id']), request_id=request_id)
            return self._internal(row).to_public()
        return self._transaction(create)

    def _clear_failures(self, session, user):
        session.execute(login_state.delete().where(login_state.c.user_id == user['id']))
        session.execute(sa.update(User).where(User.id == user['id']).values(login_consecutive_failures=0,
                                                                          login_window_failures=0))

    def recover(self, purpose, email, code, *, password, request_id, ip, username=''):
        if purpose not in {'reset', 'unfreeze'}:
            raise PublicAuthError('Invalid recovery purpose')
        email = _email(email)
        if purpose == 'reset':
            hashed, salt = hash_password(_password(password))
        elif type(password) is not str or not 1 <= len(password) <= 1024:
            raise PublicAuthError('请提供密码。')
        def recover(session, now):
            if error := self._take(session, now, 'recover', ip, 20, 600):
                return error
            if error := self._take(session, now, 'recover_account', email, 10, 600):
                return error
            user = self._user(session, email=email)
            if not self._eligible(user, purpose):
                return PublicAuthError(INVALID_CODE, 400)
            if purpose == 'unfreeze' and (type(username) is not str or user['username'].lower() != username.strip().lower()
                    or not verify_password(password, user['password_hash'], user['password_salt'])):
                return PublicAuthError('自助解冻失败：密码或验证码有误。', 401)
            challenge = self._challenge(session, now, purpose, email, code)
            if challenge is None or challenge['user_id'] != user['id'] or challenge['password_version'] != user['password_version']:
                return PublicAuthError(INVALID_CODE, 400)
            values = dict(password_version=user['password_version'] + 1, status='active', frozen_at=None,
                          frozen_by=None, frozen_reason='', updated_at=now)
            if purpose == 'reset':
                values.update(password_hash=hashed, password_salt=salt)
            self._clear_failures(session, user)
            row = session.execute(sa.update(User).where(User.id == user['id']).values(**values)
                                  .returning(*User.__table__.c)).mappings().one()
            append_audit(session, actor_user_id=user['id'], action='user.password_reset' if purpose == 'reset' else 'user.unfreeze',
                         target_type='user', target_id=str(user['id']), request_id=request_id)
            return self._internal(row).to_public()
        return self._transaction(recover)

    def login(self, username, password, *, ip, request_id, captcha_id='', captcha_code=''):
        if (type(username) is not str or not _USERNAME.fullmatch(username.strip()) or
                type(password) is not str or not 1 <= len(password) <= 1024):
            raise PublicAuthError('请提供有效用户名和密码。')
        username = username.strip()
        def login(session, now):
            if error := self._take(session, now, 'login_ip', ip, 10, 600):
                return error
            if self.captcha_enabled:
                if type(captcha_id) is not str or len(captcha_id) > 64:
                    return PublicAuthError('验证码错误或已过期，请刷新验证码后重试。')
                if self._challenge(session, now, 'captcha', captcha_id, captcha_code) is None:
                    return PublicAuthError('验证码错误或已过期，请刷新验证码后重试。')
            user = self._user(session, username=username)
            if user is None or user['deleted_at'] is not None or user['status'] not in {'active', 'locked'}:
                hash_password(password)
                return PublicAuthError('用户名或密码错误。', 401)
            if user['status'] == 'locked':
                return PublicAuthError('账号已冻结，请使用自助解冻或联系管理员。', 423,
                                       extra={'frozen_by': user['frozen_by'] or 'admin'})
            state = session.execute(sa.select(login_state).where(login_state.c.user_id == user['id'])
                                    .with_for_update()).mappings().one_or_none()
            streak = user['login_consecutive_failures']
            if state is not None and user['login_consecutive_failures'] >= 3 and (
                    retry := 300 - (now - state['last_failure_at']).total_seconds()) > 0:
                return PublicAuthError('尝试过于频繁，请稍后再试。', 429, retry_after=max(1, ceil(retry)))
            if state is not None and streak >= 3:
                streak = 0  # A completed cooldown starts a new consecutive run.
            if verify_password(password, user['password_hash'], user['password_salt']):
                self._clear_failures(session, user)
                row = session.execute(sa.update(User).where(User.id == user['id']).values(last_login_at=now)
                                      .returning(*User.__table__.c)).mappings().one()
                append_audit(session, actor_user_id=user['id'], action='user.login', target_type='user',
                             target_id=str(user['id']), request_id=request_id)
                return self._internal(row).to_public()
            times = ([t for t in state['failure_times'] if now.timestamp() - t < 600]
                     if state and user['login_window_failures'] else [])
            times.append(now.timestamp())
            session.execute(insert(login_state).values(user_id=user['id'], failure_times=times[-64:], last_failure_at=now)
                .on_conflict_do_update(index_elements=[login_state.c.user_id],
                                      set_=dict(failure_times=times[-64:], last_failure_at=now)))
            values = dict(login_consecutive_failures=streak + 1,
                          login_window_failures=len(times))
            frozen = len(times) >= 6
            if frozen:
                values.update(status='locked', frozen_by='auto', frozen_at=now, frozen_reason='login_failures',
                              password_version=user['password_version'] + 1, updated_at=now)
            session.execute(sa.update(User).where(User.id == user['id']).values(**values))
            # A failed password is NOT an authenticated actor. The target ID is
            # authoritative, but the audit actor remains anonymous.
            table = metadata().tables['audit_logs']
            session.execute(table.insert().values(actor_user_id=None, actor_identity='anonymous',
                action='auth.auto_freeze' if frozen else 'auth.login_failed', target_type='user',
                target_id=str(user['id']), request_id=request_id, metadata={'outcome': 'denied'}))
            return PublicAuthError('账号已临时冻结。' if frozen else '用户名或密码错误。', 423 if frozen else 401,
                                   extra={'frozen_by': 'auto'} if frozen else None)
        return self._transaction(login)
