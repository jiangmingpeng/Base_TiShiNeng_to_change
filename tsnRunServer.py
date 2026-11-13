import asyncio
import datetime
import enum
import json
import math
import os
import random
import time
import urllib.parse
from pathlib import Path
from loguru import logger
from sqlalchemy import select, func
from TiShiNengError import TiShiNengError
from TiShiNengRunPathManage import genTiShiNengRunPathRepeat
from database import get_db
from models import TsnAccount_Model, RunPath, TsnSubmitLog_Model
from services.tsnAccount.tsnAccountDao import getTsnAccountByid
from tsnClient import getTsnClientById
import tsn_environment
import tsn_face_provider
import tsn_fingerprint
import tsn_face_schedule
import tsn_policy
import tsn_schedule
import tsn_payload
import tsn_track_sim

class TsnRunType(enum.Enum):
    morningRun = 1
    sumRun = 2
    freedom = 3

RUN_TYPE_SCHEDULE_KEY = {TsnRunType.morningRun: 'morning', TsnRunType.sumRun: 'sun', TsnRunType.freedom: 'freedom'}

def seconds_to_time_format(total_seconds):
    hours = total_seconds // 3600
    minutes = total_seconds % 3600 // 60
    seconds = total_seconds % 60
    time_formatted = '{:02d}:{:02d}:{:02d}'.format(hours, minutes, seconds)
    return time_formatted

