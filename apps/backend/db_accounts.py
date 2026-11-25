"""Explicit account management transactions for isolated database adapters.

No legacy auth/config imports or external effects. Every operation rechecks its
actor under a row lock. Administrative mutations serialize before locking user
rows in UUID order; password verification, changes and audit commit together.
"""
from datetime import datetime, timedelta, timezone
import secrets
import string
from uuid import uuid4

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from db_audit import append_audit
from db_auth import AuthService, _EMAIL, _USERNAME, _password_error, _uuid, hash_password, verify_password
from db_models import User
from db_security import literal_like


class AccountError(Exception):
    """Fixed product errors only; database/provider exceptions are never reflected."""

    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.detail, self.status = detail, status


class AccountService(AuthService):
    def _actor(self, rows, actor_id, password_version, *, administrative=False):
        actor = next((row for row in rows if row['id'] == actor_id), None)
        if (actor is None or actor['status'] != 'active' or actor['deleted_at'] is not None
                or type(password_version) is not int or actor['password_version'] != password_version):
            raise AccountError('Invalid or revoked token', 401)
        if administrative and not self._internal(actor).is_admin:
            raise AccountError('Administrator required', 403)
        return actor

    def _lock(self, session, actor_id, password_version, *, username=None, administrative=False, include_deleted=False):
        actor_id = _uuid(actor_id)
        target_id = None
        if administrative:
            # All account management mutations use this order, including create.
            session.execute(text('SELECT pg_advisory_xact_lock(710413503)'))
        if username is not None:
            if not isinstance(username, str) or not _USERNAME.fullmatch(username):
                raise AccountError('用户不存在。', 404)
            query = select(User.id).where(func.lower(User.username) == username.lower())
            if not include_deleted:
                query = query.where(User.deleted_at.is_(None), User.status != 'deleted')
            target_id = session.scalar(query)
        identities = [actor_id] if target_id is None else [actor_id, target_id]
        rows = session.execute(select(self._table).where(User.id.in_(identities))
                               .order_by(User.id).with_for_update()).mappings().all()
        actor = self._actor(rows, actor_id, password_version, administrative=administrative)
        if username is not None:
            target = next((row for row in rows if row['id'] == target_id), None)
            if target is None or (not include_deleted and (target['deleted_at'] is not None or target['status'] == 'deleted')):
                raise AccountError('用户不存在。', 404)
            return actor, target
        return actor

    def _user_view(self, row):
        return {'user_id': str(row['id']), 'username': row['username'], 'email': row['email'] or '',
                'is_admin': self._internal(row).is_admin}

    @staticmethod
    def _email(value, *, allow_empty=False):
        if not isinstance(value, str):
            raise AccountError('邮箱格式不正确')
        clean = value.strip().lower()
        if (not clean and allow_empty):
            return None
        if len(clean) > 320 or not _EMAIL.fullmatch(clean):
            raise AccountError('邮箱格式不正确')
        return clean

    @staticmethod
    def _unique_error(exc):
        constraint = getattr(getattr(exc.orig, 'diag', None), 'constraint_name', None)
        if constraint == 'uq_users_username_lower':
            raise AccountError('用户名已被注册 (大小写不敏感)', 409) from None
        if constraint == 'uq_users_email_lower':
            raise AccountError('该邮箱已被其他用户使用', 409) from None
        raise exc

    def _protect_target(self, actor, target):
        if self._internal(target).is_admin and not self._internal(actor).is_super_admin:
            raise AccountError('仅超级管理员可以操作管理员账号。', 403)

    def change_password(self, actor_id, old_password, new_password, *, password_version, request_id):
        if not isinstance(old_password, str) or not old_password or not isinstance(new_password, str) or not new_password:
            raise AccountError('请提供旧密码和新密码。')
        if old_password == new_password:
            raise AccountError('新密码不能与旧密码相同。')
        error = _password_error(new_password)
        if error:
            raise AccountError(error)
        with self._session_factory() as session, session.begin():
            actor = self._lock(session, actor_id, password_version)
            if not verify_password(old_password, actor['password_hash'], actor['password_salt']):
                raise AccountError('旧密码错误。', 401)
            hashed, salt = hash_password(new_password)
            session.execute(update(User).where(User.id == actor['id']).values(
                password_hash=hashed, password_salt=salt, password_version=actor['password_version'] + 1,
                updated_at=datetime.now(timezone.utc)))
            append_audit(session, actor_user_id=actor['id'], action='user.password_change',
                         target_type='user', target_id=str(actor['id']), request_id=request_id,
                         changed_fields=['password'])
        return {'message': '密码修改成功，请重新登录。', 'require_relogin': True}

    def list_accounts(self, actor_id, *, password_version, keyword='', page=1, size=50):
        if type(page) is not int or type(size) is not int or page < 1 or not 1 <= size <= 200 or (page - 1) * size > 100000:
            raise AccountError('分页参数超出范围。')
        try:
            pattern = literal_like(keyword.strip())
        except (ValueError, AttributeError):
            raise AccountError('搜索内容过长或格式不正确。') from None
        with self._session_factory() as session, session.begin():
            actor_id = _uuid(actor_id)
            rows = session.execute(select(self._table).where(User.id == actor_id).with_for_update()).mappings().all()
            self._actor(rows, actor_id, password_version, administrative=True)
            condition = (User.deleted_at.is_(None) & (User.status != 'deleted')
                         & or_(User.username.ilike(pattern, escape='\\'), User.email.ilike(pattern, escape='\\')))
            count = session.scalar(select(func.count()).select_from(User).where(condition))
            rows = session.execute(select(self._table).where(condition).order_by(User.created_at.desc(), User.id)
                                   .offset((page - 1) * size).limit(size)).mappings().all()
            # Password timestamps come from committed events, never stale credential JSON.
            from db_check_schema import metadata
            audit = metadata().tables['audit_logs']
            timestamps = dict(session.execute(select(audit.c.target_id, func.max(audit.c.created_at)).where(
                audit.c.target_type == 'user', audit.c.target_id.in_([str(row['id']) for row in rows]),
                audit.c.action.in_(['user.password_change', 'user.password_reset']))
                .group_by(audit.c.target_id)).all()) if rows else {}
            items = []
            for row in rows:
                changed = timestamps.get(str(row['id']))
                items.append(dict(self._user_view(row), frozen=row['status'] == 'locked',
                                  email_bound=bool(row['email']), created_at=row['created_at'].isoformat(),
                                  password_updated_at=changed.isoformat() if changed else None))
            return {'items': items, 'total': count, 'page': page, 'size': size}

    def create_account(self, actor_id, payload, *, password_version, request_id):
        if not isinstance(payload, dict) or set(payload) - {'username', 'password', 'email', 'is_admin'}:
            raise AccountError('用户字段不合法。')
        username = payload.get('username')
        password = payload.get('password')
        if not isinstance(username, str) or not _USERNAME.fullmatch(username.strip()):
            raise AccountError('用户名仅允许字母、数字、下划线、连字符或中文，长度 2-64 字符')
        username = username.strip()
        error = _password_error(password)
        if error:
            raise AccountError(error)
        email = self._email(payload.get('email', ''), allow_empty=True)
        flag = payload.get('is_admin', False)
        if type(flag) is not bool:
            raise AccountError('管理员标记必须是布尔值。')
        try:
            with self._session_factory() as session, session.begin():
                actor = self._lock(session, actor_id, password_version, administrative=True)
                if flag and not self._internal(actor).is_super_admin:
                    raise AccountError('仅超级管理员可以创建管理员。', 403)
                # Even a superadmin cannot assign allowlist identity through this endpoint.
                if email in self._admin_emails:
                    raise AccountError('该邮箱属于系统管理员白名单，不能分配给其他账号。', 403)
                hashed, salt = hash_password(password)
                now = datetime.now(timezone.utc)
                row = session.execute(insert(self._table).values(id=uuid4(), username=username, email=email,
                    password_hash=hashed, password_salt=salt, password_version=1, is_admin=flag,
                    is_super_admin=False, status='active', created_at=now, updated_at=now, legacy_payload={})
                    .returning(*self._table.c)).mappings().one()
                append_audit(session, actor_user_id=actor['id'], action='user.create', target_type='user',
                             target_id=str(row['id']), request_id=request_id, changed_fields=['email', 'is_admin'])
                result = self._user_view(row)
        except IntegrityError as exc:
            self._unique_error(exc)
        return {'message': f'用户 {username} 创建成功。', 'user': result}

    def update_account(self, actor_id, username, payload, *, password_version, request_id):
        if not isinstance(payload, dict) or not payload or set(payload) - {'email', 'is_admin'}:
            raise AccountError('请提供邮箱或管理员标记。')
        if 'is_admin' in payload and type(payload['is_admin']) is not bool:
            raise AccountError('管理员标记必须是布尔值。')
        email = self._email(payload['email']) if 'email' in payload else None
        try:
            with self._session_factory() as session, session.begin():
                actor, target = self._lock(session, actor_id, password_version, username=username, administrative=True)
                self._protect_target(actor, target)
                if 'is_admin' in payload and not self._internal(actor).is_super_admin:
                    raise AccountError('仅超级管理员可以分配或取消管理员权限。', 403)
                if self._internal(target).is_super_admin:
                    raise AccountError('该用户由服务器白名单配置为管理员，不能通过界面修改。', 400)
                if email in self._admin_emails:
                    raise AccountError('该邮箱属于系统管理员白名单，不能分配给其他账号。', 403)
                values = {field: email if field == 'email' else payload[field] for field in payload}
                # Identity/role changes invalidate every token issued before this commit.
                values.update(password_version=target['password_version'] + 1, updated_at=datetime.now(timezone.utc))
                row = session.execute(update(self._table).where(User.id == target['id']).values(**values)
                                      .returning(*self._table.c)).mappings().one()
                append_audit(session, actor_user_id=actor['id'], action='user.update', target_type='user',
                             target_id=str(target['id']), request_id=request_id, changed_fields=payload.keys())
                result = self._user_view(row)
        except IntegrityError as exc:
            self._unique_error(exc)
        return {'message': f'用户 {result["username"]} 已更新。', 'user': result}

    def reset_password(self, actor_id, username, *, password_version, request_id):
        with self._session_factory() as session, session.begin():
            actor, target = self._lock(session, actor_id, password_version, username=username, administrative=True)
            self._protect_target(actor, target)
            if target['status'] != 'active':
                raise AccountError('账号已冻结或不可用，请先解冻。', 423)
            # Construct guaranteed mixed classes without probabilistic policy failures.
            alphabet = string.ascii_letters + string.digits
            characters = [secrets.choice(string.ascii_letters), secrets.choice(string.digits)]
            characters.extend(secrets.choice(alphabet) for _ in range(18))
            secrets.SystemRandom().shuffle(characters)
            temporary = ''.join(characters)
            hashed, salt = hash_password(temporary)
            session.execute(update(User).where(User.id == target['id']).values(password_hash=hashed,
                password_salt=salt, password_version=target['password_version'] + 1,
                updated_at=datetime.now(timezone.utc)))
            append_audit(session, actor_user_id=actor['id'], action='user.password_reset', target_type='user',
                         target_id=str(target['id']), request_id=request_id, changed_fields=['password'])
        return {'message': f'用户 {target["username"]} 的密码已重置。', 'temp_password': temporary}

    def set_frozen(self, actor_id, username, frozen, *, password_version, request_id, reason=''):
        if type(frozen) is not bool or not isinstance(reason, str) or len(reason.strip()) > 512 or '\x00' in reason:
            raise AccountError('冻结原因格式不正确或超过512字符。')
        with self._session_factory() as session, session.begin():
            actor, target = self._lock(session, actor_id, password_version, username=username, administrative=True)
            self._protect_target(actor, target)
            if actor['id'] == target['id']:
                raise AccountError('不能冻结或解冻当前登录的管理员账号。', 400)
            if target['status'] not in {'active', 'locked'}:
                raise AccountError('当前账号状态不支持冻结或解冻。', 400)
            desired = 'locked' if frozen else 'active'
            # Replays are no-ops: do not increment versions or move freeze time.
            if (target['status'] != desired or (frozen and target['frozen_by'] != 'admin')
                    or (not frozen and (target['login_consecutive_failures'] or target['login_window_failures']))):
                now = datetime.now(timezone.utc)
                session.execute(update(User).where(User.id == target['id']).values(
                    status=desired, password_version=target['password_version'] + 1, updated_at=now,
                    frozen_at=now if frozen else None, frozen_by='admin' if frozen else None,
                    frozen_reason=reason.strip() if frozen else ''))
                if not frozen:
                    session.execute(update(User).where(User.id == target['id']).values(
                        login_consecutive_failures=0, login_window_failures=0))
                append_audit(session, actor_user_id=actor['id'], action='user.freeze' if frozen else 'user.unfreeze',
                             target_type='user', target_id=str(target['id']), request_id=request_id, changed_fields=['status'])
        message = (f'账号 {target["username"]} 已冻结，该用户将无法登录与重置密码。' if frozen
                   else f'账号 {target["username"]} 已解冻。')
        return {'message': message}

    def delete_account(self, actor_id, username, admin_password, *, password_version, request_id):
        with self._session_factory() as session, session.begin():
            actor, target = self._lock(session, actor_id, password_version, username=username,
                                       administrative=True, include_deleted=True)
            if actor['id'] == target['id']:
                raise AccountError('不能删除当前登录的管理员账号。', 400)
            if not self._internal(actor).is_super_admin:
                raise AccountError('仅超级管理员可以删除账号。', 403)
            if not isinstance(admin_password, str) or not admin_password:
                raise AccountError('请填写管理员密码以确认删除操作。')
            if not verify_password(admin_password, actor['password_hash'], actor['password_salt']):
                raise AccountError('管理员密码验证失败，删除已取消。', 403)
            if (target['deleted_at'] is not None) != (target['status'] == 'deleted'):
                raise AccountError('删除记录异常，无法删除。', 400)
            if target['deleted_at'] is None and target['status'] != 'deleted':
                now = datetime.now(timezone.utc)
                session.execute(update(User).where(User.id == target['id']).values(
                    status='deleted', deleted_at=now, deleted_by=str(actor['id']),
                    deletion_previous_status=target['status'], updated_at=now,
                    password_version=target['password_version'] + 1))
                append_audit(session, actor_user_id=actor['id'], action='user.delete', target_type='user',
                             target_id=str(target['id']), request_id=request_id, changed_fields=['status'])
        return {'message': f'用户 {target["username"]} 已删除（90 天内可恢复）。'}

    def restore_account(self, actor_id, username, *, password_version, request_id):
        with self._session_factory() as session, session.begin():
            actor, target = self._lock(session, actor_id, password_version, username=username,
                                       administrative=True, include_deleted=True)
            # Restore revives privileges as well as the identity; require the same
            # authority as delete, even for formerly ordinary accounts.
            if not self._internal(actor).is_super_admin:
                raise AccountError('仅超级管理员可以恢复账号。', 403)
            if not target['deleted_at']:
                raise AccountError('该账号未被删除，无需恢复', 400)
            now = datetime.now(timezone.utc)
            if (target['status'] != 'deleted' or target['deleted_at'] > now
                    or target['deletion_previous_status'] not in {'active', 'locked', 'pending'}):
                raise AccountError('删除记录异常，无法恢复', 400)
            if now - target['deleted_at'] > timedelta(days=90):
                raise AccountError('已超过可恢复期限（删除后 90 天内可恢复）', 400)
            session.execute(update(User).where(User.id == target['id']).values(
                status=target['deletion_previous_status'], deleted_at=None, deleted_by='',
                deletion_previous_status=None, updated_at=now, password_version=target['password_version'] + 1))
            append_audit(session, actor_user_id=actor['id'], action='user.restore', target_type='user',
                         target_id=str(target['id']), request_id=request_id, changed_fields=['status'])
        return {'message': f'用户 {target["username"]} 已恢复。'}

    def list_lifecycle(self, actor_id, state, *, password_version):
        if state not in {'frozen', 'deleted'}:
            raise AccountError('无效账号列表。')
        with self._session_factory() as session, session.begin():
            actor = self._lock(session, actor_id, password_version, administrative=True)
            if state == 'deleted' and not self._internal(actor).is_super_admin:
                raise AccountError('仅超级管理员可以查询已删除账号。', 403)
            if state == 'frozen':
                query = select(self._table).where(User.status == 'locked', User.deleted_at.is_(None)).order_by(
                    User.frozen_at.desc().nulls_last(), User.id)
            else:
                now = datetime.now(timezone.utc)
                query = select(self._table).where(User.deleted_at >= now - timedelta(days=90), User.deleted_at <= now).order_by(
                    User.deleted_at.desc(), User.id)
            rows = session.execute(query.limit(10001)).mappings().all()
            if len(rows) > 10000:
                raise AccountError('账号列表过大，请使用分页管理接口。', 422)
            if state == 'frozen':
                items = [{'username': row['username'], 'frozen_at': row['frozen_at'].isoformat() if row['frozen_at'] else None,
                          'frozen_by': row['frozen_by'] or 'auto', 'frozen_reason': row['frozen_reason'],
                          'consecutive_failures': row['login_consecutive_failures'],
                          'window_failures': row['login_window_failures']} for row in rows]
            else:
                items = [{'user_id': str(row['id']), 'username': row['username'], 'email': row['email'] or '',
                          'deleted_at': row['deleted_at'].isoformat(), 'deleted_by': row['deleted_by']} for row in rows]
            return {'items': items, 'total': len(items)}
