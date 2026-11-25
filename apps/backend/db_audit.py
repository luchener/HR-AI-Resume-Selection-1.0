"""Transactional runtime audit writer; caller must authorize the mutation.

Identity is resolved from authoritative user columns in the same transaction;
the two explicit retention events use the fixed ``system:retention`` identity.
No commits, free-text details, passwords, keys, request bodies or legacy payloads.
"""
from uuid import UUID
from sqlalchemy import select, insert
from db_models import User
from db_check_schema import metadata as schema_metadata

ACTIONS = frozenset({'user.create', 'user.update', 'user.freeze', 'user.unfreeze',
                     'user.delete', 'user.restore', 'user.password_reset',
                     'user.password_change', 'user.login', 'user.invite_create',
                     'config.mail.update', 'config.llm.update', 'config.mail.test',
                     'config.llm.test', 'config.llm.models', 'quota.update',
                     'archive.create', 'archive.update', 'archive.delete', 'archive.restore', 'archive.purge',
                     'resume.create', 'resume.view', 'resume.delete', 'resume.original_info', 'resume.original_page',
                     'resume.original_content', 'resume.original_download',
                     'job.create', 'job.delete', 'analysis.view'})
ACTIONS = ACTIONS | frozenset('invite.' + action for action in
    ('approve', 'reject', 'resend', 'generate', 'revoke', 'delivery'))
ACTIONS = ACTIONS | frozenset({'retention.resume_purge', 'retention.job_purge'})
ACTIONS = ACTIONS | frozenset('announcement.' + action for action in ('create', 'cancel', 'delete', 'dismiss'))
ACTIONS = ACTIONS | frozenset('notification.' + action for action in ('image_upload', 'image_delete', 'broadcast_create'))


def append_audit(session, *, actor_user_id, action, target_type, target_id,
                 request_id, changed_fields=(), outcome='success', original_page=None,
                 metadata=None):
    if not session.in_transaction():
        raise ValueError('Audit requires caller-owned transaction')
    if action not in ACTIONS or target_type not in {'user', 'config', 'quota', 'archive', 'resume', 'job', 'invite', 'analysis', 'announcement', 'notification'}:
        raise ValueError('Unsupported audit event')
    system_retention = action in {'retention.resume_purge', 'retention.job_purge'}
    if system_retention and actor_user_id is not None:
        raise ValueError('System retention audit cannot impersonate a user')
    if metadata is None:
        metadata = {}
    if not system_retention or not isinstance(metadata, dict):
        if metadata:
            raise ValueError('Only system retention events may add metadata')
        metadata = {}
    allowed_metadata = {'archives_marked', 'original_queued'}
    if set(metadata) - allowed_metadata:
        raise ValueError('Unsupported retention metadata')
    if any(type(value) is not int or not 0 <= value <= 1000 for value in metadata.values()):
        raise ValueError('Retention metadata counts must be bounded integers')
    expected_type = (action.split('.')[1].split('_', 1)[0] if system_retention else
                     ('config' if action.startswith('config.') else action.split('.')[0]))
    if target_type != expected_type:
        raise ValueError('Audit action and target type differ')
    if outcome not in {'success', 'denied'}:
        raise ValueError('Unsupported audit outcome')
    if action == 'resume.original_page':
        if type(original_page) is not int or not 1 <= original_page <= 20:
            raise ValueError('Invalid audited original page')
    elif original_page is not None:
        raise ValueError('Page is only allowed for original page access')
    if (not system_retention and (not isinstance(request_id, str) or not request_id or len(request_id) > 128)):
        raise ValueError('Invalid audit request ID')
    if system_retention and request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 128):
        raise ValueError('Invalid audit request ID')
    if not isinstance(target_id, str) or not target_id or len(target_id) > 128:
        raise ValueError('Invalid audit target ID')
    allowed_fields = {'email', 'is_admin', 'status', 'password', 'daily_limit',
                      'analysis_enabled', 'email_notify', 'mail', 'llm', 'tags', 'category'}
    if isinstance(changed_fields, (str, bytes)):
        raise ValueError('Invalid audit field names')
    fields = list(changed_fields)
    if len(fields) > len(allowed_fields) or any(not isinstance(f, str) or f not in allowed_fields for f in fields):
        raise ValueError('Unsupported audit field names')
    if system_retention:
        actor = None
        row = {'username': 'retention'}
    else:
        actor = UUID(str(actor_user_id))
        # Lock excludes concurrent role/status/password changes until caller commit.
        row = session.execute(select(User.id, User.username, User.status, User.deleted_at).where(
            User.id == actor).with_for_update()).mappings().one_or_none()
        if row is None or row['status'] != 'active' or row['deleted_at'] is not None:
            raise ValueError('Audit actor is not active')
    snapshots = {'actor_id': 'system:retention' if system_retention else str(actor), 'actor_name': row['username'],
                 'target_id': target_id, 'changed_fields': sorted(set(fields)), 'outcome': outcome}
    snapshots.update(metadata)
    if original_page is not None:
        snapshots['page'] = original_page
    if target_type == 'user':
        target = session.execute(select(User.username).where(User.id == UUID(target_id))).scalar_one_or_none()
        if target is None:
            raise ValueError('Audit target user does not exist')
        snapshots['target_name'] = target
    table = schema_metadata().tables['audit_logs']
    session.execute(insert(table).values(actor_user_id=actor,
                    actor_identity='retention' if system_retention else str(actor),
                    action=action, target_type=target_type, target_id=target_id,
                    request_id=request_id, metadata=snapshots))