class TsnRunServer:

    def __init__(self, accountId: int, runKiloMeter: float, logRunType: TsnRunType):
        self.accountId = accountId
        self.runKiloMeter = runKiloMeter
        self.logRunType = logRunType
        self.accountModel: TsnAccount_Model | None = None
        self.tsnClient = None
        self.identify = None
        self.start_timestamp = None
        self.exerciseSetting = None
        self.geofence = None
        self.endStride = None
        self.limitSpeed = None
        self.endLimitStepFrequency = None
        self.pointList = None
        self.isMongoPath = True
        self.isPublic = False
        self.isFaceStatus = 1
        self.planUseTime = None
        self.needRunKm = None
        self.isStartFace = 0
        self.isEndFace = 0
        self.isMidwayFace = 0
        self.middleFaces = []
        self.startLongitude = 0
        self.startLatitude = 0
        # ---- 抗风控状态 ----
        seed = os.getenv('TSN_SIM_SEED', '')
        self.rng = random.Random(f'{seed}:{accountId}' if seed else None)
        self.simOptions = tsn_track_sim.SimOptions(terrain=os.getenv('TSN_TERRAIN', 'campus'))
        self.breaker = tsn_policy.CircuitBreaker()
        self.audit = tsn_policy.AuditSink()
        self.versionGuard = tsn_policy.VersionGuard(local_version='2.0.23')
        self.runtime = None
        self.strictWindow = os.getenv('TSN_STRICT_WINDOW', '0') == '1'
        self.submitDelayS = 0.0
        self.facePipeline = None

    @classmethod
    def publicRunTypeConvert(cls, runType: TsnRunType):
        if runType == TsnRunType.morningRun:
            return 2
        elif runType == TsnRunType.sumRun:
            return 1
        elif runType == TsnRunType.freedom:
            return 0
        else:
            raise ValueError('runType error')

    @staticmethod
    def _randomize_coordinates(longitude, latitude, rng=None):
        """起点抖动按真实 GPS 精度（3~12m）；原来 ±0.0005 约等于 ±55m，会跑出围栏。"""
        rng = rng or random.Random()
        sigma = rng.uniform(3.0, 12.0)
        north = rng.gauss(0.0, sigma)
        east = rng.gauss(0.0, sigma)
        lat = latitude + north / 111320.0
        lon = longitude + east / (111320.0 * max(0.05, math.cos(math.radians(latitude))))
        return (lon, lat)

    @staticmethod
    def add_random_pixels_to_image(image_bytes: bytes, num_pixels: int=None) -> bytes:
        """兼容旧接口：改调 pHash 定向扰动（原来的 5~15 个像素对 pHash 无效）。"""
        if not image_bytes:
            return image_bytes
        try:
            payload, _hash_value, distance = tsn_face_provider.perturb_face_bytes(image_bytes, random.Random())
            logger.debug(f'人脸扰动完成：pHash 距离 {distance} bit，原始 {len(image_bytes)}B -> {len(payload)}B')
            return payload or image_bytes
        except Exception as e:
            logger.error(f'人脸扰动失败: {e}, 返回原始图片')
            return image_bytes

    # ---------------------------------------------------------------- 人脸
    def _face_pool(self) -> tsn_face_provider.FacePool:
        if self.accountModel is None:
            raise TiShiNengError('账号未加载，无法定位人脸池', 20004)
        return tsn_face_provider.FacePool(self.accountModel.id)

    def _legacy_face_dir(self) -> Path:
        return Path('face_images') / str(self.accountModel.school_id) / str(self.accountModel.user_id)

    def _migrate_legacy_faces(self, pool: tsn_face_provider.FacePool) -> int:
        legacy = self._legacy_face_dir()
        if not legacy.exists():
            return 0
        pool.root.mkdir(parents=True, exist_ok=True)
        moved = 0
        for path in legacy.iterdir():
            if path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                continue
            target = pool.root / path.name
            if target.exists():
                continue
            try:
                target.write_bytes(path.read_bytes())
                moved += 1
            except OSError:
                continue
        if moved:
            logger.info(f'旧人脸目录迁移 {moved} 张到 {pool.root}')
        return moved

    def buildFacePipeline(self):
        if self.facePipeline is not None:
            return self.facePipeline
        if not self.isPublic:
            self.facePipeline = tsn_face_provider.FacePipeline([])
            return self.facePipeline
        pool = self._face_pool()
        self._migrate_legacy_faces(pool)
        providers = []
        try:
            pool.require_ready()
        except tsn_face_provider.FacePoolExhausted:
            logger.warning(f'账号 {self.accountId} 人脸池不足 {tsn_face_provider.POOL_MIN_IMAGES} 张，先下载补齐')
        providers.append(tsn_face_provider.StaticImageProvider(pool, tsn_face_provider.PerturbOptions()))
        video = os.getenv('TSN_FACE_VIDEO', '')
        if video:
            providers.append(tsn_face_provider.VideoFrameProvider(Path(video)))
        if os.getenv('TSN_FACE_DEVICE', '0') == '1':
            providers.append(tsn_face_provider.DeviceCaptureProvider(os.getenv('TSN_ADB_SERIAL') or None))
        self.facePipeline = tsn_face_provider.FacePipeline(providers)
        return self.facePipeline

    async def getFaceImage(self):
        pool = self._face_pool()
        self._migrate_legacy_faces(pool)
        images = pool.images()
        if images:
            selected = pool.pick(self.rng)
            logger.info(f'使用本地人脸图片: {selected}')
            return selected.read_bytes()
        logger.info('本地没有人脸图片，从服务器获取...')
        face_list_resp = await self.tsnClient.listBasUserImageFace()
        logger.info(f'API返回数据: {face_list_resp}')
        if not face_list_resp:
            raise TiShiNengError('获取人脸列表失败，请检查网络连接或账号状态', 20001)
        face_data = face_list_resp
        if not face_data or len(face_data) == 0:
            raise TiShiNengError('该账号没有人脸图片记录，请先在APP中上传人脸照片', 20002)
        pool.root.mkdir(parents=True, exist_ok=True)
        download_count = 0
        # 图片下载也必须走同一出口与同一 UA：用裸 httpx 会暴露 python-httpx/x.y
        client = self.runtime.http if self.runtime is not None else tsn_fingerprint.TsnHttpSession()
        try:
            for idx, face_item in enumerate(face_data):
                image_url = face_item.get('imageRouteUrl')
                if not image_url:
                    continue
                try:
                    logger.info(f'下载人脸图片 {idx + 1}/{len(face_data)}: {image_url}')
                    parsed = urllib.parse.urlparse(image_url)
                    resp = await client.get(image_url, headers=tsn_fingerprint.build_headers(parsed.netloc or 'localhost', content_type=None), timeout=30.0)
                    if resp.status_code == 200:
                        image_id = face_item.get('id', f'face_{idx}')
                        ext = '.jpg'
                        if '.' in image_url:
                            ext = '.' + image_url.rsplit('.', 1)[-1].split('?')[0]
                        file_path = pool.root / f'{image_id}{ext}'
                        with open(file_path, 'wb') as f:
                            f.write(resp.content)
                        logger.info(f'人脸图片已保存: {file_path}')
                        download_count += 1
                    else:
                        logger.warning(f'下载失败，状态码: {resp.status_code}')
                except Exception as e:
                    logger.error(f'下载人脸图片失败: {e}')
                    continue
        finally:
            if self.runtime is None:
                await client.aclose()
        images = pool.images()
        if images:
            if len(images) < tsn_face_provider.POOL_MIN_IMAGES:
                logger.warning(f'人脸池仅 {len(images)} 张（建议 >= {tsn_face_provider.POOL_MIN_IMAGES}）：同一底图反复使用的特征会累积')
            selected = pool.pick(self.rng)
            logger.info(f'使用刚下载的人脸图片: {selected} (共下载 {download_count} 张)')
            return selected.read_bytes()
        raise TiShiNengError(f'人脸图片下载失败，尝试下载 {len(face_data)} 张但都失败了，请检查网络连接', 20003)

    async def uploadFace(self, coordinates, sleep=0, faceType=1):
        if not self.isPublic:
            return
        if sleep > 0:
            await asyncio.sleep(sleep)
        pipeline = self.buildFacePipeline()
        face_bytes = await pipeline.fetch(self.rng)
        try:
            resp = await self.tsnClient.exerciseRunningFace(face_bytes, coordinates, self.identify, self.publicRunTypeConvert(self.logRunType), faceType)
        except TiShiNengError as e:
            self.breaker.record_failure(self.accountId, str(e))
            self.audit.write(tsn_policy.AuditRecord(account_id=str(self.accountId), stage=f'face:{faceType}', ok=False, error=str(e), payload={'coordinates': coordinates}))
            raise
        try:
            tsn_face_provider.assert_no_liveness(resp)
        except tsn_face_provider.LivenessRequired as exc:
            logger.error(str(exc))
            self.breaker.record_failure(self.accountId, '活体要求')
            self.audit.write(tsn_policy.AuditRecord(account_id=str(self.accountId), stage=f'face:{faceType}', ok=False, error=str(exc)))
            raise
        self.audit.write(tsn_policy.AuditRecord(account_id=str(self.accountId), stage=f'face:{faceType}', ok=True, payload={'coordinates': coordinates}))
        return resp

    # ---------------------------------------------------------------- 路径
    async def queryPath(self):
        resultStmt = select(RunPath).where(RunPath.school_code == self.tsnClient.schoolCode).order_by(func.abs(RunPath.sport_range - self.runKiloMeter)).limit(10)
        async for newDb in get_db():
            result = await newDb.execute(resultStmt)
            runPathList = result.scalars().all()
            if len(runPathList) == 0:
                raise TiShiNengError('没有可用的跑步路线', 200000)
            runPath = self.rng.choice(runPathList)
            result = {'runLinePath': json.loads(runPath.run_line_path)}
            runLinePath = result['runLinePath']
            return runLinePath
        return None

    def getOkPointList(self, startTime, endTime):
        okPointList = []
        timeStep = (endTime - startTime) // (len(self.pointList) + 4)
        timeStamp = startTime + timeStep
        for i in self.pointList:
            dt_object = datetime.datetime.fromtimestamp(timeStamp / 1000)
            formatted_string = dt_object.strftime('%Y-%m-%d %H:%M:%S')
            tmp = {'latitude': float(i['latitude']), 'content': i['content'], 'id': i['id'], 'time': formatted_string, 'longitude': float(i['longitude'])}
            if 'sort' in i:
                tmp['sort'] = i['sort']
            okPointList.append(tmp)
            timeStamp += timeStep
        return okPointList

    def _normalize_middle_faces(self):
        return tsn_face_schedule.normalize_middle_faces(self.middleFaces)

    def buildMiddleFacePoints(self, path, startTime, endTime):
        points = tsn_face_schedule.build_middle_face_points(path=path, start_timestamp_ms=int(startTime), end_timestamp_ms=int(endTime), middle_faces=self.middleFaces, is_public=bool(self.isPublic), face_type=2)
        result = [point.as_dict() for point in points]
        logger.info(f'中途人脸验证点: {result}')
        return result

    # ---------------------------------------------------------------- 提交
    async def _sleep_until(self, timestamp_s: float) -> None:
        delay = timestamp_s - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _record_submit(self, *, sport_status, error_class, distance_m, elapsed_s, steps, ok: bool) -> None:
        device_pk = None
        egress_key = None
        if self.runtime is not None:
            device_pk = self.runtime.device.device_pk
            egress_key = self.runtime.egress_key
        self.audit.write(
            tsn_policy.AuditRecord(
                account_id=str(self.accountId),
                stage='submit',
                ok=ok,
                error=error_class or '',
                payload={
                    'sport_status': str(sport_status),
                    'run_type': self.logRunType.value,
                    'distance_m': round(float(distance_m), 1),
                    'elapsed_s': round(float(elapsed_s), 1),
                    'steps': int(steps),
                    'device_pk': device_pk,
                    'egress_key': egress_key,
                },
            )
        )
        try:
            async for db in get_db():
                db.add(
                    TsnSubmitLog_Model(
                        account_id=str(self.accountId),
                        run_date=datetime.date.today().isoformat(),
                        run_type=self.logRunType.value,
                        sport_status=str(sport_status),
                        error_class=error_class,
                        distance_m=float(distance_m),
                        elapsed_s=float(elapsed_s),
                        steps=int(steps),
                        device_pk=device_pk,
                        egress_key=egress_key,
                    )
                )
                await db.commit()
        except Exception as exc:
            logger.warning(f'提交日志落库失败: {exc}')

    async def uploadRunPath(self, path, stepNumbers, sumDistance):
        sportRange = str(round(sumDistance / 1000, 2))
        gpsDistance = sportRange
        if self.isPublic:
            endTime = path[-1]['t']
        else:
            endTime = path[-1]['time']
        # 停表延迟：人手按停止键 0.9~4.2 秒（原来 100~200ms 是毫秒级机器操作）
        endTime += self.rng.randint(900, 4200)
        endTimeStr = datetime.datetime.fromtimestamp(endTime / 1000).strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f'跑步中，预计结束时间：{endTimeStr}')
        usedTime = (endTime - int(self.start_timestamp)) // 1000
        okPointList = self.getOkPointList(int(self.start_timestamp), endTime)
        distanceKm = sumDistance / 1000
        paceTime = usedTime / distanceKm
        paceMinute = int(paceTime // 60)
        paceSecond = int(paceTime % 60)
        pace = f'''{paceMinute}'{paceSecond}"'''
        logger.info(f'配速：{pace}, usedTime:{usedTime}')

        # 跑完到点提交：log-normal(4.3, 0.5) + 0~59s，中位 ~74s，绝不整分
        self.submitDelayS = tsn_schedule.submit_delay_s(self.rng)
        submitAt = endTime / 1000.0 + self.submitDelayS
        logger.info(f'计划 {datetime.datetime.fromtimestamp(submitAt).strftime("%H:%M:%S")} 提交（停表后 {self.submitDelayS:.1f}s）')

        middleFacePoints = self.buildMiddleFacePoints(path, int(self.start_timestamp), endTime)
        for middleFaceItem in middleFacePoints:
            await self._sleep_until(int(middleFaceItem['timestamp']) / 1000.0)
            coordinates = f'{middleFaceItem["latitude"]},{middleFaceItem["longitude"]}'
            await self.uploadFace(coordinates=coordinates, sleep=0, faceType=2)

        await self._sleep_until(endTime / 1000.0)
        avgSpeed = round(sumDistance / usedTime * 3.6, 2)
        if self.isEndFace == 1:
            logger.info('结束跑人脸识别')
            lastPoint = path[-1]
            if self.isPublic:
                lat = lastPoint['a']
                lng = lastPoint['o']
            else:
                lat = lastPoint['latitude']
                lng = lastPoint['longitude']
            await self.uploadFace(coordinates=f'{lat},{lng}', sleep=0, faceType=3)

        pointList = self.pointList.copy()
        for point in pointList:
            if 'isMustPoint' in point:
                point['isMustPoint'] = float(point['isMustPoint'])
            if 'okRadius' in point:
                point['okRadius'] = float(point['okRadius'])
        for item in path:
            item.pop('distance', None)

        # 提交前最后一道地理自检
        ll = [[float(p.get('o', p.get('longitude', 0))), float(p.get('a', p.get('latitude', 0)))] for p in path]
        geo_issues = tsn_track_sim.validate_geo_constraints(ll, geofence=self.geofence, must_points=pointList)
        if geo_issues:
            logger.warning('地理约束问题: ' + '; '.join(f'{i.code}: {i.message}' for i in geo_issues))

        await self._sleep_until(submitAt)
        logger.info('上传跑步数据')
        total_steps = sum((int(n) for n in stepNumbers))
        if self.isPublic:
            setting = self.exerciseSetting or {}
            submit_extras = tsn_payload.build_submit_extras(setting=setting, points=path, elapsed_s=usedTime, distance_m=sumDistance, steps=total_steps, user_open_develop=setting.get('userOpenDevelop', False), offline=0)
            logger.info(f'2.0.23 提交字段: {submit_extras}')
            logger.info(f'合格性判定明细: {tsn_payload.qualification_detail(setting, usedTime, sumDistance, total_steps)}')
            try:
                stopResp, exerciseRecordId = await self.tsnClient.addExerciseRecord(sportType=self.publicRunTypeConvert(self.logRunType), startTime=self.start_timestamp, endTime=endTime, sportTime=usedTime, sportRange=sportRange, speed=pace, avgSpeed=str(avgSpeed), gitudeLatitude=json.dumps(path), stepNumbers=json.dumps(stepNumbers), isSequencePoint='0', pointList=json.dumps(pointList, ensure_ascii=False), okPointList=json.dumps(okPointList, ensure_ascii=False), isFaceStatus=1, uploadType=0, identify=self.identify, geofence=json.dumps(self.geofence), limitSpeed=self.limitSpeed, limitStride=self.endStride, limitStepFrequency=self.endLimitStepFrequency, gpsDistance=gpsDistance, d=submit_extras.get('d'), f=submit_extras.get('f'), m=submit_extras.get('m'), h=submit_extras.get('h'), isValid=submit_extras.get('isValid'), remarkId=submit_extras.get('remarkId'), setting=setting, trackPoints=path, stepCount=total_steps, elapsedSeconds=usedTime, distanceMeters=sumDistance)
                await self.tsnClient.sumExerciseRecord()
                logger.info(stopResp)
                logger.info(exerciseRecordId)
            except TiShiNengError as exc:
                rule = self.breaker.record_failure(self.accountId, str(exc))
                await self._record_submit(sport_status='-', error_class=rule.kind, distance_m=sumDistance, elapsed_s=usedTime, steps=total_steps, ok=False)
                logger.error(f'提交失败 [{rule.severity.value}/{rule.action.value}]: {exc}')
                raise
            if exerciseRecordId is not None:
                exerciseRecordResp = await self.tsnClient.getExerciseRecord(exerciseRecordId)
                sportStatus = exerciseRecordResp['sportStatus']
                remark = exerciseRecordResp['remark']
                logger.info(remark)
                if str(sportStatus) != '1':
                    rule = self.breaker.record_failure(self.accountId, remark)
                    await self._record_submit(sport_status=sportStatus, error_class=rule.kind, distance_m=sumDistance, elapsed_s=usedTime, steps=total_steps, ok=False)
                    raise TiShiNengError(remark, 200000)
                self.breaker.record_success(self.accountId)
                await self._record_submit(sport_status=sportStatus, error_class=None, distance_m=sumDistance, elapsed_s=usedTime, steps=total_steps, ok=True)
                await self.tsnClient.getFeedbackBalance()
                await self.tsnClient.sumExerciseRecord()
                await self.tsnClient.statisticsExerciseRecord()
        else:
            formatSportTime = seconds_to_time_format(usedTime)
            try:
                stopResp = await self.tsnClient.appAddSportRecord(runType=self.logRunType.value, startTime=self.start_timestamp, endTime=endTime, gitudeLatitude=json.dumps(path), identify=self.identify, formatSportTime=formatSportTime, formatSportRange=sportRange, avgspeed=str(avgSpeed), speed=pace, okPointList=json.dumps(okPointList), stepNumbers=json.dumps(stepNumbers), isFaceStatus=0, points=json.dumps(self.pointList), uploadType=0)
                logger.info(stopResp)
                await self.tsnClient.sumSportRecord()
                await asyncio.sleep(tsn_schedule.request_gap_s(self.rng))
                appSportRecordList = await self.tsnClient.appSportRecordList()
                appSportRecordListData = appSportRecordList['data'][0]
            except TiShiNengError as exc:
                rule = self.breaker.record_failure(self.accountId, str(exc))
                await self._record_submit(sport_status='-', error_class=rule.kind, distance_m=sumDistance, elapsed_s=usedTime, steps=total_steps, ok=False)
                raise
            if appSportRecordListData['sportStatus'] != 1:
                rule = self.breaker.record_failure(self.accountId, appSportRecordListData['remark'])
                await self._record_submit(sport_status=appSportRecordListData['sportStatus'], error_class=rule.kind, distance_m=sumDistance, elapsed_s=usedTime, steps=total_steps, ok=False)
                raise TiShiNengError(appSportRecordListData['remark'], 200000)
            self.breaker.record_success(self.accountId)
            await self._record_submit(sport_status='1', error_class=None, distance_m=sumDistance, elapsed_s=usedTime, steps=total_steps, ok=True)
            logger.info('跑步数据上传完成')

    # ---------------------------------------------------------------- 调度
    def guard_schedule(self) -> bool:
        """活动日历 + 开始时间窗自检。默认只告警，``TSN_STRICT_WINDOW=1`` 才拦。"""
        key = RUN_TYPE_SCHEDULE_KEY[self.logRunType]
        now = datetime.datetime.now()
        target_minute = tsn_schedule.pick_start_minute(key, random.Random(f'{self.accountId}:{now.date()}:window'))
        actual_minute = now.hour * 60 + now.minute
        drift = abs(actual_minute - target_minute)
        window_ok = drift <= 90
        if not window_ok:
            message = f'当前 {now.strftime("%H:%M")} 与 {key} 典型开始时间 {target_minute // 60:02d}:{target_minute % 60:02d} 相差 {drift} 分钟'
            logger.warning(message + ('（严格模式，终止）' if self.strictWindow else '（仅告警）'))
        history = []
        try:
            history = [datetime.date.fromisoformat(d) for d in self.audit.recent_success_dates(str(self.accountId))]
        except ValueError:
            history = []
        calendar_ok = True
        if history and not tsn_schedule.should_run(self.accountId, now.date(), history):
            calendar_ok = False
            logger.warning(f'账号 {self.accountId} 按活动日历今天不该跑（连续天数/周频次/考试周），继续会形成全勤特征')
        if self.strictWindow:
            return window_ok and calendar_ok
        return True

    def checkVersion(self, remote_version) -> bool:
        mode = self.versionGuard.check(remote_version)
        if mode is tsn_policy.RunMode.READ_ONLY:
            self.audit.write(tsn_policy.AuditRecord(account_id=str(self.accountId), stage='version', ok=False, error=f'version drift -> {remote_version}'))
            return False
        return True

    async def startRun(self):
        allowed, reason = self.breaker.allow(self.accountId)
        if not allowed:
            raise TiShiNengError(f'熔断中，拒绝执行: {reason}', 200000)
        async for newDb in get_db():
            self.accountModel = await getTsnAccountByid(self.accountId, newDb)
            self.tsnClient = await getTsnClientById(self.accountModel.id, newDb)
        self.isPublic = self.tsnClient.isPublic()
        self.runtime = getattr(self.tsnClient, 'runtime', None)
        if self.runtime is not None:
            logger.info(f'运行时: {self.runtime.summary()}')
        if not self.guard_schedule():
            raise TiShiNengError('活动日历/时间窗自检未通过（严格模式）', 200000)
        canRunTypeList = []
        if self.isPublic:
            initSumRunResp = await self.tsnClient.sumExerciseRecord()
            if initSumRunResp['morningRun']['isShow'] == '1':
                canRunTypeList.append(TsnRunType.morningRun)
            if initSumRunResp['sunRun']['isShow'] == '1':
                canRunTypeList.append(TsnRunType.sumRun)
            if initSumRunResp['freedomRun']['isShow'] == '1':
                canRunTypeList.append(TsnRunType.freedom)
        else:
            initSettingResp = await self.tsnClient.sportRecordSetting()
            freedom = initSettingResp['freedom']
            sunRun = initSettingResp['sunRun']
            morningRun = initSettingResp['morningRun']
            if freedom == 0:
                canRunTypeList.append(TsnRunType.freedom)
            if sunRun == 0:
                canRunTypeList.append(TsnRunType.sumRun)
            if morningRun == 0:
                canRunTypeList.append(TsnRunType.morningRun)
        if len(canRunTypeList) == 0:
            raise TiShiNengError('没有可用的跑步类型', 200000)
        logger.info(f'可用跑步类型：{canRunTypeList}')
        if self.logRunType not in canRunTypeList:
            raise TiShiNengError('当前跑步类型不可用', 200000)

        # APK 版本探测：漂移则只读，避免拿老协议撞新签名
        try:
            versionResp = await self.tsnClient.tiShiNengBaseClient.stuAppVersion2()
            remoteVersion = tsn_policy.VersionGuard.extract_version(versionResp)
            if remoteVersion and not self.checkVersion(remoteVersion):
                raise TiShiNengError(f'APK 版本漂移（{remoteVersion}），进入只读模式，不提交', 200000)
        except TiShiNengError:
            raise
        except Exception as exc:
            logger.debug(f'版本探测失败（不阻塞）: {exc}')

        runLinePath = await self.queryPath()
        startPoint = runLinePath[0]
        logger.info(f'起点信息：{startPoint}')
        if self.isPublic:
            longitude = startPoint[0]
            latitude = startPoint[1]
            if longitude is not None and latitude is not None:
                longitude, latitude = self._randomize_coordinates(longitude, latitude, self.rng)
            self.startLongitude = longitude
            self.startLatitude = latitude
            self.exerciseSetting = await self.tsnClient.getExerciseSetting(self.publicRunTypeConvert(self.logRunType), longitude, latitude)
            logger.info(f'school{self.tsnClient.schoolId} setting Info:{self.exerciseSetting}')
            await self.tsnClient.getExerciseExplanation()
        else:
            self.exerciseSetting = await self.tsnClient.getSportSetting(self.logRunType.value)
        if self.identify is None or self.start_timestamp is None:
            self.identify = self.exerciseSetting['identify']
            if self.isPublic:
                startTimeResp = await self.tsnClient.getExerciseStartTime(self.identify)
            else:
                startTimeResp = await self.tsnClient.getRunningStartTime(self.identify)
            self.start_timestamp = int(startTimeResp['startTime']) + self.rng.randint(14, 20) * 1000
        self.isStartFace = int(self.exerciseSetting.get('isStartFace', 0))
        self.isEndFace = int(self.exerciseSetting.get('isEndFace', 0))
        self.middleFaces = self.exerciseSetting.get('middleFaces', [])
        self.isMidwayFace = int(self.exerciseSetting.get('isMidwayFace', 0))
        if len(self.middleFaces) == 0 and self.isMidwayFace == 1:
            self.middleFaces.append(self.rng.uniform(0.5, 1.2))
        if not (len(self.middleFaces) > 0 or self.isStartFace == 1 or self.isEndFace == 1 or (self.isMidwayFace == 1)):
            self.isFaceStatus = 0
        if hasattr(self.tsnClient, 'setEnvSignals'):
            self.tsnClient.setEnvSignals(tsn_environment.clean_device_signals())
        if self.isStartFace == 1:
            logger.info('开始人脸识别')
            await asyncio.sleep(self.rng.uniform(7, 12))
            await self.uploadFace(coordinates=f'{self.startLatitude},{self.startLongitude}', faceType=1)
        self.geofence = self.exerciseSetting['geofence']
        self.pointList = self.exerciseSetting.get('list', [])
        if self.pointList == '':
            self.pointList = []
        if 'campusList' in self.exerciseSetting:
            campusList = self.exerciseSetting['campusList']
            campusIdPointDict = {}
            for campus in campusList:
                campusIdPointDict[campus['id']] = campus['point']
            userInfo = await self.tsnClient.getLoginUserInfo()
            campusId = userInfo['campusId']
            if campusId in campusIdPointDict:
                self.pointList = campusIdPointDict[campusId]
            else:
                self.pointList = campusIdPointDict[self.rng.choice(list(campusIdPointDict.keys()))]
        logger.info(f'pointList:{self.pointList}')
        self.needRunKm = self.exerciseSetting['totalRange']
        if self.runKiloMeter < self.needRunKm:
            self.runKiloMeter = self.needRunKm + self.rng.uniform(0.1, 0.2)
        logger.info(self.exerciseSetting)
        # 同一账号配速相对稳定，只在 5.0 min/km 附近 ±0.15 漂移
        pace_bias = self.rng.uniform(-0.15, 0.15)
        self.planUseTime = self.runKiloMeter * (5.0 + pace_bias) * 60
        logger.info(f'planUseTime:{self.planUseTime}')
        if self.isPublic:
            self.endStride = self.exerciseSetting['endStride']
            self.limitSpeed = self.exerciseSetting['limitSpeed']
            self.endLimitStepFrequency = self.exerciseSetting['endLimitStepFrequency']
        await asyncio.sleep(self.rng.uniform(1, 4))
        geoData, stepList, sumDistance = genTiShiNengRunPathRepeat(
            runLinePath,
            self.runKiloMeter * 1000,
            int(self.start_timestamp) + self.rng.randint(1200, 1800),
            self.planUseTime,
            self.isPublic,
            rng=self.rng,
            sim=self.simOptions,
            pace_s_per_km=self.planUseTime / max(1e-6, self.runKiloMeter),
        )
        # 首点必须与 GetExerciseSetting 上报的坐标一致，否则起点对不上围栏/打卡点
        if self.isPublic and geoData:
            geoData[0]['o'] = self.startLongitude
            geoData[0]['a'] = self.startLatitude
            geoData[0]['t'] = int(self.start_timestamp)
        await self.uploadRunPath(geoData, stepList, sumDistance)

    async def startRunHandle(self):
        logger.info('开始运行')
        try:
            await self.startRun()
        finally:
            if self.runtime is not None:
                await self.runtime.aclose()
