"""
运行时系统配置（账号管理 → 邮件服务）。

背景
----
SMTP 配置原先只能改 .env 并重启后端容器。本模块把它搬到
data/system_config/mail.json，管理员在界面上改完即生效，不需要登录服务器。

设计约束
--------
1) 优先级：界面配置 > .env（config.SMTP_*） > 无。读取结果回显 source，
   避免"我明明改了怎么没生效"。
2) 两套发件槽位：transactional（注册验证码/找回密码/邀请码）与
   notification（公告、群发通知）。notification 未单独配置时依次回退
   到 transactional、.env。
3) 多 worker：线上 4 个 gunicorn worker 各持缓存，靠文件 mtime_ns 比对
   失效。保存后所有 worker 自然生效，无需重启、无需跨进程广播。
4) fail-safe：文件缺失、JSON 损坏、字段非法一律回退 .env —— 配置模块
   本身绝不把邮件链路打死。
5) 落盘：明文 + 0600（与 .env 同级，不引入新秘密）。encrypt/decrypt 已留
   挂载点，将来要上 Fernet 只改这两处。

安全约定
--------
- 读取接口永不返回密码，只返回 password_set 与固定掩码；
- 保存时 password 为空串/缺省 = 保持不变（前端拿到的是掩码，回传掩码
  不能覆盖真密码）；
- 发件人显示名/地址会进入邮件头，含 CR/LF 一律拒绝（防邮件头注入）。
"""
import ipaddress
import json
import logging
import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import config
import httpcore
import httpx

logger = logging.getLogger(__name__)

NAME = "mail"
SLOT_TRANSACTIONAL = "transactional"   # 注册验证码 / 找回密码 / 邀请码
SLOT_NOTIFICATION = "notification"     # 公告通知 / 群发邮件
SLOTS = (SLOT_TRANSACTIONAL, SLOT_NOTIFICATION)
SECURITY_MODES = ("ssl", "starttls", "plain")

_MAX_LEN = {"host": 253, "username": 320, "from_address": 320, "from_name": 100,
            "base_url": 500, "model": 200}
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_MASK = "\u2022" * 8                  # 掩码固定 8 个圆点，不泄露原值长度与尾字符

# name -> (mtime_ns, parsed)。进程内缓存；靠 mtime 失效实现多 worker 热更新。
_CACHE: dict = {}


from config_rules import ConfigError, _now_iso, _clean_text, _clean_slot, merge_mail


def _dir() -> str:
    """配置目录。注意这里读 config.DATA_DIR 而非模块常量，便于测试隔离。"""
    return os.path.join(config.DATA_DIR, "system_config")


def _path(name: str = NAME) -> str:
    return os.path.join(_dir(), f"{name}.json")


def _ensure_dir() -> str:
    directory = _dir()
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, _DIR_MODE)
    except OSError:  # Windows 上 chmod 语义有限，失败不影响功能
        pass
    return directory




def _read(name: str = NAME) -> tuple:
    """读取配置。返回 (data, mtime_ns)。文件不存在或损坏时返回 ({}, mtime)。"""
    path = _path(name)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _CACHE.pop(path, None)
        return {}, 0

    cached = _CACHE.get(path)
    if cached and cached[0] == mtime:
        return dict(cached[1]), mtime

    data: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("根节点必须是对象")
        data = loaded
    except Exception as exc:  # noqa: BLE001 —— 配置坏了必须能继续跑
        logger.warning("system_config %s 读取失败，回退 .env：%s", name, exc)
        data = {}

    _CACHE[path] = (mtime, data)
    return dict(data), mtime


def raw(name: str = NAME) -> dict:
    return _read(name)[0]


def _write(name: str, data: dict) -> None:
    """原子写：同目录临时文件 + os.replace；Windows 上杀毒/索引器占用句柄时重试。"""
    directory = _ensure_dir()
    handle_fd, tmp = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp, _FILE_MODE)
        except OSError:
            pass
        last_error = None
        for _ in range(5):
            try:
                os.replace(tmp, _path(name))
                _CACHE.pop(_path(name), None)   # 让本进程立刻看到新值；其它 worker 靠 mtime
                return
            except OSError as exc:  # pragma: no cover - 平台相关
                last_error = exc
                import time as _time
                _time.sleep(0.05)
        raise last_error if last_error else OSError("写入配置失败")
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── 校验 ────────────────────────────────────────────────────────────────────





# ── 解析（界面 > .env）──────────────────────────────────────────────────────

def _env_slot() -> dict:
    return {
        "host": config.SMTP_HOST or "",
        "port": config.SMTP_PORT,
        "security": "ssl" if config.SMTP_PORT == 465 else "starttls",
        "username": config.SMTP_USER or "",
        "password": config.SMTP_PASSWORD or "",
        "from_address": config.SMTP_FROM or config.SMTP_USER or "",
        "from_name": "AI 简历智选",
        "enabled": True,
    }


