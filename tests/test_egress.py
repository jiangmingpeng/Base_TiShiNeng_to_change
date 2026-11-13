import asyncio
import time

import tsn_egress as eg


def _pool():
    return eg.EgressPool([
        eg.Egress(url='http://u:p@10.0.0.1:8080', province='广东', isp='mobile', label='gd-1'),
        eg.Egress(url='http://u:p@10.0.0.2:8080', province='广东', isp='residential', label='gd-2'),
        eg.Egress(url='http://u:p@10.0.0.3:8080', province='江苏', isp='residential', label='js-1'),
        eg.Egress(url='http://u:p@10.0.0.4:8080', province='广东', isp='idc', label='bad'),
    ])


def test_idc_egress_is_not_compliant():
    pool = _pool()
    candidates = pool.for_province('广东')
    assert all(e.isp != 'idc' for e in candidates)
    assert len(candidates) == 2


def test_province_is_normalized():
    assert eg.normalize_province('广东省') == '广东'
    assert eg.normalize_province('广西壮族自治区') == '广西'
    assert eg.normalize_province(None) == ''


def test_bind_is_deterministic():
    pool = _pool()
    a = eg.bind_egress(1, '广东', pool)
    b = eg.bind_egress(1, '广东', pool)
    assert a and b and a.egress_key == b.egress_key


def test_bind_respects_existing_binding():
    pool = _pool()
    first = eg.bind_egress(1, '广东', pool)
    again = eg.bind_egress(1, '广东', pool, existing={'1': first.egress_key})
    assert again.egress_key == first.egress_key


def test_bind_caps_accounts_per_egress():
    pool = _pool()
    existing = {}
    for account in range(6):
        binding = eg.bind_egress(account, '广东', pool, existing=existing, max_per_egress=3)
        assert binding is not None
        existing[str(account)] = binding.egress_key
    counts = {}
    for key in existing.values():
        counts[key] = counts.get(key, 0) + 1
    assert max(counts.values()) <= 3


def test_bind_returns_none_without_province_pool():
    pool = _pool()
    assert eg.bind_egress(1, '新疆', pool) is None


def test_audit_flags_idc_and_single_egress():
    pool = eg.EgressPool([eg.Egress(url='http://x:1', province='广东', isp='idc')])
    problems = pool.audit('广东')
    assert problems


def test_device_as_proxy_uses_forward_not_reverse():
    commands = eg.device_as_proxy_commands(8888)
    joined = '\n'.join(commands)
    assert 'adb forward tcp:8888 tcp:8888' in joined
    assert 'reverse' not in joined.split('\n')[1]
    assert 'settings put global http_proxy :0' in joined


def test_rate_limiter_spacing():
    limiter = eg.PerEgressRateLimiter(max_qps_per_ip=1.0, jitter_ms=(0.0, 0.0))

    async def run():
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire('eg-1')
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed >= 2.0


def test_pollution_registry_threshold():
    registry = eg.PollutionRegistry(threshold=2)
    assert not registry.polluted('dev', 'eg')
    registry.strike('dev', 'eg')
    assert not registry.polluted('dev', 'eg')
    registry.strike('dev', 'eg')
    assert registry.polluted('dev', 'eg')


def test_masked_url_hides_credentials():
    egress = eg.Egress(url='http://user:pass@1.2.3.4:8080', province='广东')
    assert 'pass' not in egress.masked
    assert '1.2.3.4:8080' in egress.masked
