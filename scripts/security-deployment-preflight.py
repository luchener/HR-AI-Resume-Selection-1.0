#!/usr/bin/env python3
"""部署前校验可信 Origin 的**最终生效值**（只输出非秘密项）。

背景
----
1. Compose 的 `environment` 优先级高于 `env_file`：若 compose 里写了
   `CSRF_TRUSTED_ORIGINS: ${CSRF_TRUSTED_ORIGINS:-}`，外层未导出时最终容器值是空串，
   会覆盖 `.env` 里的正确值，后端随后对所有带 Origin 的写请求返回 403。
2. `env_file` 的内容**也会被 Compose 插值**（默认格式），自制解析器得到的值与真实
   运行时不同。例如 `.env` 里写 `https://${PUBLIC_HOST}`，外层未设置时真实值是
   `https://`（无效），而自制解析器可能把它当成合法主机名放过去。

因此：**权威值一律来自 `docker compose config` 的真实渲染结果**（该命令不需要
Docker daemon），自制解析只用于解释"是否被空值覆盖"这类诊断，不再对外宣称是最终值。

用法
----
    # JSON 后端：渲染 compose，取 backend 服务的最终环境
    python scripts/security-deployment-preflight.py --compose docker-compose.secure.yml
    # 替换容器后核对实际值
    python scripts/security-deployment-preflight.py --container hr-ai-resume-selection-backend \
        --expect https://workbench.example.invalid

    # SQL 预运行：读取容器实际使用的 deployment.json（挂载文件），而不是环境变量
    python scripts/security-deployment-preflight.py --sql-container resume-sql-preview-backend-1 \
        --expect http://127.0.0.1:13010

    # 也可以直接核对宿主机上的一份 deployment.json
    python scripts/security-deployment-preflight.py --deployment-json /opt/.../deployment.json

退出码：0 通过；2 配置缺失/非法/不可读；3 与期望值不一致。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'apps' / 'backend'))

from prompt_safety import normalize_origin  # noqa: E402

KEY = 'CSRF_TRUSTED_ORIGINS'
SQL_CONFIG_PATH = '/run/secrets/sql-deployment.json'
# 容器内只输出 trusted_origins 这一项，绝不打印整份配置或凭据。
SQL_ORIGINS_CODE = (
    "import json,sys\n"
    "with open(sys.argv[1], encoding='utf-8') as handle:\n"
    "    data = json.load(handle)\n"
    "print(json.dumps({'trusted_origins': data.get('trusted_origins')}))\n"
)


class ConfigError(RuntimeError):
    pass


def docker(args, *, timeout=60, environ=None):
    """执行 docker 命令；失败时抛出不含输出的 ConfigError。测试可替换该函数。"""
    try:
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                encoding='utf-8', errors='replace',
                                timeout=timeout, env=environ)
    except (OSError, subprocess.SubprocessError):
        raise ConfigError('docker command could not be executed') from None
    if result.returncode:
        raise ConfigError('docker command failed: %s (exit %s)' % (args[0], result.returncode))
    return result.stdout


def parse_env_file(path: Path) -> dict:
    """仅用于诊断的 env_file 原始解析（不做 Compose 插值，不是最终值）。"""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        text = line.strip()
        if not text or text.startswith('#') or '=' not in text:
            continue
        name, value = text.split('=', 1)
        name = name.strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[name] = value
    return values


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError:  # pragma: no cover
        raise ConfigError('PyYAML is required for compose diagnostics') from None
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def compose_declarations(compose_path: Path, service: str = 'backend') -> tuple[dict, dict]:
    """诊断用：返回 (env_file 原始值, environment 声明值)。不参与最终判定。"""
    compose_path = compose_path.resolve()
    services = (_load_yaml(compose_path) or {}).get('services') or {}
    if service not in services:
        raise ConfigError('service %r not found in %s' % (service, compose_path.name))
    definition = services[service] or {}
    file_values = {}
    for entry in definition.get('env_file') or []:
        raw = entry.get('path') if isinstance(entry, dict) else entry
        if not raw:
            continue
        target = (compose_path.parent / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
        file_values.update(parse_env_file(target))
    overrides = definition.get('environment') or {}
    if isinstance(overrides, list):
        parsed = {}
        for item in overrides:
            text = str(item)
            name, _, value = text.partition('=')
            parsed[name.strip()] = value
        overrides = parsed
    return file_values, {name: ('' if value is None else str(value)) for name, value in overrides.items()}


def render_compose(compose_path: Path, service: str = 'backend', *, environ=None) -> dict:
    """用真实的 `docker compose config` 计算服务的最终环境（不需要 daemon）。

    完整渲染结果只在进程内解析，绝不记录或打印；调用方只能取需要的键。
    """
    compose_path = compose_path.resolve()
    output = docker(['compose', '-f', str(compose_path), '--project-directory',
                     str(compose_path.parent), 'config', '--format', 'json'],
                    environ=environ, timeout=120)
    try:
        rendered = json.loads(output)
    except json.JSONDecodeError:
        raise ConfigError('compose rendering produced unreadable output') from None
    services = (rendered or {}).get('services') or {}
    if service not in services:
        raise ConfigError('service %r missing from rendered compose' % service)
    environment = services[service].get('environment') or {}
    return {name: ('' if value is None else str(value)) for name, value in environment.items()}


def validate_origins(raw, *, source: str) -> list[str]:
    if isinstance(raw, (list, tuple)):
        entries = [str(item).strip() for item in raw if str(item).strip()]
    elif isinstance(raw, str):
        entries = [item.strip() for item in raw.split(',') if item.strip()]
    else:
        raise ConfigError('trusted origins have an unsupported type (source: %s)' % source)
    if not entries:
        raise ConfigError('trusted origins are missing or empty (source: %s)' % source)
    normalized = []
    for item in entries:
        origin = normalize_origin(item)
        if origin is None:
            raise ConfigError('invalid trusted origin %r (source: %s)' % (item[:120], source))
        normalized.append(origin)
    return list(dict.fromkeys(normalized))


def inspect_container(name: str, *, environ=None) -> dict:
    output = docker(['inspect', name], environ=environ)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        raise ConfigError('container %r is not readable' % name) from None
    if not isinstance(payload, list) or not payload:
        raise ConfigError('container %r is not readable' % name)
    return payload[0]


def container_environment(container: dict) -> dict:
    env = {}
    for item in container.get('Config', {}).get('Env') or []:
        if '=' in item:
            key, value = item.split('=', 1)
            env[key] = value
    return env


def container_origins(name: str, *, environ=None) -> list[str]:
    """JSON 后端：可信源来自容器环境变量。"""
    env = container_environment(inspect_container(name, environ=environ))
    return validate_origins(env.get(KEY, ''), source='container:%s' % name)


def read_sql_origins(name: str, *, environ=None) -> list[str]:
    """SQL 后端：可信源来自容器实际加载的 deployment.json（挂载文件）。

    与应用一致：只认 `trusted_origins` 列表；缺失、类型不对或含非法项都视为配置错误。
    只取这一项返回，不读取也不输出其它字段。
    """
    container = inspect_container(name, environ=environ)
    env = container_environment(container)
    path = env.get('SQL_DEPLOYMENT_FILE') or SQL_CONFIG_PATH
    output = docker(['exec', name, 'python', '-c', SQL_ORIGINS_CODE, path], environ=environ)
    try:
        payload = json.loads(output.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ConfigError('container configuration is unreadable') from None
    if not isinstance(payload, dict):
        raise ConfigError('container configuration is unreadable')
    raw = payload.get('trusted_origins')
    if isinstance(raw, str):
        raise ConfigError('trusted_origins must be a list')
    return validate_origins(raw, source='sql-container:%s' % name)


def _check_expect(entries: list, expect: str) -> bool:
    expected = validate_origins(expect, source='--expect')
    return expected == entries


def run(args) -> int:
    report = {'ok': False}
    try:
        if args.compose:
            compose_path = Path(args.compose)
            rendered = render_compose(compose_path, args.service)
            raw = rendered.get(KEY, '')
            if not str(raw).strip():
                file_values, overrides = compose_declarations(compose_path, args.service)
                if KEY in overrides and str(file_values.get(KEY, '')).strip():
                    raise ConfigError(
                        'compose environment overrides env_file with an empty value for %s; '
                        'remove that environment entry and keep the value in the env_file' % KEY)
            entries = validate_origins(raw, source='compose:%s' % compose_path.name)
            report.update(ok=True, mode='compose', compose=str(compose_path), service=args.service,
                          origins=entries, count=len(entries), rendered_by='docker compose config')
        elif args.deployment_json:
            path = Path(args.deployment_json)
            if not path.is_file():
                raise ConfigError('deployment file not found')
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            raw = data.get('trusted_origins')
            if isinstance(raw, str):
                raise ConfigError('trusted_origins must be a list, not a string')
            entries = validate_origins(raw, source='deployment:%s' % path.name)
            report.update(ok=True, mode='deployment-json', file=str(path.name),
                          origins=entries, count=len(entries))
        elif args.sql_container:
            entries = read_sql_origins(args.sql_container)
            report.update(ok=True, mode='sql-container', container=args.sql_container,
                          origins=entries, count=len(entries), source=SQL_CONFIG_PATH)
            if args.expect and not _check_expect(entries, args.expect):
                report.update(ok=False, mismatch=True)
                print(json.dumps(report, ensure_ascii=False))
                return 3
        elif args.container:
            entries = container_origins(args.container)
            report.update(ok=True, mode='container', container=args.container,
                          origins=entries, count=len(entries), source=KEY)
            if args.expect and not _check_expect(entries, args.expect):
                report.update(ok=False, mismatch=True)
                print(json.dumps(report, ensure_ascii=False))
                return 3
        else:  # pragma: no cover - argparse 已保证
            raise ConfigError('nothing to check')
    except ConfigError as error:
        report.update(ok=False, error=str(error))
        print(json.dumps(report, ensure_ascii=False))
        return 2
    except (OSError, json.JSONDecodeError, TypeError) as error:
        report.update(ok=False, error='unreadable input: %s' % type(error).__name__)
        print(json.dumps(report, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Validate effective CSRF trusted origins before deployment')
    parser.add_argument('--compose', help='compose file; authoritative value comes from docker compose config')
    parser.add_argument('--service', default='backend', help='compose service name (default: backend)')
    parser.add_argument('--deployment-json', help='SQL preview deployment file with trusted_origins')
    parser.add_argument('--container', help='JSON backend container: reads CSRF_TRUSTED_ORIGINS from its env')
    parser.add_argument('--sql-container', help='SQL backend container: reads the mounted deployment.json')
    parser.add_argument('--expect', help='expected origins (comma separated) for container comparisons')
    args = parser.parse_args(argv)
    modes = [args.compose, args.deployment_json, args.container, args.sql_container]
    if sum(bool(value) for value in modes) != 1:
        parser.error('choose exactly one of --compose, --deployment-json, --container, --sql-container')
    if args.expect and not (args.container or args.sql_container):
        parser.error('--expect is only meaningful with --container or --sql-container')
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