def _ready(cfg: dict) -> bool:
    return bool(cfg.get("host") and cfg.get("username") and cfg.get("password"))


def _coerce(slot_cfg: dict) -> dict:
    cfg = dict(slot_cfg)
    cfg["port"] = int(cfg.get("port") or 465)
    cfg["security"] = str(cfg.get("security") or "ssl").lower()
    cfg["from_address"] = cfg.get("from_address") or cfg.get("username") or ""
    cfg["from_name"] = cfg.get("from_name") or "AI 简历智选"
    return cfg


def resolve_mail(slot: str = SLOT_TRANSACTIONAL) -> dict:
    """解析某槽位最终生效的发件配置。

    返回字段：host/port/security/username/password/from_address/from_name/
             ready/source/slot。source 取值：ui / ui.transactional / env / none。
    """
    if slot not in SLOTS:
        slot = SLOT_TRANSACTIONAL

    slots = raw(NAME).get("slots") or {}

    ui = slots.get(slot)
    if isinstance(ui, dict) and ui.get("enabled", True) and ui.get("host"):
        cfg = _coerce(ui)
        cfg.update({"source": "ui", "slot": slot, "ready": True})
        if _ready(cfg):
            return cfg

    if slot == SLOT_NOTIFICATION:
        shared = slots.get(SLOT_TRANSACTIONAL)
        if isinstance(shared, dict) and shared.get("enabled", True) and shared.get("host"):
            cfg = _coerce(shared)
            cfg.update({"source": "ui.transactional", "slot": slot, "ready": True})
            if _ready(cfg):
                return cfg

    env = _env_slot()
    if _ready(env):
        env.update({"source": "env", "slot": slot, "ready": True})
        return env

    env.update({"source": "none", "slot": slot, "ready": False})
    return env


# ── 对外视图与写入 ──────────────────────────────────────────────────────────

def _slot_view(slot: str) -> dict:
    cfg = resolve_mail(slot)
    return {
        "slot": slot,
        "host": cfg.get("host", ""),
        "port": cfg.get("port", 465),
        "security": cfg.get("security", "ssl"),
        "username": cfg.get("username", ""),
        "password_set": bool(cfg.get("password")),
        "password_mask": _MASK if cfg.get("password") else "",
        "from_address": cfg.get("from_address", ""),
        "from_name": cfg.get("from_name", ""),
        "enabled": bool(cfg.get("enabled", True)),
        "source": cfg.get("source", "none"),
        "ready": _ready(cfg),
    }


def public_mail() -> dict:
    """给管理界面的只读视图。任何情况下都不含密码明文。"""
    data = raw(NAME)
    return {
        "slots": {slot: _slot_view(slot) for slot in SLOTS},
        "env_available": _ready(_env_slot()),
        "updated_at": data.get("updated_at", ""),
        "updated_by": data.get("updated_by", ""),
    }




def save_mail(payload: dict, operator: str = "") -> dict:
    """保存界面配置。只写传入的槽位；password 留空表示沿用旧值。"""
    _write(NAME, merge_mail(raw(NAME), payload, operator))
    return public_mail()


def clear_mail(operator: str = "") -> dict:
    """清空界面配置，立即回退到 .env 兜底。"""
    path = _path(NAME)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(f"清空配置失败：{exc}")
    _CACHE.pop(path, None)
    if operator:
        logger.info("mail config cleared by %s", operator)
    return public_mail()


# ── 加密挂载点（当前明文 + 0600，将来上 Fernet 只改这两处）─────────────────

def encrypt(value: str) -> str:  # pragma: no cover - 预留
    return value


def decrypt(value: str) -> str:  # pragma: no cover - 预留
    return value

# ── 模型配置（账号管理 → 模型配置）──────────────────────────────────────────
#
# 定位是"服务端这一层"：用户在自己浏览器里带的 Key 优先，界面配置次之，.env 最后。
# 所有值都在调用时解析（绝不快照进模块常量），保存后下一个请求立即生效。
# 读取接口永不返回 API Key；base_url 走白名单校验，防止被当成 SSRF 跳板。

NAME_LLM = "llm"
_LLM_MAX = {"base_url": 500, "model": 200, "api_key": 4096}
_LLM_TIMEOUT_RANGE = (1, 3600)
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "100.100.100.200"}
_METADATA_IPS = frozenset(ipaddress.ip_address(host) for host in (
    "169.254.169.254", "100.100.100.200", "fd00:ec2::254"))
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


@dataclass(frozen=True)
class ResolvedAIEndpoint:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


