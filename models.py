import uuid
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, Index, UniqueConstraint, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base

class RunPath(Base):
    __tablename__ = 'tsn_run_path'
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_path_id = Column(String(100), unique=True, nullable=False, index=True, comment='跑步路径ID')
    school_code = Column(String(50), nullable=False, index=True, comment='学校代码')
    sport_range = Column(Float, nullable=False, comment='运动范围/距离')
    run_line_path = Column(Text, nullable=False, comment='跑步路线(JSON格式的GeoJSON)')
    point_id_list = Column(Text, nullable=True, comment='打卡点ID列表(JSON格式)')
    ok_point_list_json = Column(Text, nullable=True, comment='已打卡点列表(JSON格式)')
    is_public = Column(Boolean, default=True, comment='是否公开')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment='创建时间')
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment='更新时间')
    __table_args__ = (Index('idx_school_code_run_path_id', 'school_code', 'run_path_id'),)

    def __repr__(self):
        return f'<RunPath(id={self.id}, run_path_id={self.run_path_id}, school_code={self.school_code})>'

def getUUID4Str():
    return str(uuid.uuid4())

class TsnAccount_Model(Base):
    __tablename__ = 'tsn_account'
    __table_args__ = (UniqueConstraint('user_id', 'school_id', name='user_id_school_id_unique'),)
    id = Column(Integer, primary_key=True)
    student_id = Column(String(64))
    user_id = Column(String(64), index=True)
    school_id = Column(Integer, ForeignKey('tsn_school.school_id'), nullable=False)
    school = relationship('TsnSchool_Model', overlaps='accounts')
    username = Column(String(64), nullable=False)
    password = Column(String(64), nullable=False)
    mobile_device_id = Column(String(128), default=getUUID4Str, nullable=False)
    access_token = Column(String(128), nullable=False)
    refresh_token = Column(String(128), nullable=False)
    expires_in = Column(Integer, nullable=False)
    auth_code = Column(String(128), default=getUUID4Str)

class TsnSchool_Model(Base):
    __tablename__ = 'tsn_school'
    id = Column(Integer, primary_key=True)
    school_id = Column(Integer, unique=True)
    accounts = relationship('TsnAccount_Model', overlaps='school')
    school_name = Column(String(128), nullable=False)
    school_url = Column(String(128), nullable=False)
    lan_url = Column(String(128))
    open_id = Column(String(128), nullable=False)
    is_open_keep = Column(Boolean, default=False)
    is_open_live = Column(Boolean, default=False)
    is_open_encry = Column(Boolean, default=False)
    sys_type = Column(Integer, nullable=False)
    school_code = Column(String(128), nullable=False)
    province_name = Column(String(50), nullable=True)

    def isPublicVersion(self):
        return self.sys_type == 2


class TsnDevice_Model(Base):
    """账号级设备指纹（方案 P0-3）：一个账号锁死一台"物理手机"。"""

    __tablename__ = 'tsn_device'
    device_pk = Column(String(64), primary_key=True, comment='设备主键')
    account_id = Column(String(64), ForeignKey('tsn_account.id'), unique=True, nullable=False, comment='绑定账号（1:1）')
    android_id = Column(String(32), nullable=False, index=True, comment='16 位 hex，账号内恒定')
    manufacturer = Column(String(64), nullable=False)
    model = Column(String(64), nullable=False)
    board = Column(String(64), nullable=False)
    os_version = Column(String(16), nullable=False)
    abi = Column(String(32), nullable=False)
    screen_w = Column(Integer, nullable=False)
    screen_h = Column(Integer, nullable=False)
    density = Column(Integer, nullable=False)
    boot_time_ms = Column(BigInteger, nullable=False, comment='开机时刻（13 位 ms）')
    apk_version = Column(String(32), nullable=False, default='2.0.23')
    province = Column(String(32), nullable=True)
    bound_at = Column(DateTime(timezone=True), server_default=func.now(), comment='绑定时间')
    last_seen_ip = Column(String(64), nullable=True)
    last_seen_lat = Column(Float, nullable=True)
    last_seen_lng = Column(Float, nullable=True)

    def __repr__(self):
        return f'<TsnDevice(account={self.account_id}, {self.manufacturer} {self.model}, android_id={self.android_id})>'


class TsnEgressBinding_Model(Base):
    """账号 -> 出口 IP 的稳定绑定（方案 P0-1 / P2-4）。"""

    __tablename__ = 'tsn_egress_binding'
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String(64), ForeignKey('tsn_account.id'), unique=True, nullable=False)
    egress_key = Column(String(32), nullable=False, index=True, comment='出口哈希，避免明文落库')
    province = Column(String(32), nullable=True)
    bound_at = Column(DateTime(timezone=True), server_default=func.now())
    polluted = Column(Boolean, default=False, comment='该组合是否已被判定污染')


class TsnSubmitLog_Model(Base):
    """每次提交的结果摘要：有效提交率 / 账号存活天数 的数据源（方案 P2-5）。"""

    __tablename__ = 'tsn_submit_log'
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String(64), nullable=False, index=True)
    run_date = Column(String(10), nullable=False, index=True, comment='YYYY-MM-DD')
    run_type = Column(Integer, nullable=True)
    sport_status = Column(String(16), nullable=True, comment='服务端 sportStatus')
    error_class = Column(String(32), nullable=True)
    distance_m = Column(Float, nullable=True)
    elapsed_s = Column(Float, nullable=True)
    steps = Column(Integer, nullable=True)
    device_pk = Column(String(64), nullable=True)
    egress_key = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index('idx_submit_account_date', 'account_id', 'run_date'),)
