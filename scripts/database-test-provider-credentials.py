"""Run on the existing Linux server: validate replacements without installing them.

Reads a private JSON file and passes it to the existing backend via stdin. Never
put values in command arguments. Makes a minimal DeepSeek call and sends one
fixed diagnostic email to each distinct configured sender's own address. SMTP
acceptance does not certify inbox delivery. Does not revoke or replace anything.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess


CHECK = r'''
import json,logging,sys,ssl,smtplib
from email.message import EmailMessage
from urllib.parse import urlsplit
logging.disable(logging.CRITICAL)
import system_config
from openai import OpenAI

report={'new_llm_authenticated':False,'new_llm_minimal_call':False,'smtp':[],
        'configuration_updated':False,'old_credentials_revoked':False,'production_acceptance':False}
try:
    incoming=json.load(sys.stdin)
    llm=system_config.resolve_llm()
    slots={slot:system_config.resolve_mail(slot) for slot in ('transactional','broadcast')}
    if (llm.get('source')!='env' or urlsplit(llm.get('base_url','')).hostname!='api.deepseek.com'
        or urlsplit(llm.get('base_url','')).scheme!='https'
        or any(c.get('source')!='env' or c.get('host')!='smtp.163.com' for c in slots.values())):
        raise ValueError('Unexpected runtime provider configuration')
    if incoming['llm_api_key']==llm.get('api_key') or any(incoming['smtp_password']==c.get('password') for c in slots.values()):
        raise ValueError('A replacement equals a current credential')
    if len({c.get('username') for c in slots.values()})!=1:
        raise ValueError('SMTP slots need separate replacement credentials')
    try:
        client=OpenAI(api_key=incoming['llm_api_key'],base_url=llm['base_url'],timeout=30,max_retries=0)
        try:
            result=client.chat.completions.create(model=llm['model'],messages=[{'role':'user','content':'Reply OK.'}],
                                                  max_tokens=64,extra_body={'thinking':{'type':'disabled'}})
            report['new_llm_authenticated']=True
            report['new_llm_minimal_call']=bool(result.choices and result.choices[0].message.content)
        finally:
            client.close()
    except Exception:
        report['llm_error']='provider_check_failed'
    # Do not send diagnostic mail if the model replacement is already unusable.
    if report['new_llm_minimal_call']:
        checked={}
        for slot,cfg in slots.items():
            identity=(cfg.get('host'),cfg.get('port'),cfg.get('username'),cfg.get('security'),cfg.get('from_address'))
            if identity in checked:
                report['smtp'].append({'slot':slot,**checked[identity],'reused_same_sender_check':True})
                continue
            outcome={'authenticated':False,'test_mail_accepted':False,'inbox_delivery_confirmed':False}
            try:
                if not cfg.get('from_address') or cfg.get('security') not in ('ssl','starttls'):
                    raise ValueError('Secure SMTP configuration required')
                context=ssl.create_default_context()
                if cfg['security']=='ssl':
                    connection=smtplib.SMTP_SSL(cfg['host'],int(cfg['port']),timeout=20,context=context)
                else:
                    connection=smtplib.SMTP(cfg['host'],int(cfg['port']),timeout=20)
                    connection.ehlo()
                    connection.starttls(context=context)
                    connection.ehlo()
                with connection:
                    connection.login(cfg['username'],incoming['smtp_password'])
                    outcome['authenticated']=True
                    message=EmailMessage()
                    message['From']=cfg['from_address']
                    message['To']=cfg['from_address']
                    message['Subject']='AI resume credential rotation verification'
                    message.set_content('Credential rotation verification. No business data or credentials are included.')
                    refused=connection.send_message(message)
                    outcome['test_mail_accepted']=not refused
            except Exception:
                outcome['error']='provider_check_failed'
            checked[identity]=outcome
            report['smtp'].append({'slot':slot,**outcome})
except Exception:
    report['error']='replacement_or_runtime_configuration_rejected'
print(json.dumps(report))
'''


def replacements(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.parent.is_symlink():
        raise ValueError('Private credential file required')
    fd = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'r',encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()
                or stat.S_IMODE(path.parent.stat().st_mode) != 0o700 or info.st_size > 4096):
            raise ValueError('Private credential permissions required')
        data = json.load(handle)
    if not isinstance(data,dict) or set(data) != {'llm_api_key','smtp_password'}:
        raise ValueError('Expected two replacement fields')
    if any(not isinstance(v,str) or re.fullmatch(r'[A-Za-z0-9_-]{8,256}',v) is None for v in data.values()):
        raise ValueError('Replacement fields must be filled with provider-issued values')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-file',type=Path,required=True)
    parser.add_argument('--execute-provider-checks',action='store_true',required=True)
    args = parser.parse_args()
    try:
        if os.name != 'posix':
            raise ValueError('Run on the Linux server')
        data = replacements(args.credential_file)
        child = subprocess.run(['sudo','-n','docker','exec','-i','hr-ai-resume-selection-backend',
                                'python','-c',CHECK],input=json.dumps(data),text=True,capture_output=True,timeout=100)
        if child.returncode:
            raise ValueError('Backend credential checks could not run')
        report = json.loads(child.stdout)
        allowed = {'new_llm_authenticated','new_llm_minimal_call','smtp','configuration_updated',
                   'old_credentials_revoked','production_acceptance','llm_error','error'}
        if not isinstance(report,dict) or not set(report)<=allowed:
            raise ValueError('Unexpected credential check output')
        report['observed_at'] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(report))
        return 0 if (report.get('new_llm_minimal_call') and len(report.get('smtp',[]))==2
                     and all(slot.get('test_mail_accepted') for slot in report['smtp'])) else 1
    except Exception:
        print('{"error":"Credential check unavailable or replacement file invalid; no configuration changed"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