class _PinnedNetworkBackend(httpcore.SyncBackend):
    def __init__(self, hostname: str, addresses: tuple[str, ...]):
        self.hostname = hostname
        self.addresses = addresses

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        normalized_host = host.rstrip(".").lower()
        if normalized_host != self.hostname:
            raise httpcore.ConnectError("AI connection host differs from validated endpoint")

        last_error = None
        for address in self.addresses:
            try:
                return super().connect_tcp(
                    address, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("No validated AI endpoint address is available")


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, endpoint: ResolvedAIEndpoint):
        super().__init__(trust_env=False, retries=0)
        pool = getattr(self, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            self.close()
            raise RuntimeError("Unsupported HTTP transport; pinned DNS is unavailable")
        pool._network_backend = _PinnedNetworkBackend(endpoint.hostname, endpoint.addresses)


def _allow_private_base_url() -> bool:
    """
    是否允许 base_url 指向内网/回环（仅供本地联调）。

    生产保持关闭：自定义接口地址本身就是 SSRF 面，默认只放行公网 https。
    本地要连自建模型服务时，显式设置 LLM_ALLOW_PRIVATE_BASE_URL=1。
    """
    if str(config.ENV or "").strip().lower() == "production":
        return False
    flag = str(os.environ.get("LLM_ALLOW_PRIVATE_BASE_URL") or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _is_multicast_address(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if value.is_multicast:
        return True
    if isinstance(value, ipaddress.IPv6Address):
        if value.ipv4_mapped is not None:
            return _is_multicast_address(value.ipv4_mapped)
        if value.sixtofour is not None:
            return _is_multicast_address(value.sixtofour)
        if value.teredo is not None:
            return _is_multicast_address(value.teredo[1])
        if value in _NAT64_WELL_KNOWN_PREFIX:
            return _is_multicast_address(ipaddress.IPv4Address(int(value) & 0xffffffff))
    return False


def _is_public_address(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if _is_multicast_address(value):
        return False
    if isinstance(value, ipaddress.IPv6Address):
        if value.ipv4_mapped is not None:
            return _is_public_address(value.ipv4_mapped)
        if value.sixtofour is not None:
            return _is_public_address(value.sixtofour)
        if value.teredo is not None:
            return _is_public_address(value.teredo[1])
        if value in _NAT64_WELL_KNOWN_PREFIX:
            return _is_public_address(ipaddress.IPv4Address(int(value) & 0xffffffff))
    return value.is_global and value not in _METADATA_IPS


def _resolve_host_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            answers = socket.getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM)
        except OSError:
            raise ConfigError("接口地址主机名解析失败") from None
        addresses = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(answer[4][0])
            except (IndexError, TypeError, ValueError):
                continue
            normalized = address.compressed
            if normalized not in addresses:
                addresses.append(normalized)
    else:
        addresses = [literal.compressed]

    if not addresses:
        raise ConfigError("接口地址主机名没有可用地址")
    return tuple(addresses)


def _resolve_ai_endpoint(value: str) -> ResolvedAIEndpoint:
    text = _clean_text(value, "base_url")
    if not text:
        raise ConfigError("接口地址不能为空")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in text):
        raise ConfigError("接口地址包含非法控制字符")
    if "?" in text or "#" in text:
        raise ConfigError("接口地址不能包含查询参数或锚点")

    parsed = urlsplit(text)
    if parsed.scheme not in ("https", "http"):
        raise ConfigError("接口地址必须使用 https://")
    if parsed.username or parsed.password:
        raise ConfigError("接口地址不能包含用户名或密码")
    if not parsed.hostname:
        raise ConfigError("接口地址缺少主机名")
    if "%" in parsed.hostname:
        raise ConfigError("接口地址不能包含带作用域的主机名")
    try:
        explicit_port = parsed.port
    except ValueError:
        raise ConfigError("接口地址端口无效") from None
    port = explicit_port if explicit_port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ConfigError("接口地址端口无效")

    raw_hostname = parsed.hostname.rstrip(".").lower()
    if not raw_hostname or raw_hostname in _METADATA_HOSTS:
        raise ConfigError("接口地址不能指向云元数据服务")
    literal = None
    try:
        literal = ipaddress.ip_address(raw_hostname)
    except ValueError:
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ConfigError("接口地址主机名无效") from None
        if len(hostname) > 253 or not hostname:
            raise ConfigError("接口地址主机名无效")
    else:
        hostname = literal.compressed

    allow_private = _allow_private_base_url()
    if parsed.scheme == "http" and not allow_private:
        raise ConfigError("接口地址必须使用 https://")

    addresses = _resolve_host_addresses(hostname, port)
    parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    if any(_is_multicast_address(address) for address in parsed_addresses):
        raise ConfigError("接口地址不能指向组播地址")
    if any(address in _METADATA_IPS for address in parsed_addresses):
        raise ConfigError("接口地址不能指向云元数据服务")
    if not allow_private and any(
            not _is_public_address(address) for address in parsed_addresses):
        raise ConfigError("接口地址必须解析到公网地址")

    if literal is not None:
        url_hostname = f"[{hostname}]" if literal.version == 6 else hostname
    else:
        url_hostname = hostname
    netloc = url_hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{port}"
    normalized_url = urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))
    return ResolvedAIEndpoint(normalized_url, hostname, port, addresses)


