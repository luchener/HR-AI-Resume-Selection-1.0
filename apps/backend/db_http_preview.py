"""Isolated DB HTTP preview with explicitly injected analysis/management writes.

Compose explicitly with create_preview_app(Store(...), AuthService(...), secret).
A caller may serve that returned Flask app on a local interface. Importing this
module neither connects to a database nor imports legacy app/auth/store/config.
The bounded, process-local limiter below is preview protection, not a shared
production login policy. /ping reports process liveness, not DB connectivity.
Optional analysis_workflow enables explicit analysis rehearsal routes only.
Optional account_service/config_writes enable isolated management rehearsal.
"""
from __future__ import annotations

from collections import OrderedDict
from math import ceil
from threading import Lock
from time import monotonic, time
from uuid import UUID, uuid4

import jwt
from flask import Flask, Request, g, jsonify, request
from werkzeug.exceptions import HTTPException

from db_auth import InternalUser
from db_config_service import ConfigPermissionError
from db_drain import DrainTracker
import prompt_safety


class PreviewLoginLimiter:
    """Bounded fixed-window IP attempt limiter; distrust forwarded IP headers.

    When capacity is full, reject new keys until an entry expires instead of
    evicting an existing limit. Counts all attempts, including successful ones.
    """

    def __init__(self, attempts=10, window_seconds=60, max_keys=1024, *, clock=monotonic):
        if attempts < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("Positive preview limiter bounds are required")
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._entries = OrderedDict()
        self._lock = Lock()

    def check(self, key):
        """Consume an attempt; return retry seconds, or zero when allowed."""
        with self._lock:
            now = self._clock()
            while self._entries:
                _, (expires, _) = next(iter(self._entries.items()))
                if expires > now:
                    break
                self._entries.popitem(last=False)
            if key not in self._entries:
                if len(self._entries) >= self.max_keys:
                    return max(1, ceil(next(iter(self._entries.values()))[0] - now))
                self._entries[key] = (now + self.window_seconds, 0)
            expires, count = self._entries[key]
            if count >= self.attempts:
                return max(1, ceil(expires - now))
            self._entries[key] = (expires, count + 1)
            return 0


def _uuid(value):
    identity = UUID(str(value))
    if not identity.int:
        raise ValueError("nonzero UUID required")
    return str(identity)


def _archive_payload(rec, *, include_analysis=False):
    result = {key: rec.get(key, "") for key in (
        "candidate_name", "fit_tag", "recruitment_recommendation", "job_title")}
    result.update(archive_id=rec["archive_id"], resume_id=rec.get("resume_id"),
                  job_id=rec.get("job_id"), source_purged=bool(rec.get("source_purged")),
                  final_score=rec.get("final_score", 0) or 0,
                  category=rec.get("category") or rec.get("job_title", ""),
                  custom_tags=rec.get("custom_tags") or [],
                  analysis_snapshot=rec.get("analysis_snapshot") or {},
                  status=rec.get("status", "active"), trashed_at=rec.get("trashed_at"),
                  created_at=rec.get("created_at"))
    if include_analysis:
        result["analysis"] = rec.get("analysis") or {}
    return result


