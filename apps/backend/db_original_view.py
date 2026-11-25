"""Explicit SQL superadmin original access, authorized and audited per call."""
from contextlib import contextmanager
from copy import deepcopy

import sqlalchemy as sa

from db_audit import append_audit
from db_content_service import ContentError, _uuid
from db_models import Resume, User
from db_original_render import MAX_PAGES, inspect_pdf


class OriginalViewService:
    def __init__(self, session_factory, originals, *, admin_emails):
        if not callable(session_factory) or isinstance(admin_emails, (str, bytes)):
            raise ValueError('Explicit sessions and administrator policy required')
        self.sessions, self.originals = session_factory, originals
        self.admin_emails = frozenset(x.strip().lower() for x in admin_emails if x.strip())

    @contextmanager
    def _access(self, actor, identity, password_version):
        with self.sessions() as session, session.begin():
            # Same actor -> source lock order as content writers. No owner lock
            # after source: a superadmin can view another tenant's resume.
            user = session.execute(sa.select(User.__table__).where(User.id == actor)
                                   .with_for_update()).mappings().one_or_none()
            if (user is None or user['status'] != 'active' or user['deleted_at'] is not None or
                    type(password_version) is not int or user['password_version'] != password_version):
                raise ContentError('Invalid or revoked token', 401)
            if (user['email'] or '').strip().lower() not in self.admin_emails:
                raise ContentError('Superadministrator required', 403)
            row = session.execute(sa.select(Resume.__table__).where(Resume.id == identity,
                Resume.deleted_at.is_(None)).with_for_update()).mappings().one_or_none()
            if row is None:
                raise ContentError('Record not found', 404)
            meta = deepcopy((row['legacy_payload'] or {}).get('original'))
            key = row['object_key']
            # Imported originals use the legacy UUID filename; their verified
            # manifest has no storage_id. New uploads always carry a store ID.
            if not key and meta:
                key = str(identity) + '.' + str(meta.get('ext', ''))
            yield session, (key, meta)

    def view(self, actor_id, resume_id, *, password_version, request_id, page=None):
        actor, identity = _uuid(actor_id), _uuid(resume_id)
        if page is not None and (type(page) is not int or not 1 <= page <= MAX_PAGES):
            raise ContentError('Original page not found', 404)
        with self._access(actor, identity, password_version) as (_, snapshot):
            key, meta = snapshot
            if bool(key) != bool(meta):
                raise ContentError('Original unavailable', 503)
            try:
                data = self.originals.read_verified(key, meta) if key else None
            except (ValueError, OSError):
                raise ContentError('Original unavailable', 503) from None
        # Native parsing happens outside database locks. Reauthorize afterwards
        # so revoked access or concurrent source deletion cannot publish a page.
        result = {'available': False}
        png, total = b'', 0
        if data is not None:
            try:
                png, total = inspect_pdf(data, meta['ext'], page=page)
            except Exception:
                raise ContentError('Original unavailable', 503) from None
            result = dict(available=True, **{k: meta.get(k, '') for k in
                          ('ext', 'name', 'bytes', 'sha256', 'stored_at')}, renderable=bool(total), page_count=total)
        if page is not None and not png:
            raise ContentError('Original page not found', 404)
        with self._access(actor, identity, password_version) as (session, current):
            if current != snapshot:
                raise ContentError('Original changed during access', 409)
            append_audit(session, actor_user_id=actor, action='resume.original_page' if page else 'resume.original_info',
                         target_type='resume', target_id=str(identity), request_id=request_id, original_page=page)
        # The audit commit must succeed before any bytes reach HTTP.
        return (png, total) if page is not None else result

    def download(self, actor_id, resume_id, *, password_version, request_id):
        """Attachment delivery of the stored original, same admin/audit boundary."""
        actor, identity = _uuid(actor_id), _uuid(resume_id)
        with self._access(actor, identity, password_version) as (_, snapshot):
            key, meta = snapshot
            if not key or not meta:
                raise ContentError('Original unavailable', 503)
            try:
                data = self.originals.read_verified(key, meta)
            except (ValueError, OSError):
                raise ContentError('Original unavailable', 503) from None
        if data is None:
            raise ContentError('Original unavailable', 503)
        with self._access(actor, identity, password_version) as (session, current):
            if current != snapshot:
                raise ContentError('Original changed during access', 409)
            append_audit(session, actor_user_id=actor, action='resume.original_download',
                         target_type='resume', target_id=str(identity), request_id=request_id)
        return data, meta

    def preview_content(self, actor_id, resume_id, *, password_version, request_id):
        """Inline DOCX rendering: same admin/audit boundary, no download semantics."""
        actor, identity = _uuid(actor_id), _uuid(resume_id)
        with self._access(actor, identity, password_version) as (_, snapshot):
            key, meta = snapshot
            if not key or not meta:
                raise ContentError('Original unavailable', 503)
            if str(meta.get('ext', '')).lower() != 'docx':
                raise ContentError('Preview is only available for DOCX originals', 415)
            try:
                data = self.originals.read_verified(key, meta)
            except (ValueError, OSError):
                raise ContentError('Original unavailable', 503) from None
        if data is None:
            raise ContentError('Original unavailable', 503)
        try:
            from docx_preview import render_docx_html
            content = render_docx_html(data)
        except Exception:
            raise ContentError('Original preview failed', 503) from None
        with self._access(actor, identity, password_version) as (session, current):
            if current != snapshot:
                raise ContentError('Original changed during access', 409)
            append_audit(session, actor_user_id=actor, action='resume.original_content',
                         target_type='resume', target_id=str(identity), request_id=request_id)
        return {'html': content, 'ext': 'docx', 'name': meta.get('name', '')}
