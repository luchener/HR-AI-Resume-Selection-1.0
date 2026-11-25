"""Opt-in SQL adapter for model diagnostics and resume improvement."""
from __future__ import annotations

from time import monotonic
from uuid import UUID, uuid4

import sqlalchemy as sa

from db_models import Job, Resume, User
from db_repository import _uuid


class AIServiceError(Exception):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail, self.status = detail, status


class AIService:
    """Provider calls are injected so this module never reaches a real vendor in tests.

    ``improver`` receives keyword arguments ``resume``, ``job`` and
    ``runtime_config``.  Resume improvement requires an AnalysisWorkflow so
    every provider call is owned by a durable task/quota transaction.  The
    direct provider callback remains available only as an injected dependency
    and is never used as an improvement fallback.
    """

    def __init__(self, session_factory, *, model_tester, improver, workflow=None, normalizer=None):
        if not callable(session_factory) or not callable(model_tester) or not callable(improver):
            raise TypeError("Explicit sessions and provider callbacks are required")
        self.sessions = session_factory
        self.model_tester = model_tester
        self.improver = improver
        self.workflow = workflow
        self.normalizer = normalizer

    def _actor(self, session, actor_id, password_version):
        try:
            identity = _uuid(actor_id, "user_id")
        except (TypeError, ValueError):
            raise AIServiceError("Invalid or revoked token", 401) from None
        row = session.execute(sa.select(User.__table__).where(User.id == identity).with_for_update()).mappings().one_or_none()
        if row is None or row["status"] != "active" or row["deleted_at"] is not None or type(password_version) is not int or row["password_version"] != password_version:
            raise AIServiceError("Invalid or revoked token", 401)
        return identity

    def normalize(self, value):
        if self.normalizer is None:
            if value is None:
                raise AIServiceError("AI model configuration is required", 422)
            if not isinstance(value, dict):
                raise AIServiceError("Invalid AI model configuration", 422)
            return dict(value)
        try:
            return self.normalizer(value)
        except (TypeError, ValueError):
            raise AIServiceError("Invalid AI model configuration", 422) from None

    def test(self, actor_id, *, password_version, runtime_config):
        with self.sessions() as session, session.begin():
            self._actor(session, actor_id, password_version)
        started = monotonic()
        try:
            result = self.model_tester(runtime_config=runtime_config)
        except Exception:
            raise AIServiceError("AI model connection failed", 502) from None
        if not isinstance(result, dict):
            result = {}
        return {"ok": True, "provider": runtime_config.get("provider", "custom"),
                "model": runtime_config.get("model", ""),
                "elapsed_ms": int((monotonic() - started) * 1000),
                **{key: result[key] for key in ("provider", "model") if key in result}}

    def _sources(self, actor_id, password_version, resume_id, job_id):
        try:
            resume_identity, job_identity = _uuid(resume_id, "resume_id"), _uuid(job_id, "job_id")
        except (TypeError, ValueError):
            raise AIServiceError("Invalid UUID", 400) from None
        with self.sessions() as session, session.begin():
            owner = self._actor(session, actor_id, password_version)
            resume = session.execute(sa.select(Resume.__table__).where(
                Resume.id == resume_identity, Resume.owner_user_id == owner, Resume.deleted_at.is_(None),
                Resume.parse_status == "parsed")).mappings().one_or_none()
            job = session.execute(sa.select(Job.__table__).where(
                Job.id == job_identity, Job.owner_user_id == owner, Job.deleted_at.is_(None))).mappings().one_or_none()
            if resume is None:
                raise AIServiceError("Resume not found", 404)
            if job is None:
                raise AIServiceError("Job not found", 404)
            return {"resume_id": str(resume_identity), "content": resume["raw_content"], "processed": resume["processed_data"]},\
                   {"job_id": str(job_identity), "content": job["raw_content"], "processed": job["parsed_requirements"]}

    def improve(self, actor_id, *, password_version, resume_id, job_id, runtime_config=None, request_id=None):
        if self.workflow is None:
            raise AIServiceError("Analysis workflow is not configured", 503)
        if runtime_config is not None:
            raise AIServiceError('Improvement uses the configured server model', 422)
        self._sources(actor_id, password_version, resume_id, job_id)
        try:
            task_id, future = self.workflow.submit(owner_user_id=actor_id, password_version=password_version,
                request_id=request_id or str(uuid4()), job_id=job_id, resume_ids=[resume_id], kind='improvement')
            future.result(timeout=300)
            snapshot = self.workflow.snapshot(task_id=task_id, owner_user_id=actor_id, password_version=password_version)
        except TimeoutError:
            raise AIServiceError("Analysis is still running", 202) from None
        except AIServiceError:
            raise
        except Exception:
            raise AIServiceError("Analysis failed", 503) from None
        if snapshot['status'] != 'completed' or not snapshot.get('improvement', {}).get('analysis_result'):
            raise AIServiceError("Analysis failed", 502)
        return {"resume_id": str(resume_id), "job_id": str(job_id), "analysis_result": snapshot['improvement']['analysis_result'],
                "details": "Analysis completed successfully using hr_judge prompt template.",
                "commentary": "The resume has been analyzed against the job description using the hr_judge prompt template."}
