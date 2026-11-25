"""Unwired SQL uploads and archive transactions, with explicit local originals.

All writes lock actor first, then source/archive rows. Parsing and immutable
object creation happen outside SQL locks, with authorization checked before and
after. This service never imports JSON storage or connects itself to production.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from uuid import uuid4
import zipfile

import sqlalchemy as sa

from db_analysis_service import _validate_json
from db_audit import append_audit
from db_models import Archive, Job, Resume, User
from db_original_gc import original_gc
from db_original_objects import MAX_ORIGINAL_BYTES
from db_repository import ArchiveRepository, JobRepository, ResumeRepository, _uuid as _parse_uuid
from resume_names import resolve_candidate_name
from db_source_scope import has_unfinished_task_for_source
from db_store import _record


class ContentError(Exception):
    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.detail, self.status = detail, status


def _uuid(value, label='record_id'):
    try:
        return _parse_uuid(value, label)
    except ValueError:
        raise ContentError('Invalid UUID', 400) from None


def _json(value):
    try:
        _validate_json(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf8')) > 2 * 1024 * 1024:
            raise ValueError()
        return json.loads(encoded)
    except (ValueError, RecursionError, OverflowError):
        raise ContentError('Invalid or oversized content') from None


def _text(value, limit, *, empty=True):
    if type(value) is not str or len(value) > limit or (not empty and not value.strip()):
        raise ContentError('Invalid content field')
    _json(value)
    return value.strip()


def _tags(value):
    if type(value) is not list or len(value) > 50:
        raise ContentError('custom_tags must contain at most 50 strings')
    return [clean for item in value if (clean := _text(item, 100))]


class ContentService:
    def __init__(self, session_factory, originals, *, extract_text, summarize_job):
        if not all(callable(x) for x in (session_factory, extract_text, summarize_job)):
            raise ValueError('Explicit sessions and local parsers are required')
        self.sessions, self.originals = session_factory, originals
        self.extract_text, self.summarize_job = extract_text, summarize_job

    @contextmanager
    def _operation(self, actor_id, password_version):
        owner = _uuid(actor_id)
        with self.sessions() as session, session.begin():
            actor = session.execute(sa.select(User.__table__).where(User.id == owner)
                                    .with_for_update()).mappings().one_or_none()
            if (actor is None or actor['status'] != 'active' or actor['deleted_at'] is not None or
                    type(password_version) is not int or actor['password_version'] != password_version):
                raise ContentError('Invalid or revoked token', 401)
            yield session, owner

    def _audit(self, session, owner, kind, identity, action, request_id, fields=()):
        append_audit(session, actor_user_id=owner, action=kind + '.' + action,
                     target_type=kind, target_id=str(identity), request_id=request_id, changed_fields=fields)

    def upload_resume(self, actor_id, data, filename, *, password_version, request_id):
        with self._operation(actor_id, password_version):
            pass
        if type(data) is not bytes or not data or len(data) > MAX_ORIGINAL_BYTES:
            raise ContentError('Uploaded file exceeds the 30 MB limit or is empty', 413)
        filename = _text(filename, 512, empty=False)
        if filename.lower().endswith('.pdf') and data.startswith(b'%PDF'):
            ext, mime = 'pdf', 'application/pdf'
        elif filename.lower().endswith('.docx') and data.startswith(b'PK'):
            ext, mime = 'docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as doc:
                    members = doc.infolist()
                    if (len(members) > 512 or sum(x.file_size for x in members) > 64 * 1024 * 1024 or
                            any(x.flag_bits & 1 or x.file_size > 16 * 1024 * 1024 for x in members)):
                        raise ValueError()
            except (ValueError, zipfile.BadZipFile):
                raise ContentError('Invalid or oversized DOCX', 400) from None
        else:
            raise ContentError('Unsupported file type', 400)
        try:
            text = self.extract_text(data, mime)
        except Exception as exc:
            status = 413 if getattr(exc, 'http_status', None) == 413 else 400
            raise ContentError('File conversion failed', status) from None
        text = _text(text, 2 * 1024 * 1024, empty=False)
        key, meta = self.originals.put(data, ext=ext, filename=filename)
        with self._operation(actor_id, password_version) as (session, owner):
            row = ResumeRepository(session).insert(owner, dict(raw_content=text, processed_data={},
                candidate_name=resolve_candidate_name(text=text, filename=meta['name']) or None,
                source_filename=meta['name'], object_key=key, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                content_type='md', legacy_payload={'original': meta}))
            self._audit(session, owner, 'resume', row['id'], 'create', request_id)
            result = dict(resume_id=str(row['id']), extracted_characters=len(text), original_saved=True)
        return result

    def upload_jobs(self, actor_id, resume_id, descriptions, *, password_version, request_id):
        if type(descriptions) is not list or not 1 <= len(descriptions) <= 50:
            raise ContentError('One to fifty job descriptions are required')
        descriptions = [_text(item, 20000, empty=False) for item in descriptions]
        resume_id = _uuid(resume_id, 'resume_id')
        with self._operation(actor_id, password_version) as (session, owner):
            self._source(session, Resume, owner, resume_id)
        processed = [_json(self.summarize_job(item)) for item in descriptions]
        if any(type(item) is not dict for item in processed):
            raise ContentError('Invalid parser result', 503)
        result = []
        with self._operation(actor_id, password_version) as (session, owner):
            self._source(session, Resume, owner, resume_id)
            for content, parsed in zip(descriptions, processed):
                title = _text(parsed.get('job_title') or '', 512)
                row = JobRepository(session).insert(owner, dict(source_resume_id=resume_id, raw_content=content,
                    parsed_requirements=parsed, title=title, legacy_payload={'resume_id': str(resume_id)}))
                self._audit(session, owner, 'job', row['id'], 'create', request_id)
                result.append(str(row['id']))
        return result

    @staticmethod
    def _source(session, model, owner, identity):
        row = session.execute(sa.select(model.__table__).where(model.id == identity,
            model.owner_user_id == owner, model.deleted_at.is_(None)).with_for_update()).mappings().one_or_none()
        if row is None:
            raise ContentError('Record not found', 404)
        return row

    def create_archive(self, actor_id, payload, *, password_version, request_id):
        payload = _json(payload)
        if type(payload) is not dict or not payload.get('resume_id') or not payload.get('job_id'):
            raise ContentError('resume_id and job_id are required')
        resume_id, job_id = _uuid(payload['resume_id']), _uuid(payload['job_id'])
        with self._operation(actor_id, password_version) as (session, owner):
            resume = self._source(session, Resume, owner, resume_id)
            job = self._source(session, Job, owner, job_id)
            # Actor lock serializes all service archive writes for this tenant,
            # including creation while a matching record is in the trash.
            existing = session.execute(sa.select(Archive.__table__).where(Archive.owner_user_id == owner,
                Archive.resume_id == resume_id, Archive.job_id == job_id,
                Archive.status.in_(['active', 'trashed'])).order_by(Archive.status, Archive.created_at, Archive.id)
                .limit(1)).mappings().one_or_none()
            if existing is not None:
                return _record(existing, 'archive'), False
            hr = payload.get('analysis', {})
            if type(hr) is not dict:
                raise ContentError('analysis must be an object')
            reported_name = _text(payload.get('candidate_name') or hr.get('candidate_name') or '', 256)
            name = resolve_candidate_name(
                resume['candidate_name'],
                text=resume['raw_content'], filename=resume['source_filename'] or '',
                fallback_names=(reported_name, hr.get('candidate_name')),
            ) or '未提供'
            lines = job['raw_content'].strip().splitlines()
            title = _text(job['parsed_requirements'].get('job_title') or (lines[0] if lines else ''), 512)
            category = _text(payload.get('category') or '', 128) or title[:128]
            raw_score = hr.get('final_score', payload.get('final_score', 0))
            if type(raw_score) not in (int, float) or not 0 <= raw_score <= 100:
                raise ContentError('final_score must be between 0 and 100')
            score = int(raw_score)
            fit = _text(hr.get('fit_tag') or '', 64)
            recommendation = _text(hr.get('recruitment_recommendation') or '', 64)
            work = hr.get('work_history') or {}
            if type(work) is not dict or any(type(hr.get(key) or []) is not list for key in
                                          ('gate_blockers', 'non_mitigable_blockers')):
                raise ContentError('Invalid analysis structure')
            snapshot = dict(candidate_name=name, final_score=score, fit_tag=fit,
                recruitment_recommendation=recommendation, job_fit_percentage=hr.get('job_fit_percentage'),
                summary=str(hr.get('summary') or ''), ai_risk_label=str(hr.get('ai_risk_label') or ''),
                relevant_years=work.get('relevant_years'), analysis_result_md=str(payload.get('analysis_result') or ''),
                capability_score=hr.get('capability_score'), capability_basis=hr.get('capability_basis'),
                override_review=bool(hr.get('override_review')), gate_cap_applied=hr.get('gate_cap_applied'),
                gate_blockers=[{key: str(item.get(key) or '') for key in ('text', 'tier', 'evidence_label')}
                    for item in (hr.get('gate_blockers') or [])[:3] if type(item) is dict],
                non_mitigable_blockers=[str(item.get('text') or '')
                    for item in (hr.get('non_mitigable_blockers') or [])[:3] if type(item) is dict])
            row = ArchiveRepository(session).insert(owner, dict(resume_id=resume_id, job_id=job_id,
                candidate_name=name, category=category, tags=_tags(payload.get('custom_tags', [])),
                payload=dict(final_score=score, fit_tag=fit, recruitment_recommendation=recommendation,
                    job_title=title, analysis_snapshot=snapshot, analysis=hr)))
            self._audit(session, owner, 'archive', row['id'], 'create', request_id)
            return _record(row, 'archive'), True

    def mutate_archive(self, actor_id, archive_id, action, value=None, *, password_version, request_id):
        identity = _uuid(archive_id, 'archive_id')
        if action not in {'tags', 'category', 'trash', 'restore', 'purge'}:
            raise ContentError('Invalid archive action')
        if action == 'tags':
            value = _tags(value)
        elif action == 'category':
            value = _text(value, 128, empty=False)
        with self._operation(actor_id, password_version) as (session, owner):
            repo = ArchiveRepository(session)
            current = repo._locked(owner, identity)
            required = 'trashed' if action in {'restore', 'purge'} else 'active'
            if current is None or current['status'] != required:
                raise ContentError('Archive not found or not editable', 404)
            if action in {'tags', 'category'}:
                row = repo.update(owner, identity, {action: value})
            elif action == 'purge':
                repo.delete(owner, identity)
                row = None
            else:
                getattr(repo, 'soft_delete' if action == 'trash' else 'restore')(owner, identity)
                row = repo.get(owner, identity)
            audit_action = {'tags':'update', 'category':'update', 'trash':'delete', 'purge':'purge', 'restore':'restore'}[action]
            self._audit(session, owner, 'archive', identity, audit_action, request_id,
                        [action] if action in {'tags', 'category'} else [])
            return _record(row, 'archive') if row else None

    def empty_trash(self, actor_id, *, password_version, request_id):
        with self._operation(actor_id, password_version) as (session, owner):
            rows = session.execute(sa.select(Archive.id).where(Archive.owner_user_id == owner,
                Archive.status == 'trashed').order_by(Archive.id).with_for_update()).scalars().all()
            for identity in rows:
                ArchiveRepository(session).delete(owner, identity)
                self._audit(session, owner, 'archive', identity, 'purge', request_id)
            return len(rows)

    def delete_source(self, actor_id, kind, identity, *, password_version, request_id):
        if kind not in {'resume', 'job'}:
            raise ContentError('Invalid source type')
        identity = _uuid(identity, 'record_id')
        model = Resume if kind == 'resume' else Job
        with self._operation(actor_id, password_version) as (session, owner):
            source = self._source(session, model, owner, identity)
            # Accepted dispatch snapshots contain source text; refuse deletion
            # until worker recovery/settlement, rather than race with inference.
            if has_unfinished_task_for_source(session, owner, kind, identity):
                raise ContentError('Finish or recover unfinished analysis tasks before deleting sources', 409)
            rows = session.execute(sa.select(Archive.__table__).where(Archive.owner_user_id == owner,
                (Archive.resume_id if kind == 'resume' else Archive.job_id) == identity,
                Archive.status.in_(['active', 'trashed'])).order_by(Archive.id).with_for_update()).mappings().all()
            trashed = 0
            for row in rows:
                if row['status'] == 'active':
                    ArchiveRepository(session).soft_delete(owner, row['id'])
                    trashed += 1
                session.execute(sa.update(Archive).where(Archive.id == row['id']).values(
                    payload={**deepcopy(row['payload']), 'source_purged': True}))
            now = datetime.now(timezone.utc)
            values = dict(raw_content='', legacy_payload={}, deleted_at=now, updated_at=now)
            if kind == 'resume':
                meta = (source['legacy_payload'] or {}).get('original')
                key = source['object_key']
                if not key and meta:
                    key = str(identity) + '.' + str(meta.get('ext', ''))
                if key:
                    # Validation before queue creation; an unknown remote key
                    # must not be represented as a successful local deletion.
                    self.originals._path(key)
                    if meta and meta.get('storage_id') not in (None, str(self.originals.storage_id)):
                        raise ContentError('Original storage identity mismatch', 503)
                    from sqlalchemy.dialects.postgresql import insert
                    session.execute(insert(original_gc).values(storage_id=self.originals.storage_id,
                                    object_key=key, owner_user_id=owner)
                                    .on_conflict_do_nothing())
                values.update(processed_data={}, object_key=None, content_sha256=None, source_filename=None,
                              candidate_name=None)
            else:
                values.update(parsed_requirements={}, title=None, company_name=None)
            # Redact only dispatch source text, preserving certified results,
            # task/billing state and FKs. This is not a complete retention purge.
            from db_task_inputs import task_inputs
            snapshots = session.execute(sa.select(task_inputs).where(task_inputs.c.owner_user_id == owner)
                                        .order_by(task_inputs.c.task_id).with_for_update()).mappings().all()
            for row in snapshots:
                snap = deepcopy(row['snapshot'])
                if kind == 'resume':
                    for item in snap['resumes']:
                        if item['resume_id'] == str(identity):
                            item.update(content='', processed={})
                elif snap['job']['job_id'] == str(identity):
                    snap['job'].update(content='', processed={})
                if snap != row['snapshot']:
                    session.execute(task_inputs.update().where(task_inputs.c.task_id == row['task_id']).values(snapshot=snap))
            session.execute(sa.update(model).where(model.id == identity, model.owner_user_id == owner).values(**values))
            self._audit(session, owner, kind, identity, 'delete', request_id)
        return trashed

    def collect_originals(self, *, limit=50):
        """Explicit worker call; failures leave durable intents for safe replay.

        Never called by HTTP uploads/deletes. A successful unlink followed by a
        DB failure is safe to replay. Objects cannot be reused or republished.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid cleanup limit')
        count = 0
        for _ in range(limit):
            with self.sessions() as session, session.begin():
                row = session.execute(sa.select(original_gc).where(original_gc.c.storage_id == self.originals.storage_id)
                    .order_by(original_gc.c.created_at, original_gc.c.object_key)
                    .limit(1).with_for_update(skip_locked=True)).mappings().one_or_none()
                if row is None:
                    break
                if session.scalar(sa.select(sa.exists().where(Resume.object_key == row['object_key']))):
                    raise ValueError('Original is still referenced')
                self.originals.remove(row['object_key'])
                session.execute(original_gc.delete().where(original_gc.c.storage_id == self.originals.storage_id,
                                                          original_gc.c.object_key == row['object_key']))
            count += 1
        return count
