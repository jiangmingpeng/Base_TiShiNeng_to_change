"""人脸层：定向扰动 + 底图池 + 活体降级（方案 P1-9 / P1-10）。

先纠正原稿里的一处误判：**"改 5~15 个像素"对 pHash 和 embedding 都没有
实质影响**。pHash 走的是"缩到 32x32 -> DCT -> 取低频 8x8 -> 比中位数"，
几个孤立像素点在缩采样时就没了；CNN embedding 更是局部平移不变。

有效扰动必须动**低频结构**：重采样比例、小幅旋转、亮度/对比、全局噪声、
JPEG 量化。本模块的目标是让同一张底图两次提交的 pHash 汉明距离落在
4~12 bit（同一人、不同照片的典型区间），而不是 0~1 bit（同一文件）。

活体是硬边界：一旦服务端冒出新字段（``livenessToken`` / ``antiSpoof`` 等），
说明平台上了新检测面，此时应当熔断该账号而不是继续硬撞。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

import numpy as np
from loguru import logger
from PIL import Image, ImageEnhance

FACE_ROOT = Path('face_images')
POOL_MIN_IMAGES = 5
MAX_CONSECUTIVE_REUSE = 2
PHASH_MIN_DISTANCE = 4
PHASH_MAX_DISTANCE = 12

#: 服务端一旦出现这些字段，说明活体检测已上线，必须熔断。
LIVENESS_KEYS = ('livenessToken', 'liveness', 'antiSpoof', 'anti_spoof', 'faceToken', 'verifyToken', 'depthMap', 'actionType')


class LivenessRequired(RuntimeError):
    """服务端要求活体采集：静态图路线到此为止。"""


class FacePoolExhausted(RuntimeError):
    pass


# ---------------------------------------------------------------- pHash


def phash(image: Image.Image, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    """64-bit DCT pHash（与常见实现一致，便于跨端比对）。"""
    size = hash_size * highfreq_factor
    img = image.convert('L').resize((size, size), Image.Resampling.LANCZOS)
    pixels = np.asarray(img, dtype=np.float64)
    dct = _dct2(pixels)
    low = dct[:hash_size, :hash_size]
    med = np.median(low[1:].flatten()) if low.size > 1 else np.median(low)
    bits = 0
    for value in low.flatten():
        bits = (bits << 1) | (1 if value > med else 0)
    return bits


def _dct_matrix(n: int) -> np.ndarray:
    matrix = np.zeros((n, n), dtype=np.float64)
    factor = math.pi / (2 * n)
    for k in range(n):
        alpha = math.sqrt(1.0 / n) if k == 0 else math.sqrt(2.0 / n)
        for x in range(n):
            matrix[k, x] = alpha * math.cos((2 * x + 1) * k * factor)
    return matrix


_DCT_CACHE: Dict[int, np.ndarray] = {}


def _dct2(pixels: np.ndarray) -> np.ndarray:
    n = pixels.shape[0]
    matrix = _DCT_CACHE.get(n)
    if matrix is None:
        matrix = _dct_matrix(n)
        _DCT_CACHE[n] = matrix
    return matrix @ pixels @ matrix.T


def phash_distance(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count('1')


def phash_hex(value: int) -> str:
    return f'{value:016x}'


# ---------------------------------------------------------------- 扰动


@dataclass
class PerturbOptions:
    scale: Tuple[float, float] = (0.92, 1.08)
    rotation_deg: Tuple[float, float] = (-3.0, 3.0)
    brightness: Tuple[float, float] = (0.94, 1.06)
    contrast: Tuple[float, float] = (0.95, 1.05)
    noise_sigma: float = 2.0
    jpeg_quality: Tuple[int, int] = (82, 92)
    target_size: Tuple[int, int] = (224, 224)
    min_phash_distance: int = PHASH_MIN_DISTANCE
    max_attempts: int = 6


def perturb_face(image: Image.Image, rng: Optional[random.Random] = None, options: Optional[PerturbOptions] = None) -> Image.Image:
    """重采样 + 旋转 + 亮度/对比 + 全局噪声 + JPEG 量化。"""
    rng = rng or random.Random()
    options = options or PerturbOptions()
    img = image.convert('RGB')
    scale = rng.uniform(*options.scale)
    width = max(32, int(img.width * scale))
    height = max(32, int(img.height * scale))
    img = img.resize((width, height), Image.Resampling.BICUBIC)
    img = img.resize(options.target_size, Image.Resampling.BICUBIC)
    angle = rng.uniform(*options.rotation_deg)
    fill = tuple(int(v) for v in np.asarray(img).reshape(-1, 3).mean(axis=0))
    img = img.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=fill)
    img = ImageEnhance.Brightness(img).enhance(rng.uniform(*options.brightness))
    img = ImageEnhance.Contrast(img).enhance(rng.uniform(*options.contrast))
    arr = np.asarray(img, dtype=np.float64)
    arr = arr + np.random.normal(0.0, options.noise_sigma, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=rng.randint(*options.jpeg_quality))
    buffer.seek(0)
    return Image.open(buffer).convert('RGB')


def perturb_face_bytes(image_bytes: bytes, rng: Optional[random.Random] = None, options: Optional[PerturbOptions] = None, base_hash: Optional[int] = None) -> Tuple[bytes, int, int]:
    """扰动直到 pHash 距离达标，返回 ``(jpeg_bytes, 新 phash, 距离)``。"""
    rng = rng or random.Random()
    options = options or PerturbOptions()
    source = Image.open(io.BytesIO(image_bytes))
    base = base_hash if base_hash is not None else phash(source)
    best: Tuple[bytes, int, int] = (b'', base, 0)
    for _ in range(max(1, options.max_attempts)):
        candidate = perturb_face(source, rng, options)
        buffer = io.BytesIO()
        candidate.save(buffer, format='JPEG', quality=rng.randint(*options.jpeg_quality))
        payload = buffer.getvalue()
        value = phash(candidate)
        distance = phash_distance(base, value)
        if distance > best[2]:
            best = (payload, value, distance)
        if distance >= options.min_phash_distance:
            return (payload, value, distance)
    return best


# ---------------------------------------------------------------- 底图池


class FaceProvider(Protocol):
    name: str

    async def fetch(self, rng: Optional[random.Random] = None) -> bytes:
        ...


@dataclass
class PoolState:
    uses: Dict[str, int] = field(default_factory=dict)
    last: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {'uses': dict(self.uses), 'last': self.last}


class FacePool:
    """每账号 ≥5 张底图，同一张图不允许连续使用超过 2 次。"""

    def __init__(self, account_id: object, root: Optional[Path] = None, min_images: int = POOL_MIN_IMAGES) -> None:
        self.account_id = str(account_id)
        self.root = (root or FACE_ROOT) / 'pool' / self.account_id
        self.min_images = min_images
        self._state_path = self.root / '.state.json'
        self._state = self._load_state()

    def _load_state(self) -> PoolState:
        try:
            with open(self._state_path, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
            return PoolState(uses={str(k): int(v) for k, v in (raw.get('uses') or {}).items()}, last=raw.get('last'))
        except (OSError, ValueError, TypeError):
            return PoolState()

    def _save_state(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self._state_path, 'w', encoding='utf-8') as fh:
                json.dump(self._state.as_dict(), fh, ensure_ascii=False)
        except OSError as exc:
            logger.warning(f'人脸池状态写入失败: {exc}')

    def images(self) -> List[Path]:
        if not self.root.exists():
            return []
        files = [p for p in self.root.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') and not p.name.startswith('.')]
        return sorted(files)

    def require_ready(self) -> None:
        images = self.images()
        if len(images) < self.min_images:
            raise FacePoolExhausted(f'账号 {self.account_id} 底图不足：{len(images)} < {self.min_images}')

    def pick(self, rng: Optional[random.Random] = None) -> Path:
        rng = rng or random.Random()
        images = self.images()
        if not images:
            raise FacePoolExhausted(f'账号 {self.account_id} 没有底图')
        candidates = [p for p in images if not (self._state.last == p.name and self._state.uses.get(p.name, 0) >= MAX_CONSECUTIVE_REUSE)]
        if not candidates:
            # 全都连续用满了，重置计数而不是复用同一张
            for p in images:
                self._state.uses[p.name] = 0
            candidates = images
        choice = rng.choice(candidates)
        if self._state.last == choice.name:
            self._state.uses[choice.name] = self._state.uses.get(choice.name, 0) + 1
        else:
            self._state.last = choice.name
            self._state.uses[choice.name] = 1
        self._save_state()
        return choice


class StaticImageProvider:
    """底图池 + 定向扰动。默认路线。"""

    name = 'static'

    def __init__(self, pool: FacePool, options: Optional[PerturbOptions] = None) -> None:
        self.pool = pool
        self.options = options or PerturbOptions()

    async def fetch(self, rng: Optional[random.Random] = None) -> bytes:
        rng = rng or random.Random()
        self.pool.require_ready()
        path = self.pool.pick(rng)
        raw = path.read_bytes()
        payload, value, distance = perturb_face_bytes(raw, rng, self.options)
        if distance < self.options.min_phash_distance:
            logger.warning(f'人脸扰动 pHash 距离仅 {distance} bit，低于目标 {self.options.min_phash_distance}')
        logger.info(f'人脸底图 {path.name} -> phash {phash_hex(value)} 距离 {distance} bit')
        return payload


class VideoFrameProvider:
    """从预录视频抽帧（需要 ffmpeg）。抽帧天然带光照/姿态变化。"""

    name = 'video'

    def __init__(self, video_path: Path, tmp_dir: Optional[Path] = None) -> None:
        self.video_path = Path(video_path)
        self.tmp_dir = tmp_dir or Path('face_images/_frames')

    def _ffmpeg(self) -> Optional[str]:
        return shutil.which('ffmpeg')

    async def fetch(self, rng: Optional[random.Random] = None) -> bytes:
        rng = rng or random.Random()
        binary = self._ffmpeg()
        if binary is None or not self.video_path.exists():
            raise FacePoolExhausted('视频抽帧不可用（缺 ffmpeg 或文件）')
        duration = max(1.0, float(rng.uniform(0.5, 8.0)))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        out = self.tmp_dir / f'frame_{rng.randint(0, 10**6)}.jpg'
        cmd = [binary, '-y', '-ss', f'{duration:.2f}', '-i', str(self.video_path), '-frames:v', '1', '-q:v', '3', str(out)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        if not out.exists():
            raise FacePoolExhausted('ffmpeg 抽帧失败')
        payload, _, _ = perturb_face_bytes(out.read_bytes(), rng)
        try:
            out.unlink()
        except OSError:
            pass
        return payload


class DeviceCaptureProvider:
    """真机抓拍（adb）：唯一能过 IMU / 活体的路线。"""

    name = 'device'

    def __init__(self, serial: Optional[str] = None) -> None:
        self.serial = serial

    async def fetch(self, rng: Optional[random.Random] = None) -> bytes:
        adb = shutil.which('adb')
        if adb is None:
            raise FacePoolExhausted('adb 不可用')
        base = [adb] + (['-s', self.serial] if self.serial else [])
        proc = await asyncio.create_subprocess_exec(*base, 'exec-out', 'screencap', '-p', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await proc.communicate()
        if not stdout:
            raise FacePoolExhausted('adb 抓拍失败')
        return stdout


class FacePipeline:
    """阶梯降级：静态图 -> 视频抽帧 -> 真机采集。"""

    def __init__(self, providers: Sequence[FaceProvider]) -> None:
        self.providers = list(providers)

    async def fetch(self, rng: Optional[random.Random] = None) -> bytes:
        errors: List[str] = []
        for provider in self.providers:
            try:
                return await provider.fetch(rng)
            except Exception as exc:
                errors.append(f'{provider.name}: {exc}')
                logger.warning(f'人脸 provider {provider.name} 失败，降级下一个: {exc}')
        raise FacePoolExhausted('所有人脸 provider 均失败 -> ' + '; '.join(errors))


def detect_liveness_requirement(payload: Any) -> Optional[str]:
    """扫描服务端响应，发现活体相关新字段就返回字段名。"""
    stack: List[Any] = [payload]
    seen = 0
    while stack and seen < 500:
        item = stack.pop()
        seen += 1
        if isinstance(item, dict):
            for key, value in item.items():
                if key in LIVENESS_KEYS and value:
                    return key
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item[:50])
    return None


def assert_no_liveness(payload: Any) -> None:
    key = detect_liveness_requirement(payload)
    if key:
        raise LivenessRequired(f'服务端要求活体字段 {key}：该账号应熔断，改真机采集')


def pool_report(account_id: object, root: Optional[Path] = None) -> str:
    pool = FacePool(account_id, root)
    images = pool.images()
    lines = [f'账号 {account_id} 人脸池: {len(images)} 张（要求 >= {POOL_MIN_IMAGES}）']
    for path in images:
        try:
            with Image.open(path) as img:
                lines.append(f'  - {path.name} {img.width}x{img.height} phash={phash_hex(phash(img))}')
        except OSError:
            lines.append(f'  - {path.name} 无法解析')
    return '\n'.join(lines)