def validate_base_url(value: str) -> str:
    """Resolve and validate the URL; the connection factory pins these answers."""
    return _resolve_ai_endpoint(value).url


def create_ai_http_client(base_url: str, *, timeout: float | int = 300) -> httpx.Client:
    """Build a redirect-free client whose TCP connections use validated DNS answers."""
    endpoint = _resolve_ai_endpoint(base_url)
    transport = _PinnedHTTPTransport(endpoint)
    return httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                        timeout=timeout)


def _env_llm() -> dict:
    """服务器 .env 里那一层。"""
    return {
        "api_key": config.LLM_API_KEY or "",
        "base_url": config.LLM_BASE_URL or "",
        "model": config.LL_MODEL or "",
        "timeout": 300,
        "enabled": True,
    }


def _coerce_llm(data: dict) -> dict:
    cfg = dict(data or {})
    cfg["api_key"] = str(cfg.get("api_key") or "")
    cfg["base_url"] = str(cfg.get("base_url") or config.LLM_BASE_URL or "")
    cfg["model"] = str(cfg.get("model") or config.LL_MODEL or "deepseek-chat")
    try:
        cfg["timeout"] = int(cfg.get("timeout") or 300)
    except (TypeError, ValueError):
        cfg["timeout"] = 300
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def _llm_ready(cfg: dict) -> bool:
    return bool(cfg.get("api_key") and cfg.get("base_url"))


def resolve_llm() -> dict:
    """
    解析服务端最终生效的模型配置。

    返回 api_key/base_url/model/timeout/ready/source（source ∈ ui|env|none）。
    用户请求里自带的 Key 优先级更高，本函数只负责服务端这一层。
    """
    data = raw(NAME_LLM)
    if data and data.get("enabled", True):
        cfg = _coerce_llm(data)
        if _llm_ready(cfg):
            cfg.update({"source": "ui", "ready": True})
            return cfg

    env = _coerce_llm(_env_llm())
    if _llm_ready(env):
        env.update({"source": "env", "ready": True})
        return env

    env.update({"source": "none", "ready": False})
    return env


def public_llm() -> dict:
    """给管理界面的只读视图。任何情况下都不含 API Key 明文。"""
    data = raw(NAME_LLM)
    cfg = resolve_llm()
    models = data.get("models") if isinstance(data.get("models"), list) else []
    return {
        "api_key_set": bool(cfg.get("api_key")),
        "api_key_mask": _MASK if cfg.get("api_key") else "",
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "timeout": cfg.get("timeout", 300),
        "enabled": bool(cfg.get("enabled", True)),
        "source": cfg.get("source", "none"),
        "ready": bool(cfg.get("ready")),
        "env_available": _llm_ready(_coerce_llm(_env_llm())),
        "models": [str(item) for item in models][:200],
        "models_fetched_at": str(data.get("models_fetched_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
        "updated_by": str(data.get("updated_by") or ""),
    }


def merge_llm(current: dict, payload: dict, operator: str = "", *, default_model=None) -> dict:
    from config_rules import merge_llm as merge
    return merge(current, payload, operator, default_model=default_model, validate_base_url=validate_base_url)


def save_llm(payload: dict, operator: str = "") -> dict:
    """保存界面配置。API Key 留空或缺省时沿用旧值。"""
    _write(NAME_LLM, merge_llm(raw(NAME_LLM), payload, operator, default_model=config.LL_MODEL))
    return public_llm()


def store_models(models, operator: str = "") -> dict:
    """缓存「获取模型列表」的结果，刷新页面后下拉框还能用。"""
    data = dict(raw(NAME_LLM))
    cleaned = []
    for item in models or []:
        text = str(item or "").strip()
        if text and len(text) <= 200 and text not in cleaned:
            cleaned.append(text)
    data["models"] = cleaned[:200]
    data["models_fetched_at"] = _now_iso()
    if operator:
        data["updated_by"] = operator
    _write(NAME_LLM, data)
    return public_llm()


def clear_llm(operator: str = "") -> dict:
    """清空界面配置，立即回退到 .env。"""
    path = _path(NAME_LLM)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(f"清空配置失败：{exc}")
    _CACHE.pop(path, None)
    if operator:
        logger.info("llm config cleared by %s", operator)
    return public_llm()

