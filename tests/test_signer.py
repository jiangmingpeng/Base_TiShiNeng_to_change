import tsn_signer as sg
from TiShiNengSdkPrivate import TiShiNengPrivate
from TiShiNengSdkPublic import TiShiNengSdkPublic

#: 黄金样本：v0.2 改造前由原 SDK 实现生成的签名值。
#: SDK 已委托 signer 实现，因此这组常量是唯一能发现"签名漂移"的回归锚点。
GOLDEN_PUBLIC = 'a4d9eee03303e09d2395cbbdc9317693'
GOLDEN_FACE = '219845a5f1b82064ff6512946c8c7f36'
GOLDEN_PRIVATE = '73013d6de8d19e471cd8b734ebf7091b'
GOLDEN_PRIVATE_ENC = 'SRuCtnkrXm3Z2ChQZhcbppzRoBhWvhOzdfP+HZHUDviPQOR53CrXjp0SIJyzUEd8QSJ5OewuAbxQw0SqtTgxhEzMnrHzlnCb8TwgwMTrknEh59VL314dWGrN1dXTmsVLsAtDLyFLT20XXv8aKmMtj+ZQDowQ3KwU7iAH4/1gYPs='


def _public_sdk():
    return TiShiNengSdkPublic(0, 1, 'SCHOOL', 'openid', 'device-id', 'Xiaomi', '23127PN0CC', '14', 'token-abc', 'arm64-v8a')


def _private_sdk(encry: bool = False):
    return TiShiNengPrivate(0, 1, 'SCHOOL', encry, 'device-id', 'OPPO', 'PKD130', 'token-abc')


def test_public_signer_matches_sdk():
    sdk = _public_sdk()
    params = {'a': '1', 'b': '2'}
    timestamp = '1700000000000'
    expected = sdk.getSign(dict(params), timestamp)
    assert expected == GOLDEN_PUBLIC
    ctx = sg.SignContext(token='token-abc', timestamp=timestamp, app_id=sdk.appId, app_secret=sdk.appSecret, aes_encrypt=sdk.aesUtils.encrypt)
    assert sg.resolve_signer('2.0.23:public').sign('/x', params, ctx)['sign'] == GOLDEN_PUBLIC


def test_public_face_signer_matches_golden():
    sdk = _public_sdk()
    assert sdk.getFaceSign({'key': 'k', 'param': 'p'}, '1700000000000') == GOLDEN_FACE
    ctx = sg.SignContext(token='token-abc', timestamp='1700000000000', app_id=sdk.appId, app_secret=sdk.appSecret, aes_encrypt=sdk.aesUtils.encrypt)
    assert sg.resolve_signer('2.0.23:public-face').sign('/x', {'key': 'k', 'param': 'p'}, ctx)['sign'] == GOLDEN_FACE


def test_private_signer_matches_sdk():
    sdk = _private_sdk()
    params = {'uid': '1', 'runType': 2, 'timestamp': '1700000000000'}
    expected = sdk.getSign(dict(params))
    assert expected == GOLDEN_PRIVATE
    ctx = sg.SignContext(token='token-abc', timestamp='1700000000000', app_id=sdk.appId, app_secret=sdk.appSecret)
    assert sg.resolve_signer('2.0.23:private').sign('/app/x', params, ctx)['sign'] == GOLDEN_PRIVATE


def test_private_encrypted_signer_matches_sdk():
    sdk = _private_sdk(encry=True)
    params = {'uid': '1', 'runType': 2, 'timestamp': '1700000000000'}
    expected = sdk.getSign2(dict(params), '1700000000000')
    assert expected == GOLDEN_PRIVATE_ENC
    ctx = sg.SignContext(
        token='token-abc',
        timestamp='1700000000000',
        app_id=sdk.appId,
        app_secret=sdk.appSecret,
        is_open_encry=True,
        token_aes_encrypt=sdk.getTokenAesEncrypt,
    )
    signed = sg.resolve_signer('2.0.23:private').sign('app/sportRecordSetting/getSetting', params, ctx)
    assert signed['sign'] == expected


def test_base_signer_uses_static_secret():
    ctx = sg.SignContext(token='t', timestamp='1')
    signed = sg.resolve_signer('2.0.23:base').sign('/x', {'provinceId': '1'}, ctx)
    assert len(signed['sign']) == 32


def test_apply_sign_merges_params():
    ctx = sg.SignContext(token='t', timestamp='1')
    merged = sg.apply_sign(sg.resolve_signer('2.0.23:base'), '/x', {'a': 'b'}, ctx)
    assert merged['a'] == 'b' and 'sign' in merged


def test_unknown_signer_raises():
    try:
        sg.resolve_signer('9.9.9:public')
    except sg.UnknownSignerError:
        pass
    else:
        raise AssertionError('未知签名器应当报错')


def test_signer_for_chooses_public_and_private():
    assert sg.signer_for(is_public=True).name == '2.0.23:public'
    assert sg.signer_for(is_public=False).name == '2.0.23:private'


def test_register_custom_signer():
    class Dummy:
        name = 'test:signer'
        version = '0.0.1'

        def sign(self, path, params, ctx):
            return {'sign': 'dummy'}

    sg.register_signer(Dummy())
    assert sg.resolve_signer('test:signer').sign('/x', {}, sg.SignContext(token='t', timestamp='1'))['sign'] == 'dummy'
