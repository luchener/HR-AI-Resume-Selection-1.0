"""Explicitly injected configuration probes; no provider clients or retries.

Probe callbacks receive private configurations and must not log their inputs.
They run outside database locks. Permissions are rechecked before returning and
model-cache writes compare the original document under its transaction lock.
External probe effects cannot be rolled back by a database transaction.
"""
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID

from sqlalchemy import select

from db_audit import append_audit
from db_config_resolved import MASK, public_config_document, private_config_document
from db_config_service import ConfigPermissionError, ConfigValidationError
from db_json_compare import json_equal
from db_models import User
from db_secrets import EncryptedConfigRepository


class ConfigProbeError(Exception):
    def __init__(self, detail='诊断调用失败。', status=422):
        super().__init__(detail)
        self.detail, self.status = detail, status


class ConfigDiagnostics:
    def __init__(self, service, *, validate_base_url, llm_probe=None, models_probe=None, mail_probe=None):
        if not callable(validate_base_url) or any(value is not None and not callable(value)
                for value in (llm_probe, models_probe, mail_probe)):
            raise TypeError('Explicit validator and optional probe callbacks required')
        self.service = service
        self.validate_base_url = validate_base_url
        self.callbacks = {'llm':llm_probe, 'models':models_probe, 'mail':mail_probe}

    def _actor(self, session, actor_id, password_version):
        identity = UUID(str(actor_id))
        row = session.execute(select(User.id, User.email, User.username, User.status, User.deleted_at,
                                     User.password_version).where(User.id==identity).with_for_update()).mappings().one_or_none()
        if (row is None or row['status']!='active' or row['deleted_at'] is not None
                or type(password_version) is not int or row['password_version']!=password_version
                or (row['email'] or '').lower() not in self.service.admin_emails):
            raise ConfigPermissionError('Active superadministrator required')
        return row

    def _configuration(self, document, current, slot):
        return private_config_document(document, current, self.service.environment, slot=slot)

    def run(self, actor_id, kind, payload, *, password_version, request_id):
        callback = self.callbacks.get(kind)
        if callback is None:
            raise ConfigProbeError('DB preview unsupported',503)
        if not isinstance(payload,dict):
            raise ConfigValidationError('Object required')
        allowed = {'slot','send'} if kind=='mail' else {'api_key','base_url','model'}
        if set(payload)-allowed:
            raise ConfigValidationError('Invalid probe fields')
        document = 'mail' if kind=='mail' else 'llm'
        slot = payload.get('slot','transactional')
        if kind=='mail' and (slot not in ('transactional','notification') or type(payload.get('send',False)) is not bool):
            raise ConfigValidationError('Invalid mail probe')
        service = self.service
        with service.sessions() as session, session.begin():
            actor = self._actor(session,actor_id,password_version)
            documents = EncryptedConfigRepository(session,service.cipher).get() or {}
            before = documents.get('system_config/'+document+'.json')
            cfg = self._configuration(document,before,slot)
            recipient = actor['email'] if kind=='mail' and payload.get('send',False) else ''
        if document=='llm':
            for name in allowed:
                value = payload.get(name,'')
                if not isinstance(value,str) or len(value)> (4096 if name=='api_key' else 500) or '\r' in value or '\n' in value:
                    raise ConfigValidationError('Invalid probe value')
                if value.strip() and value.strip() not in (MASK,'********'):
                    cfg[name] = value.strip()
            try:
                cfg['base_url'] = self.validate_base_url(cfg['base_url'])
            except ValueError:
                raise ConfigValidationError('Invalid probe URL') from None
            if not cfg['api_key'] or not cfg['base_url']:
                raise ConfigValidationError('Model configuration incomplete')
            cfg['ready'] = True
        elif not cfg['ready']:
            raise ConfigValidationError('Mail configuration incomplete')
        started = perf_counter()
        try:
            outcome = callback(dict(cfg),recipient) if kind=='mail' else callback(dict(cfg))
        except Exception:
            raise ConfigProbeError() from None
        seconds = round(perf_counter()-started,3)
        if kind=='llm':
            if not isinstance(outcome,dict) or type(outcome.get('reply_chars')) is not int or outcome['reply_chars']<0:
                raise ConfigProbeError('诊断返回格式不正确。',503)
            result = {'base_url':cfg['base_url'],'model':cfg['model'],'seconds':seconds,
                      'reply_chars':outcome['reply_chars'],'message':'连接测试成功。'}
        elif kind=='mail':
            if type(outcome) is not bool or outcome!=bool(recipient):
                raise ConfigProbeError('诊断返回格式不正确。',503)
            result = {key:cfg[key] for key in ('slot','source','host','port','security','from_address')}
            result.update(test_email_sent=outcome,seconds=seconds,message='邮件服务测试成功。')
        else:
            if not isinstance(outcome,list) or len(outcome)>10000 or any(not isinstance(item,str) for item in outcome):
                raise ConfigProbeError('诊断返回格式不正确。',503)
            models = []
            for item in outcome:
                name=item.strip()
                if cfg['api_key'] and cfg['api_key'] in name:
                    raise ConfigProbeError('诊断返回格式不正确。',503)
                if name and len(name)<=200 and name not in models:
                    models.append(name)
                    if len(models)==200:
                        break
        with service.sessions() as session, session.begin():
            actor = self._actor(session,actor_id,password_version)
            if kind=='models':
                cached = None
                def merge(current,incoming):
                    nonlocal cached
                    if not json_equal(current,before or {}):
                        raise ConfigProbeError('配置已变化，请重新获取模型列表。',409)
                    cached = dict(current,models=models,models_fetched_at=datetime.now(timezone.utc).isoformat(),
                                  updated_by=actor['username'])
                    return cached
                EncryptedConfigRepository(session,service.cipher).merge_document('system_config/llm.json',{},merge)
                public = public_config_document('llm',cached,service.environment)
                result = {'models':models,'count':len(models),'models_fetched_at':public['models_fetched_at'],
                          'seconds':seconds,'config':public}
            action = 'config.mail.test' if kind=='mail' else 'config.llm.models' if kind=='models' else 'config.llm.test'
            append_audit(session,actor_user_id=actor['id'],action=action,target_type='config',target_id=document,
                         request_id=request_id,changed_fields=[document] if kind=='models' else [])
        return result
