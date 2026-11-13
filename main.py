import asyncio
import random
import sys
import uuid
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from TiShiNengSdkPrivate import TiShiNengPrivate
from database import get_db, init_db
from models import TsnAccount_Model
from services.tsnSchool.tsnSchoolDao import getSchoolListDao, addOrUpdateSchool
from tsnClient import tsnPasswordAuthServer, getTsnClientById
from tsnRunServer import TsnRunServer, TsnRunType
from spiderServer import startSpider
from validCountServer import crawl_valid_count_for_account
import tsn_device
import tsn_egress
import tsn_face_provider
import tsn_fingerprint
import tsn_policy
import tsn_runtime
import tsn_track_sim

def getClient():
    tsn = TiShiNengPrivate(1, 1, '', False, str(uuid.uuid4()), 'Xiaomi', '25053RT47C', '')
    return tsn

async def getSchoolInfo(schoolCode):
    """学校云地址查询：同样走统一指纹客户端，不暴露 python-httpx UA。"""
    url = f'https://h.tsnkj.com/upms/sysSchool/getSchoolInfo?schoolCode={schoolCode}'
    session = tsn_fingerprint.TsnHttpSession()
    try:
        resp = await session.get(url, headers=tsn_fingerprint.build_headers('h.tsnkj.com', content_type=None))
        return resp.json()['data']
    finally:
        await session.aclose()

