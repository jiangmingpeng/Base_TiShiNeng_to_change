"""统一出口 HTTP 层：OkHttp4 指纹对齐 + 出口绑定 + 节流。

设计约束（对应《抗风控加强方案》P0-2）：

1. 业务层不得再直接构造 ``httpx.AsyncClient`` / ``requests.Session``，
   所有请求收敛到 :class:`TsnHttpSession`。
2. Header 发送顺序固定为 OkHttp4 真实顺序：
   ``Host`` -> ``Connection`` -> ``Accept-Encoding`` -> ``User-Agent``
   -> ``Content-Type``（文件字段由 curl 追加 ``Content-Length``）。
3. ``Accept-Encoding`` 只能是 ``gzip``；UA 只能是 ``okhttp/4.12.0``；
   ``Connection`` 只能是 ``Keep-Alive``（大小写也是指纹的一部分）。
4. 时间戳统一 13 位毫秒（:func:`now_ms`），JSON 一律紧凑分隔符
   ``(",", ":")``，表单空格编码为 ``+``（:func:`compact_json` /
   :func:`form_encode`）。
5. TLS 从 Python ``ssl`` 换成 libcurl（BoringSSL/OpenSSL）后端，
   避免 ``python-httpx/x.y`` 这类可直接判死的 ClientHello。

已知边界：libcurl 无法改写 TLS1.3 cipher suite 顺序，也无法设置
Conscrypt 的完整 extension 顺序；需要 100% 复刻时必须用 uTLS
（``pyutls``）或真机抓包得到的 ClientHello。:func:`describe` 会把
当前实际生效的配置打印出来，便于 mitmproxy / tlsfingerprint.io 对拍。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

OKHTTP_UA = 'okhttp/4.12.0'
OKHTTP_VERSION_NAME = '2.0.23'

#: OkHttp4 直连时的 header 顺序（顺序本身就是指纹）。
OKHTTP_HEADER_ORDER = ('Host', 'Connection', 'Accept-Encoding', 'User-Agent', 'Content-Type')

#: Conscrypt/OkHttp 在 Android 上的 TLS1.2 cipher 偏好（从前往后优先）。
OKHTTP_CIPHERS_TLS12 = ':'.join(
    [
        'ECDHE-ECDSA-AES128-GCM-SHA256',
        'ECDHE-RSA-AES128-GCM-SHA256',
        'ECDHE-ECDSA-AES256-GCM-SHA384',
        'ECDHE-RSA-AES256-GCM-SHA384',
        'ECDHE-ECDSA-CHACHA20-POLY1305',
        'ECDHE-RSA-CHACHA20-POLY1305',
        'ECDHE-ECDSA-AES128-SHA',
        'ECDHE-RSA-AES128-SHA',
        'ECDHE-ECDSA-AES256-SHA',
        'ECDHE-RSA-AES256-SHA',
        'AES128-GCM-SHA256',
        'AES256-GCM-SHA384',
        'AES128-SHA',
        'AES256-SHA',
    ]
)

#: OkHttp 的 HTTP/2 SETTINGS：MAX_CONCURRENT_STREAMS / ENABLE_PUSH /
#: INITIAL_WINDOW_SIZE / MAX_FRAME_SIZE。
OKHTTP_HTTP2_SETTINGS = '2:0;4:16777216;6:16384;'
OKHTTP_HTTP2_WINDOW_UPDATE = '16777216'

DEFAULT_TIMEOUT = 30.0


def now_ms() -> str:
    """13 位毫秒时间戳（秒级时间戳直接被判为伪造）。"""
    return str(int(time.time() * 1000))


def compact_json(payload: Any) -> str:
    """Gson 风格紧凑 JSON：无空格、保留非 ASCII。"""
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)


def form_encode(pairs: Mapping[str, Any] | Sequence[Tuple[str, Any]]) -> str:
    """``application/x-www-form-urlencoded`` 编码，空格编成 ``+``。

    与 ``urllib.parse.urlencode`` 一致，但保持传入顺序，方便逐字节对齐。
    """
    items = pairs.items() if isinstance(pairs, Mapping) else pairs
    parts: List[str] = []
    for key, value in items:
        if value is None:
            value = ''
        parts.append(f'{urllib.parse.quote_plus(str(key))}={urllib.parse.quote_plus(str(value))}')
    return '&'.join(parts)


def build_headers(
    host: str,
    *,
    user_agent: str = OKHTTP_UA,
    content_type: Optional[str] = 'application/x-www-form-urlencoded',
    extra: Optional[Mapping[str, str]] = None,
) -> List[Tuple[str, str]]:
    """构造有序 header 列表（list 而非 dict，顺序不被重排）。"""
    headers: List[Tuple[str, str]] = [
        ('Host', host),
        ('Connection', 'Keep-Alive'),
        ('Accept-Encoding', 'gzip'),
        ('User-Agent', user_agent),
    ]
    if content_type:
        headers.append(('Content-Type', content_type))
    for key, value in (extra or {}).items():
        headers.append((key, value))
    return headers


def headers_to_dict(headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    return {key: value for key, value in headers}


class TsnResponse:
    """对 curl_cffi / httpx 响应的最小统一封装。"""

    __slots__ = ('status_code', 'content', 'text', 'headers', 'url', 'backend')

    def __init__(self, status_code: int, content: bytes, headers: Mapping[str, str], url: str, backend: str) -> None:
        self.status_code = int(status_code)
        self.content = content or b''
        self.headers = dict(headers or {})
        self.url = url
        self.backend = backend
        try:
            self.text = self.content.decode('utf-8', 'replace')
        except Exception:
            self.text = ''

    def json(self) -> Any:
        return json.loads(self.text)

    def __repr__(self) -> str:
        return f'<TsnResponse {self.status_code} {len(self.content)}B via {self.backend}>'


@dataclass
class FingerprintConfig:
    """一次出口会话的指纹参数。"""

    impersonate: str = 'chrome131_android'
    header_order: str = ','.join(OKHTTP_HEADER_ORDER)
    http2: bool = True
    timeout: float = DEFAULT_TIMEOUT
    allow_redirects: bool = True
    ssl_cipher_list: str = OKHTTP_CIPHERS_TLS12
    http2_settings: str = OKHTTP_HTTP2_SETTINGS
    extra_fp: Dict[str, Any] = field(default_factory=dict)

    def as_extra_fp(self):
        from curl_cffi.requests.impersonate import ExtraFingerprints

        base = {
            'header_order': self.header_order,
            'tls_permute_extensions': False,
            'tls_grease': False,
        }
        base.update(self.extra_fp)
        return ExtraFingerprints(**base)


class _CurlCffiBackend:
    name = 'curl_cffi'

    def __init__(self, config: FingerprintConfig, proxy: Optional[str]) -> None:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession

        self._config = config
        options: Dict[Any, Any] = {}
        if config.ssl_cipher_list:
            options[CurlOpt.SSL_CIPHER_LIST] = config.ssl_cipher_list
        if config.http2 and config.http2_settings:
            options[CurlOpt.HTTP2_SETTINGS] = config.http2_settings
        self._session = AsyncSession(
            impersonate=config.impersonate,
            extra_fp=config.as_extra_fp(),
            default_headers=False,
            curl_options=options or None,
            timeout=config.timeout,
            allow_redirects=config.allow_redirects,
            proxy=proxy,
            trust_env=False,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        headers: Optional[Sequence[Tuple[str, str]] | Mapping[str, str]] = None,
        content: Optional[bytes | str] = None,
        timeout: Optional[float] = None,
    ) -> TsnResponse:
        from curl_cffi.const import CurlHttpVersion

        kwargs: Dict[str, Any] = {}
        # OkHttp 在明文 http:// 上不做 h2c 升级（不发 Upgrade/HTTP2-Settings），
        # curl 默认会尝试，那串头本身就是特征。HTTPS 仍走 ALPN h2。
        if url.startswith('http://'):
            kwargs['http_version'] = CurlHttpVersion.V1_1
        if params:
            kwargs['params'] = dict(params)
        payload = dict(data or {})
        if files:
            fields, real_files = _split_files(files)
            payload.update(fields)
            normalized = _normalize_files_curl(real_files)
            if normalized:
                kwargs['files'] = normalized
        if payload:
            kwargs['data'] = payload
        if content is not None:
            kwargs['content'] = content
        if timeout is not None:
            kwargs['timeout'] = timeout
        if headers:
            kwargs['headers'] = list(headers) if not isinstance(headers, Mapping) else dict(headers)
        resp = await self._session.request(method, url, **kwargs)
        return TsnResponse(resp.status_code, resp.content, resp.headers, url, self.name)

    async def aclose(self) -> None:
        await self._session.close()


class _HttpxBackend:
    """无 curl_cffi 时的降级路径：只能对齐 header，无法对齐 TLS。"""

    name = 'httpx'

    def __init__(self, config: FingerprintConfig, proxy: Optional[str]) -> None:
        import httpx

        limits = httpx.Limits(max_keepalive_connections=8, max_connections=16)
        self._client = httpx.AsyncClient(
            timeout=config.timeout,
            follow_redirects=config.allow_redirects,
            proxy=proxy or None,
            limits=limits,
            trust_env=False,
            http2=False,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        headers: Optional[Sequence[Tuple[str, str]] | Mapping[str, str]] = None,
        content: Optional[bytes | str] = None,
        timeout: Optional[float] = None,
    ) -> TsnResponse:
        kwargs: Dict[str, Any] = {}
        if params:
            kwargs['params'] = dict(params)
        payload = dict(data or {})
        if files:
            fields, real_files = _split_files(files)
            payload.update(fields)
            normalized = _normalize_files_httpx(real_files)
            if normalized:
                kwargs['files'] = normalized
        if payload:
            kwargs['data'] = payload
        if content is not None:
            kwargs['content'] = content
        if timeout is not None:
            kwargs['timeout'] = timeout
        if headers:
            kwargs['headers'] = list(headers) if not isinstance(headers, Mapping) else dict(headers)
        resp = await self._client.request(method, url, **kwargs)
        return TsnResponse(resp.status_code, resp.content, resp.headers, url, self.name)

    async def aclose(self) -> None:
        await self._client.aclose()


def _split_files(files: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把 httpx 风格 ``(None, b'..', 'text/plain')`` 的普通字段拆回 data。"""
    fields: Dict[str, Any] = {}
    real_files: Dict[str, Any] = {}
    for key, value in files.items():
        if isinstance(value, (bytes, bytearray)):
            real_files[key] = bytes(value)
            continue
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            filename, payload = value[0], value[1]
            if filename is None:
                fields[key] = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
                continue
            real_files[key] = tuple(value)
            continue
        fields[key] = value
    return fields, real_files


