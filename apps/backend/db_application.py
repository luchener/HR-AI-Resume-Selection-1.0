"""Explicit SQL application composition and process lifecycle.

No environment/config reads, connections, threads or migrations at import time.
The operator supplies settings, persistent object storage and provider adapters.
The executable entry serves only loopback for pre-cutover rehearsals.
"""
from dataclasses import dataclass, field, fields
import math
from threading import Lock

from db_runtime import build_runtime, verify_runtime


@dataclass(frozen=True)
class ApplicationSettings:
    database_url: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    auth_secret: bytes = field(repr=False)
    config_key: bytes = field(repr=False)
    invite_key: bytes = field(repr=False)
    admin_emails: tuple = ()
    config_key_id: str = 'sql-application-v1'
    captcha_enabled: bool = True
    max_workers: int = 2
    max_pending: int = 8
    trusted_origins: tuple = ()

    def __post_init__(self):
        if not isinstance(self.jwt_secret, str) or len(self.jwt_secret.encode()) < 32:
            raise ValueError('An explicit JWT secret of at least 32 bytes is required')
        if type(self.auth_secret) is not bytes or len(self.auth_secret) < 32:
            raise ValueError('An explicit auth HMAC secret of at least 32 bytes is required')
        if isinstance(self.admin_emails, (str, bytes)):
            raise ValueError('Administrator emails must be an explicit collection')
        admins = tuple(self.admin_emails)
        if any(not isinstance(email, str) or not email.strip() for email in admins):
            raise ValueError('Invalid administrator email policy')
        object.__setattr__(self, 'admin_emails', admins)
        if type(self.captcha_enabled) is not bool:
            raise ValueError('captcha_enabled must be boolean')
        if (type(self.max_workers) is not int or type(self.max_pending) is not int
                or not 1 <= self.max_workers <= self.max_pending <= 32):
            raise ValueError('Require 1 <= max_workers <= max_pending <= 32')
        if isinstance(self.trusted_origins, (str, bytes)):
            raise ValueError('Trusted origins must be an explicit collection')
        from prompt_safety import normalize_origin
        origins = tuple(self.trusted_origins)
        normalized = []
        for entry in origins:
            origin = normalize_origin(entry)
            if origin is None:
                raise ValueError('Invalid trusted origin in settings')
            normalized.append(origin)
        object.__setattr__(self, 'trusted_origins', tuple(dict.fromkeys(normalized)))


@dataclass(frozen=True, repr=False)
class ApplicationAdapters:
    extract_text: object
    summarize_job: object
    analyze: object
    send_auth_mail: object
    send_notification: object
    config_validator: object
    config_merger: object
    validate_base_url: object
    llm_probe: object
    models_probe: object
    mail_probe: object
    model_tester: object
    improver: object

    def __post_init__(self):
        if any(not callable(getattr(self, item.name)) for item in fields(self)):
            raise ValueError('Every application adapter must be explicitly callable')


