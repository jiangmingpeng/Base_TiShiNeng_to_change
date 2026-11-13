import hashlib
import io
import time
import urllib.parse
from loguru import logger
from AesUtils import AESCrypto
from RsaUtils import RSACrypto
from TiShiNengError import TiShiNengError
from TiShiNengSdkBase import TiShiNengSdkBase
import tsn_fingerprint
import tsn_signer

class TiShiNengPrivate:

    def __init__(self, uid, schoolId, schoolCode, isOpenEncry, deviceId, brandName, deviceNum, token, session=None, proxy=None):
        self.uid = uid
        self.schoolCode = schoolCode
        self.isOpenEncry = isOpenEncry
        self.schoolId = schoolId
        self.deviceId = deviceId
        self.brandName = brandName
        self.deviceNum = deviceNum
        self.appId = '791f3afb1da44db3bd0c67ffdfb2394b'
        self.appSecret = 'e8167ef026cbc5e456ab837d9d6d9254'
        self.appSign = '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3'
        self.schoolUrl = ''
        self.platform = '1'
        self._host = None
        self.tiShiNengBaseClient = TiShiNengSdkBase(uid, schoolId, deviceId, brandName, deviceNum, token, session=session, proxy=proxy)
        self.versionName = self.tiShiNengBaseClient.versionName
        self.httpClient = self.tiShiNengBaseClient.getHttpClient()
        self.useXieQu = False
        self.token = token
        self.rsaUtils = RSACrypto('MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAq4laolA7zAk7jzsqDb3Oa5pS/uCPlZfASK8Soh/NzEmry77QDZ2koyr96M5Wx+A9cxwewQMHzi8RoOfb3UcQO4UDQlMUImLuzUnfbk3TTppijSLH+PU88XQxcgYm2JTa546c7JdZSI6dBeXOJH20quuxWyzgLk9jAlt3ytYygPQ7C6o6ZSmjcMgE3xgLaHGvixEVpOjL/pdVLzXhrMqWVAnB/snMjpCqesDVTDe5c6OOmj2q5J8n+tzIXtnvrkxQSDaUp8DWF8meMwyTErmYklMXzKic2rjdYZpHh4x98Fg0Q28sp6i2ZoWiGrJDKW29mntVQQiDNhKDawb4B45zUwIDAQAB')
        self.baseHeaders = {'token': self.token, 'channel': 'Android', 'version': self.tiShiNengBaseClient.version, 'type': '0'}
        self.headers = self.baseHeaders

    def buildHeaders(self, content_type='application/x-www-form-urlencoded', extra=None):
        """OkHttp4 顺序的 header 列表；multipart 时传 content_type=None 由客户端补 boundary。"""
        merged = dict(self.baseHeaders)
        merged.update(extra or {})
        return tsn_fingerprint.build_headers(self._host or 'localhost', content_type=content_type, extra=merged)

    def isPublic(self):
        return False

    @staticmethod
    def kVtoStr(key, value, is_encoded):
        if is_encoded:
            try:
                encoded_value = urllib.parse.quote(value)
                return f'{key}={encoded_value}'
            except Exception:
                return f'{key}={value}'
        else:
            return f'{key}={value}'

    @staticmethod
    def getMd5(text):
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def getTokenAesEncrypt(self, text):
        md5Token = self.getMd5(self.token)
        key = md5Token[0:16]
        iv = md5Token[16:32]
        aesUtils = AESCrypto(key, iv)
        return aesUtils.encrypt(text)

    def setAccessToken(self, token):
        self.token = token
        self.baseHeaders = {'token': self.token, 'channel': 'Android', 'version': self.tiShiNengBaseClient.version, 'type': '0'}
        self.headers = self.baseHeaders

    def setSchoolUrl(self, schoolUrl):
        if schoolUrl[-1] == '/':
            schoolUrl = schoolUrl[:-1]
        self.schoolUrl = schoolUrl
        parsed = urllib.parse.urlparse(schoolUrl)
        if parsed.netloc:
            self._host = parsed.netloc

    def setAppId(self, appId):
        self.appId = appId

    def getEncryptAppId(self, timeStamp):
        text = self.appId + self.token + timeStamp
        return self.getMd5(self.getTokenAesEncrypt(text))

    def getEncryptAppSecret(self, timeStamp):
        text = self.token + timeStamp
        return self.getMd5(self.getTokenAesEncrypt(text))

    def signerContext(self, timestamp: str) -> 'tsn_signer.SignContext':
        return tsn_signer.SignContext(token=self.token, timestamp=timestamp, app_id=self.appId, app_secret=self.appSecret, is_open_encry=self.isOpenEncry, token_aes_encrypt=self.getTokenAesEncrypt, md5=self.getMd5)

    def getSign2(self, params: dict, timestamp: str):
        """加密签名：委托 signer，输出与原实现逐字节一致。"""
        ctx = self.signerContext(timestamp)
        # getSign2 被调用即代表走加密分支，与 isOpenEncry 的当前值无关
        ctx.is_open_encry = True
        path = tsn_signer.PRIVATE_ENCRYPTED_PATHS[0]
        signature = tsn_signer.resolve_signer('2.0.23:private').sign(path, params, ctx)['sign']
        params.pop('sign', None)
        params.update({'appId': ctx.md5(ctx.token_aes_encrypt(self.appId + self.token + timestamp)), 'appSecret': ctx.md5(ctx.token_aes_encrypt(self.token + timestamp))})
        return signature

    def getSign(self, params: dict):
        ctx = self.signerContext('')
        signature = tsn_signer.resolve_signer('2.0.23:private').sign('/api', params, ctx)['sign']
        params.pop('sign', None)
        params.update({'appId': self.appId, 'appSecret': self.appSecret})
        return signature

    async def httpPost(self, url, data):
        url = self.schoolUrl + url
        try:
            timestamp = str(int(time.time() * 1000))
            data['timestamp'] = timestamp
            if self.isOpenEncry and ('app/sportRecordSetting/getSetting' in url or 'app/appSportRecord/appAddSportRecord' in url):
                sign = self.getSign2(data.copy(), timestamp)
            else:
                sign = self.getSign(data.copy())
            data['sign'] = sign
            headers = self.buildHeaders()
            resp = await self.httpClient.post(url=url, data=data, headers=headers)
            if resp.status_code == 200:
                try:
                    resp = resp.json()
                except Exception:
                    logger.info(resp.text)
                if resp['returnCode'] == '200':
                    return resp['data']
                raise TiShiNengError(resp['returnMsg'])
            else:
                raise TiShiNengError(resp.text[:100], resp.status_code)
        except TiShiNengError as e:
            raise e

    async def findAllProvince(self):
        return await self.tiShiNengBaseClient.findAllProvince()

    async def listSchoolByProvinceId(self, provinceId):
        return await self.tiShiNengBaseClient.listSchoolByProvinceId(provinceId)

    async def getSchoolById(self, schoolId):
        return await self.tiShiNengBaseClient.getSchoolById(schoolId)

    async def addUploadRecord(self):
        return await self.tiShiNengBaseClient.addUploadRecord()

    async def stuAppVersion2(self):
        return await self.tiShiNengBaseClient.stuAppVersion2()

    async def getReplyFeedBackCountUser(self):
        return await self.tiShiNengBaseClient.getReplyFeedBackCountUser()

    async def appAnnouncementContentList(self):
        url = '/app/announcementContent/appAnnouncementContentList'
        params = {}
        return await self.httpPost(url, params)

    async def appLogin(self, username, password):
        url = '/app/appstu/login'
        encryptedPassword = self.rsaUtils.encrypt(password)
        params = {'uname': username, 'pwd': encryptedPassword}
        return await self.httpPost(url, params)

    async def noReadNotice(self):
        url = '/app/appnotice/noReadNotice'
        params = {'userId': self.uid, 'identity': '0'}
        return await self.httpPost(url, params)

    async def ispwdmod(self, username):
        url = '/app/ispwdmod'
        params = {'userNum': username}
        return await self.httpPost(url, params)

    async def appListNoticeData(self):
        url = '/app/appnotice/appListNoticeData'
        params = {'userId': self.uid, 'identity': '0', 'pageIndex': '1', 'pageSize': '10'}
        return await self.httpPost(url, params)

    async def getStudentInfo(self):
        url = '/app/getStudentInfo'
        params = {}
        return await self.httpPost(url, params)

    async def getCampusList(self):
        url = '/app/syscampus/getCampusList'
        params = {}
        return await self.httpPost(url, params)

    async def getFieldByCampus(self, campusId):
        url = '/app/field/getFieldByCampus'
        params = {'campusId': campusId}
        return await self.httpPost(url, params)

    async def sumSportRecord(self):
        url = '/app/appSportRecord/sumSportRecord'
        params = {'userId': self.uid}
        return await self.httpPost(url, params)

    async def sportRecordSetting(self):
        url = '/app/sportRecordSetting/setting'
        params = {'uid': self.uid}
        return await self.httpPost(url, params)

    async def getSportSetting(self, runType=2):
        url = '/app/sportRecordSetting/getSetting'
        params = {'uid': self.uid, 'runType': runType}
        return await self.httpPost(url, params)

    async def getSportSpecification(self, runType=2):
        url = '/app/appSportRecord/getSportSpecification'
        params = {'runType': runType}
        return await self.httpPost(url, params)

    async def getRunningStartTime(self, identify):
        url = '/app/sportRecordSetting/getRunningStartTime'
        params = {'identify': identify}
        return await self.httpPost(url, params)

    async def appSportRecordList(self, sportType=2, pageIndex=1, pageSize=10):
        url = '/app/appSportRecord/appSportRecordList'
        params = {'userId': self.uid, 'sportType': str(sportType), 'pageIndex': str(pageIndex), 'pageSize': str(pageSize)}
        return await self.httpPost(url, params)

    async def getSportRecordId(self, sportRecordId):
        url = '/app/appSportRecord/getSportRecordId'
        params = {'sportRecordId': sportRecordId}
        return await self.httpPost(url, params)

    async def appAddSportRecord(self, runType, startTime, endTime, gitudeLatitude, identify, formatSportTime, formatSportRange, avgspeed, speed, okPointList, stepNumbers, isFaceStatus, points, uploadType):
        url = '/app/appSportRecord/appAddSportRecord'
        params = {'userId': self.uid, 'runType': runType, 'startTime': startTime, 'endTime': endTime, 'gitudeLatitude': gitudeLatitude, 'identify': identify, 'formatSportTime': formatSportTime, 'formatSportRange': formatSportRange, 'avgspeed': avgspeed, 'speed': speed, 'okPointList': okPointList, 'brand': self.brandName, 'model': self.deviceNum, 'system': 'Android', 'version': '13', 'appVersion': self.versionName, 'stepNumbers': stepNumbers, 'isFaceStatus': isFaceStatus, 'points': points, 'uploadType': uploadType}
        return await self.httpPost(url, params)

    async def appRuningFace(self, faceBytes: bytes, identify):
        url = self.schoolUrl + '/app/runingFace/appRuningFace'
        timestamp = str(int(time.time() * 1000))
        fileNames = f'avatar{timestamp}.png'
        data = {'userId': self.uid, 'identify': identify, 'timestamp': timestamp}
        sign = self.getSign(data)
        headers = self.buildHeaders(content_type=None)
        file = {'userId': (None, str(self.uid).encode(), 'multipart/form-data; charset=utf-8'), 'identify': (None, identify.encode(), 'multipart/form-data; charset=utf-8'), 'file': (fileNames, io.BytesIO(faceBytes), 'image/png'), 'timestamp': (None, timestamp.encode(), 'multipart/form-data; charset=utf-8'), 'sign': (None, sign.encode(), 'multipart/form-data; charset=utf-8')}
        resp = await self.httpClient.post(url=url, files=file, headers=headers, timeout=30)
        if resp.status_code == 200:
            resp = resp.json()
            if int(resp['returnCode']) == 200:
                return resp['data']
            raise TiShiNengError(resp['returnMsg'])
        else:
            raise TiShiNengError(resp.text[:100], resp.status_code)