def _normalize_files_curl(files: Mapping[str, Any]) -> Dict[str, Any]:
    _, real_files = _split_files(files)
    out: Dict[str, Any] = {}
    for key, value in real_files.items():
        if isinstance(value, (bytes, bytearray)):
            out[key] = bytes(value)
            continue
        filename = value[0] or 'blob'
        if len(value) >= 3 and value[2]:
            out[key] = (filename, value[1], value[2])
        else:
            out[key] = (filename, value[1])
    return out


def _normalize_files_httpx(files: Mapping[str, Any]) -> Dict[str, Any]:
    fields, real_files = _split_files(files)
    # httpx 里 (None, payload, ctype) 是普通字段，调用方已经按 data 传入，
    # 这里只保留真正的文件字段。
    merged = dict(fields)
    merged.update(real_files)
    return merged


class TsnHttpSession:
    """统一出口客户端。

    Parameters
    ----------
    proxy:
        出口代理（``http://user:pass@host:port`` 或 ``socks5://...``）。
        ``None`` 表示直连 —— 直连在 D1/D2 上必中，仅用于本地调试。
    config:
        指纹参数；默认值即 OkHttp4 近似。
    backend:
        ``auto`` / ``curl_cffi`` / ``httpx``。
    """

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        config: Optional[FingerprintConfig] = None,
        backend: str = 'auto',
        rate_limiter: Optional[Any] = None,
        egress_key: str = 'direct',
    ) -> None:
        self.proxy = proxy
        self.config = config or FingerprintConfig()
        self._backend = self._make_backend(backend)
        self._limiter = rate_limiter
        self.egress_key = egress_key
        self.submitted: int = 0

    def _make_backend(self, backend: str):
        if backend in ('auto', 'curl_cffi'):
            try:
                return _CurlCffiBackend(self.config, self.proxy)
            except Exception as exc:  # pragma: no cover - 依赖环境
                if backend == 'curl_cffi':
                    raise
                logger.warning(f'curl_cffi 后端不可用，降级 httpx（TLS 指纹将不匹配）: {exc}')
        return _HttpxBackend(self.config, self.proxy)

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        headers: Optional[Sequence[Tuple[str, str]] | Mapping[str, str]] = None,
        content: Optional[bytes | str] = None,
        timeout: Optional[float] = None,
    ) -> TsnResponse:
        if self._limiter is not None:
            await self._limiter.acquire(self.egress_key)
        self.submitted += 1
        return await self._backend.request(
            method,
            url,
            params=params,
            data=data,
            files=files,
            headers=headers,
            content=content,
            timeout=timeout,
        )

    async def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Sequence[Tuple[str, str]] | Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> TsnResponse:
        return await self.request('GET', url, params=params, headers=headers, timeout=timeout)

    async def post(
        self,
        url: str,
        *,
        data: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        headers: Optional[Sequence[Tuple[str, str]] | Mapping[str, str]] = None,
        content: Optional[bytes | str] = None,
        timeout: Optional[float] = None,
    ) -> TsnResponse:
        return await self.request('POST', url, data=data, params=params, files=files, headers=headers, content=content, timeout=timeout)

    async def aclose(self) -> None:
        await self._backend.aclose()

    async def __aenter__(self) -> 'TsnHttpSession':
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def describe(self) -> Dict[str, Any]:
        """导出当前指纹配置，供 mitmproxy / tlsfingerprint.io 对拍。"""
        return {
            'backend': self.backend_name,
            'proxy': self.proxy or 'direct',
            'impersonate': self.config.impersonate,
            'header_order': list(OKHTTP_HEADER_ORDER),
            'user_agent': OKHTTP_UA,
            'accept_encoding': 'gzip',
            'connection': 'Keep-Alive',
            'http2_settings': self.config.http2_settings,
            'ssl_cipher_list': self.config.ssl_cipher_list,
            'timestamp_format': 'ms-13',
            'json_separators': [',', ':'],
        }


def build_session_matrix(describe_out: Optional[Mapping[str, Any]] = None) -> str:
    """把 :meth:`TsnHttpSession.describe` 渲染成对拍表（markdown）。"""
    data = dict(describe_out or {})
    rows = [
        ('backend', data.get('backend')),
        ('proxy', data.get('proxy')),
        ('impersonate', data.get('impersonate')),
        ('header order', ' -> '.join(data.get('header_order') or [])),
        ('User-Agent', data.get('user_agent')),
        ('Accept-Encoding', data.get('accept_encoding')),
        ('Connection', data.get('connection')),
        ('h2 SETTINGS', data.get('http2_settings')),
        ('时间戳', data.get('timestamp_format')),
    ]
    return '\n'.join(f'| {name} | {value} |' for name, value in rows)