def create_preview_app(store, auth_service, jwt_secret, admin_emails=(), *, login_limiter=None,
                       config_service=None, analysis_workflow=None, account_service=None, config_writes=False,
                       config_diagnostics=None, content_service=None, original_service=None, activity_tracker=None,
                       public_auth=None, invite_service=None, quota_service=None,
                       analysis_history_service=None, announcement_service=None, email_service=None,
                       admin_service=None, ai_service=None, readiness_probe=None,
                       trusted_origins=()):
    """Build an unwired preview using exclusively the explicitly injected services.

    Optional login_limiter implements check(remote_addr) -> retry_seconds (0
    allows the request). Never pass the production filesystem-backed auth module.
    Optional config_service must be a closed db_config_service.ConfigService;
    when absent, configuration routes answer 503 instead of falling back.
    Explicit analysis_workflow additionally enables local DB analysis rehearsal
    routes. Optional account_service/config_writes enable local management
    rehearsal. Default construction remains read-only. No production imports.
    """
    if not isinstance(jwt_secret, str) or not jwt_secret.strip():
        raise ValueError("An explicit JWT secret is required")
    if isinstance(trusted_origins, (str, bytes)):
        raise TypeError("trusted_origins must be an explicit collection of absolute origins")
    normalized_origins = []
    for entry in trusted_origins:
        origin = prompt_safety.normalize_origin(entry)
        if origin is None:
            raise ValueError("Invalid trusted origin: %r" % (entry,))
        normalized_origins.append(origin)
    trusted_origins = tuple(dict.fromkeys(normalized_origins))
    if readiness_probe is not None and not callable(readiness_probe):
        raise TypeError('Readiness probe must be explicitly callable')
    if isinstance(admin_emails, (str, bytes)):
        raise TypeError("admin_emails must be an iterable of email addresses")
    if type(config_writes) is not bool or (config_writes and config_service is None):
        raise ValueError('Configuration writes require an explicitly injected service')
    if config_diagnostics is not None and (not config_writes or config_diagnostics.service is not config_service):
        raise ValueError('Diagnostics require the same explicit configuration service and write opt-in')
    admins = frozenset(email.strip().lower() for email in admin_emails if email.strip())
    if public_auth is not None and (public_auth.admin_emails != admins or
            public_auth._session_factory is not auth_service._session_factory):
        raise ValueError('Public authentication requires identical database sessions and superadmin policy')
    if invite_service is not None and (public_auth is None or invite_service.auth is not public_auth):
        raise ValueError('Invitations require the same explicit public auth service')
    if original_service is not None and original_service.admin_emails != admins:
        raise ValueError('Original viewing requires the same explicit superadmin policy')
    if analysis_history_service is not None and analysis_history_service.admin_emails != admins:
        raise ValueError('Analysis history requires the same explicit superadmin policy')
    if announcement_service is not None and announcement_service.admin_emails != admins:
        raise ValueError('Announcements require the same explicit superadmin policy')
    if email_service is not None and email_service.admin_emails != admins:
        raise ValueError('Email notifications require the same explicit superadmin policy')
    from db_ai_service import AIService
    from db_analysis_workflow import AnalysisWorkflow
    if isinstance(ai_service, AIService) and isinstance(ai_service.workflow, AnalysisWorkflow):
        if analysis_workflow is not None and analysis_workflow is not ai_service.workflow:
            raise ValueError('AI and analysis services require the same task workflow')
        analysis_workflow = ai_service.workflow
    if analysis_workflow is not None and activity_tracker is not None and activity_tracker is not analysis_workflow.tracker:
        raise ValueError('All preview activity must use the same drain tracker')
    tracker = activity_tracker if activity_tracker is not None else (
        analysis_workflow.tracker if analysis_workflow is not None else DrainTracker())
    limiter = login_limiter if login_limiter is not None else PreviewLoginLimiter()
    app = Flask(__name__, static_folder=None)
    app.extensions['db_drain_tracker'] = tracker
    if content_service is not None or analysis_history_service is not None or announcement_service is not None:
        class ContentPreviewRequest(Request):
            @property
            def max_content_length(self):
                # Flask 3.0 exposes this property without a per-request setter.
                # Endpoint resolution precedes body parsing in the preview gate.
                if self.endpoint in {'db_review_markers', 'db_improved_markdown'}:
                    return 2 * 1024 * 1024
                if self.endpoint == 'announcement_create':
                    return 64 * 1024
                if str(self.endpoint).startswith('email_'):
                    return 64 * 1024
                if not str(self.endpoint).startswith('content_'):
                    return 16 * 1024
                return super().max_content_length
        app.request_class = ContentPreviewRequest
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024 * 1024 if content_service is not None else 16 * 1024,
                      DB_HTTP_PREVIEW=True)

    def error(detail, status):
        return jsonify(detail=detail, request_id=g.request_id), status

    def success(data):
        return jsonify(request_id=g.request_id, data=data)

    @app.before_request
    def preview_gate():
        g.request_id = "db-preview:" + str(uuid4())
        # CSRF 纵深防御：认证只用 Bearer（不落 Cookie），这里再挡一层跨站写请求。
        # 只认显式配置的 trusted_origins；不用 request.host —— Next.js rewrite 的
        # changeOrigin 会把上游 Host 改成后端地址，Host 不能作为可信来源。
        if prompt_safety.cross_origin_blocked(
            request.method, request.headers.get("Origin"), trusted_origins,
            request.headers.get("Sec-Fetch-Site"),
        ):
            return error("Cross-origin request rejected", 403)
        # Flask resolves the endpoint before this hook, including method mismatch.
        allowed = {"ping": "GET", "login": "POST", "me": "GET", "resume": "GET",
                   "job": "GET", "archives": "GET", "trash": "GET", "archive": "GET",
                   "mail_config": "GET", "llm_config": "GET"}
        if readiness_probe is not None:
            allowed['ready'] = 'GET'
        if analysis_workflow is not None:
            allowed.update(analysis='POST', improvement_task='POST', analysis_task='GET', analysis_task_events='GET')
        if analysis_history_service is not None:
            allowed.update(db_review_markers='POST', db_improved_markdown='POST',
                           db_analysis_history_list='GET', db_analysis_history_detail='GET',
                           db_admin_analysis_list='GET', db_admin_analysis_detail='GET')
        if announcement_service is not None:
            allowed.update(announcement_list='GET', announcement_create='POST', announcement_cancel='POST',
                           announcement_delete='DELETE', announcement_active='GET', announcement_dismiss='POST')
        if email_service is not None:
            allowed.update(email_preview='POST', email_image_data='GET', email_images='GET', email_image_upload='POST',
                           email_image_delete='DELETE', email_recipients='GET', email_send='POST', email_logs='GET',
                           email_log_detail='GET')
        if admin_service is not None:
            allowed.update(admin_ops='GET', admin_ops_export='GET', admin_resumes='GET', admin_resume_detail='GET',
                           admin_resume_delete='DELETE',
                           admin_user_usage='GET', admin_usage_ranking='GET')
        if ai_service is not None:
            allowed.update(ai_test='POST', improve='POST')
        if account_service is not None:
            allowed.update(account_password='POST', account_list='GET', account_create='POST',
                           account_update='PATCH', account_reset='POST', account_freeze='POST', account_unfreeze='POST',
                           account_delete='DELETE', account_restore='POST', account_frozen_list='GET', account_deleted_list='GET')
        if config_writes:
            allowed.update(mail_update='PUT', mail_clear='DELETE', llm_update='PUT', llm_clear='DELETE')
        if config_diagnostics is not None:
            allowed.update(mail_probe='POST', llm_probe='POST', llm_models='POST')
        if content_service is not None:
            allowed.update(content_upload_resume='POST', content_upload_jobs='POST', content_create_archive='POST',
                           content_tags='PATCH', content_category='PATCH', content_trash='DELETE',
                           content_restore='POST', content_purge='DELETE', content_empty='DELETE',
                           content_delete_resume='DELETE', content_delete_job='DELETE')
        if original_service is not None:
            allowed.update(original_info='GET', original_page='GET', original_content='GET', original_download='GET')
        public_endpoints = {'login'}
        if invite_service is not None:
            allowed.update(invite_submit='POST', invite_requests='GET', invite_approve='POST',
                           invite_reject='POST', invite_resend='POST', invite_codes='GET',
                           invite_generate='POST', invite_revoke='POST')
            public_endpoints.add('invite_submit')
        if quota_service is not None:
            allowed.update(quota_settings='GET', quota_update_settings='PUT',
                           quota_update_user='PUT', quota_reset_user='DELETE', quota_me='GET')
        if public_auth is not None:
            allowed.update(auth_register='POST', auth_register_config='GET', auth_invite_check='POST',
                           auth_register_code='POST', auth_email_code='POST', auth_captcha='GET',
                           auth_reset_request='POST', auth_reset_confirm='POST',
                           auth_unfreeze_request='POST', auth_unfreeze_confirm='POST')
            public_endpoints.update(key for key in allowed if key.startswith('auth_'))
        if allowed.get(request.endpoint) != request.method:
            return error("DB preview unsupported", 503)
        if request.endpoint != 'ping':
            g.preview_activity_close = tracker.enter('requests')
        if request.endpoint == 'original_page':
            # Keep the buffered image registered until WSGI closes the response,
            # including a slow client after request teardown has already run.
            g.preview_original_close = tracker.enter('streams')
        if public_auth is not None and request.endpoint in public_endpoints:
            public_auth.check_request(request.endpoint, request.remote_addr or '127.0.0.1')
        if request.endpoint in {'ping', 'ready'} or request.endpoint in public_endpoints:
            return None
        if request.endpoint in {"mail_config", "llm_config"} and config_service is None:
            return error("DB preview unsupported", 503)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return error("Authentication required", 401)
        try:
            claims = jwt.decode(header[7:], jwt_secret, algorithms=["HS256"],
                                issuer="resume-matcher", options={"require": [
                                    "user_id", "username", "pwd_ver", "iss", "jti", "iat", "exp"]})
            owner = _uuid(claims["user_id"])
            if (not isinstance(claims["username"], str) or not claims["username"]
                    or type(claims["pwd_ver"]) is not int):
                raise ValueError("Invalid claims")
        except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
            return error("Invalid or expired token", 401)
        # DB/service exceptions deliberately propagate to the generic 503 handler.
        user = auth_service.find_user_by_id(owner)
        if (not isinstance(user, InternalUser) or str(user.user_id) != owner
                or not user.can_authenticate or claims["pwd_ver"] != user.password_version):
            return error("Invalid or revoked token", 401)
        g.preview_user = user
        if request.endpoint in {'account_list', 'account_create', 'account_update', 'account_reset',
                                'account_freeze', 'account_unfreeze', 'account_frozen_list',
                                'account_delete', 'account_restore', 'account_deleted_list'}:
            if not (user.is_admin or user.email.strip().lower() in admins):
                return error('Administrator required', 403)
        if quota_service is not None and request.endpoint in {
                'quota_settings', 'quota_update_settings', 'quota_update_user', 'quota_reset_user'}:
            if not (user.is_admin or user.email.strip().lower() in admins):
                return error('Administrator required', 403)
        if announcement_service is not None and request.endpoint in {
                'announcement_list', 'announcement_create', 'announcement_cancel', 'announcement_delete'}:
            if not (user.is_admin or user.email.strip().lower() in admins):
                return error('Administrator required', 403)
        if email_service is not None and request.endpoint in {
                'email_preview', 'email_image_data', 'email_images', 'email_image_upload',
                'email_image_delete', 'email_recipients', 'email_send', 'email_logs', 'email_log_detail'}:
            if not (user.is_admin or user.email.strip().lower() in admins):
                return error('Administrator required', 403)
        if analysis_history_service is not None and request.endpoint in {
                'db_admin_analysis_list', 'db_admin_analysis_detail'}:
            if user.email.strip().lower() not in admins:
                return error('Superadministrator required', 403)
        if request.endpoint in {'mail_update', 'mail_clear', 'llm_update', 'llm_clear',
                                'account_delete', 'account_restore', 'account_deleted_list',
                                'mail_probe', 'llm_probe', 'llm_models', 'original_info', 'original_page', 'original_content',
                                'invite_requests', 'invite_approve', 'invite_reject', 'invite_resend',
                                'invite_codes', 'invite_generate', 'invite_revoke'}:
            if user.email.strip().lower() not in admins:
                return error('Superadministrator required', 403)

    @app.errorhandler(Exception)
    def unavailable(exc):
        if isinstance(exc, HTTPException) and exc.code == 413:
            return error("Request body too large", 413)
        # Never reflect exception strings, SQL, parameters, or credentials.
        return error("DB preview unavailable", 503)

    @app.after_request
    def preview_headers(response):
        original_close = getattr(g, 'preview_original_close', None)
        if original_close is not None:
            if response.status_code == 200:
                response.call_on_close(original_close)
                g.preview_original_transferred = True
            else:
                original_close()
        close = getattr(g, 'preview_activity_close', None)
        if close is not None:
            close()
        response.headers["X-DB-Preview"] = ('email-local' if email_service is not None else
                                            'admin-ai-local' if admin_service is not None or ai_service is not None else
                                            'announcements-local' if announcement_service is not None else
                                            'analysis-history-local' if analysis_history_service is not None else
                                            'content-local' if content_service is not None or original_service is not None else
                                            'auth-local' if public_auth is not None else
                                            'management-local' if account_service is not None or config_writes else
                                            'analysis-local' if analysis_workflow is not None else 'read-only-local')
        response.headers.setdefault("Cache-Control", "no-store")
        # 接口只返回 JSON：关掉脚本/框架/表单能力，并禁止被嵌入。
        for header, value in prompt_safety.API_SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    @app.teardown_request
    def release_preview_activity(exc):
        original_close = getattr(g, 'preview_original_close', None)
        if original_close is not None and not getattr(g, 'preview_original_transferred', False):
            original_close()
        close = getattr(g, 'preview_activity_close', None)
        if close is not None:
            close()

    @app.get("/ping")
    def ping():
        return jsonify(message="pong", preview=True, database="not_checked")

    @app.get('/ready')
    def ready():
        try:
            available = readiness_probe is not None and readiness_probe() is True
        except Exception:
            available = False
        return jsonify(ready=available), 200 if available else 503

    @app.post("/api/v1/auth/login")
    def login():
        retry = limiter.check(request.remote_addr or "local") if public_auth is None else 0
        if retry:
            response, status = error("Too many preview login attempts", 429)
            response.headers["Retry-After"] = str(max(1, ceil(retry)))
            return response, status
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return error("Username and password are required", 422)
        username, password = data.get("username"), data.get("password")
        if (not isinstance(username, str) or not username.strip()
                or not isinstance(password, str) or not password):
            return error("Username and password are required", 422)
        authenticated = (public_auth.login(username, password, ip=request.remote_addr or '127.0.0.1',
            request_id=g.request_id, captcha_id=data.get('captcha_id', ''), captcha_code=data.get('captcha_code', ''))
            if public_auth is not None else auth_service.authenticate_user(username.strip(), password))
        if not authenticated:
            return error("Invalid username or password", 401)
        return authenticated_response(authenticated)

    def authenticated_response(authenticated, *, message=None):
        user = auth_service.find_user_by_id(_uuid(authenticated["user_id"]))
        if (not isinstance(user, InternalUser) or not user.can_authenticate
                or str(user.user_id) != str(authenticated["user_id"])
                or user.password_version != authenticated.get("password_version")):
            return error("Invalid username or password", 401)
        now = time()
        token = jwt.encode(dict(user_id=str(user.user_id), username=user.username,
                                pwd_ver=user.password_version, iss="resume-matcher",
                                jti=str(uuid4()), iat=now, exp=now + 7 * 24 * 3600),
                           jwt_secret, algorithm="HS256")
        result = dict(user_id=str(user.user_id), username=user.username, token=token)
        if message is not None:
            result['message'] = message
        return success(result)

    @app.get("/api/v1/auth/me")
    def me():
        user = g.preview_user
        super_admin = user.email.strip().lower() in admins
        return success(dict(user_id=str(user.user_id), username=user.username, email=user.email,
                            is_admin=bool(user.is_admin or super_admin), is_super_admin=super_admin))

    def config_view(document):
        user = g.preview_user
        if user.email.strip().lower() not in admins:
            return error("Superadministrator required", 403)
        try:
            result = config_service.public_document(str(user.user_id), document,
                                                    password_version=user.password_version)
        except ConfigPermissionError:
            return error("Superadministrator required", 403)
        return success(result)

    @app.get("/api/v1/admin/system/mail")
    def mail_config():
        return config_view("mail")

    @app.get("/api/v1/admin/system/llm")
    def llm_config():
        return config_view("llm")

    def record_view(kind):
        raw_id = request.args.get(kind + "_id")
        if not raw_id:
            return error(kind + "_id is required", 422)
        try:
            record_id = _uuid(raw_id)
        except (ValueError, TypeError, AttributeError):
            return error("Invalid UUID", 400)
        getter = store.get_resume_view if kind == "resume" else store.get_job_view
        result = getter(record_id, user_id=str(g.preview_user.user_id))
        if result is None:
            return error("Record not found", 404)
        return success(result)

    @app.get("/api/v1/resumes")
    def resume():
        return record_view("resume")

    @app.get("/api/v1/jobs")
    def job():
        return record_view("job")

    @app.get("/api/v1/archives")
    def archives():
        owner = str(g.preview_user.user_id)
        args = request.args
        sort = args.get("sort", "score").strip()
        records = store.query_archives(owner, name_keyword=args.get("name", "").strip(),
                                       job_title=args.get("job_title", "").strip(),
                                       category=args.get("category", "").strip(),
                                       tag=args.get("tag", "").strip(),
                                       sort=sort if sort in {"score", "created"} else "score")
        return success(dict(archives=[_archive_payload(rec) for rec in records], meta=dict(
            job_titles=store.get_distinct_job_titles(owner),
            categories=store.get_distinct_categories(owner))))

    @app.get("/api/v1/archives/trash")
    def trash():
        records = store.list_archives(str(g.preview_user.user_id), status="trashed")
        return success(dict(archives=[_archive_payload(rec) for rec in records],
                            meta=dict(job_titles=[], categories=[])))

    @app.get("/api/v1/archives/<archive_id>")
    def archive(archive_id):
        try:
            record_id = _uuid(archive_id)
        except (ValueError, TypeError, AttributeError):
            return error("Invalid UUID", 400)
        rec = store.get_archive(record_id, user_id=str(g.preview_user.user_id))
        if rec is None:
            return error("Record not found", 404)
        if rec.get("status") != "active":
            return error("Trashed archive details are unavailable", 400)
        return success(_archive_payload(rec, include_analysis=True))

    if analysis_workflow is not None:
        from db_analysis_http import register_analysis_routes
        register_analysis_routes(app, analysis_workflow, error, success)
        app.extensions['db_analysis_workflow'] = analysis_workflow
    if analysis_history_service is not None:
        from db_analysis_history_http import register_analysis_history_routes
        register_analysis_history_routes(app, analysis_history_service, error=error, success=success)
    if account_service is not None or config_writes:
        from db_management_http import register_management_routes
        register_management_routes(app, account_service, config_service, config_writes=config_writes,
                                   error=error, success=success, config_diagnostics=config_diagnostics)
    if content_service is not None:
        from db_content_http import register_content_routes
        register_content_routes(app, content_service, error=error, success=success)
    if original_service is not None:
        from db_original_http import register_original_routes
        register_original_routes(app, original_service, error=error, success=success)
    if public_auth is not None:
        from db_public_auth_http import register_public_auth_routes
        register_public_auth_routes(app, public_auth, error=error, success=success,
                                    authenticated_response=authenticated_response)
    if invite_service is not None:
        from db_invite_http import register_invite_routes
        register_invite_routes(app, invite_service, success=success)
    if quota_service is not None:
        from db_quota_http import register_quota_routes
        register_quota_routes(app, quota_service, error=error, success=success)
    if announcement_service is not None:
        from db_announcement_http import register_announcement_routes
        register_announcement_routes(app, announcement_service, error=error, success=success)
    if email_service is not None:
        from db_email_http import register_email_routes
        register_email_routes(app, email_service, error=error, success=success)
    if admin_service is not None:
        from db_admin_http import register_admin_routes
        register_admin_routes(app, admin_service, error=error, success=success)
    if ai_service is not None:
        from db_ai_http import register_ai_routes
        register_ai_routes(app, ai_service, error=error, success=success)
    return app
