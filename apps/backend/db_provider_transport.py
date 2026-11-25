"""Public-only provider transports. No environment, config, or JSON-store reads."""
from dataclasses import dataclass
import ipaddress
import re
import smtplib
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx


class EndpointError(ValueError):
    pass


_METADATA = {'169.254.169.254', '100.100.100.200', 'fd00:ec2::254', 'metadata.google.internal'}
_NAT64 = ipaddress.ip_network('64:ff9b::/96')


def public_address(value):
    address = ipaddress.ip_address(value)
    if address.is_multicast or str(address) in _METADATA:
        return False
    if address.version == 6:
        mapped = address.ipv4_mapped or address.sixtofour
        if mapped is not None:
            return public_address(mapped)
        if address.teredo is not None:
            return all(public_address(part) for part in address.teredo)
        if address in _NAT64:
            return public_address(ipaddress.IPv4Address(int(address) & 0xffffffff))
    return address.is_global


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    addresses: tuple
    url: str = ''


def resolve_host(host, port):
    if (not isinstance(host, str) or not host or len(host) > 253
            or any(char.isspace() or ord(char) < 32 for char in host)
            or type(port) is not int or not 1 <= port <= 65535):
        raise EndpointError('Invalid provider endpoint')
    host = host.rstrip('.').lower()
    if host in _METADATA:
        raise EndpointError('Provider endpoint is forbidden')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            raise EndpointError('Invalid provider hostname') from None
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or len(host) > 253:
            raise EndpointError('Invalid provider hostname')
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = tuple(dict.fromkeys(str(ipaddress.ip_address(answer[4][0])) for answer in answers))
        except (OSError, ValueError, IndexError, TypeError):
            raise EndpointError('Provider DNS resolution failed') from None
    else:
        host = str(address)
        addresses = (str(address),)
    if not addresses or not all(public_address(value) for value in addresses):
        raise EndpointError('Provider endpoint must resolve only to public unicast addresses')
    return Endpoint(host, port, addresses)


def resolve_https(url):
    if (not isinstance(url, str) or not url or len(url) > 500
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or '?' in url or '#' in url or '\\' in url):
        raise EndpointError('Invalid provider URL')
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError()
        endpoint = resolve_host(parsed.hostname, parsed.port if parsed.port is not None else 443)
    except (ValueError, UnicodeError):
        raise EndpointError('A public HTTPS provider URL is required') from None
    host = '[' + endpoint.host + ']' if ':' in endpoint.host else endpoint.host
    netloc = host + (':' + str(endpoint.port) if parsed.port is not None else '')
    normalized = urlunsplit(('https', netloc, parsed.path.rstrip('/'), '', ''))
    return Endpoint(endpoint.host, endpoint.port, endpoint.addresses, normalized)


class PinnedBackend(httpcore.SyncBackend):
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        endpoint = self.endpoint
        if host.rstrip('.').lower() != endpoint.host or port != endpoint.port:
            raise httpcore.ConnectError('Provider origin differs from validated endpoint')
        last = None
        for address in endpoint.addresses:
            try:
                return super().connect_tcp(address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last = error
        raise httpcore.ConnectError('Provider connection failed') from last


class PinnedTransport(httpx.HTTPTransport):
    def __init__(self, endpoint):
        super().__init__(trust_env=False, retries=0)
        if not hasattr(self._pool, '_network_backend'):
            self.close()
            raise EndpointError('Unsupported pinned HTTP transport')
        self._pool._network_backend = PinnedBackend(endpoint)


def http_client(endpoint, *, timeout):
    return httpx.Client(transport=PinnedTransport(endpoint), trust_env=False,
                        follow_redirects=False, timeout=timeout)


def smtp_client(endpoint, *, security, timeout):
    """Preserve TLS hostname verification while dialing validated literal IPs."""
    if security not in {'ssl', 'starttls'}:
        raise EndpointError('SMTP requires verified TLS')
    context = ssl.create_default_context()

    def dial(host, port, duration):
        if host != endpoint.host or port != endpoint.port:
            raise EndpointError('SMTP origin differs from validated endpoint')
        for address in endpoint.addresses:
            try:
                return socket.create_connection((address, port), timeout=duration)
            except OSError:
                pass
        raise EndpointError('SMTP connection failed')

    class PublicSMTP(smtplib.SMTP):
        def _get_socket(self, host, port, duration):
            return dial(host, port, duration)

    class PublicSMTPSSL(smtplib.SMTP_SSL):
        def _get_socket(self, host, port, duration):
            raw = dial(host, port, duration)
            try:
                return context.wrap_socket(raw, server_hostname=host)
            except BaseException:
                raw.close()
                raise

    client = None
    try:
        client = (PublicSMTPSSL(endpoint.host, endpoint.port, timeout=timeout, context=context)
                  if security == 'ssl' else PublicSMTP(endpoint.host, endpoint.port, timeout=timeout))
        client.ehlo()
        if security == 'starttls':
            client.starttls(context=context)
            client.ehlo()
        return client
    except BaseException:
        if client is not None:
            client.close()
        raise
