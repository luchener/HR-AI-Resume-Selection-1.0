"""HTTP adapters for read-only SQL analysis history and helper tools."""
from __future__ import annotations

from flask import g, request

from db_analysis_history import AnalysisHistoryError
from db_analysis_markdown import (_extract_md_block, _normalize_md_for_a4cv,
                                  _resume_studio_markdown, _count_sections)
from resume_review import build_review_markers
from resume_sanitize import sanitize_resume_content


def register_analysis_history_routes(app, service, *, error, success):
    """Register routes only when an explicit AnalysisHistoryService is injected."""
    if service is None:
        raise ValueError("analysis history service is required")

    @app.errorhandler(AnalysisHistoryError)
    def analysis_history_error(exc):
        return error(exc.detail, exc.status)

    def actor_args():
        return dict(actor_id=str(g.preview_user.user_id), password_version=g.preview_user.password_version)

    def query_int(name, default):
        value = request.args.get(name, default)
        return value

    @app.post('/api/v1/resumes/review-markers')
    def db_review_markers():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            raise AnalysisHistoryError('Request body must be an object')
        resume_id = data.get('resume_id')
        if not isinstance(resume_id, str) or not resume_id.strip():
            raise AnalysisHistoryError('resume_id is required')
        context = service.get_context(
            **actor_args(), resume_id=resume_id, analysis_id=data.get('analysis_id'),
            analysis=data.get('analysis') if type(data.get('analysis')) is dict else None,
        )
        payload = build_review_markers(
            sanitize_resume_content(context['content']), context['analysis'],
            candidate_name=str(data.get('candidate_name') or '候选人'),
        )
        return success(payload)

    @app.post('/api/v1/resumes/improved-markdown')
    def db_improved_markdown():
        data = request.get_json(silent=True)
        if type(data) is not dict:
            raise AnalysisHistoryError('Request body must be an object')
        resume_id = data.get('resume_id')
        analysis_result = data.get('analysis_result') or ''
        if not isinstance(analysis_result, str):
            raise AnalysisHistoryError('analysis_result must be a string')
        context = None
        # Match the legacy contract: a valid markdown block is self-contained
        # and must not force a database lookup merely because resume_id was sent.
        require_optimized = data.get('require_optimized') is True
        md, source = _extract_md_block(analysis_result, require_step_four=require_optimized)
        if not md and resume_id is not None and not require_optimized:
            if not isinstance(resume_id, str) or not resume_id.strip():
                raise AnalysisHistoryError('Invalid resume_id')
            try:
                context = service.get_context(
                    **actor_args(), resume_id=resume_id, analysis_id=data.get('analysis_id'),
                )
            except AnalysisHistoryError as exc:
                if exc.status != 404 or data.get('analysis_id'):
                    raise
        if md:
            md = _normalize_md_for_a4cv(md)
        elif context is not None:
            md = _resume_studio_markdown(context)
            source = 'fallback' if md else 'none'
        else:
            md, source = '', 'none'
        return success({'markdown': md, 'source': source, 'sections_detected': _count_sections(md)})

    @app.get('/api/v1/analysis-history')
    def db_analysis_history_list():
        return success(service.list_user(
            **actor_args(), resume_id=request.args.get('resume_id'),
            job_id=request.args.get('job_id'), task_id=request.args.get('task_id'),
            status=request.args.get('status'), page=query_int('page', 1), size=query_int('size', 20),
        ))

    @app.get('/api/v1/analysis-history/<analysis_id>')
    def db_analysis_history_detail(analysis_id):
        return success(service.get_user(analysis_id=analysis_id, **actor_args()))

    @app.get('/api/v1/admin/analyses')
    def db_admin_analysis_list():
        result = service.list_admin(
            **actor_args(), owner_user_id=request.args.get('owner_user_id'),
            resume_id=request.args.get('resume_id'), job_id=request.args.get('job_id'),
            status=request.args.get('status'), keyword=request.args.get('keyword', ''),
            page=query_int('page', 1), size=query_int('size', 20),
        )
        return success(result)

    @app.get('/api/v1/admin/analyses/<analysis_id>')
    def db_admin_analysis_detail(analysis_id):
        return success(service.get_admin(analysis_id=analysis_id, request_id=g.request_id, **actor_args()))
