# -*- coding: utf-8 -*-
"""不可信内容的统一处理：间接提示注入（IPI）、外泄通道、跨站写入。

威胁模型
--------
简历、岗位描述、网页检索结果都是**攻击者可影响的文本**。它们会进入模型提示词，
也可能被拼进搜索查询。三条必须守住的线：

1. **结构**：不可信文本不能伪造我们自己的分节标题、不能提前关闭 Markdown 围栏，
   否则它后面那句"忽略以上指令"就落到了指令区。
2. **边界**：围栏必须带**每次请求随机生成的 nonce**。静态围栏（```text / 外部数据开始）
   一旦内容里出现同名字符串就能被提前闭合，nonce 让这种闭合不可构造。
3. **外泄**：模型输出与检索查询可能被注入内容操纵。查询要剥掉 URL / 邮箱 / 超长载荷，
   避免把简历内容当成"搜索词"送到第三方；渲染侧不自动加载远程资源（由前端保证）。

这里只做**确定性的文本处理**，不调用模型、不读配置、不写文件，便于单测与复用。
"""
from __future__ import annotations

import re
import secrets
from typing import Any, Iterable
from urllib.parse import urlsplit

# ── 字符层 ────────────────────────────────────────────────────────────────
# 零宽 / 双向控制 / 软连字符：肉眼看不见，但能让"安全声明""系统消息"的字面匹配失效
# （"安\u200b全声明"），也能在结果里视觉反转语义顺序。
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad]")
# 其余控制字符（保留 \n \r \t）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ── 结构层 ────────────────────────────────────────────────────────────────
# Markdown 围栏：简历里写一行 ``` 就能提前关掉"候选人简历：```text ... ```"。
_MD_FENCE_RE = re.compile(r"`{2,}|~{3,}")
# 我们自己的分节标记 / 外部数据围栏 / 模板里的标签行。
_MARKER_RE = re.compile(
    r"[【\[]{0,2}\s*(?:目标岗位|候选人简历|外部检索数据|外部数据|安全声明|系统消息|"
    r"外部数据开始|外部数据结束)\s*[】\]]{0,2}"
    r"|^[ \t]*(?:Job\s*Description|Original\s*Resume|Candidate\s*Resume)\s*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# 我们生成的围栏定界符，内容里出现即剥离，避免伪造闭合。
_FENCE_TOKEN_RE = re.compile(r"<<<\s*(?:DATA|END)\b|>>>", re.I)

_MARKER_REPLACEMENT = "[已中和]"


def _strip_invisible(text: str) -> str:
    return _INVISIBLE_RE.sub("", text)


def neutralize_structure(text: Any, *, extra_tokens: Iterable[str] = ()) -> str:
    """拆掉不可信文本里的结构伪造：围栏、分节标记、定界符、控制字符。

    只改结构不改语义——正文内容全部保留，符合"简历就是数据"的前提。
    """
    value = _strip_invisible(str(text if text is not None else ""))
    value = _CONTROL_RE.sub(" ", value)
    value = _MD_FENCE_RE.sub(lambda m: "'" * min(len(m.group(0)), 4), value)
    value = _MARKER_RE.sub(_MARKER_REPLACEMENT, value)
    value = _FENCE_TOKEN_RE.sub("[已中和]", value)
    for token in extra_tokens:
        if token:
            value = value.replace(token, "[已中和]")
    return value


def fence_data(text: Any, label: str, *, nonce: str | None = None, limit: int | None = None) -> str:
    """把不可信文本包进带随机 nonce 的边界，返回可直接放进提示词的字符串。

    返回格式（nonce 每次随机，内容无法构造出闭合行）::

        <<<DATA 3f9a… resume>>>
        ……正文……
        <<<END 3f9a…>>>
    """
    token = nonce or secrets.token_hex(8)
    safe_label = re.sub(r"[^a-z0-9_-]", "", (label or "data").lower())[:32] or "data"
    body = neutralize_structure(text, extra_tokens=(token,))
    if limit is not None and len(body) > limit:
        body = body[:limit] + "\n…（内容已截断）"
    return f"<<<DATA {token} {safe_label}>>>\n{body}\n<<<END {token}>>>"


# ── 注入意图筛查（只记录，不拦截）─────────────────────────────────────────
# 命中不等于攻击：正常的简历也可能写"负责系统消息推送"。所以这些信号只用于审计日志，
# 不用于拒绝请求，避免误伤把真实投递挡在门外。
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override_zh", re.compile(r"(?:忽略|无视|忘记|抛开).{0,8}(?:以上|之前|上面|前面|上述|以上所有).{0,8}(?:指令|提示|要求|规则|设定)")),
    ("override_en", re.compile(r"(?i)ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|above|prior|earlier)\s+(?:instructions?|prompts?|rules?)")),
    ("role_hijack", re.compile(r"(?:你现在是|你现在扮演|从现在开始你是|from\s+now\s+on\s+you\s+are|you\s+are\s+now\s+a)")),
    ("fake_system", re.compile(r"(?:系统消息|系统提示|system\s*prompt|system\s*message|developer\s*message)")),
    ("score_tamper", re.compile(
        r"(?:把|将)?\s*(?:分数|评分|最终得分|满分|打分|final\s*score|job_fit_score)[^\n]{0,12}"
        r"(?:改成|设为|改为|调整到|必须为|set\s+to)"
        r"|(?:给|打)\s*(?:满分|高分|9[05]|100)\b"
    )),
    ("exfil_secret", re.compile(r"(?:泄露|泄漏|透露|输出|打印).{0,10}(?:密钥|密码|令牌|token|api[_-]?key|环境变量|配置)")),
    ("tool_hijack", re.compile(r"(?:调用|使用|执行).{0,8}(?:工具|搜索|浏览器|http|curl|命令)")),
    ("output_override", re.compile(r"(?:只|仅)输出(?:以下|如下|下面)|(?:必须|务必)输出(?:以下|如下)内容")),
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BASE64ISH_RE = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")


def screen_untrusted(text: Any) -> dict:
    """返回注入/外泄信号的命中情况，供审计日志使用（不改变文本、不拦截）。"""
    value = _strip_invisible(str(text if text is not None else ""))
    hits = {name: len(pattern.findall(value)) for name, pattern in _INJECTION_PATTERNS}
    hits = {name: count for name, count in hits.items() if count}
    return {
        "flags": sorted(hits),
        "counts": hits,
        "urls": len(_URL_RE.findall(value)),
        "emails": len(_EMAIL_RE.findall(value)),
        "long_tokens": len(_BASE64ISH_RE.findall(value)),
    }


def prepare_untrusted(text: Any, label: str, *, limit: int | None = None,
                      nonce: str | None = None) -> dict:
    """一次完成：筛查（审计）+ 中和（结构）+ 围栏（边界）。

    调用方把 ``fenced`` 放进提示词占位符，把 ``screening`` 写进日志即可。
    """
    return {
        "fenced": fence_data(text, label, nonce=nonce, limit=limit),
        "screening": screen_untrusted(text),
        "label": label,
    }


# ── 外泄通道：检索查询 ────────────────────────────────────────────────────
_MAX_QUERY_CHARS = 80


def sanitize_search_query(query: Any) -> str | None:
    """把检索查询限制成"公司名/关键词"，剥掉一切可当外泄载荷的东西。

    注入内容常把 URL、邮箱或长 base64 塞进"公司名"，一旦拼进搜索查询就等于把简历
    内容送到了第三方搜索引擎。这里只保留短、干净、含字母或中文的查询。
    """
    value = neutralize_structure(query)
    value = _URL_RE.sub(" ", value)
    value = _EMAIL_RE.sub(" ", value)
    value = _BASE64ISH_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" \t-—_,;:|")
    if not value or len(value) > _MAX_QUERY_CHARS:
        return None
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", value):
        return None
    return value


