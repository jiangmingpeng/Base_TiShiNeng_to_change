"""Header 顺序对拍工具：本地起一个原始 socket 服务，抓真实发送的字节顺序。

用法::

    .venv/bin/python tools/verify_headers.py

它不依赖 mitmproxy，也不需要联网：请求打到 127.0.0.1 上的一次性 HTTP 服务，
由服务端原样打印 curl 实际发出去的 header 顺序。用来验收 P0-2：

    期望 Host -> Connection -> Accept-Encoding -> User-Agent -> Content-Type

退出码 0 表示顺序与 OkHttp4 一致。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tsn_fingerprint as fp  # noqa: E402

EXPECTED = list(fp.OKHTTP_HEADER_ORDER)


class RawCapture:
    def __init__(self) -> None:
        self.headers: list[str] = []
        self.raw: bytes = b''

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b'\r\n\r\n')
        self.raw = head
        lines = head.decode('latin-1').split('\r\n')
        self.headers = [line.split(':', 1)[0] for line in lines[1:] if ':' in line]
        body = b'{"ok":true}'
        writer.write(
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
            + str(len(body)).encode()
            + b'\r\nConnection: close\r\n\r\n'
            + body
        )
        await writer.drain()
        writer.close()


async def main() -> int:
    capture = RawCapture()
    server = await asyncio.start_server(capture.handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    session = fp.TsnHttpSession(proxy=None)
    try:
        resp = await session.post(
            f'http://127.0.0.1:{port}/addExerciseRecord',
            data={'a': '1', 'b': 'two words'},
            headers=fp.build_headers(f'127.0.0.1:{port}'),
        )
        assert resp.status_code == 200, resp.status_code
        assert resp.json() == {'ok': True}
    finally:
        await session.aclose()
        server.close()
        await server.wait_closed()

    got = capture.headers
    print('实际请求行/头:')
    for line in capture.raw.decode('latin-1').split('\r\n'):
        if line:
            print('  ' + line)
    print()
    print('header 顺序:', ' -> '.join(got))
    print('OkHttp4 期望:', ' -> '.join(EXPECTED))

    ok = got[: len(EXPECTED)] == EXPECTED
    if 'User-Agent' in got:
        agent_line = [line for line in capture.raw.decode('latin-1').split('\r\n') if line.lower().startswith('user-agent:')]
        if agent_line:
            ok = ok and 'okhttp/4.12.0' in agent_line[0]
    print()
    print('结果:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
