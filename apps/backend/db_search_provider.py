"""Explicit bounded public HTTPS search; never logs queries or secrets."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ipaddress
import json
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit, unquote

from db_provider_transport import http_client, resolve_https
from tools import web_search as parsers


def _result_url(raw):
    if not isinstance(raw, str) or len(raw) > 2000 or any(ord(c) < 32 for c in raw):
        return ''
    if raw.startswith('//'):
        raw = 'https:' + raw
    value = urlsplit(raw)
    if value.scheme not in {'http', 'https'} or not value.hostname or value.username or value.password:
        return ''
    if value.hostname.lower() in {'localhost', 'metadata.google.internal'}:
        return ''
    try:
        if not ipaddress.ip_address(value.hostname).is_global:
            return ''
    except ValueError:
        pass
    return raw[:500]


class SQLSearchProvider:
    def __init__(self, config=None):
        config = deepcopy(config or {})
        if type(config) is not dict or set(config) - {'endpoint', 'api_key'}:
            raise ValueError('Invalid explicit search configuration')
        self.endpoint = config.get('endpoint', '')
        self.api_key = config.get('api_key', '')
        if (not isinstance(self.endpoint, str) or not isinstance(self.api_key, str)
                or len(self.api_key) > 4096 or any(ord(c) < 32 for c in self.api_key)
                or (self.api_key and not self.endpoint)):
            raise ValueError('Invalid search provider credentials')
        if self.endpoint:
            value = urlsplit(self.endpoint)
            if (value.scheme != 'https' or not value.hostname or value.username or value.password
                    or value.query or value.fragment or len(self.endpoint) > 500):
                raise ValueError('Explicit HTTPS search endpoint required')

    def _fetch(self, address, *, params=None, payload=None, key='', redirects=0):
        endpoint = resolve_https(address)
        headers = {'User-Agent': parsers._UA, 'Accept-Encoding': 'identity'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        with http_client(endpoint, timeout=8) as client:
            with client.stream('POST' if payload is not None else 'GET', endpoint.url,
                               params=params, json=payload, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308} and payload is None and redirects < 2:
                    target = urlsplit(urljoin(str(response.url), response.headers.get('Location', '')))
                    if target.hostname not in {'cn.bing.com', 'www.bing.com', 'html.duckduckgo.com', 'duckduckgo.com'}:
                        raise ValueError('Unexpected search redirect')
                    return self._fetch(urlunsplit((target.scheme, target.netloc, target.path, '', '')),
                        params=parse_qsl(target.query), redirects=redirects + 1)
                if response.status_code != 200 or response.headers.get('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
                    raise ValueError('Search response unavailable')
                body = bytearray()
                for chunk in response.iter_raw(8192):
                    body.extend(chunk)
                    if len(body) > 524288:
                        raise ValueError('Search response exceeded limit')
                return bytes(body).decode('utf-8', errors='replace')

    def _one(self, query):
        if self.endpoint:
            try:
                raw = json.loads(self._fetch(self.endpoint, payload={'query': query, 'max_results': 3}, key=self.api_key))
                items = raw.get('results') if isinstance(raw, dict) else raw
                if not isinstance(items, list):
                    return None
                found = []
                for item in items[:3]:
                    if not isinstance(item, dict):
                        continue
                    url = _result_url(item.get('url') or item.get('link'))
                    if url:
                        found.append({'title': str(item.get('title') or '')[:160], 'url': url,
                                      'snippet': str(item.get('snippet') or item.get('description') or '')[:300]})
                return found or None
            except Exception:
                # Explicit/custom endpoint never falls back to a public service.
                return None
        for endpoint, params, links, snippets in (
            ('https://cn.bing.com/search', {'q': query, 'setlang': 'zh-hans', 'setmkt': 'zh-CN', 'count': '6'}, parsers._BING_LINK_RE, parsers._BING_SNIPPET_RE),
            ('https://html.duckduckgo.com/html/', {'q': query, 'kl': 'cn-zh'}, parsers._DDG_LINK_RE, parsers._DDG_SNIPPET_RE)):
            try:
                body = self._fetch(endpoint, params=params)
                descriptions, found = snippets.findall(body), []
                for index, (href, title) in enumerate(links.findall(body)[:3]):
                    parsed = urlsplit('https:' + href if href.startswith('//') else href)
                    redirect = dict(parse_qsl(parsed.query)).get('uddg')
                    url = _result_url(unquote(redirect) if redirect else parsers._unescape(href))
                    if url:
                        found.append({'title': parsers._unescape(parsers._strip_tags(title))[:160], 'url': url,
                            'snippet': parsers._unescape(parsers._strip_tags(descriptions[index]))[:300] if index < len(descriptions) else ''})
                if found:
                    return found
            except Exception:
                pass
        return None

    def __call__(self, queries):
        if (not isinstance(queries, list) or len(queries) > 3 or
                any(not isinstance(q, str) or not q.strip() or len(q) > 200 for q in queries)):
            raise ValueError('Bounded explicit company queries required')
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(self._one, queries))
