-- Generated once from migrations/versions/0001_initial_schema.py.
-- Reference only: use Alembic for schema changes; do not hand-edit this snapshot.
-- No database connection was used to generate this PostgreSQL DDL.

CREATE TABLE legacy_records (
	id BIGSERIAL NOT NULL,
	root_name VARCHAR(128) NOT NULL,
	record_key VARCHAR(1024) NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	imported_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_legacy_records_root_key UNIQUE (root_name, record_key)
);
CREATE INDEX ix_legacy_records_root ON legacy_records (root_name);

CREATE TABLE users (
	id UUID NOT NULL,
	username VARCHAR(64) NOT NULL,
	email VARCHAR(320),
	password_hash TEXT NOT NULL,
	password_salt TEXT NOT NULL,
	password_version INTEGER DEFAULT '1' NOT NULL,
	is_admin BOOLEAN DEFAULT false NOT NULL,
	is_super_admin BOOLEAN DEFAULT false NOT NULL,
	status VARCHAR(16) DEFAULT 'active' NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_login_at TIMESTAMP WITH TIME ZONE,
	legacy_payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_users_password_version CHECK (password_version > 0),
	CONSTRAINT ck_users_status CHECK (status IN ('active', 'locked', 'deleted', 'pending'))
);
CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email)) WHERE email IS NOT NULL AND email <> '';
CREATE UNIQUE INDEX uq_users_username_lower ON users (lower(username));

