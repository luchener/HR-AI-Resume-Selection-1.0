"""
可选的 Web Search 工具（Agent 工具调用）。

设计目标：
- 零依赖：只用标准库 urllib，不引入 requests/httpx。
- 优雅降级：任何异常（网络失败、超时、被限流、解析失败）都返回 None，
  调用方必须能安全地继续，绝不因搜索失败而中断 Agent 主流程。
- 默认关闭：只有 runtime_config / agent_config 显式开启时才发起网络请求。

端点策略（重要）：
- 默认使用 Bing HTML 端点（cn.bing.com/search，国内服务器实测可达，无需 API Key）。
- DuckDuckGo HTML 端点（html.duckduckgo.com）在部分网络（含国内）被墙/仅解析 IPv6，
  仅在 Bing 失败时作为备选回退。实测国内服务器 Bing HTTP 200 / DDG 000。
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 8.0
_MAX_RESULTS = 3
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_BING_ENDPOINT = "https://cn.bing.com/search"
_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"

# Bing HTML：<li class="b_algo"><h2><a href="...">标题</a></h2>...<p>摘要</p>
_BING_LINK_RE = re.compile(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET_RE = re.compile(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', re.S)
# DuckDuckGo HTML
_DDG_LINK_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text or "")).strip()


def _unescape(text: str) -> str:
    """解码 HTML 实体 + 百分号编码（保留中文）。"""
    text = html.unescape(text or "")
    return urllib.parse.unquote(urllib.parse.unquote_plus(text))


def _valid_url(url: str) -> str:
    """清洗搜索结果 URL：仅保留 http/https，防 javascript: 等伪协议注入提示词。"""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    return url[:500]


def _bing_search(query: str, max_results: int) -> list[dict] | None:
    """Bing HTML 端点搜索（国内服务器可达）。失败返回 None。"""
    params = urllib.parse.urlencode({
        "q": query,
        "setlang": "zh-hans",
        "setmkt": "zh-CN",
        "count": str(max_results * 2),
    })
    request = urllib.request.Request(
        f"{_BING_ENDPOINT}?{params}",
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
    )
    with urllib.request.urlopen(request, timeout=_SEARCH_TIMEOUT) as response:
        raw = response.read().decode("utf-8", errors="replace")
    links = _BING_LINK_RE.findall(raw)
    snippets = _BING_SNIPPET_RE.findall(raw)
    results: list[dict] = []
    for index, (raw_href, raw_title) in enumerate(links[:max_results]):
        url = _valid_url(raw_href.strip())
        if not url:
            continue
        snippet = _strip_tags(snippets[index]) if index < len(snippets) else ""
        results.append({
            "title": _unescape(_strip_tags(raw_title))[:160],
            "url": url,
            "snippet": _unescape(snippet)[:300],
        })
    return results or None


def _ddg_search(query: str, max_results: int) -> list[dict] | None:
    """DuckDuckGo HTML 端点搜索（备选回退）。失败返回 None。"""
    params = urllib.parse.urlencode({"q": query, "kl": "cn-zh"})
    request = urllib.request.Request(
        f"{_DDG_ENDPOINT}?{params}",
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
    )
    with urllib.request.urlopen(request, timeout=_SEARCH_TIMEOUT) as response:
        raw = response.read().decode("utf-8", errors="replace")
    links = _DDG_LINK_RE.findall(raw)
    snippets = _DDG_SNIPPET_RE.findall(raw)
    results: list[dict] = []
    for index, (raw_href, raw_title) in enumerate(links[:max_results]):
        href = raw_href.strip()
        # DuckDuckGo 的跳转链接形如 //duckduckgo.com/l/?uddg=<encoded>&rut=...
        match = re.search(r"[?&]uddg=([^&]+)", href)
        if match:
            href = urllib.parse.unquote(urllib.parse.unquote_plus(match.group(1)))
        elif href.startswith("//"):
            href = "https:" + href
        url = _valid_url(href)
        if not url:
            continue
        snippet = _strip_tags(snippets[index]) if index < len(snippets) else ""
        results.append({
            "title": _unescape(_strip_tags(raw_title))[:160],
            "url": url,
            "snippet": _unescape(snippet)[:300],
        })
    return results or None


def _custom_search(query: str, endpoint: str, api_key: str | None, max_results: int) -> list[dict] | None:
    """通用搜索端点（可选）：POST {"query": ...}，期望返回 {"results":[{title,url,snippet}]}。"""
    payload = json.dumps({"query": query, "max_results": max_results}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": _UA}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_SEARCH_TIMEOUT) as response:
        raw = json.loads(response.read().decode("utf-8", errors="replace"))
    raw_results = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(raw_results, list):
        return None
    results = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        url = _valid_url(str(item.get("url") or item.get("link") or ""))
        results.append({
            "title": str(item.get("title") or "")[:160],
            "url": url,
            "snippet": str(item.get("snippet") or item.get("description") or "")[:300],
        })
    return results or None


def search_web(query: str, *, max_results: int = _MAX_RESULTS, search_config: dict | None = None) -> list[dict] | None:
    """执行一次 Web 搜索。任何失败返回 None（绝不抛异常）。

    端点策略：默认 Bing（国内可达）；DDG 作为 Bing 失败后的备选。
    search_config: 可选 {"endpoint": "...", "api_key": "..."}，用于通用搜索端点。
    """
    query = str(query or "").strip()
    if not query or len(query) > 200:
        return None
    try:
        config = search_config if isinstance(search_config, dict) else {}
        endpoint = str(config.get("endpoint") or "").strip()
        api_key = str(config.get("api_key") or "").strip() or None
        if endpoint:
            return _custom_search(query, endpoint, api_key, max_results)
        results = _bing_search(query, max_results)
        if results:
            return results
        logger.info("bing empty for query=%s…, fallback to ddg", query[:40])
        return _ddg_search(query, max_results)
    except Exception as exc:  # 网络/解析/限流一律静默降级
        logger.info("web search failed (query=%s…): %s", query[:40], exc)
        try:
            return _ddg_search(query, max_results)
        except Exception:
            return None


def search_web_parallel(queries: list[str], *, max_results: int = _MAX_RESULTS, search_config: dict | None = None) -> list[dict | None]:
    """并行执行多个搜索，返回与 queries 同序的结果列表（失败项为 None）。"""
    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=min(3, len(queries))) as executor:
        futures = [
            executor.submit(search_web, query, max_results=max_results, search_config=search_config)
            for query in queries
        ]
        return [future.result() for future in futures]


def summarize_results(results: list[dict] | None, *, limit: int = 3) -> str:
    """把搜索结果压缩成给 LLM 的辅助文本；无结果时返回空串。"""
    if not results:
        return ""
    lines = []
    for item in results[:limit]:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        url = str(item.get("url") or "").strip()
        if title or snippet:
            lines.append(f"- {title}：{snippet}" + (f"（{url}）" if url else ""))
    return "\n".join(lines)