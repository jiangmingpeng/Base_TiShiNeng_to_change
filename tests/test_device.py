
import tsn_device as dev


NOW = 1_763_000_000_000


def test_provision_is_deterministic():
    a = dev.provision_device(42, '广东省', now_ms=NOW)
    b = dev.provision_device(42, '广东省', now_ms=NOW)
    assert a == b
    assert a.android_id == b.android_id
    assert len(a.android_id) == 16


def test_android_id_is_stable_across_rebinding_key():
    # 同一学校 + 学号（key）永远是同一台设备，即使 DB 主键变了
    key = dev.device_key_for(10, '20230001')
    a = dev.provision_device(1, '江苏', key=key, now_ms=NOW)
    b = dev.provision_device(99, '江苏', key=key, now_ms=NOW)
    assert a.android_id == b.android_id
    assert a.model == b.model


def test_different_accounts_get_different_models_or_ids():
    records = [dev.provision_device(i, '广东', now_ms=NOW) for i in range(60)]
    ids = {r.android_id for r in records}
    assert len(ids) == 60
    models = {r.model for r in records}
    assert len(models) > 3, '机型分布过度集中，会被 D4 聚类'


def test_provisioned_device_passes_self_check():
    for account in range(30):
        record = dev.provision_device(account, '四川', now_ms=NOW)
        issues = dev.validate_device_consistency(record, now_ms=NOW)
        assert issues == [], f'账号 {account} 不自洽: {issues}'


def test_board_manufacturer_mismatch_detected():
    record = dev.provision_device(7, '北京', now_ms=NOW)
    broken = dev.TsnDeviceRecord(**{**record.as_dict(), 'board': 'not-a-board'})
    codes = {i.code for i in dev.validate_device_consistency(broken, now_ms=NOW)}
    assert 'board_mismatch' in codes


def test_os_version_outside_profile_detected():
    record = dev.provision_device(8, '上海', now_ms=NOW)
    broken = dev.TsnDeviceRecord(**{**record.as_dict(), 'os_version': '5'})
    codes = {i.code for i in dev.validate_device_consistency(broken, now_ms=NOW)}
    assert 'os_not_in_profile' in codes
    assert 'apk_incompatible' in codes


def test_bad_android_id_detected():
    record = dev.provision_device(9, '湖北', now_ms=NOW)
    broken = dev.TsnDeviceRecord(**{**record.as_dict(), 'android_id': '9774d56d682e549c'})
    codes = {i.code for i in dev.validate_device_consistency(broken, now_ms=NOW)}
    assert 'android_id_known_bad' in codes


def test_future_boot_time_detected():
    record = dev.provision_device(10, '湖南', now_ms=NOW)
    broken = dev.TsnDeviceRecord(**{**record.as_dict(), 'boot_time_ms': NOW + 10_000_000})
    codes = {i.code for i in dev.validate_device_consistency(broken, now_ms=NOW)}
    assert 'boot_time_future' in codes


def test_ua_matches_model_and_os():
    record = dev.provision_device(11, '浙江', now_ms=NOW)
    ua = record.webview_user_agent()
    assert f'Android {record.os_version}; {record.model} Build/' in ua
    assert record.build_id() in ua


def test_dp_metrics_in_phone_range():
    record = dev.provision_device(12, '福建', now_ms=NOW)
    dp_w, dp_h = record.dp_metrics()
    assert 350 <= dp_w <= 460
    assert 600 <= dp_h <= 1000


def test_catalog_entries_are_internally_consistent():
    for model, profile in dev.DEVICE_CATALOG.items():
        assert profile.model == model
        assert profile.manufacturer and profile.board
        dp_w = profile.screen_w * 160.0 / profile.density
        assert 350 <= dp_w <= 460, f'{model} dp 宽 {dp_w}'
        assert all(v in ('8', '9', '10', '11', '12', '13', '14', '15', '16') for v in profile.os_versions)


def test_province_bias_shifts_brand_mix():
    guangdong = [dev.provision_device(i, '广东', now_ms=NOW).manufacturer for i in range(200)]
    heilongjiang = [dev.provision_device(i, '黑龙江', now_ms=NOW).manufacturer for i in range(200)]
    assert guangdong != heilongjiang
    assert 'OPPO' in guangdong or 'vivo' in guangdong


def test_assert_device_consistent_raises():
    record = dev.provision_device(13, '山东', now_ms=NOW)
    broken = dev.TsnDeviceRecord(**{**record.as_dict(), 'density': 120})
    try:
        dev.assert_device_consistent(broken, now_ms=NOW)
    except ValueError as exc:
        assert '设备指纹不自洽' in str(exc)
    else:
        raise AssertionError('应当抛出不自信')