CREATE TABLE audit_logs (
	id BIGSERIAL NOT NULL,
	actor_user_id UUID,
	action VARCHAR(128) NOT NULL,
	target_type VARCHAR(64),
	target_id VARCHAR(128),
	request_id VARCHAR(128),
	ip_address INET,
	user_agent TEXT,
	metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_audit_logs_actor_created ON audit_logs (actor_user_id, created_at);

CREATE TABLE quotas (
	id BIGSERIAL NOT NULL,
	user_id UUID NOT NULL,
	quota_date DATE NOT NULL,
	daily_limit INTEGER NOT NULL,
	used_count INTEGER DEFAULT '0' NOT NULL,
	analysis_enabled BOOLEAN DEFAULT true NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_quotas_nonnegative CHECK (daily_limit >= 0 AND used_count >= 0),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	CONSTRAINT uq_quotas_user_date UNIQUE (user_id, quota_date)
);

CREATE TABLE resumes (
	id UUID NOT NULL,
	owner_user_id UUID NOT NULL,
	candidate_name VARCHAR(256),
	source_filename VARCHAR(512),
	content_type VARCHAR(32) DEFAULT 'md' NOT NULL,
	content_sha256 VARCHAR(64),
	object_key TEXT,
	raw_content TEXT DEFAULT '' NOT NULL,
	legacy_payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	processed_data JSONB DEFAULT '{}'::jsonb NOT NULL,
	parse_status VARCHAR(16) DEFAULT 'parsed' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	retention_until TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_resumes_parse_status CHECK (parse_status IN ('pending', 'parsed', 'failed')),
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
	CONSTRAINT uq_resumes_id_owner UNIQUE (id, owner_user_id)
);
CREATE INDEX ix_resumes_content_sha256 ON resumes (content_sha256);
CREATE INDEX ix_resumes_owner_created ON resumes (owner_user_id, created_at);

CREATE TABLE system_configs (
	key VARCHAR(128) NOT NULL,
	value JSONB DEFAULT '{}'::jsonb NOT NULL,
	updated_by UUID,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (key),
	FOREIGN KEY(updated_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE jobs (
	id UUID NOT NULL,
	owner_user_id UUID NOT NULL,
	source_resume_id UUID,
	title VARCHAR(512),
	company_name VARCHAR(512),
	raw_content TEXT DEFAULT '' NOT NULL,
	legacy_payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	parsed_requirements JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(source_resume_id) REFERENCES resumes (id) ON DELETE SET NULL,
	CONSTRAINT fk_jobs_source_resume_owner FOREIGN KEY(source_resume_id, owner_user_id) REFERENCES resumes (id, owner_user_id),
	CONSTRAINT uq_jobs_id_owner UNIQUE (id, owner_user_id)
);
CREATE INDEX ix_jobs_owner_created ON jobs (owner_user_id, created_at);

CREATE TABLE analysis_tasks (
	id UUID NOT NULL,
	owner_user_id UUID NOT NULL,
	job_id UUID,
	status VARCHAR(24) DEFAULT 'pending' NOT NULL,
	total_candidates INTEGER DEFAULT '0' NOT NULL,
	completed_candidates INTEGER DEFAULT '0' NOT NULL,
	failed_candidates INTEGER DEFAULT '0' NOT NULL,
	request_id VARCHAR(128),
	error_code VARCHAR(64),
	error_message TEXT,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_analysis_tasks_counts CHECK (total_candidates >= 0 AND completed_candidates >= 0 AND failed_candidates >= 0 AND completed_candidates + failed_candidates <= total_candidates),
	CONSTRAINT ck_analysis_tasks_status CHECK (status IN ('pending', 'running', 'partial_success', 'completed', 'failed', 'cancelled')),
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE SET NULL,
	CONSTRAINT fk_analysis_tasks_job_owner FOREIGN KEY(job_id, owner_user_id) REFERENCES jobs (id, owner_user_id),
	CONSTRAINT uq_analysis_tasks_id_owner UNIQUE (id, owner_user_id)
);
CREATE INDEX ix_analysis_tasks_owner_created ON analysis_tasks (owner_user_id, created_at);

CREATE TABLE analyses (
	id UUID NOT NULL,
	task_id UUID,
	resume_id UUID NOT NULL,
	job_id UUID NOT NULL,
	owner_user_id UUID NOT NULL,
	status VARCHAR(24) DEFAULT 'completed' NOT NULL,
	final_score NUMERIC(5, 2),
	fit_grade VARCHAR(32),
	fit_tag VARCHAR(64),
	recommendation VARCHAR(64),
	summary TEXT,
	score_breakdown JSONB DEFAULT '{}'::jsonb NOT NULL,
	scoring_meta JSONB DEFAULT '{}'::jsonb NOT NULL,
	evidence_assessments JSONB DEFAULT '[]'::jsonb NOT NULL,
	agent_validation JSONB DEFAULT '{}'::jsonb NOT NULL,
	analysis_result JSONB DEFAULT '{}'::jsonb NOT NULL,
	model_name VARCHAR(128),
	model_config_hash VARCHAR(64),
	pipeline_version VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_analyses_final_score CHECK (final_score IS NULL OR (final_score >= 0 AND final_score <= 100)),
	CONSTRAINT ck_analyses_status CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
	FOREIGN KEY(task_id) REFERENCES analysis_tasks (id) ON DELETE SET NULL,
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE RESTRICT,
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
	CONSTRAINT fk_analyses_job_owner FOREIGN KEY(job_id, owner_user_id) REFERENCES jobs (id, owner_user_id),
	CONSTRAINT fk_analyses_resume_owner FOREIGN KEY(resume_id, owner_user_id) REFERENCES resumes (id, owner_user_id),
	CONSTRAINT fk_analyses_task_owner FOREIGN KEY(task_id, owner_user_id) REFERENCES analysis_tasks (id, owner_user_id),
	CONSTRAINT uq_analyses_id_owner UNIQUE (id, owner_user_id),
	CONSTRAINT uq_analyses_task_resume UNIQUE (task_id, resume_id)
);
CREATE INDEX ix_analyses_owner_created ON analyses (owner_user_id, created_at);

CREATE TABLE archives (
	id UUID NOT NULL,
	owner_user_id UUID NOT NULL,
	resume_id UUID,
	job_id UUID,
	analysis_id UUID,
	candidate_name VARCHAR(256),
	category VARCHAR(128),
	tags JSONB DEFAULT '[]'::jsonb NOT NULL,
	status VARCHAR(16) DEFAULT 'active' NOT NULL,
	note TEXT,
	legacy_payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_archives_status CHECK (status IN ('active', 'trashed', 'deleted')),
	FOREIGN KEY(resume_id) REFERENCES resumes (id) ON DELETE SET NULL,
	FOREIGN KEY(analysis_id) REFERENCES analyses (id) ON DELETE SET NULL,
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE SET NULL,
	CONSTRAINT fk_archives_analysis_owner FOREIGN KEY(analysis_id, owner_user_id) REFERENCES analyses (id, owner_user_id),
	CONSTRAINT fk_archives_job_owner FOREIGN KEY(job_id, owner_user_id) REFERENCES jobs (id, owner_user_id),
	CONSTRAINT fk_archives_resume_owner FOREIGN KEY(resume_id, owner_user_id) REFERENCES resumes (id, owner_user_id)
);
CREATE INDEX ix_archives_owner_status_created ON archives (owner_user_id, status, created_at);
