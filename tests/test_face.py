import io
import random
from pathlib import Path

import numpy as np
from PIL import Image

import tsn_face_provider as face


def _portrait(seed: int = 0) -> Image.Image:
    """合成一张"人脸照片"：渐变背景 + 椭圆脸部 + 五官，低频结构明显。"""
    rng = np.random.RandomState(seed)
    width, height = 240, 320
    yy, xx = np.mgrid[0:height, 0:width]
    # 背景色/脸部位置/五官大小都随 seed 变化，保证结构不同（而不是只差噪声）
    tint = rng.randint(0, 60, size=3)
    cx = 120 + rng.randint(-35, 35)
    cy = 150 + rng.randint(-30, 30)
    background = np.stack(
        [(xx / width) * (120 + tint[0]) + 40, (yy / height) * (110 + tint[1]) + 50, np.full_like(xx, 90 + tint[2])],
        axis=-1,
    )
    face_w = 60 + rng.randint(-12, 20)
    face_h = 85 + rng.randint(-15, 25)
    mask = ((xx - cx) ** 2 / face_w ** 2 + (yy - cy) ** 2 / face_h ** 2) < 1
    skin = np.array([200 + rng.randint(-25, 25), 170 + rng.randint(-25, 25), 150 + rng.randint(-25, 25)])
    background[mask] = skin
    eye_r = 9 + rng.randint(-3, 5)
    eye_dx = 28 + rng.randint(-6, 8)
    eyes = (((xx - (cx - eye_dx)) ** 2 + (yy - (cy - 20)) ** 2) < eye_r ** 2 * 1.6) | (
        ((xx - (cx + eye_dx)) ** 2 + (yy - (cy - 20)) ** 2) < eye_r ** 2 * 1.6
    )
    background[eyes] = [40, 40, 40]
    mouth = ((xx - cx) ** 2 / (40 + rng.randint(-10, 15)) ** 2 + (yy - (cy + 60)) ** 2 / (12 + rng.randint(-4, 6)) ** 2) < 1
    background[mouth] = [150, 60, 60]
    noise = rng.normal(0, 3, background.shape)
    arr = np.clip(background + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def _jpeg_bytes(image: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    image.save(buf, 'JPEG', quality=quality)
    return buf.getvalue()


def test_phash_identical_images_zero_distance():
    img = _portrait(1)
    assert face.phash_distance(face.phash(img), face.phash(img)) == 0


def test_phash_different_images_large_distance():
    assert face.phash_distance(face.phash(_portrait(1)), face.phash(_portrait(2))) > 4


def test_phash_is_64_bit():
    value = face.phash(_portrait(3))
    assert 0 <= value < (1 << 64)
    assert len(face.phash_hex(value)) == 16


def test_perturb_changes_phash_but_keeps_face():
    source = _portrait(4)
    payload, value, distance = face.perturb_face_bytes(_jpeg_bytes(source), random.Random(9))
    assert payload, '扰动应当产出字节'
    assert face.PHASH_MIN_DISTANCE <= distance <= 12, distance


def test_perturb_is_randomized_between_runs():
    raw = _jpeg_bytes(_portrait(5))
    a, _, _ = face.perturb_face_bytes(raw, random.Random(1))
    b, _, _ = face.perturb_face_bytes(raw, random.Random(2))
    assert a != b


def test_pixel_level_edit_is_useless_for_phash():
    """证明原方案"改 5~15 个像素"对 pHash 没有实质影响。"""
    img = _portrait(6)
    base = face.phash(img)
    pixels = img.convert('RGB').copy()
    for i in range(15):
        pixels.putpixel((10 + i * 7, 20 + i * 5), (255, 0, 0))
    assert face.phash_distance(base, face.phash(pixels)) == 0


def test_face_pool_enforces_reuse_limit(tmp_path: Path):
    pool_dir = tmp_path / 'pool' / '1'
    pool_dir.mkdir(parents=True)
    for i in range(5):
        (pool_dir / f'f{i}.jpg').write_bytes(_jpeg_bytes(_portrait(i)))
    pool = face.FacePool(1, root=tmp_path)
    pool.require_ready()
    picks = [pool.pick(random.Random(i)).name for i in range(12)]
    for i in range(1, len(picks)):
        if picks[i] == picks[i - 1]:
            # 同一张最多连续两次
            assert i + 1 >= len(picks) or picks[i + 1] != picks[i]


def test_face_pool_requires_min_images(tmp_path: Path):
    pool_dir = tmp_path / 'pool' / '2'
    pool_dir.mkdir(parents=True)
    (pool_dir / 'only.jpg').write_bytes(_jpeg_bytes(_portrait(9)))
    pool = face.FacePool(2, root=tmp_path)
    try:
        pool.require_ready()
    except face.FacePoolExhausted:
        pass
    else:
        raise AssertionError('底图不足时应当抛 FacePoolExhausted')


def test_liveness_detection_finds_new_field():
    assert face.detect_liveness_requirement({'code': 0, 'data': {'livenessToken': 'abc'}}) == 'livenessToken'
    assert face.detect_liveness_requirement({'code': 0, 'data': {'ok': 1}}) is None


def test_assert_no_liveness_raises():
    try:
        face.assert_no_liveness({'data': {'antiSpoof': {'score': 0.1}}})
    except face.LivenessRequired as exc:
        assert 'antiSpoof' in str(exc)
    else:
        raise AssertionError('应当抛 LivenessRequired')