class TsnCliManager:

    def __init__(self):
        self.running = True

    def print_header(self, title: str):
        print('\n' + '=' * 60)
        print(f'  {title}')
        print('=' * 60)

    def print_menu(self):
        self.print_header('TiShiNeng 管理系统')
        print('1. 更新学校列表')
        print('2. 授权账号')
        print('3. 开始跑步')
        print('4. 爬取路径数据')
        print('5. 更新人脸图片')
        print('6. 查询跑步有效次数和里程')
        print('7. 环境与指纹自检')
        print('0. 退出系统')
        print('=' * 60)

    async def update_school_list(self):
        self.print_header('更新学校列表')
        print('正在从服务器获取省份列表...')
        try:
            tsn = getClient()
            resp = await tsn.findAllProvince()
            if not resp or 'data' not in resp:
                print('❌ 获取省份列表失败')
                return
            provinces = resp['data']
            print('\n请选择要更新的省份（输入编号）:')
            print('-' * 60)
            for idx, province in enumerate(provinces, 1):
                print(f"{idx}. {province['province_name']}")
            print('0. 更新全部省份')
            print('-' * 60)
            choice = input('\n请输入省份编号 (0=全部): ').strip()
            if choice == '0':
                selected_provinces = provinces
                print('将更新全部省份...')
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(provinces):
                        selected_provinces = [provinces[idx]]
                        print(f"已选择: {provinces[idx]['province_name']}")
                    else:
                        print('❌ 无效编号，将更新全部省份')
                        selected_provinces = provinces
                except ValueError:
                    print('❌ 输入无效，将更新全部省份')
                    selected_provinces = provinces
            total_schools = 0
            seq = 0
            async for db in get_db():
                for province in selected_provinces:
                    province_name = province['province_name']
                    print(f'\n正在处理省份: {province_name}')
                    resp = await tsn.listSchoolByProvinceId(province['province_id'])
                    if not resp or 'data' not in resp:
                        continue
                    schools = resp['data']
                    for school in schools:
                        school_name = school['school_name']
                        if 'demo' in school_name.lower() or 'test' in school_name.lower():
                            continue
                        seq += 1
                        lan_url = None
                        if school['sysType'] == '2':
                            try:
                                schoolInfo = await getSchoolInfo(school['schoolCode'])
                                if schoolInfo:
                                    lan_url = schoolInfo.get('url')
                                    if lan_url:
                                        lan_url = f'https://{lan_url}'
                            except Exception as e:
                                logger.debug(f'Failed to get school info for {school_name}: {e}')
                        await addOrUpdateSchool(school['school_id'], school['school_name'], school['school_url'], lan_url, school['openId'], school['isOpenKeep'] == '1', school['isOpenLive'] == '1', school['isOpenEncry'] == '1', int(school['sysType']), school['schoolCode'], db, province_name=province['province_name'])
                        total_schools += 1
                        print(f'  {seq}. {school_name}')
            print(f'\n✅ 学校列表更新完成！共更新 {total_schools} 所学校')
        except Exception as e:
            logger.exception(e)
            print(f'❌ 更新失败: {str(e)}')

    async def authorize_account(self):
        self.print_header('授权账号')
        try:
            async for db in get_db():
                all_schools = await getSchoolListDao(db)
                province_set = set()
                for s in all_schools:
                    if s.province_name:
                        province_set.add(s.province_name)
                province_list = sorted(province_set)
                if not province_list:
                    print('❌ 数据库中没有省份信息，请先更新学校列表')
                    return
                print('\n请选择省份（输入编号）:')
                print('-' * 60)
                for idx, pname in enumerate(province_list, 1):
                    print(f'{idx}. {pname}')
                print('0. 显示所有学校')
                print('-' * 60)
                try:
                    p_choice = input('\n请输入省份编号 (0=全部): ').strip()
                    if p_choice == '0':
                        filtered_schools = all_schools
                    else:
                        p_idx = int(p_choice) - 1
                        if 0 <= p_idx < len(province_list):
                            selected_province = province_list[p_idx]
                            print(f'已选择省份: {selected_province}')
                            filtered_schools = [s for s in all_schools if s.province_name == selected_province]
                        else:
                            print('❌ 无效编号，将显示全部学校')
                            filtered_schools = all_schools
                except ValueError:
                    print('❌ 输入无效，将显示全部学校')
                    filtered_schools = all_schools
                filtered_schools.sort(key=lambda s: s.school_id)
                if not filtered_schools:
                    print('❌ 所选省份下没有学校，请先更新学校列表')
                    return
                print('\n请选择学校:')
                print('-' * 60)
                for idx, school in enumerate(filtered_schools, 1):
                    sys_type_name = '公版' if school.sys_type == 2 else '私版'
                    print(f'{idx}. {school.school_name} ({sys_type_name})')
                print('-' * 60)
                try:
                    choice = input('\n请输入学校编号 (0=取消): ').strip()
                    if choice == '0':
                        return
                    school_idx = int(choice) - 1
                    if school_idx < 0 or school_idx >= len(filtered_schools):
                        print('❌ 无效的学校编号')
                        return
                    selected_school = filtered_schools[school_idx]
                    print(f'\n已选择: {selected_school.school_name}')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n' + '-' * 60)
                username = input('请输入用户名: ').strip()
                if not username:
                    print('❌ 用户名不能为空')
                    return
                password = input('请输入密码: ').strip()
                if not password:
                    print('❌ 密码不能为空')
                    return
                print('\n正在进行授权验证...')
                try:
                    uid = await tsnPasswordAuthServer(selected_school.school_id, username, password, db)
                    print('\n✅ 授权成功！')
                    print(f'用户ID: {uid}')
                    print(f'学校: {selected_school.school_name}')
                except Exception as e:
                    logger.exception(e)
                    print(f'❌ 授权失败: {str(e)}')
        except Exception as e:
            logger.exception(e)
            print(f'❌ 操作失败: {str(e)}')

    async def start_running(self):
        self.print_header('开始跑步')
        try:
            async for db in get_db():
                stmt = select(TsnAccount_Model).options(selectinload(TsnAccount_Model.school))
                result = await db.execute(stmt)
                accounts = result.scalars().all()
                if not accounts:
                    print('❌ 没有可用的账号，请先授权账号')
                    return
                print('\n请选择账号:')
                print('-' * 60)
                for idx, account in enumerate(accounts, 1):
                    school_name = account.school.school_name if account.school else '未知学校'
                    print(f'{idx}. {account.username} - {school_name}')
                print('-' * 60)
                try:
                    choice = input('\n请输入账号编号 (0=取消): ').strip()
                    if choice == '0':
                        return
                    account_idx = int(choice) - 1
                    if account_idx < 0 or account_idx >= len(accounts):
                        print('❌ 无效的账号编号')
                        return
                    selected_account = accounts[account_idx]
                    print(f'\n已选择账号: {selected_account.username}')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n请选择跑步类型:')
                print('-' * 60)
                print('1. 晨跑 (Morning Run)')
                print('2. 阳光跑 (Sun Run)')
                print('3. 自由跑 (Freedom Run)')
                print('-' * 60)
                run_type_map = {'1': TsnRunType.morningRun, '2': TsnRunType.sumRun, '3': TsnRunType.freedom}
                run_type_name_map = {'1': '晨跑', '2': '阳光跑', '3': '自由跑'}
                try:
                    run_choice = input('\n请输入跑步类型编号 (0=取消): ').strip()
                    if run_choice == '0':
                        return
                    if run_choice not in run_type_map:
                        print('❌ 无效的跑步类型')
                        return
                    selected_run_type = run_type_map[run_choice]
                    print(f'已选择: {run_type_name_map[run_choice]}')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n' + '-' * 60)
                try:
                    distance_str = input('请输入跑步距离(公里，例如: 2.5): ').strip()
                    distance = float(distance_str)
                    if distance <= 0:
                        print('❌ 距离必须大于0')
                        return
                    if distance > 50:
                        print('❌ 距离过长，请输入合理的距离')
                        return
                    print(f'跑步距离: {distance}km')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n' + '=' * 60)
                print('跑步任务信息:')
                print(f'  账号: {selected_account.username}')
                print(f'  学校: {selected_account.school.school_name}')
                print(f'  类型: {run_type_name_map[run_choice]}')
                print(f'  距离: {distance}km')
                print('=' * 60)
                confirm = input('\n确认开始跑步? (y/n): ').strip().lower()
                if confirm != 'y':
                    print('已取消')
                    return
                print('\n开始执行跑步任务...')
                try:
                    run_server = TsnRunServer(accountId=selected_account.id, runKiloMeter=distance, logRunType=selected_run_type)
                    await run_server.startRunHandle()
                    print('\n✅ 跑步任务完成！')
                except Exception as e:
                    logger.exception(e)
                    print(f'\n❌ 跑步失败: {str(e)}')
        except Exception as e:
            logger.exception(e)
            print(f'❌ 操作失败: {str(e)}')

    async def update_face_images(self):
        self.print_header('更新人脸图片')
        try:
            async for db in get_db():
                stmt = select(TsnAccount_Model).options(selectinload(TsnAccount_Model.school))
                result = await db.execute(stmt)
                accounts = result.scalars().all()
                if not accounts:
                    print('❌ 没有可用的账号，请先授权账号')
                    return
                print('\n请选择要更新人脸图片的账号:')
                print('-' * 60)
                for idx, account in enumerate(accounts, 1):
                    school_name = account.school.school_name if account.school else '未知学校'
                    print(f'{idx}. {account.username} - {school_name}')
                print('-' * 60)
                try:
                    choice = input('\n请输入账号编号 (0=取消): ').strip()
                    if choice == '0':
                        return
                    account_idx = int(choice) - 1
                    if account_idx < 0 or account_idx >= len(accounts):
                        print('❌ 无效的账号编号')
                        return
                    selected_account = accounts[account_idx]
                    school_name = selected_account.school.school_name if selected_account.school else '未知学校'
                    print(f'\n已选择账号: {selected_account.username} - {school_name}')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n' + '=' * 60)
                print('更新人脸图片任务信息:')
                print(f'  账号: {selected_account.username}')
                print(f'  学校: {school_name}')
                print('  说明: 将从服务器下载最新的人脸图片并保存到本地')
                print('=' * 60)
                confirm = input('\n确认开始更新? (y/n): ').strip().lower()
                if confirm != 'y':
                    print('已取消')
                    return
                print('\n开始更新人脸图片...')
                try:
                    run_server = TsnRunServer(accountId=selected_account.id, runKiloMeter=2.0, logRunType=TsnRunType.freedom)
                    run_server.accountModel = selected_account
                    run_server.tsnClient = await getTsnClientById(selected_account.id, db)
                    run_server.isPublic = run_server.tsnClient.isPublic()
                    face_image_data = await run_server.getFaceImage()
                    if face_image_data:
                        print('\n✅ 人脸图片更新完成！')
                    else:
                        print('\n⚠️ 未获取到人脸图片，请检查账号设置')
                except Exception as e:
                    logger.exception(e)
                    print(f'\n❌ 更新失败: {str(e)}')
        except Exception as e:
            logger.exception(e)
            print(f'❌ 操作失败: {str(e)}')

    async def crawl_paths(self):
        self.print_header('爬取路径数据')
        try:
            async for db in get_db():
                stmt = select(TsnAccount_Model).options(selectinload(TsnAccount_Model.school))
                result = await db.execute(stmt)
                accounts = result.scalars().all()
                if not accounts:
                    print('❌ 没有可用的账号，请先授权账号')
                    return
                print('\n请选择要爬取的账号:')
                print('-' * 60)
                for idx, account in enumerate(accounts, 1):
                    school_name = account.school.school_name if account.school else '未知学校'
                    sys_type = '公版' if account.school and account.school.sys_type == 2 else '私版'
                    print(f'{idx}. {account.username} - {school_name} ({sys_type})')
                print('-' * 60)
                try:
                    choice = input('\n请输入账号编号 (0=取消): ').strip()
                    if choice == '0':
                        return
                    account_idx = int(choice) - 1
                    if account_idx < 0 or account_idx >= len(accounts):
                        print('❌ 无效的账号编号')
                        return
                    selected_account = accounts[account_idx]
                    school_name = selected_account.school.school_name if selected_account.school else '未知学校'
                    print(f'\n已选择账号: {selected_account.username} - {school_name}')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n' + '=' * 60)
                print('爬取任务信息:')
                print(f'  账号: {selected_account.username}')
                print(f'  学校: {school_name}')
                print('  说明: 将从该账号的历史运动记录中爬取路径数据')
                print('=' * 60)
                confirm = input('\n确认开始爬取? (y/n): ').strip().lower()
                if confirm != 'y':
                    print('已取消')
                    return
                print('\n开始爬取路径数据...')
                print('提示: 爬取过程可能需要一些时间，请耐心等待...\n')
                try:
                    await startSpider(selected_account.id)
                    print('\n✅ 路径数据爬取完成！')
                except Exception as e:
                    logger.exception(e)
                    print(f'\n❌ 爬取失败: {str(e)}')
        except Exception as e:
            logger.exception(e)
            print(f'❌ 操作失败: {str(e)}')

    async def query_running_distance(self):
        self.print_header('查询跑步统计')
        try:
            async for db in get_db():
                stmt = select(TsnAccount_Model).options(selectinload(TsnAccount_Model.school))
                result = await db.execute(stmt)
                accounts = result.scalars().all()
                if not accounts:
                    print('❌ 没有可用的账号，请先授权账号')
                    return
                print('\n请选择要查询的账号:')
                print('-' * 60)
                for idx, account in enumerate(accounts, 1):
                    school_name = account.school.school_name if account.school else '未知学校'
                    sys_type = '公版' if account.school and account.school.sys_type == 2 else '私版'
                    print(f'{idx}. {account.username} - {school_name} ({sys_type})')
                print('-' * 60)
                try:
                    choice = input('\n请输入账号编号 (0=取消): ').strip()
                    if choice == '0':
                        return
                    account_idx = int(choice) - 1
                    if account_idx < 0 or account_idx >= len(accounts):
                        print('❌ 无效的账号编号')
                        return
                    selected_account = accounts[account_idx]
                    school_name = selected_account.school.school_name if selected_account.school else '未知学校'
                    print(f'\n已选择账号: {selected_account.username} - {school_name}')
                except ValueError:
                    print('❌ 请输入有效的数字')
                    return
                print('\n正在查询跑步统计...')
                try:
                    vc = await crawl_valid_count_for_account(selected_account.id)
                    print('\n' + '=' * 60)
                    print(f'  账号: {selected_account.username}')
                    print(f'  学校: {school_name}')
                    print('-' * 60)
                    print(vc)
                    print('=' * 60)
                    print('\n✅ 查询完成！')
                except Exception as e:
                    print(f'\n❌ 查询失败: {str(e)}')
        except Exception as e:
            logger.exception(e)
            print(f'❌ 操作失败: {str(e)}')


    async def environment_selfcheck(self):
        """离线自检：设备自洽、出口池、HTTP 指纹、人脸池、熔断状态、轨迹物理量。"""
        self.print_header('环境与指纹自检')
        report = await run_selfcheck()
        print(report)

    async def run(self):
        await init_db()
        print('\n欢迎使用 TiShiNeng 管理系统!')
        while self.running:
            self.print_menu()
            choice = input('\n请选择操作 (0-7): ').strip()
            if choice == '1':
                await self.update_school_list()
            elif choice == '2':
                await self.authorize_account()
            elif choice == '3':
                await self.start_running()
            elif choice == '4':
                await self.crawl_paths()
            elif choice == '5':
                await self.update_face_images()
            elif choice == '6':
                await self.query_running_distance()
            elif choice == '7':
                await self.environment_selfcheck()
            elif choice == '0':
                print('\n感谢使用，再见!')
                self.running = False
            else:
                print('\n❌ 无效的选项，请重新选择')
            if self.running:
                input('\n按 Enter 键继续...')

