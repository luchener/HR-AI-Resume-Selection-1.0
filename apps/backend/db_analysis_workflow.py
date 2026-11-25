"""Unwired, bounded analysis worker for isolated PostgreSQL HTTP rehearsals.

Input acceptance, quota reservation and immutable dispatch snapshots commit
before dispatch. A renewable durable claim prevents inference redispatch across
process restarts. Expired claims are fenced and settled, never automatically
redispatched because provider completion may be uncertain.
"""
from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import BoundedSemaphore, Event, Thread
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from db_analysis_service import _outcomes, write_analysis_batch
from db_drain import DrainTracker
from db_models import Analysis, AnalysisTask, Job, Resume, User
from db_quota import quota_policies, quota_reservations, reserve, resolve_policy
from db_repository import _uuid
from db_task_inputs import task_inputs
from db_task_lifecycle import (
    WORKER_LEASE_SECONDS, _task, create_task, recover_expired_tasks,
    renew_worker_lease, start_task,
)

TERMINAL = frozenset({'completed', 'partial_success', 'failed', 'cancelled'})

logger = logging.getLogger(__name__)
class WorkflowError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status_code = status


class AnalysisWorkflow:
    def __init__(self, session_factory, analyzer, *, admin_emails=(), max_workers=2,
                 max_pending=8, tracker=None, improver=None, worker_lease_seconds=WORKER_LEASE_SECONDS,
                 heartbeat_seconds=None, recovery_interval_seconds=15):
        if not callable(session_factory) or not callable(analyzer):
            raise TypeError('Explicit database sessions and analyzer are required')
        if type(max_workers) is not int or type(max_pending) is not int or not 1 <= max_workers <= max_pending <= 32:
            raise ValueError('Require 1 <= max_workers <= max_pending <= 32')
        if type(worker_lease_seconds) is not int or not 3 <= worker_lease_seconds <= 3600:
            raise ValueError('worker_lease_seconds must be an integer from 3 to 3600')
        if heartbeat_seconds is None:
            heartbeat_seconds = max(1, worker_lease_seconds // 3)
        if type(heartbeat_seconds) is not int or not 1 <= heartbeat_seconds < worker_lease_seconds:
            raise ValueError('heartbeat_seconds must be an integer below the worker lease')
        if type(recovery_interval_seconds) is not int or not 1 <= recovery_interval_seconds <= 3600:
            raise ValueError('recovery_interval_seconds must be an integer from 1 to 3600')
        if isinstance(admin_emails, (str, bytes)):
            raise TypeError('admin_emails must be an iterable')
        self.sessions = session_factory
        self.analyzer = analyzer
        if improver is not None and not callable(improver):
            raise TypeError("Improver must be callable")
        self.improver = improver
        self.admin_emails = frozenset(email.strip().lower() for email in admin_emails if email.strip())
        self.tracker = tracker if tracker is not None else DrainTracker()
        self.worker_lease_seconds = worker_lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.recovery_interval_seconds = recovery_interval_seconds
        self._capacity = BoundedSemaphore(max_pending)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='db-analysis-preview')
        self._recovery_stop = Event()
        self._recovery_thread = Thread(
            target=self._reap_expired_loop, name='db-analysis-lease-reaper', daemon=True)
        self._recovery_thread.start()

    def close(self):
        self.tracker.begin()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._recovery_stop.set()
        self._recovery_thread.join(timeout=self.recovery_interval_seconds + 1)
        return not self._recovery_thread.is_alive()

    def _reap_expired_loop(self):
        while not self._recovery_stop.wait(self.recovery_interval_seconds):
            try:
                with self.sessions() as session, session.begin():
                    recovered = recover_expired_tasks(session.connection(), limit=100)
            except Exception:
                logger.warning('analysis task lease recovery check failed')
                continue
            if recovered:
                logger.info('analysis task lease recovery completed count=%s', recovered)

    def drain_report(self):
        """Counts only; the process tracker alone cannot prove durable completion."""
        with self.sessions() as session, session.begin():
            unfinished = session.scalar(sa.select(sa.func.count()).select_from(AnalysisTask.__table__).where(
                AnalysisTask.status.not_in(TERMINAL)))
            reservations = session.scalar(sa.select(sa.func.count()).select_from(quota_reservations).where(
                quota_reservations.c.status == 'pending'))
        activities = self.tracker.status()
        return {**activities, 'unfinished_tasks': unfinished, 'pending_reservations': reservations,
                'complete': activities['draining'] and not any(activities[key] for key in
                ('requests', 'streams', 'workers')) and unfinished == reservations == 0}

    def _actor(self, conn, owner, password_version):
        user = conn.execute(sa.select(User.__table__).where(User.id == owner).with_for_update()).mappings().one_or_none()
        if (user is None or user['status'] != 'active' or user['deleted_at'] is not None or
                type(password_version) is not int or user['password_version'] != password_version):
            raise WorkflowError('Invalid or revoked identity', 401)
        return user

    def submit(self, *, owner_user_id, password_version, request_id, job_id, resume_ids, kind="screening", web_search=False):
        """Commit acceptance before submitting a bounded local worker.

        Returns (task_id, Future). The future is process-local; task state is the
        durable authority. Executor failure leaves a pending, reserved task that
        the SAME request can dispatch again. Running claims are never retried.
        """
        owner, request, job = (_uuid(owner_user_id), _uuid(request_id, 'request_id'), _uuid(job_id, 'job_id'))
        if not isinstance(resume_ids, (list, tuple)) or not 1 <= len(resume_ids) <= 3:
            raise WorkflowError('One to three resumes are required', 422)
        if kind not in {'screening', 'improvement'} or type(web_search) is not bool or (kind == 'improvement' and (len(resume_ids) != 1 or web_search)):
            raise WorkflowError('Invalid task kind or search option', 422)
        resumes = [_uuid(value, 'resume_id') for value in resume_ids]
        if len(set(resumes)) != len(resumes):
            raise WorkflowError('Duplicate resumes are not allowed', 422)
        digest_inputs = {'job_id': str(job), 'resume_ids': sorted(str(value) for value in resumes)}
        if kind != 'screening' or web_search:
            digest_inputs.update(kind=kind, web_search=web_search)
        request_digest = hashlib.sha256(json.dumps(
            digest_inputs,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        close_activity = self.tracker.enter('workers')
        if not self._capacity.acquire(blocking=False):
            close_activity()
            raise WorkflowError('Analysis preview is busy', 503)
        try:
            with self.sessions() as session, session.begin():
                conn = session.connection()
                actor = self._actor(conn, owner, password_version)
                proposed_task_id = uuid4()
                try:
                    task = create_task(conn, owner_user_id=owner, job_id=job,
                                       total_candidates=len(resumes), request_id=str(request),
                                       task_id=proposed_task_id, kind=kind, web_search=web_search)
                except ValueError:
                    raise WorkflowError('Request key conflicts with an existing task', 409) from None
                except IntegrityError as exc:
                    if getattr(exc.orig, 'sqlstate', None) == '23503':
                        raise WorkflowError('Resume or job not found or unavailable', 404) from None
                    raise
                task_id = task['id']
                existing = conn.execute(sa.select(task_inputs).where(task_inputs.c.task_id == task_id)).mappings().one_or_none()
                if existing is not None:
                    if existing['owner_user_id'] != owner or existing['request_digest'] != request_digest:
                        raise WorkflowError('Request key conflicts with accepted inputs', 409)
                else:
                    if task_id != proposed_task_id:
                        raise WorkflowError('Existing task has no dispatch input certificate', 409)
                    job_row = conn.execute(sa.select(Job.__table__).where(
                        Job.id == job, Job.owner_user_id == owner, Job.deleted_at.is_(None)
                    ).with_for_update(read=True)).mappings().one_or_none()
                    resume_rows = conn.execute(sa.select(Resume.__table__).where(
                        Resume.id.in_(resumes), Resume.owner_user_id == owner,
                        Resume.deleted_at.is_(None), Resume.parse_status == 'parsed'
                    ).order_by(Resume.id).with_for_update(read=True)).mappings().all()
                    if job_row is None or len(resume_rows) != len(resumes):
                        raise WorkflowError('Resume or job not found or unavailable', 404)
                    policy_rows = conn.execute(sa.select(quota_policies).where(
                        quota_policies.c.scope_key.in_(['global', str(owner)])
                    ).order_by(quota_policies.c.scope_key).with_for_update(read=True)).mappings().all()
                    policies = {row['scope_key']: row for row in policy_rows}
                    is_admin = bool(actor['is_admin'] or (actor['email'] or '').strip().lower() in self.admin_emails)
                    policy = resolve_policy(policies.get('global'), policies.get(str(owner)), is_admin=is_admin)
                    reserve(conn, request_id=request, user_id=owner, task_id=task_id,
                            count=len(resumes), policy=policy)
                    by_id = {row['id']: row for row in resume_rows}
                    snapshot = {
                        'job': {'job_id': str(job), 'content': job_row['raw_content'],
                                'processed': job_row['parsed_requirements']},
                        'resumes': [{'resume_id': str(identity), 'content': by_id[identity]['raw_content'],
                                     'processed': by_id[identity]['processed_data']} for identity in resumes],
                    }
                    conn.execute(task_inputs.insert().values(
                        task_id=task_id, owner_user_id=owner, password_version=password_version,
                        request_digest=request_digest, snapshot=snapshot))
            # All accept/reserve/input writes have committed before worker access.
            future = self._executor.submit(self._run, task_id=task_id, owner_user_id=owner)
        except BaseException:
            self._capacity.release()
            close_activity()
            raise
        def release(_):
            self._capacity.release()
            close_activity()
        future.add_done_callback(release)
        return str(task_id), future

    def _run(self, *, task_id, owner_user_id):
        """Trusted worker entry; never expose this method as a public callback."""
        task_id, owner = _uuid(task_id, 'task_id'), _uuid(owner_user_id)
        token = uuid4()
        with self.sessions() as session, session.begin():
            conn = session.connection()
            # Actor -> task -> usage -> reservation is the admission lock order.
            inputs = conn.execute(sa.select(task_inputs).where(
                task_inputs.c.task_id == task_id, task_inputs.c.owner_user_id == owner
            )).mappings().one_or_none()
            if inputs is None:
                raise WorkflowError('Task not found', 404)
            self._actor(conn, owner, inputs['password_version'])
            task = _task(conn, task_id, owner)
            if task is None:
                raise WorkflowError('Task not found', 404)
            inputs = conn.execute(sa.select(task_inputs).where(task_inputs.c.task_id == task_id)).mappings().one()
            if task['status'] in TERMINAL or inputs['worker_token'] is not None:
                return False
            start_task(conn, task_id=task_id, owner_user_id=owner)
            conn.execute(task_inputs.update().where(
                task_inputs.c.task_id == task_id,
                task_inputs.c.owner_user_id == owner,
            ).values(
                worker_token=token,
                lease_until=sa.func.clock_timestamp() + sa.text(
                    f"INTERVAL '{self.worker_lease_seconds} seconds'"),
            ))
            snapshot = deepcopy(inputs['snapshot'])
            kind, web_search = task['kind'], task['web_search']
        stop_heartbeat = Event()
        lease_lost = Event()

        def heartbeat():
            while not stop_heartbeat.wait(self.heartbeat_seconds):
                try:
                    with self.sessions() as session, session.begin():
                        renewed = renew_worker_lease(
                            session.connection(), task_id=task_id, owner_user_id=owner,
                            worker_token=token, lease_seconds=self.worker_lease_seconds)
                except Exception:
                    # The result writer still checks the deadline; an expired
                    # lease cannot revive, even after a transient DB outage.
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_thread = Thread(target=heartbeat, name='db-analysis-lease-heartbeat', daemon=True)
        heartbeat_thread.start()
        try:
            analyzer = self.improver if kind == 'improvement' else self.analyzer
            binding = None
            preparation_failed = not callable(analyzer)
            if kind == 'screening' and web_search:
                configure = getattr(type(analyzer), 'for_task', None)
                if configure is None:
                    preparation_failed = True
                else:
                    try:
                        analyzer = analyzer.for_task(web_search=True)
                    except Exception:
                        preparation_failed = True
            if getattr(type(analyzer), 'prepare_task', None) is not None:
                try:
                    analyzer, binding = analyzer.prepare_task()
                    if not callable(analyzer):
                        raise ValueError('Prepared analyzer must be callable')
                    from db_model_binding import bind_model
                    with self.sessions() as session, session.begin():
                        conn = session.connection()
                        self._actor(conn, owner, inputs['password_version'])
                        bind_model(conn, task_id=task_id, owner_user_id=owner,
                                   worker_token=token, binding=binding)
                except Exception:
                    # No provider work has started. Fail the accepted candidates
                    # through the ordinary atomic result/quota path below.
                    preparation_failed = True
            for resume in snapshot['resumes']:
                if lease_lost.is_set():
                    return False
                # Recheck authorization and the durable fence before external work.
                with self.sessions() as session, session.begin():
                    conn = session.connection()
                    self._actor(conn, owner, inputs['password_version'])
                    task = _task(conn, task_id, owner)
                    if task['status'] in TERMINAL:
                        return False
                    live_claim = conn.scalar(sa.select(task_inputs.c.task_id).where(
                        task_inputs.c.task_id == task_id,
                        task_inputs.c.owner_user_id == owner,
                        task_inputs.c.worker_token == token,
                        task_inputs.c.lease_until > sa.func.clock_timestamp(),
                    ))
                    if live_claim is None:
                        return False
                from db_provider_gateway import ProviderUnavailable
                try:
                    if preparation_failed:
                        raise ValueError('Analysis preparation unavailable')

                    def persist_event(event):
                        from db_task_events import append_worker_event
                        with self.sessions() as session, session.begin():
                            conn = session.connection()
                            self._actor(conn, owner, inputs['password_version'])
                            append_worker_event(conn, task_id=task_id, owner_user_id=owner,
                                worker_token=token, resume_id=_uuid(resume['resume_id']), stage=event['status'])

                    def run_analysis():
                        event_analyzer = getattr(type(analyzer), 'analyze_with_events', None)
                        if event_analyzer is not None:
                            from db_provider_gateway import screening_task_id
                            diagnostic_token = screening_task_id.set(task_id)
                            try:
                                return analyzer.analyze_with_events(resume=deepcopy(resume),
                                    job=deepcopy(snapshot['job']), on_event=persist_event)
                            finally:
                                screening_task_id.reset(diagnostic_token)
                        return analyzer(resume=deepcopy(resume), job=deepcopy(snapshot['job']))

                    # 同一简历 + 同一岗位 + 同一模型/策略版本：复用已完成结果，避免重复分析分数漂移。
                    cached_result = None
                    if kind == 'screening' and binding is not None:
                        with self.sessions() as session:
                            cached = session.execute(
                                sa.select(Analysis.__table__)
                                .join(task_inputs, task_inputs.c.task_id == Analysis.task_id)
                                .where(
                                    Analysis.owner_user_id == owner,
                                    Analysis.resume_id == _uuid(resume['resume_id']),
                                    Analysis.job_id == task['job_id'],
                                    Analysis.status == 'completed',
                                    ~Analysis.analysis_result.contains({'analysis_incomplete': True}),
                                    ~Analysis.scoring_meta.contains({'analysis_incomplete': True}),
                                    ~Analysis.scoring_meta.contains({'incomplete': True}),
                                    Analysis.task_id != task_id,
                                    task_inputs.c.model_binding['config_revision'].astext == binding['config_revision'],
                                    task_inputs.c.model_binding['policy_revision'].astext == binding['policy_revision'],
                                )
                                .order_by(Analysis.created_at.desc())
                                .limit(1)
                            ).mappings().first()
                        if cached is not None:
                            cached_result = deepcopy(cached['analysis_result'])
                    result = cached_result if cached_result is not None else run_analysis()
                except ProviderUnavailable as error:
                    if kind == 'improvement':
                        code = {'finish_length': 'improvement_truncated', 'timeout': 'improvement_timeout',
                                'empty_content': 'invalid_improvement'}.get(error.kind, 'improvement_failed')
                    else:
                        code = 'model_invalid_response' if error.kind == 'invalid_format' else 'analysis_unavailable'
                    item = {'resume_id': resume['resume_id'], 'status': 'failed', 'result': {'error_code': code}}
                except Exception:
                    item = {'resume_id': resume['resume_id'], 'status': 'failed',
                            'result': {'error_code': 'improvement_failed' if kind == 'improvement' else 'analysis_unavailable'}}
                else:
                    if kind == 'improvement':
                        try:
                            from db_task_artifacts import validate_markdown
                            validate_markdown(result)
                            valid = True
                        except (ValueError, UnicodeError):
                            valid = False
                        item = {'resume_id': resume['resume_id'], 'status': 'completed' if valid else 'failed',
                                'result': {'markdown': result} if valid else {'error_code': 'invalid_improvement'}}
                    else:
                        item = {'resume_id': resume['resume_id'], 'status': 'completed', 'result': result}
                        try:
                            _outcomes([item])
                        except (ValueError, TypeError):
                            item = {'resume_id': resume['resume_id'], 'status': 'failed',
                                    'result': {'error_code': 'invalid_analysis_result'}}
                if lease_lost.is_set():
                    return False
                with self.sessions() as session, session.begin():
                    conn = session.connection()
                    self._actor(conn, owner, inputs['password_version'])
                    if kind == 'improvement':
                        from db_task_artifacts import write_improvement
                        write_improvement(conn, task_id=task_id, owner_user_id=owner,
                            worker_token=token, resume_id=resume['resume_id'],
                            markdown=item['result'].get('markdown'), error_code=item['result'].get('error_code'))
                    else:
                        write_analysis_batch(conn, task_id=task_id, owner_user_id=owner,
                                         batch_id='candidate:' + resume['resume_id'], outcomes=[item],
                                         worker_token=token)
                    from db_task_events import append_locked_event
                    append_locked_event(conn, task_id=task_id, owner_user_id=owner,
                        resume_id=_uuid(resume['resume_id']), stage='candidate_' + item['status'])
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=max(1, self.heartbeat_seconds * 2))
        return True

    def event_page(self, *, task_id, owner_user_id, password_version, after=0, limit=100):
        from db_task_events import task_events, MESSAGES
        if type(after) is not int or not 0 <= after <= 256 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid event cursor or page size')
        owner, task_id = _uuid(owner_user_id), _uuid(task_id, 'task_id')
        with self.sessions() as session, session.begin():
            conn = session.connection()
            self._actor(conn, owner, password_version)
            task = _task(conn, task_id, owner)
            if task is None:
                raise WorkflowError('Task not found', 404)
            highest = conn.scalar(sa.select(sa.func.max(task_events.c.sequence)).where(
                task_events.c.task_id == task_id, task_events.c.owner_user_id == owner)) or 0
            if after > highest:
                raise ValueError('Event cursor is ahead of task')
            rows = conn.execute(sa.select(task_events).where(task_events.c.task_id == task_id,
                task_events.c.owner_user_id == owner, task_events.c.sequence > after
            ).order_by(task_events.c.sequence).limit(limit)).mappings().all()
            events = [{'id': row['sequence'], 'status': row['stage'], 'task_id': str(task_id),
                       'resume_id': str(row['resume_id']), 'message': MESSAGES[row['stage']]} for row in rows]
            cursor = events[-1]['id'] if events else after
            return {'events': events, 'next_cursor': cursor, 'has_more': cursor < highest, 'status': task['status']}

    def snapshot(self, *, task_id, owner_user_id, password_version):
        owner, task_id = _uuid(owner_user_id), _uuid(task_id, 'task_id')
        with self.sessions() as session, session.begin():
            conn = session.connection()
            self._actor(conn, owner, password_version)
            task = _task(conn, task_id, owner)
            if task is None:
                raise WorkflowError('Task not found', 404)
            from db_task_artifacts import artifacts
            artifact = conn.execute(sa.select(artifacts).where(artifacts.c.task_id == task_id,
                artifacts.c.owner_user_id == owner)).mappings().one_or_none()
            rows = conn.execute(sa.select(Analysis.__table__).where(
                Analysis.task_id == task_id, Analysis.owner_user_id == owner
            ).order_by(Analysis.created_at, Analysis.id)).mappings().all()
            binding = conn.scalar(sa.select(task_inputs.c.model_binding).where(
                task_inputs.c.task_id == task_id, task_inputs.c.owner_user_id == owner))
            return {
                **({'model_binding': binding} if binding is not None else {}),
                'kind': task['kind'], 'web_search': task['web_search'],
                **({'improvement': {'resume_id': str(artifact['resume_id']), 'job_id': str(artifact['job_id']),
                    'analysis_result': artifact['markdown'], 'error_code': artifact['error_code']}} if artifact is not None else {}),
                'task_id': str(task_id), 'status': task['status'],
                'total_candidates': task['total_candidates'],
                'completed_candidates': task['completed_candidates'],
                'failed_candidates': task['failed_candidates'],
                'analyses': [{'analysis_id': str(row['id']), 'resume_id': str(row['resume_id']),
                              'job_id': str(row['job_id']), 'status': row['status'],
                              'hr_analysis': row['analysis_result'] if row['status'] == 'completed' else None,
                              'error_code': None if row['status'] == 'completed' else row['analysis_result'].get('error_code')}
                             for row in rows],
            }
