"""可插拔签名层（方案 P2-1）。

线上签名算法与 ``appVersion`` 强绑定：一旦平台换版本，``appId`` /
``appSecret`` / AES 派生方式都可能变。业务层不应该知道这些细节，
只需要 ``SIGNERS['2.0.23:public']`` 拿到一个能签的对象。

实现刻意保持 byte-for-byte 兼容：拼接顺序、是否 URL decode、
AES 输出是否再套 md5，全部沿用原 SDK 的行为，并用
:func:`verify_against_sdk` 做回归。
"""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple

import tsn_payload

APP_ID_PUBLIC = 'c9292ee89d2f49492f983f5931af0d09'
APP_ID_PRIVATE = '791f3afb1da44db3bd0c67ffdfb2394b'
APP_SECRET = 'e8167ef026cbc5e456ab837d9d6d9254'
APP_SIGN = '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3'
VERSION_NAME = '2.0.23'
VERSION_CODE = '20230'

#: 私版开了加密开关后，只有这两个路径走 getSign2 的 AES 拼接。
PRIVATE_ENCRYPTED_PATHS: Tuple[str, ...] = (
    'app/sportRecordSetting/getSetting',
    'app/appSportRecord/appAddSportRecord',
)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


@dataclass
class SignContext:
    """一次签名所需的全部上下文。"""

    token: str
    timestamp: str
    app_id: str = APP_ID_PUBLIC
    app_secret: str = APP_SECRET
    version: str = VERSION_NAME
    is_open_encry: bool = False
    aes_encrypt: Optional[Callable[[str], str]] = None
    token_aes_encrypt: Optional[Callable[[str], str]] = None
    md5: Callable[[str], str] = field(default=md5_hex)

    def derived_app_id(self) -> str:
        if self.aes_encrypt is None:
            raise ValueError('SignContext.aes_encrypt 未注入，无法派生公版 appId')
        return self.md5(self.aes_encrypt(self.app_id + self.token + self.timestamp))

    def derived_app_secret(self) -> str:
        if self.aes_encrypt is None:
            raise ValueError('SignContext.aes_encrypt 未注入，无法派生公版 appSecret')
        return self.md5(self.aes_encrypt(self.token + self.timestamp))


class Signer(Protocol):
    name: str
    version: str

    def sign(self, path: str, params: Mapping[str, Any], ctx: SignContext) -> Dict[str, Any]:
        ...


def _concat(params: Mapping[str, Any]) -> str:
    return tsn_payload.build_auth_info(params)


class PrivateSignerV2023:
    """私版 2.0.23：``md5(sorted k=v & ... & appId=.. & appSecret=..)``。"""

    name = '2.0.23:private'
    version = VERSION_NAME

    def sign(self, path: str, params: Mapping[str, Any], ctx: SignContext) -> Dict[str, Any]:
        data = {k: v for k, v in params.items() if k != 'sign'}
        encrypted = ctx.is_open_encry and path.lstrip('/').startswith(PRIVATE_ENCRYPTED_PATHS)
        if encrypted:
            if ctx.token_aes_encrypt is None:
                raise ValueError('私版加密签名需要 token_aes_encrypt')
            data['appId'] = ctx.md5(ctx.token_aes_encrypt(ctx.app_id + ctx.token + ctx.timestamp))
            data['appSecret'] = ctx.md5(ctx.token_aes_encrypt(ctx.token + ctx.timestamp))
            data['timestamp'] = ctx.timestamp
            return {'sign': ctx.token_aes_encrypt(_concat(data))}
        data['appId'] = ctx.app_id
        data['appSecret'] = ctx.app_secret
        return {'sign': ctx.md5(_concat(data))}


class PublicSignerV2023:
    """公版 2.0.23：先 AES(token 派生 key/iv) 再 md5。"""

    name = '2.0.23:public'
    version = VERSION_NAME

    def sign(self, path: str, params: Mapping[str, Any], ctx: SignContext) -> Dict[str, Any]:
        data = {k: v for k, v in params.items() if k != 'sign'}
        data['appId'] = ctx.derived_app_id()
        data['appSecret'] = ctx.derived_app_secret()
        if ctx.aes_encrypt is None:
            raise ValueError('公版签名需要 aes_encrypt')
        auth_info = _concat(data)
        return {'sign': ctx.md5(ctx.aes_encrypt(urllib.parse.unquote(auth_info)))}


class FaceSignerV2023(PublicSignerV2023):
    """人脸接口：``key``/``param`` 已经是密文，签名不许再 unquote 两遍。"""

    name = '2.0.23:public-face'

    def sign(self, path: str, params: Mapping[str, Any], ctx: SignContext) -> Dict[str, Any]:
        return super().sign(path, params, ctx)


class LegacyBaseSigner:
    """``m.boxkj.com`` 公共接口：``md5(k=v&...)`` + 静态 appSecret。"""

    name = '2.0.23:base'
    version = VERSION_NAME

    def sign(self, path: str, params: Mapping[str, Any], ctx: SignContext) -> Dict[str, Any]:
        data = {k: v for k, v in params.items() if k != 'sign'}
        data['appId'] = 'move'
        data['appSecret'] = APP_SECRET
        return {'sign': ctx.md5(_concat(data))}


SIGNERS: Dict[str, Signer] = {
    '2.0.23:private': PrivateSignerV2023(),
    '2.0.23:public': PublicSignerV2023(),
    '2.0.23:public-face': FaceSignerV2023(),
    '2.0.23:base': LegacyBaseSigner(),
}


class UnknownSignerError(KeyError):
    pass


def resolve_signer(name: str) -> Signer:
    if name not in SIGNERS:
        raise UnknownSignerError(f'未注册的签名器: {name}，可用: {sorted(SIGNERS)}')
    return SIGNERS[name]


def signer_for(*, is_public: bool, is_open_encry: bool = False) -> Signer:
    if is_public:
        return SIGNERS['2.0.23:public']
    return SIGNERS['2.0.23:private']


def register_signer(signer: Signer) -> None:
    SIGNERS[signer.name] = signer


def apply_sign(signer: Signer, path: str, params: Mapping[str, Any], ctx: SignContext) -> Dict[str, Any]:
    """签名并返回合并后的完整参数（业务层直接拿去发请求）。"""
    merged = dict(params)
    merged.update(signer.sign(path, merged, ctx))
    return merged


def verify_against_sdk(signer_name: str, path: str, params: Mapping[str, Any], ctx: SignContext, expected: str) -> bool:
    """回归用：签名器输出必须与原 SDK 完全一致。"""
    return apply_sign(resolve_signer(signer_name), path, params, ctx).get('sign') == expected