# ── 跨站写入（CSRF）──────────────────────────────────────────────────────
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 主机名白名单：字母、数字、点、连字符。**不接受** `$`、`{}` 等字符——否则
# `https://${HOST}` 这类未展开的变量表达式会被当成合法 hostname 混过校验。
_HOSTNAME_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")
_IPV6_RE = re.compile(r"^[0-9a-f:.]+$")


def normalize_origin(value: Any) -> str | None:
    """把一个 Origin 规范化成 ``scheme://host[:port]``；无法规范化时返回 None。

    只接受绝对 http/https 源，且**不含**路径、查询串、片段或用户信息——配置项和请求头
    都走同一套规范化，保证比较是严格的字符串相等，而不是前缀或通配匹配。
    主机名必须是合法的 ASCII 主机名或 IPv6 字面量，未展开的 `${...}` 一律拒绝。
    """
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("/")
    if not text or text.lower() == "null":
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 字面量
        if not _IPV6_RE.fullmatch(host):
            return None
        host = f"[{host}]"
    elif not _HOSTNAME_RE.fullmatch(host):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and not 0 < port < 65536:
        return None
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")


def cross_origin_blocked(method: str, origin: str | None, trusted_origins: Iterable[str],
                         sec_fetch_site: str | None = None) -> bool:
    """判断一个写请求是否必须被拒绝。

    认证只用 ``Authorization: Bearer``（不落 Cookie），经典 CSRF 本就缺少自动携带的
    凭据；这道闸是纵深防御，规则是**白名单 + 严格相等**：

    - 写方法（POST/PUT/PATCH/DELETE）且 ``Sec-Fetch-Site: cross-site`` → 拒绝
    - ``Origin`` 规范化后不在 ``trusted_origins`` 里 → 拒绝（含 ``null``、同名不同端口）
    - 不带 ``Origin`` 的非浏览器客户端（CLI / 测试 / 服务间调用）→ 放行，这是设计行为

    这里**不看** ``request.host``，也**不看** ``X-Forwarded-Host``：反向代理（包括
    Next.js rewrite 的 ``changeOrigin``）会把上游 Host 改成后端地址，Host 既不可靠，
    又由客户端或代理可控，不能当作可信来源。
    """
    verb = (method or "").upper()
    if verb not in _UNSAFE_METHODS:
        return False
    if (sec_fetch_site or "").lower() == "cross-site":
        return True
    if not (origin or "").strip():
        return False
    normalized = normalize_origin(origin)
    if normalized is None:
        return True
    allowed = {item for item in (normalize_origin(entry) for entry in trusted_origins) if item}
    return normalized not in allowed


# ── 响应安全头 ────────────────────────────────────────────────────────────
# API 只返回 JSON，所以可以上最严格的 CSP；页面级 CSP 由反向代理对 HTML 单独下发。
API_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}
