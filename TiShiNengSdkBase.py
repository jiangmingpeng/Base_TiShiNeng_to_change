import hashlib
import urllib.parse
from loguru import logger
import tsn_fingerprint
import tsn_signer

class TiShiNengSdkBase:

    def __init__(self, uid, schoolId, deviceId, brandName, deviceNum, token, session=None, proxy=None):
        self.uid = uid
        self.schoolId = schoolId
        self.deviceId = deviceId
        self.brandName = brandName
        self.deviceNum = deviceNum
        self.appId = 'move'
        self.appSecret = 'e8167ef026cbc5e456ab837d9d6d9254'
        self.appSign = '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3'
        self.apiUrl = 'https://m.boxkj.com'
        self._host = 'm.boxkj.com'
        self.platform = '1'
        self.versionName = tsn_fingerprint.OKHTTP_VERSION_NAME
        self.version = '20230'
        # 统一出口客户端：业务层不再直接构造 httpx.AsyncClient
        self.httpClient = session or tsn_fingerprint.TsnHttpSession(proxy=proxy)
        self.token = token
        self.baseHeaders = {'token': self.token, 'channel': 'Android', 'version': self.version, 'type': '0'}
        self.headers = self.baseHeaders

    def buildHeaders(self, extra=None):
        """OkHttp4 顺序的 header 列表（顺序本身是指纹的一部分）。"""
        merged = dict(self.baseHeaders)
        merged.update(extra or {})
        return tsn_fingerprint.build_headers(self._host, content_type='application/x-www-form-urlencoded', extra=merged)

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

    def getSign(self, params: dict):
        ctx = tsn_signer.SignContext(token=self.token, timestamp='', app_id='move', app_secret=self.appSecret)
        signature = tsn_signer.resolve_signer('2.0.23:base').sign('/api', params, ctx)['sign']
        params.update({'appId': 'move', 'appSecret': self.appSecret})
        return signature

    async def httpPost(self, url, data):
        url = self.apiUrl + url
        try:
            data['timestamp'] = tsn_fingerprint.now_ms()
            sign = self.getSign(data.copy())
            data['sign'] = sign
            resp = await self.httpClient.post(url=url, data=data, headers=self.buildHeaders())
            if resp.status_code == 200:
                resp = resp.json()
                if resp['returnCode'] == '200':
                    return resp
                raise Exception(resp['returnMsg'])
            else:
                return None
        except Exception as e:
            logger.exception(e)
            return None

    async def findAllProvince(self):
        params = {}
        url = '/app/province/findAllProvince'
        return await self.httpPost(url, params)

    async def listSchoolByProvinceId(self, provinceId):
        params = {'provinceId': provinceId}
        url = '/app/sch/listSchoolByProvinceId'
        return await self.httpPost(url, params)

    async def getSchoolById(self, schoolId):
        params = {'schoolId': schoolId}
        url = '/app/sch/getSchoolById'
        return await self.httpPost(url, params)

    async def addUploadRecord(self):
        params = {'schoolId': self.schoolId, 'platform': self.platform, 'deviceId': self.deviceId, 'brandName': self.brandName, 'deviceNum': self.deviceNum, 'sysUserId': '', 'versionName': self.versionName}
        url = '/admin/analysis/addUploadRecord'
        return await self.httpPost(url, params)

    async def stuAppVersion2(self):
        params = {'schoolId': self.schoolId, 'appSign': self.appSign}
        url = '/admin/studentAppUpdate/stuAppVersion2'
        return await self.httpPost(url, params)

    async def getReplyFeedBackCountUser(self):
        url = '/app/replyFeedBack/getReplyFeedBackCountUser'
        params = {'schoolId': self.schoolId, 'userId': self.uid}
        return await self.httpPost(url, params)

    def getHttpClient(self):
        return self.httpClient
