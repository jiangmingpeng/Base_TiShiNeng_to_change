import time

import tsn_policy as pol


def test_classify_signature_failure_is_fatal():
    rule = pol.classify_error('签名错误')
    assert rule.severity is pol.Severity.FATAL
    assert rule.action is pol.Action.STOP_ALL


def test_classify_by_alias():
    assert pol.classify_error('appSign invalid').kind == '签名错误'
    assert pol.classify_error('数据不合格：配速异常').kind == '数据不合格'
    assert pol.classify_error('存在作弊风险，已拦截').kind == '风控拒绝'
    assert pol.classify_error('账号异常，请联系管理员').kind == '账号异常'
    assert pol.classify_error('connection reset by peer').kind == '网络错误'
    assert pol.classify_error(None).severity is pol.Severity.TRANSIENT


def test_breaker_fatal_stops_everything(tmp_path):
    breaker = pol.CircuitBreaker(state_path=tmp_path / 'b.json')
    breaker.record_failure('1', '签名错误')
    allowed, reason = breaker.allow('2')
    assert not allowed and '全局熔断' in reason
    breaker.resume_global()
    assert breaker.allow('2')[0]


def test_breaker_freeze_seven_days(tmp_path):
    breaker = pol.CircuitBreaker(state_path=tmp_path / 'b.json')
    breaker.record_failure('1', '风控拒绝')
    allowed, reason = breaker.allow('1')
    assert not allowed and '冷却' in reason
    breaker.record_success('1')
    assert breaker.allow('1')[0]


def test_breaker_persists_state(tmp_path):
    path = tmp_path / 'b.json'
    first = pol.CircuitBreaker(state_path=path)
    first.record_failure('9', '数据不合格')
    second = pol.CircuitBreaker(state_path=path)
    assert not second.allow('9')[0]


def test_version_guard_detects_drift():
    guard = pol.VersionGuard(local_version='2.0.23')
    assert guard.check('2.0.23') is pol.RunMode.NORMAL
    assert guard.check('2.0.24') is pol.RunMode.READ_ONLY
    assert guard.check('') is pol.RunMode.READ_ONLY


def test_extract_version_from_nested_payload():
    payload = {'code': 0, 'data': {'list': [{'versionName': '2.0.24'}]}}
    assert pol.VersionGuard.extract_version(payload) == '2.0.24'


def test_audit_sink_roundtrip(tmp_path):
    sink = pol.AuditSink(root=tmp_path)
    sink.write(pol.AuditRecord(account_id='1', stage='submit', ok=True, payload={'sport_status': '1'}))
    rows = sink.tail('1')
    assert len(rows) == 1 and rows[0]['payload']['sport_status'] == '1'


def test_recent_success_dates_only_counts_ok_submits(tmp_path):
    sink = pol.AuditSink(root=tmp_path)
    now = int(time.time() * 1000)
    sink.write(pol.AuditRecord(account_id='2', stage='submit', ok=True, ts_ms=now))
    sink.write(pol.AuditRecord(account_id='2', stage='submit', ok=False, ts_ms=now))
    sink.write(pol.AuditRecord(account_id='2', stage='face:1', ok=True, ts_ms=now))
    assert len(sink.recent_success_dates('2')) == 1


def test_canary_gate_blocks_until_observation_passes():
    plan = pol.CanaryPlan(canary_accounts=('1',), observe_hours=24)
    assert plan.is_canary('1')
    assert plan.gate('1', None)[0]
    assert not plan.gate('2', None)[0]
    assert not plan.gate('2', int(time.time() * 1000))[0]
    assert plan.gate('2', int((time.time() - 25 * 3600) * 1000))[0]


def test_error_table_markdown_lists_all_classes():
    table = pol.error_table_markdown()
    for kind in pol.ERROR_CLASS:
        assert kind in table
