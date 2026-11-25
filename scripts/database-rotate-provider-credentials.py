"""Linux server only: rotate the existing JSON app without restarting workers.

Reads provider-issued replacements from a private file. Stores old credentials
only in a private server directory for supplier revocation checks. Captured
subprocess output is never printed. No secrets are command arguments.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile


CONTAINER = 'hr-ai-resume-selection-backend'
ENV_PATH = Path(os.environ.get('RESUME_BACKEND_ENV_FILE', Path(__file__).resolve().parents[1] / 'apps/backend/.env'))
SECRET_ROOT = Path(os.environ.get('RESUME_SECRET_DIR', Path.home() / '.secrets'))
STATE_ROOT = SECRET_ROOT/'resume-provider-rotation-state'

CAPTURE = r'''
import json,logging
logging.disable(logging.CRITICAL)
import system_config as s
from pathlib import Path
from urllib.parse import urlsplit
llm=s.resolve_llm()
mail={slot:s.resolve_mail(slot) for slot in s.SLOTS}
if (urlsplit(llm['base_url']).hostname!='api.deepseek.com' or urlsplit(llm['base_url']).scheme!='https'
    or any(c['host']!='smtp.163.com' for c in mail.values())):
    raise ValueError('Unexpected providers')
print(json.dumps({'llm':llm,'mail':mail,'documents':{
    name:Path(s._path(name)).read_text(encoding='utf-8') if Path(s._path(name)).exists() else None
    for name in (s.NAME,s.NAME_LLM)}}))
'''

INSTALL = r'''
import json,logging,os,sys
logging.disable(logging.CRITICAL)
import system_config as s
from pathlib import Path
d=json.load(sys.stdin)
old=d['previous']; new=d['replacement']
for name,value in old['documents'].items():
    p=Path(s._path(name))
    if (p.read_text(encoding='utf-8') if p.exists() else None)!=value:
        raise ValueError('Configuration changed during rotation')
llm=old['llm']; mail=old['mail']
if s.resolve_llm().get('api_key')!=llm.get('api_key') or any(s.resolve_mail(slot).get('password')!=cfg.get('password') for slot,cfg in mail.items()):
    raise ValueError('Effective credentials changed during rotation')
# Construct and validate both documents before changing either one. The deployed
# JSON version predates merge_*; intercept its writer inside this short-lived
# process to run the actual validators without changing production files.
llm_payload={k:llm[k] for k in ('base_url','model','timeout','enabled') if k in llm}
llm_payload['api_key']=new['llm_api_key']
slots={slot:{k:cfg[k] for k in ('host','port','security','username','from_address','from_name','enabled') if k in cfg} for slot,cfg in mail.items()}
for cfg in slots.values():cfg['password']=new['smtp_password']
staged={}
writer=s._write
try:
    s._write=lambda name,data:staged.__setitem__(name,json.loads(json.dumps(data)))
    s.save_llm(llm_payload,operator='credential-rotation')
    s.save_mail({'slots':slots},operator='credential-rotation')
finally:
    s._write=writer
if set(staged)!={s.NAME,s.NAME_LLM}:
    raise ValueError('Unexpected validated documents')
try:
    for name in (s.NAME_LLM,s.NAME):
        s._write(name,staged[name])
    if s.resolve_llm()['api_key']!=new['llm_api_key'] or any(s.resolve_mail(slot)['password']!=new['smtp_password'] for slot in s.SLOTS):
        raise ValueError('Effective credential mismatch')
except BaseException:
    for name,value in old['documents'].items():
        p=Path(s._path(name))
        if value is None:
            p.unlink(missing_ok=True)
        else:
            s._write(name,json.loads(value))
        s._CACHE.pop(str(p),None)
    raise
print(json.dumps({'hot_configuration_updated':True,'runtime_new_credentials_match':True,
                 'config_modes_private':all((Path(s._path(name)).stat().st_mode&0o777)==0o600 for name in (s.NAME,s.NAME_LLM))}))
'''

VERIFY = r'''
import json,logging,secrets,sys,ssl,smtplib,urllib.request,urllib.error
logging.disable(logging.CRITICAL)
import system_config as s
from openai import OpenAI,AuthenticationError,APIStatusError
d=json.load(sys.stdin); new=d['replacement']; old=d['previous']
report={'runtime_new_credentials_match':False,'new_llm_call':False,'new_smtp_authentication':False,
        'old_llm_authentication':'inconclusive','old_smtp_authentication':'inconclusive',
        'old_credentials_revoked':False,'service_restarted':False}
try:
    llm=s.resolve_llm(); mail=s.resolve_mail('transactional')
    report['runtime_new_credentials_match']=llm['api_key']==new['llm_api_key'] and all(s.resolve_mail(slot)['password']==new['smtp_password'] for slot in s.SLOTS)
    with OpenAI(api_key=llm['api_key'],base_url=llm['base_url'],timeout=30,max_retries=0) as client:
        response=client.chat.completions.create(model=llm['model'],messages=[{'role':'user','content':'Reply OK.'}],
                    max_tokens=64,extra_body={'thinking':{'type':'disabled'}})
        report['new_llm_call']=bool(response.choices and response.choices[0].message.content)
except Exception:
    report['new_llm_error']='provider_check_failed'

def smtp_auth(cfg,password):
    try:
        ctx=ssl.create_default_context()
        if cfg.get('security')=='ssl':
            connection=smtplib.SMTP_SSL(cfg['host'],int(cfg['port']),timeout=20,context=ctx)
        elif cfg.get('security')=='starttls':
            connection=smtplib.SMTP(cfg['host'],int(cfg['port']),timeout=20)
            connection.ehlo();connection.starttls(context=ctx);connection.ehlo()
        else:return 'inconclusive'
        with connection:connection.login(cfg['username'],password)
        return 'accepted'
    except smtplib.SMTPAuthenticationError as exc:
        return 'rejected' if exc.smtp_code==535 else 'inconclusive'
    except Exception:return 'inconclusive'

report['new_smtp_authentication']=smtp_auth(s.resolve_mail('transactional'),new['smtp_password'])=='accepted'
try:
    # Exercise inference too, rather than relying only on model-list access.
    # Only explicit 401 proves rejection; timeout/403/429 do not.
    client=OpenAI(api_key=old['llm']['api_key'],base_url=old['llm']['base_url'],timeout=20,max_retries=0)
    try:
        client.models.list()
        report['old_llm_authentication']='accepted'
        response=client.chat.completions.create(model=old['llm']['model'],
            messages=[{'role':'user','content':'Reply OK.'}],max_tokens=64,
            extra_body={'thinking':{'type':'disabled'}})
        report['old_llm_minimal_call']=bool(response.choices and response.choices[0].message.content)
    except AuthenticationError:report['old_llm_authentication']='rejected'
    finally:client.close()
except Exception:pass
report['invalid_llm_key_authentication']='inconclusive'
try:
    with OpenAI(api_key='sk-'+secrets.token_hex(24),base_url=old['llm']['base_url'],timeout=20,max_retries=0) as client:
        try:
            client.models.list()
            report['invalid_llm_key_authentication']='accepted'
        except AuthenticationError:report['invalid_llm_key_authentication']='rejected'
except Exception:pass
old_results=[smtp_auth(cfg,cfg['password']) for cfg in {cfg['username']:cfg for cfg in old['mail'].values()}.values()]
report['old_smtp_authentication']='rejected' if old_results and all(x=='rejected' for x in old_results) else 'accepted' if 'accepted' in old_results else 'inconclusive'
report['invalid_smtp_password_authentication']=smtp_auth(s.resolve_mail('transactional'),secrets.token_hex(24))
report['old_credentials_revoked']=report['old_llm_authentication']=='rejected' and report['old_smtp_authentication']=='rejected'
try:
    with urllib.request.urlopen('http://127.0.0.1:8000/ping',timeout=10) as response:
        report['backend_ping_ok']=response.status==200
except Exception:report['backend_ping_ok']=False
print(json.dumps(report))
'''

HTTP_VERIFY = r'''
import json,logging,time,uuid,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
logging.disable(logging.CRITICAL)
import auth,jwt,system_config
report={'http_configuration_samples':0,'http_hot_configuration_visible':False}
try:
    expected={'llm':system_config.public_llm(),'mail':system_config.public_mail()}
    users=[]
    for name in auth._list_user_files():
        if not name.endswith('.json') or name.startswith('_'):continue
        user=auth._read_json(str(Path(auth.USERS_DIR)/name))
        if user and not user.get('deleted_at') and auth.is_admin_email(user.get('email','')):
            users.append(user)
    if not users:raise ValueError('No active super administrator')
    user=users[0];now=time.time()
    token=jwt.encode({'user_id':user['user_id'],'username':user['username'],
        'pwd_ver':user.get('password_version',1),'iss':'resume-matcher',
        'jti':str(uuid.uuid4()),'iat':now,'exp':now+60},auth._jwt_secret(),algorithm='HS256')
    def check(kind):
        try:
            request=urllib.request.Request('http://127.0.0.1:8000/api/v1/admin/system/'+kind,
                        headers={'Authorization':'Bearer '+token,'Connection':'close'})
            with urllib.request.urlopen(request,timeout=10) as response:
                data=json.load(response)['data']
            # An administrator can legitimately re-save the same new values.
            # Compare the persisted version, not the last editor's identity.
            if not expected[kind].get('updated_at') or data.get('updated_at')!=expected[kind]['updated_at']:return False
            if kind=='llm':return data.get('source')=='ui' and data.get('api_key_set') and data.get('ready')
            slots=data.get('slots',{})
            return len(slots)==2 and all(c.get('source')=='ui' and c.get('password_set') and c.get('ready') for c in slots.values())
        except Exception:return False
    kinds=['llm','mail']*4
    with ThreadPoolExecutor(max_workers=4) as executor:
        checks=list(executor.map(check,kinds))
    report['http_configuration_samples']=len(checks)
    report['http_hot_configuration_visible']=all(checks)
except Exception:pass
print(json.dumps(report))
'''


def container(code, payload=None):
    result = subprocess.run(['docker','exec','-i',CONTAINER,'python','-c',code],
                            input=json.dumps(payload) if payload is not None else '',
                            capture_output=True,text=True,encoding='utf-8',timeout=180)
    if result.returncode:
        raise RuntimeError('Backend operation failed')
    return json.loads(result.stdout)


def atomic_write(path, data, mode=0o600, uid=None, gid=None):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        if uid is not None:
            os.fchown(fd,uid,gid)
        with os.fdopen(fd,'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        directory = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def replacement_file(path):
    owner = pwd.getpwnam('ubuntu').pw_uid
    if path.parent != SECRET_ROOT or path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Unexpected credential location')
    if stat.S_IMODE(path.parent.stat().st_mode)!=0o700 or path.parent.stat().st_uid!=owner:
        raise ValueError('Invalid credential directory permissions')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r',encoding='utf-8-sig') as handle:
        st=os.fstat(handle.fileno())
        if st.st_uid!=owner or stat.S_IMODE(st.st_mode)!=0o600 or st.st_size>4096:
            raise ValueError('Invalid credential permissions')
        data=json.load(handle)
    if not isinstance(data,dict) or set(data)!={'llm_api_key','smtp_password'}:
        raise ValueError('Invalid credential document')
    if any(not isinstance(v,str) or re.fullmatch(r'[A-Za-z0-9_-]{8,256}',v) is None for v in data.values()):
        raise ValueError('Invalid credential values')
    return data


def update_env(contents, values):
    result=contents.decode('utf-8-sig')
    for name,value in values.items():
        pattern=re.compile(r'(?m)^[ \t]*(?:export[ \t]+)?'+re.escape(name)+r'[ \t]*=.*$')
        if len(pattern.findall(result))!=1:
            raise ValueError('Expected one environment declaration per credential')
        result=pattern.sub(lambda m:name+'='+value,result)
    return result.encode('utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-file',type=Path,default=SECRET_ROOT/'resume-provider-rotation.json')
    parser.add_argument('action',choices=('apply','verify'))
    args=parser.parse_args()
    if os.geteuid()!=0:
        print('{"error":"Run with server administrative privileges; no credentials displayed"}')
        return 2
    report={'observed_at':datetime.now(timezone.utc).isoformat(),'production_database_cutover':False}
    try:
        new=replacement_file(args.credential_file)
        STATE_ROOT.mkdir(mode=0o700,exist_ok=True)
        if STATE_ROOT.is_symlink() or stat.S_IMODE(STATE_ROOT.stat().st_mode)!=0o700 or STATE_ROOT.stat().st_uid!=0:
            raise ValueError('Invalid rotation state directory')
        with (STATE_ROOT/'rotation.lock').open('a') as lock:
            os.chmod(lock.name,0o600)
            fcntl.flock(lock,fcntl.LOCK_EX)
            previous_path=STATE_ROOT/'previous.json'
            if args.action=='apply':
                if previous_path.exists():
                    raise ValueError('Prior rotation state exists; use verify')
                old=container(CAPTURE)
                if old['llm'].get('source')!='env' or any(cfg.get('source')!='env' for cfg in old['mail'].values()):
                    raise ValueError('Unexpected credential sources')
                if old['llm']['api_key']==new['llm_api_key'] or any(cfg['password']==new['smtp_password'] for cfg in old['mail'].values()):
                    raise ValueError('Replacement must differ')
                if len({cfg['username'] for cfg in old['mail'].values()})!=1:
                    raise ValueError('Independent SMTP replacements required')
                before=ENV_PATH.read_bytes()
                env_stat=ENV_PATH.stat()
                if ENV_PATH.is_symlink() or stat.S_IMODE(env_stat.st_mode)!=0o600:
                    raise ValueError('Invalid authoritative environment file')
                after=update_env(before,{'LLM_API_KEY':new['llm_api_key'],'SMTP_PASSWORD':new['smtp_password']})
                atomic_write(previous_path,json.dumps(old).encode())
                if ENV_PATH.read_bytes()!=before:
                    raise ValueError('Concurrent environment change')
                atomic_write(ENV_PATH,after,uid=env_stat.st_uid,gid=env_stat.st_gid)
                try:
                    installed=container(INSTALL,{'previous':old,'replacement':new})
                except Exception:
                    if ENV_PATH.read_bytes()==after:
                        atomic_write(ENV_PATH,before,uid=env_stat.st_uid,gid=env_stat.st_gid)
                    raise
                report.update(installed)
                report['authoritative_env_updated']=ENV_PATH.read_bytes()==after
                report['service_restarted']=False
                atomic_write(STATE_ROOT/'installed.json',json.dumps({'at':report['observed_at'],
                    'replacement_digest':hashlib.sha256(json.dumps(new,sort_keys=True).encode()).hexdigest()}).encode())
            else:
                old=json.loads(previous_path.read_text(encoding='utf-8'))
                installed=json.loads((STATE_ROOT/'installed.json').read_text(encoding='utf-8'))
                if installed['replacement_digest']!=hashlib.sha256(json.dumps(new,sort_keys=True).encode()).hexdigest():
                    raise ValueError('Replacement file changed')
            verification=container(VERIFY,{'previous':old,'replacement':new})
            report.update(verification)
            report.update(container(HTTP_VERIFY))
            current_env=ENV_PATH.read_bytes()
            report['authoritative_env_new_credentials_match']=update_env(current_env,{
                'LLM_API_KEY':new['llm_api_key'],'SMTP_PASSWORD':new['smtp_password']})==current_env
            atomic_write(STATE_ROOT/'latest-verification.json',json.dumps(report,indent=2).encode())
            print(json.dumps(report))
            service_ok=(report.get('runtime_new_credentials_match') and report.get('new_llm_call')
                        and report.get('new_smtp_authentication') and report.get('backend_ping_ok')
                        and report.get('authoritative_env_new_credentials_match')
                        and report.get('http_hot_configuration_visible'))
            if not service_ok:
                return 1
            # A healthy service does not imply the supplier revoked old values.
            return 0 if args.action=='apply' or report.get('old_credentials_revoked') else 3
    except Exception:
        print(json.dumps({'error':'Rotation operation failed; inspect private server state without printing secrets',
                          'production_database_cutover':False}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