async def run_selfcheck() -> str:
    """把 8 个检测面对应的自检项汇总成一份可读报告（不发起任何线上请求）。"""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    lines = []
    lines.append('--- 1. 设备指纹 ---')
    async for db in get_db():
        stmt = select(TsnAccount_Model).options(selectinload(TsnAccount_Model.school))
        accounts = (await db.execute(stmt)).scalars().all()
        if not accounts:
            lines.append('  (库里没有账号，下面用演示档案)')
            demo = tsn_device.provision_device('demo', '广东省')
            issues = tsn_device.validate_device_consistency(demo)
            lines.append(f'  demo: {demo.manufacturer} {demo.model} Android {demo.os_version} android_id={demo.android_id}')
            if issues:
                lines.extend(f'  ! {i.code}: {i.message}' for i in issues)
            else:
                lines.append('  物理自洽 ✓')
        for account in accounts:
            record = await tsn_runtime.ensure_device(account, db)
            issues = tsn_device.validate_device_consistency(record)
            school = account.school.school_name if account.school else '-'
            lines.append(f'  [{account.id}] {account.username} @ {school}: {record.manufacturer} {record.model} Android {record.os_version} ({record.screen_w}x{record.screen_h}@{record.density}) android_id={record.android_id}')
            if issues:
                lines.extend(f'    ! {i.code}: {i.message}' for i in issues)
            else:
                lines.append('    ✓ 物理自洽')
        await db.commit()

    lines.append('')
    lines.append('--- 2. 出口池 ---')
    pool = tsn_egress.EgressPool.load()
    if not pool.egresses:
        lines.append('  未配置 tsn_egress.json：当前所有请求直连，D1/D2 必中')
        lines.append('  Device-as-Proxy 正确命令（注意是 forward，不是 reverse）:')
        lines.extend(f'    {cmd}' for cmd in tsn_egress.device_as_commands_preview())
    else:
        for province in sorted({e.province for e in pool.egresses}):
            lines.append('  ' + tsn_egress.egress_report(pool, province).replace('\n', '\n  '))

    lines.append('')
    lines.append('--- 3. HTTP / TLS 指纹 ---')
    session = tsn_fingerprint.TsnHttpSession()
    describe = session.describe()
    lines.append('  backend=' + describe['backend'] + f'  impersonate={describe["impersonate"]}')
    lines.append('  | 项 | 值 |')
    lines.append('  |---|---|')
    lines.extend('  ' + line for line in tsn_fingerprint.build_session_matrix(describe).splitlines())
    await session.aclose()

    lines.append('')
    lines.append('--- 4. 人脸底图池 ---')
    lines.append(tsn_face_provider.pool_report('demo').replace('\n', '\n  '))

    lines.append('')
    lines.append('--- 5. 熔断 / 版本 / 审计 ---')
    breaker = tsn_policy.CircuitBreaker()
    lines.append('  ' + breaker.snapshot().replace('\n', '\n  '))
    lines.append('  ' + '本地 APK 版本 2.0.23；漂移检测会切 READ_ONLY')
    usage = tsn_policy.audit_disk_usage()
    lines.append(f'  审计目录: {usage["files"]} 文件 / {usage["mb"]} MB')

    lines.append('')
    lines.append('--- 6. 轨迹物理量（演示 2.5km / 5:00 配速）---')
    route = [[113.30 + i * 0.0002, 23.10 + i * 0.0001] for i in range(200)]
    track = tsn_track_sim.simulate_run(route, 2500.0, 1763000000000, 300.0, rng=random.Random(7))
    stats = tsn_track_sim.describe_simulation(track)
    lines.append('  ' + json_dumps(stats))
    geo_issues = tsn_track_sim.validate_geo_constraints(track.points)
    lines.append('  地理约束: ' + ('通过' if not geo_issues else '; '.join(f'{i.code}' for i in geo_issues)))
    return '\n'.join(lines)


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


async def main():
    cli = TsnCliManager()
    await cli.run()


async def _selftest():
    await init_db()
    print(await run_selfcheck())
if __name__ == '__main__':
    try:
        if '--selftest' in sys.argv:
            asyncio.run(_selftest())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        print('\n\n程序已被用户中断')
    except Exception as e:
        logger.exception(e)
        print(f'\n程序异常退出: {str(e)}')