class SQLApplication:
    """Own one process's engine, HTTP app, analysis executor and original GC.

    Construct after process fork, never in a preloaded WSGI master. close() is
    retryable if an admitted HTTP response or worker is still active. A closed
    process may leave durable work; its report must not certify that as drained.
    """

    def __init__(self, app, engine, sessions, workflow, gc_worker):
        self.app, self.engine, self.sessions = app, engine, sessions
        self.workflow, self.gc_worker = workflow, gc_worker
        self._lock = Lock()
        self._closed_report = None

    def readiness(self):
        with self._lock:
            if self._closed_report is not None or self.workflow.tracker.status()['draining']:
                return False
            try:
                return bool(verify_runtime(self.engine))
            except Exception:
                return False

    def close(self, timeout=10):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError('Close timeout must be between zero and 60 seconds')
        with self._lock:
            if self._closed_report is not None:
                return dict(self._closed_report)
            from time import monotonic
            deadline = monotonic() + timeout
            self.workflow.tracker.begin()
            stopped = self.gc_worker.stop(timeout=timeout)
            idle = self.workflow.tracker.wait(max(0, deadline - monotonic()))
            if not stopped or not idle:
                return {'closed': False, 'complete': False}
            if not self.workflow.close():
                return {'closed': False, 'complete': False}
            try:
                analysis = self.workflow.drain_report()
                originals = self.gc_worker.report()
                report = {'closed': True, 'complete': analysis['complete'] and originals['complete'],
                          'analysis': analysis, 'originals': originals}
            except Exception:
                report = {'closed': True, 'complete': False, 'database_available': False}
            finally:
                self.engine.dispose()
            self._closed_report = report
            return dict(report)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def create_application(settings, adapters, originals, *, environment=None):
    """Compose all SQL HTTP services; probe before starting owned workers.

    environment is an explicit mail/LLM fallback mapping, never os.environ.
    originals must already have its persistent storage marker; no auto-init.
    Adapter callbacks own provider timeouts, validation and outbound policy.
    """
    if (not isinstance(settings, ApplicationSettings)
            or not (isinstance(adapters, ApplicationAdapters) or callable(adapters))):
        raise TypeError('Explicit application settings and adapters are required')
    from db_original_objects import OriginalObjects
    if not isinstance(originals, OriginalObjects):
        raise TypeError('An explicitly mounted OriginalObjects store is required')
    # Validate keys/fallback before any connection or worker starts.
    from db_secrets import ConfigCipher
    from db_invite_cipher import InviteCipher
    from db_config_resolved import environment_snapshot
    cipher = ConfigCipher(settings.config_key, key_id=settings.config_key_id)
    invite_cipher = InviteCipher(settings.invite_key)
    fallback = environment_snapshot(environment)
    engine, sessions = build_runtime(settings.database_url)
    workflow = gc_worker = None
    try:
        verify_runtime(engine)
        if not isinstance(adapters, ApplicationAdapters):
            adapters = adapters(sessions, cipher, fallback)
            if not isinstance(adapters, ApplicationAdapters):
                raise TypeError('Adapter factory must return ApplicationAdapters')
        from db_accounts import AccountService
        from db_analysis_workflow import AnalysisWorkflow
        from db_analysis_history import AnalysisHistoryService
        from db_announcements import AnnouncementService
        from db_auth import AuthService
        from db_config_service import ConfigService
        from db_config_diagnostics import ConfigDiagnostics
        from db_content_service import ContentService
        from db_drain import DrainTracker
        from db_email_notifications import EmailNotificationService
        from db_http_preview import create_preview_app
        from db_invites import InviteService
        from db_original_view import OriginalViewService
        from db_original_worker import OriginalGCWorker
        from db_public_auth import PublicAuthService
        from db_quota_service import QuotaService
        from db_admin_service import AdminService
        from db_ai_service import AIService
        from db_store import Store

        policy = dict(admin_emails=settings.admin_emails)
        tracker = DrainTracker()
        auth = AuthService(sessions, **policy)
        public = PublicAuthService(sessions, secret=settings.auth_secret,
            send_mail=adapters.send_auth_mail, captcha_enabled=settings.captcha_enabled, **policy)
        config = ConfigService(sessions, cipher, validator=adapters.config_validator,
            merger=adapters.config_merger, environment=fallback, **policy)
        diagnostics = ConfigDiagnostics(config, validate_base_url=adapters.validate_base_url,
            llm_probe=adapters.llm_probe, models_probe=adapters.models_probe, mail_probe=adapters.mail_probe)
        content = ContentService(sessions, originals, extract_text=adapters.extract_text,
            summarize_job=adapters.summarize_job)
        workflow = AnalysisWorkflow(sessions, adapters.analyze, tracker=tracker,
            improver=adapters.improver,
            max_workers=settings.max_workers, max_pending=settings.max_pending, **policy)
        app = create_preview_app(Store(session_factory=sessions), auth, settings.jwt_secret,
            trusted_origins=settings.trusted_origins,
            activity_tracker=tracker, public_auth=public,
            invite_service=InviteService(public, cipher=invite_cipher, send_mail=adapters.send_auth_mail),
            config_service=config, config_writes=True, config_diagnostics=diagnostics,
            analysis_workflow=workflow, account_service=AccountService(sessions, **policy),
            content_service=content, original_service=OriginalViewService(sessions, originals, **policy),
            quota_service=QuotaService(sessions, **policy),
            analysis_history_service=AnalysisHistoryService(sessions, **policy),
            announcement_service=AnnouncementService(sessions, **policy),
            email_service=EmailNotificationService(sessions, send_email=adapters.send_notification, **policy),
            admin_service=AdminService(sessions, original_storage_id=originals.storage_id, **policy),
            ai_service=AIService(sessions, model_tester=adapters.model_tester,
                improver=adapters.improver, workflow=workflow),
            readiness_probe=lambda: application.readiness(), **policy)
        gc_worker = OriginalGCWorker(content, tracker)
        application = SQLApplication(app, engine, sessions, workflow, gc_worker)
        app.extensions['db_application'] = application
        gc_worker.start()
        return application
    except BaseException:
        if gc_worker is not None:
            gc_worker.stop()
        if workflow is not None:
            workflow.close()
        engine.dispose()
        raise


def main(argv=None):
    import argparse
    import importlib
    import signal
    from werkzeug.serving import make_server
    parser = argparse.ArgumentParser(description='Serve an explicit SQL application on loopback for rehearsal')
    parser.add_argument('--factory', required=True, help='Trusted Python module:callable returning SQLApplication')
    parser.add_argument('--port', type=int, default=8010)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.factory.count(':') != 1:
        parser.error('Require a valid port and module:callable factory')
    application = None
    server = None
    complete = False
    previous_signal = None
    try:
        module, name = args.factory.split(':')
        application = getattr(importlib.import_module(module), name)()
        if not isinstance(application, SQLApplication):
            raise TypeError('Factory must return SQLApplication')
        def stop(signum, frame):
            raise KeyboardInterrupt
        previous_signal = signal.signal(signal.SIGTERM, stop)
        server = make_server('127.0.0.1', args.port, application.app, threaded=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        # Configuration/DB/provider exceptions may contain credentials.
        print('SQL application startup or serving failed')
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)
        if server is not None:
            server.server_close()
        if isinstance(application, SQLApplication):
            complete = application.close()['complete']
    return 0 if complete else 2


if __name__ == '__main__':
    raise SystemExit(main())
