import json
import random


import tsn_fingerprint as fp


def test_header_order_is_okhttp():
    headers = fp.build_headers('m.boxkj.com', extra={'token': 'abc'})
    names = [k for k, _ in headers]
    assert names[:5] == ['Host', 'Connection', 'Accept-Encoding', 'User-Agent', 'Content-Type']
    assert names[5:] == ['token']
    assert len(names) == 6


def test_header_values_match_okhttp4():
    headers = dict(fp.build_headers('m.boxkj.com'))
    assert headers['User-Agent'] == 'okhttp/4.12.0'
    assert headers['Accept-Encoding'] == 'gzip'
    assert headers['Connection'] == 'Keep-Alive'


def test_content_type_can_be_dropped_for_multipart():
    names = [k for k, _ in fp.build_headers('h', content_type=None)]
    assert 'Content-Type' not in names


def test_compact_json_has_no_spaces():
    payload = {'b': 1, 'a': [1, 2]}
    text = fp.compact_json(payload)
    assert ' ' not in text
    assert text == '{"b":1,"a":[1,2]}'
    assert json.loads(text) == payload


def test_form_encode_uses_plus_for_space():
    assert fp.form_encode([('q', 'a b')]) == 'q=a+b'
    assert fp.form_encode({'k': None}) == 'k='


def test_now_ms_is_13_digits():
    value = fp.now_ms()
    assert len(value) == 13
    assert value.isdigit()


def test_describe_reports_expected_fingerprint():
    session = fp.TsnHttpSession(backend='httpx')
    info = session.describe()
    assert info['header_order'] == list(fp.OKHTTP_HEADER_ORDER)
    assert info['user_agent'] == 'okhttp/4.12.0'
    matrix = fp.build_session_matrix(info)
    assert 'okhttp/4.12.0' in matrix


def test_session_can_be_closed_without_network():
    import asyncio

    async def run():
        session = fp.TsnHttpSession(backend='httpx')
        assert session.backend_name == 'httpx'
        await session.aclose()

    asyncio.run(run())


def test_split_files_moves_plain_fields_out_of_files():
    fields, real = fp._split_files({'key': (None, b'v', 'text/plain'), 'file': ('a.jpg', b'x', 'image/jpeg')})
    assert fields == {'key': 'v'}
    assert real == {'file': ('a.jpg', b'x', 'image/jpeg')}


def test_rng_free_fingerprint_config_is_stable():
    a = fp.FingerprintConfig()
    b = fp.FingerprintConfig()
    assert a.header_order == b.header_order == ','.join(fp.OKHTTP_HEADER_ORDER)
    assert random.Random(0).randint(0, 9) == 6
