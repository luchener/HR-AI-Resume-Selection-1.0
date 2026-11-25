"""Flask-client preview contracts using fake services; no server or database."""
import ast
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import time
import unittest
from unittest.mock import Mock
from uuid import UUID, uuid4

import jwt

from db_auth import InternalUser
from db_http_preview import PreviewLoginLimiter, create_preview_app


class PreviewTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.owner = str(uuid4())
        self.record_id = str(uuid4())
        self.secret = "preview-test-secret-with-at-least-48-bytes-for-algorithm-tests"
        self.user = InternalUser(user_id=UUID(self.owner), username="alice",
                                 email="alice@example.com", password_hash="PRIVATE_HASH",
                                 password_salt="PRIVATE_SALT", password_version=3,
                                 is_admin=False, is_super_admin=False, status="active",
                                 deleted_at=None, created_at=now, updated_at=now)
        self.auth = Mock(spec=["authenticate_user", "find_user_by_id"])
        self.auth.authenticate_user.return_value = self.user.to_public()
        self.auth.find_user_by_id.return_value = self.user
        self.store = Mock(spec=["get_resume_view", "get_job_view", "query_archives",
                               "get_distinct_job_titles", "get_distinct_categories",
                               "list_archives", "get_archive"])
        self.store.get_resume_view.return_value = {"resume_id": self.record_id,
            "raw_resume": {"content": "resume"}, "processed_resume": {}}
        self.store.get_job_view.return_value = {"job_id": self.record_id,
            "raw_job": {"content": "job"}, "processed_job": {}}
        self.archive = dict(archive_id=self.record_id, candidate_name="Alice", status="active",
                            analysis={"summary": "report"}, user_id=self.owner,
                            private_internal="do not expose")
        self.store.query_archives.return_value = [self.archive]
        self.store.list_archives.return_value = [dict(self.archive, status="trashed")]
        self.store.get_archive.return_value = self.archive
        self.store.get_distinct_job_titles.return_value = ["Engineer"]
        self.store.get_distinct_categories.return_value = ["Engineering"]
        self.app = create_preview_app(self.store, self.auth, self.secret)
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + self.token()}

    def token(self, **overrides):
        now = time.time()
        claims = dict(user_id=self.owner, username="alice", pwd_ver=3,
                      iss="resume-matcher", jti=str(uuid4()), iat=now, exp=now + 3600)
        claims.update(overrides)
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def test_original_policy_and_analysis_tracker_must_match_explicit_composition(self):
        from db_drain import DrainTracker
        service = Mock(admin_emails=frozenset({'owner@example.test'}))
        with self.assertRaises(ValueError):
            create_preview_app(self.store, self.auth, self.secret, original_service=service)
        with self.assertRaises(ValueError):
            create_preview_app(self.store, self.auth, self.secret, analysis_workflow=Mock(tracker=DrainTracker()),
                               activity_tracker=DrainTracker())

    def test_docx_content_route_requires_superadmin_and_passes_audit_context(self):
        service = Mock(admin_emails=frozenset({'owner@example.test'}))
        service.preview_content.return_value = {'html': '<p>Fixture</p>', 'ext': 'docx', 'name': 'fixture.docx'}
        client = create_preview_app(self.store, self.auth, self.secret, admin_emails=service.admin_emails,
                                    original_service=service).test_client()
        path = f'/api/v1/admin/resumes/{self.record_id}/original/content'
        self.assertEqual(client.get(path).status_code, 401)
        self.assertEqual(client.get(path, headers=self.headers).status_code, 403)
        service.preview_content.assert_not_called()
        self.auth.find_user_by_id.return_value = replace(self.user, email='owner@example.test')
        response = client.get(path, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data'], service.preview_content.return_value)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        service.preview_content.assert_called_once_with(self.owner, self.record_id, password_version=3,
                                                       request_id=response.json['request_id'])

    def test_drain_rejects_login_and_reads_without_leaking_activity(self):
        tracker = self.app.extensions['db_drain_tracker']
        tracker.begin()
        self.assertEqual(self.client.get('/api/v1/auth/me', headers=self.headers).status_code, 503)
        self.assertEqual(self.client.post('/api/v1/auth/login', json={}).status_code, 503)
        self.assertEqual(self.client.get('/ping').status_code, 200)
        self.assertTrue(tracker.wait(0))

    def test_login_shape_claims_and_no_credentials(self):
        response = self.client.post("/api/v1/auth/login", json={"username": " alice ",
                                    "password": " password 9 ", "is_admin": True})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(set(payload), {"request_id", "data"})
        self.assertEqual(set(payload["data"]), {"user_id", "username", "token"})
        self.auth.authenticate_user.assert_called_once_with("alice", " password 9 ")
        self.auth.find_user_by_id.assert_called_once_with(self.owner)
        claims = jwt.decode(payload["data"]["token"], self.secret, algorithms=["HS256"],
                            issuer="resume-matcher")
        self.assertEqual(set(claims), {"user_id", "username", "pwd_ver", "iss", "jti", "iat", "exp"})
        self.assertEqual(claims["pwd_ver"], 3)
        self.assertAlmostEqual(claims["exp"] - claims["iat"], 7 * 24 * 3600)
        self.assertNotIn("PRIVATE", response.get_data(as_text=True))
        self.assertEqual(self.store.mock_calls, [])

    def test_content_opt_in_supports_existing_flask_and_preserves_login_body_limit(self):
        client = create_preview_app(self.store, self.auth, self.secret, content_service=Mock()).test_client()
        response = client.post('/api/v1/auth/login', json={'username':'alice', 'password':'password 9'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-DB-Preview'], 'content-local')
        self.assertEqual(client.post('/api/v1/auth/login', json={'username':'alice',
            'password':'x' * 17000}).status_code, 413)

    def test_me_uses_authoritative_allowlisted_data_and_explicit_admin_policy(self):
        self.auth.find_user_by_id.return_value = replace(self.user, username="renamed", is_super_admin=True)
        response = self.client.get("/api/v1/auth/me", headers=self.headers)
        self.assertEqual(response.get_json()["data"], dict(user_id=self.owner, username="renamed",
            email="alice@example.com", is_admin=False, is_super_admin=False))
        client = create_preview_app(self.store, self.auth, self.secret,
                                    admin_emails=[" ALICE@example.com "]).test_client()
        self.assertTrue(client.get("/api/v1/auth/me", headers=self.headers).get_json()["data"]["is_super_admin"])

    def test_all_reads_propagate_owner_and_preserve_shapes(self):
        for kind in ("resume", "job"):
            with self.subTest(kind=kind):
                response = self.client.get(f"/api/v1/{kind}s?{kind}_id={self.record_id}&user_id={uuid4()}",
                                           headers=self.headers)
                self.assertEqual(response.status_code, 200)
                method = getattr(self.store, f"get_{kind}_view")
                method.assert_called_once_with(self.record_id, user_id=self.owner)
                self.assertEqual(response.get_json()["data"], method.return_value)
        response = self.client.get("/api/v1/archives?name=Al&job_title=Engineer&tag=x&category=Eng&sort=created",
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.store.query_archives.assert_called_once_with(self.owner, name_keyword="Al", job_title="Engineer",
                                                          tag="x", category="Eng", sort="created")
        self.store.get_distinct_job_titles.assert_called_once_with(self.owner)
        self.store.get_distinct_categories.assert_called_once_with(self.owner)
        data = response.get_json()["data"]
        self.assertEqual(data["meta"], {"job_titles": ["Engineer"], "categories": ["Engineering"]})
        self.assertNotIn("analysis", data["archives"][0])
        self.assertNotIn("private_internal", data["archives"][0])
        response = self.client.get("/api/v1/archives/trash", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.store.list_archives.assert_called_once_with(self.owner, status="trashed")
        self.assertEqual(response.get_json()["data"]["meta"], {"job_titles": [], "categories": []})
        response = self.client.get("/api/v1/archives/" + self.record_id, headers=self.headers)
        self.store.get_archive.assert_called_once_with(self.record_id, user_id=self.owner)
        self.assertEqual(response.get_json()["data"]["analysis"], {"summary": "report"})
        self.assertEqual(self.auth.find_user_by_id.call_count, 5)

    def test_revocation_on_each_request_before_store(self):
        path = "/api/v1/resumes?resume_id=" + self.record_id
        self.assertEqual(self.client.get(path, headers=self.headers).status_code, 200)
        self.store.reset_mock()
        for user in (None, replace(self.user, password_version=4), replace(self.user, status="locked"),
                     replace(self.user, status="pending"), replace(self.user, deleted_at=self.user.created_at),
                     replace(self.user, user_id=uuid4()), self.user.to_public()):
            with self.subTest(user=repr(user)):
                self.auth.find_user_by_id.return_value = user
                self.assertEqual(self.client.get(path, headers=self.headers).status_code, 401)
        self.assertEqual(self.store.mock_calls, [])

    def test_invalid_tokens_never_reach_services(self):
        tokens = ["bad-token", self.token(exp=time.time() - 10), self.token(iss="other"),
                  self.token(user_id="invalid-uuid"), self.token(pwd_ver=True),
                  self.token(pwd_ver="3"), self.token(username="")]
        missing = jwt.decode(self.token(), self.secret, algorithms=["HS256"], issuer="resume-matcher")
        del missing["pwd_ver"]
        tokens.extend([jwt.encode(missing, self.secret, algorithm="HS256"),
                       jwt.encode(missing, self.secret, algorithm="HS384")])
        for token in tokens:
            response = self.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(self.auth.mock_calls, [])
        self.assertEqual(self.store.mock_calls, [])

    def test_invalid_record_uuid_is_400_and_missing_query_is_422(self):
        paths = ["/api/v1/resumes?resume_id=bad", "/api/v1/jobs?job_id=bad",
                 "/api/v1/archives/not-a-uuid", "/api/v1/archives/" + str(UUID(int=0))]
        for path in paths:
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/resumes", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers).status_code, 422)
        self.assertEqual(self.store.mock_calls, [])

    def test_not_found_and_trashed_detail(self):
        for kind in ("resume", "job"):
            getattr(self.store, f"get_{kind}_view").return_value = None
            self.assertEqual(self.client.get(f"/api/v1/{kind}s?{kind}_id={self.record_id}",
                                             headers=self.headers).status_code, 404)
        self.store.get_archive.return_value = None
        self.assertEqual(self.client.get("/api/v1/archives/" + self.record_id, headers=self.headers).status_code, 404)
        self.store.get_archive.return_value = dict(self.archive, status="trashed")
        self.assertEqual(self.client.get("/api/v1/archives/" + self.record_id, headers=self.headers).status_code, 400)

    def test_unsupported_routes_and_methods_short_circuit_without_any_service_access(self):
        paths = ["/", "/unknown", "/api/v1/auth/register", "/api/v1/admin/users", "/api/v1/resumes/upload",
                 "/api/v1/resumes/review-markers", "/api/v1/archives/" + self.record_id + "/restore"]
        for path in paths:
            for method in ("GET", "POST", "PATCH", "DELETE", "OPTIONS", "HEAD"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.client.open(path, method=method).status_code, 503)
        for path in ("/ping", "/api/v1/auth/me", "/api/v1/resumes", "/api/v1/jobs",
                     "/api/v1/archives", "/api/v1/archives/trash", "/api/v1/archives/" + self.record_id):
            for method in ("POST", "PATCH", "DELETE", "PUT", "OPTIONS", "HEAD"):
                self.assertEqual(self.client.open(path, method=method).status_code, 503)
        self.assertEqual(self.client.get("/api/v1/auth/login").status_code, 503)
        self.assertEqual(self.auth.mock_calls, [])
        self.assertEqual(self.store.mock_calls, [])

    def test_generic_database_errors_and_no_fallback(self):
        secret = "postgres://user:password@host/private SQL bound-password"
        for method in (self.store.get_resume_view, self.store.get_job_view, self.store.query_archives,
                       self.store.list_archives, self.store.get_archive):
            method.side_effect = RuntimeError(secret)
        for path in ("/api/v1/resumes?resume_id=" + self.record_id,
                     "/api/v1/jobs?job_id=" + self.record_id, "/api/v1/archives",
                     "/api/v1/archives/trash", "/api/v1/archives/" + self.record_id):
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["detail"], "DB preview unavailable")
            self.assertNotIn(secret, response.get_data(as_text=True))
        self.auth.find_user_by_id.side_effect = RuntimeError(secret)
        response = self.client.get("/api/v1/auth/me", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.auth.authenticate_user.side_effect = RuntimeError(secret)
        response = self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "p"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_bad_login_and_authoritative_race_rejection(self):
        for data in (None, [], {"username": []}, {"username": "alice", "password": 123}):
            self.assertEqual(self.client.post("/api/v1/auth/login", json=data).status_code, 422)
        self.assertEqual(self.auth.mock_calls, [])
        self.auth.authenticate_user.return_value = None
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"}).status_code, 401)
        self.auth.authenticate_user.return_value = self.user.to_public()
        for user in (None, replace(self.user, status="locked"), replace(self.user, password_version=4)):
            self.auth.find_user_by_id.return_value = user
            self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "p"}).status_code, 401)

    def test_injected_limiter_and_no_forwarded_ip_trust(self):
        limiter = Mock()
        limiter.check.return_value = 5
        client = create_preview_app(self.store, self.auth, self.secret, login_limiter=limiter).test_client()
        response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "p"},
                               headers={"X-Forwarded-For": "attacker-controlled"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "5")
        limiter.check.assert_called_once_with("127.0.0.1")
        self.assertEqual(self.auth.mock_calls, [])

    def test_default_limiter_limits_attempts(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/api/v1/auth/login", json={}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={}).status_code, 429)

    def test_limiter_bounded_capacity_and_expiry(self):
        now = [0]
        limiter = PreviewLoginLimiter(attempts=2, window_seconds=10, max_keys=2, clock=lambda: now[0])
        self.assertEqual(limiter.check("a"), 0)
        self.assertEqual(limiter.check("a"), 0)
        self.assertEqual(limiter.check("a"), 10)
        self.assertEqual(limiter.check("b"), 0)
        self.assertEqual(limiter.check("new"), 10)
        self.assertEqual(len(limiter._entries), 2)
        now[0] = 10
        self.assertEqual(limiter.check("new"), 0)
        self.assertEqual(len(limiter._entries), 1)

    def test_trusted_origins_fail_closed_without_configuration(self):
        """没有显式可信源时，带 Origin 的写请求必须被拒绝（不能退回信任 Host）。"""
        response = self.client.post('/api/v1/auth/login', json={"username": "a", "password": "b"},
                                    headers={"Origin": "http://127.0.0.1:6888"})
        self.assertEqual(response.status_code, 403)

    def test_trusted_origin_passes_even_when_upstream_host_is_rewritten(self):
        """Next.js rewrite 会把上游 Host 改成后端地址；判定只看显式可信源，不看 Host。"""
        app = create_preview_app(self.store, self.auth, self.secret,
                                 trusted_origins=["http://127.0.0.1:6888"])
        client = app.test_client()
        response = client.post('/api/v1/auth/login', json={"username": "a", "password": "b"},
                               headers={"Origin": "http://127.0.0.1:6888", "Host": "backend:8000"},
                               environ_base={"REMOTE_ADDR": "127.0.0.2"})
        self.assertNotEqual(response.status_code, 403)

    def test_untrusted_lookalike_and_null_origins_are_rejected(self):
        app = create_preview_app(self.store, self.auth, self.secret,
                                 trusted_origins=["http://127.0.0.1:6888"])
        client = app.test_client()
        for origin in ("http://127.0.0.1:6889", "http://127.0.0.1:6888.evil.example", "null"):
            response = client.post('/api/v1/auth/login', json={"username": "a", "password": "b"},
                                   headers={"Origin": origin}, environ_base={"REMOTE_ADDR": "127.0.0.2"})
            self.assertEqual(response.status_code, 403, origin)

    def test_no_origin_client_is_allowed(self):
        """CLI/服务间调用不带 Origin，保持原有行为。"""
        app = create_preview_app(self.store, self.auth, self.secret,
                                 trusted_origins=["http://127.0.0.1:6888"])
        client = app.test_client()
        response = client.post('/api/v1/auth/login', json={"username": "a", "password": "b"})
        self.assertNotEqual(response.status_code, 403)

    def test_invalid_trusted_origin_configuration_is_rejected(self):
        for value in ("http://127.0.0.1:6888", "null", "https://a.example/path"):
            with self.assertRaises((TypeError, ValueError)):
                create_preview_app(self.store, self.auth, self.secret, trusted_origins=value)
        with self.assertRaises(ValueError):
            create_preview_app(self.store, self.auth, self.secret, trusted_origins=["https://a.example/x"])

    def test_preview_ping_no_cors_or_database_access(self):
        response = self.client.get("/ping", headers={"Origin": "https://external.example"})
        self.assertEqual(response.get_json(), {"message": "pong", "preview": True, "database": "not_checked"})
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-DB-Preview"], "read-only-local")
        self.assertEqual(self.auth.mock_calls, [])
        self.assertEqual(self.store.mock_calls, [])

    def test_config_routes_without_service_fail_closed(self):
        for document in ('mail','llm'):
            self.assertEqual(self.client.get('/api/v1/admin/system/'+document,headers=self.headers).status_code,503)
        self.auth.assert_not_called()
        self.assertEqual(self.auth.mock_calls,[])

    def test_config_reads_authorize_and_forward_authoritative_identity(self):
        service = Mock(spec=['public_document'])
        service.public_document.return_value = {'configured':False,'source':'none'}
        client = create_preview_app(self.store,self.auth,self.secret,
                                    admin_emails=['ALICE@example.com'],config_service=service).test_client()
        for document in ('mail','llm'):
            response = client.get('/api/v1/admin/system/'+document,headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.get_json()['data'],{'configured':False,'source':'none'})
            service.public_document.assert_called_with(self.owner,document,password_version=3)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.store.mock_calls,[])

    def test_config_denials_and_failures_never_expose_details(self):
        from db_config_service import ConfigPermissionError
        service = Mock(spec=['public_document'])
        client = create_preview_app(self.store,self.auth,self.secret,config_service=service).test_client()
        self.auth.find_user_by_id.return_value = replace(self.user,is_admin=True,is_super_admin=True)
        self.assertEqual(client.get('/api/v1/admin/system/mail',headers=self.headers).status_code,403)
        service.public_document.assert_not_called()
        client = create_preview_app(self.store,self.auth,self.secret,
                                    admin_emails=[self.user.email],config_service=service).test_client()
        for exception,status in ((ConfigPermissionError('PRIVATE'),403),(RuntimeError('PRIVATE'),503)):
            service.public_document.side_effect = exception
            response = client.get('/api/v1/admin/system/llm',headers=self.headers)
            self.assertEqual(response.status_code,status)
            self.assertNotIn('PRIVATE',response.get_data(as_text=True))

    def test_composed_config_service_masks_and_rechecks_before_decrypting(self):
        from unittest.mock import MagicMock
        from cryptography.fernet import Fernet
        from db_config_service import ConfigService
        from db_secrets import ConfigCipher
        session = MagicMock()
        session.__enter__.return_value = session
        session.in_transaction.return_value = True
        row = dict(email=self.user.email,status='active',deleted_at=None,password_version=3)
        actor_result = Mock()
        actor_result.mappings.return_value.one_or_none.return_value = row
        cipher = ConfigCipher(Fernet.generate_key(),key_id='http-test')
        encrypted = Mock()
        encrypted.scalar_one_or_none.return_value = cipher.encrypt({
            'system_config/llm.json':{'api_key':'PRIVATE_KEY','base_url':'https://api.example.test','model':'m','unknown':'PRIVATE_UNKNOWN'},
            'system_config/mail.json':{'slots':{'transactional':{'password':'PRIVATE_PASSWORD','host':'smtp.test','username':'sender'}}}})
        service = ConfigService(lambda:session,cipher,validator=lambda d,p:p,admin_emails=[self.user.email])
        client = create_preview_app(self.store,self.auth,self.secret,
                                    admin_emails=[self.user.email],config_service=service).test_client()
        for document in ('mail','llm'):
            session.execute.side_effect = [actor_result,encrypted]
            response = client.get('/api/v1/admin/system/'+document,headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('PRIVATE',response.get_data(as_text=True))
            data = response.get_json()['data']
            self.assertTrue(data['api_key_set'] if document=='llm' else data['slots']['transactional']['password_set'])
        decrypt = Mock(wraps=cipher.decrypt)
        cipher.decrypt = decrypt
        for change in ({'email':'ordinary@test'},{'status':'locked'},
                       {'password_version':4},{'deleted_at':self.user.created_at}):
            actor_result.mappings.return_value.one_or_none.return_value = dict(row,**change)
            session.execute.side_effect = [actor_result]
            self.assertEqual(client.get('/api/v1/admin/system/llm',headers=self.headers).status_code,403)
        decrypt.assert_not_called()

    def test_config_write_methods_and_anonymous_reads_are_closed(self):
        service = Mock(spec=['public_document'])
        client = create_preview_app(self.store,self.auth,self.secret,
                                    admin_emails=[self.user.email],config_service=service).test_client()
        for document in ('mail','llm'):
            path = '/api/v1/admin/system/'+document
            self.assertEqual(client.get(path).status_code,401)
            for method in ('PUT','POST','PATCH','DELETE','HEAD','OPTIONS'):
                self.assertEqual(client.open(path,method=method,headers=self.headers).status_code,503)
        service.public_document.assert_not_called()
        self.assertEqual(self.auth.mock_calls,[])

    def test_module_has_no_legacy_imports_or_file_fallback(self):
        source = Path(__file__).with_name("db_http_preview.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module)
        self.assertFalse(set(imports) & {"app", "auth", "store", "config", "json"})
        self.assertNotIn("open(", source)
        with self.assertRaises(ValueError):
            create_preview_app(self.store, self.auth, "")


if __name__ == "__main__":
    unittest.main()
