# server_gui.py
import gc
import os
import random
import re
import shutil
import sys
import json
import socket
import threading
import socketserver
import time
import traceback
import uuid
import webbrowser
from datetime import datetime, timedelta

from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
import pytz

def resource_path(relative_path):
    """获取资源的绝对路径，支持开发环境和 PyInstaller 打包后的环境"""
    if getattr(sys, 'frozen', False):
        # 打包后的路径（PyInstaller 创建的临时目录）
        base_path = sys._MEIPASS
    else:
        # 开发环境路径
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

# ---------- 路径与时区配置 ----------
if getattr(sys, 'frozen', False):
    # 打包成 exe 时，使用 exe 所在目录
    SERVER_DIR = os.path.dirname(sys.executable)
else:
    # 开发环境使用当前文件所在目录
    SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DATA = os.path.join(SERVER_DIR, 'server_data')
CHAT_HISTORY_FILE = os.path.join(SERVER_DATA, 'chat_history.json')
PLAYERS_DIR = os.path.join(SERVER_DATA, 'players')
SERVER_CODES_FILE = os.path.join(SERVER_DATA, 'redeem_codes.json')
BANNED_AREAS_FILE = os.path.join(SERVER_DATA, 'banned_areas.json')
BANNED_TIMEZONES_FILE = os.path.join(SERVER_DATA, 'banned_timezones.json')
SERVER_CONFIG_FILE = os.path.join(SERVER_DATA, 'server_config.json')
# 添加自定义配置路径
CUSTOM_MENU_FILE = os.path.join(SERVER_DATA, 'custom_menu.json')
CUSTOM_ITEMS_FILE = os.path.join(SERVER_DATA, 'custom_items.json')
WEB_RESOURCES_DIR = os.path.join(SERVER_DATA, 'web_resources')  # 存放抽卡HTML等
os.makedirs(WEB_RESOURCES_DIR, exist_ok=True)

WHITELIST_FILE = os.path.join(SERVER_DATA, 'whitelist.json')
BLACKLIST_FILE = os.path.join(SERVER_DATA, 'blacklist.json')

HOST = '0.0.0.0'
PORT = 20082
SERVER_VERSION = "4.5.1"

# 全局版本放行开关（默认关闭）
ALLOW_ALL_VERSIONS = False

player_file_lock = threading.Lock()

# 服务器时区（全局，可动态修改）
SERVER_TIMEZONE = pytz.timezone('Asia/Shanghai')   # 东八区（默认）

AVAILABLE_CLOTHES = {
    "normal": {"name": "普通时装"},
    "26_Newyear": {"name": "柿柿如意"},
}

# 维护模式状态
maintenance_mode = False
maintenance_message = "服务器正在维护中，暂时无法加入服务器。"
maintenance_lock = threading.Lock()

def rename_player_files(old_server_uid, new_server_uid, log_callback=None):
    """
    安全重命名玩家的所有存档文件（主文件、_inv、_mails）。
    使用全局锁防止并发冲突，并自动处理目标文件已存在、文件占用重试等问题。
    所有日志通过 log_callback 输出（不 print）。
    返回 (success, error_message)
    """
    if not log_callback:
        log_callback = lambda msg: None

    # ★ 调试日志：确认函数被调用
    log_callback(f"[rename] rename_player_files 被调用: {old_server_uid} -> {new_server_uid}")

    if old_server_uid == new_server_uid:
        log_callback("[rename] 新旧UID相同，无需操作")
        return True, None

    # 构建路径
    suffixes = ['', '_inv', '_mails']
    old_paths = []
    new_paths = []

    # 只处理实际存在的文件
    for suf in suffixes:
        old_p = os.path.join(PLAYERS_DIR, f'{old_server_uid}{suf}.json')
        new_p = os.path.join(PLAYERS_DIR, f'{new_server_uid}{suf}.json')
        if os.path.exists(old_p):
            old_paths.append(old_p)
            new_paths.append(new_p)
            log_callback(f"[rename] 找到旧文件: {old_p}")
        else:
            log_callback(f"[rename] 旧文件不存在，跳过: {old_p}")

    # 主文件必须存在，否则无法继续
    main_exists = any(p.endswith(f'{old_server_uid}.json') for p in old_paths)
    if not main_exists:
        err = f"主文件缺失: {old_server_uid}.json"
        log_callback(f"[rename] 错误: {err}")
        return False, err

    if not old_paths:
        return True, None

    with player_file_lock:
        # ---------- 1. 备份并删除已存在的目标文件 ----------
        for new_p in new_paths:
            if os.path.exists(new_p):
                backup = new_p + '.bak'
                try:
                    log_callback(f"[rename] 目标文件已存在，备份至 {backup}")
                    shutil.copy2(new_p, backup)
                    os.remove(new_p)
                    log_callback(f"[rename] 已删除原目标文件 {new_p}")
                except Exception as e:
                    err = f"无法处理已存在的目标文件 {new_p}: {e}"
                    log_callback(f"[rename] 错误: {err}")
                    return False, err

        # ---------- 2. 重命名附属文件（_inv, _mails） ----------
        for idx, old_p in enumerate(old_paths):
            new_p = new_paths[idx]
            if old_p.endswith(f'{old_server_uid}.json'):
                continue
            success, err = _safe_rename_single(old_p, new_p, log_callback)
            if not success:
                log_callback(f"[rename] 附属文件重命名失败，操作中止")
                return False, err

        # ---------- 3. 最后重命名主文件 ----------
        main_old = os.path.join(PLAYERS_DIR, f'{old_server_uid}.json')
        main_new = os.path.join(PLAYERS_DIR, f'{new_server_uid}.json')
        success, err = _safe_rename_single(main_old, main_new, log_callback)
        if not success:
            log_callback(f"[rename] 主文件重命名失败，但附属文件已改名，状态不一致！需人工修复。")
            return False, err

        log_callback("[rename] 所有文件重命名成功")
        return True, None


def _safe_rename_single(old_path, new_path, log_callback, retry=3, delay=0.1):
    """
    对单个文件执行带重试的重命名，并处理 Windows 目标存在问题（已由调用方删除）。
    返回 (success, error_message)
    """
    for attempt in range(retry):
        try:
            gc.collect()  # 强制垃圾回收，释放残留句柄
            os.rename(old_path, new_path)
            log_callback(f"[rename] 成功: {old_path} → {new_path}")
            return True, None
        except OSError as e:
            log_callback(f"[rename] 第{attempt+1}次尝试失败: {e}")
            traceback.print_exc()  # 可保留，但会输出到控制台，如果不想要可注释掉
            if attempt < retry - 1:
                time.sleep(delay)
            else:
                err_msg = f"重命名失败 {old_path} -> {new_path}: {e}"
                return False, err_msg
        except Exception as e:
            log_callback(f"[rename] 未知异常: {e}")
            traceback.print_exc()
            return False, str(e)
    return False, "超过最大重试次数"

def set_server_timezone(tzname):
    """动态设置服务器时区，成功返回 True，失败返回 False"""
    global SERVER_TIMEZONE
    try:
        SERVER_TIMEZONE = pytz.timezone(tzname)
        return True
    except Exception:
        return False

def parse_version(ver_str):
    """
    解析版本字符串，返回 (数字元组, 后缀)
    例如 "4.3.6-beta" -> ((4,3,6), "beta")
         "4.3.6"     -> ((4,3,6), None)
    """
    if not ver_str:
        return None, None
    match = re.search(r'(\d+(?:\.\d+){1,2})', ver_str)
    if not match:
        return None, None
    num_part = match.group(1)
    num_tuple = tuple(map(int, num_part.split('.')))
    suffix = None
    if '-' in ver_str:
        suffix = ver_str.split('-', 1)[1].strip()
        if not suffix:
            suffix = None
    return num_tuple, suffix

def versions_compatible(client_ver, server_ver):
    """
    检查客户端版本与服务端版本是否兼容。
    仅比较数字部分（如 4.4.1），忽略后缀（如 -beta）。
    """
    c_num, _ = parse_version(client_ver)
    s_num, _ = parse_version(server_ver)
    if c_num is None or s_num is None:
        return False
    return c_num == s_num

# ==================== 数据持久化函数 ====================
def load_chat_history():
    if not os.path.exists(CHAT_HISTORY_FILE):
        return []
    with open(CHAT_HISTORY_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_chat_history(msg):
    os.makedirs(SERVER_DATA, exist_ok=True)
    history = load_chat_history()
    history.append(msg)
    if len(history) > 500:
        history = history[-500:]
    with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2)

# ==================== 聊天历史归档 ====================
def archive_chat_history():
    """将当前聊天记录归档为前一天的备份文件，并清空主历史文件"""
    if not os.path.exists(CHAT_HISTORY_FILE):
        return
    # 确定昨天的日期
    yesterday = (datetime.now(SERVER_TIMEZONE) - timedelta(days=1)).strftime('%Y-%m-%d')
    archive_file = os.path.join(SERVER_DATA, f'chat_history_{yesterday}.json')
    try:
        # 如果主文件存在，重命名为归档文件
        if os.path.exists(CHAT_HISTORY_FILE):
            shutil.move(CHAT_HISTORY_FILE, archive_file)
            # 创建新的空历史文件
            with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)
    except Exception as e:
        print(f"[归档] 失败: {e}")

def load_player_data(server_uid):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
    default_data = {
        'name': server_uid,  # 使用 server_uid 作为默认名称
        'userid': '',
        'uid': '',
        'permission': 'user',
        'banned': False,
        'banned_until': None,
        'redeemed_codes': [],
        'timezone': None,
        'muted_until': None,
        'total_interactions': 0,
        'total_open_count': 0,
        'total_sign_days': 0,
        'total_affection_up': 0,
        'total_sleep_count': 0,
        'total_chat_count': 0,
        'monthly_interactions': 0,
        'monthly_open_count': 0,
        'monthly_sign_days': 0,
        'monthly_affection_up': 0,
        'monthly_sleep_count': 0,
        'monthly_chat_count': 0,
        'monthly_credit_gained': 0,
        'monthly_cake_gained': 0,
        'current_month': None,
        'last_change_name_time': None,
        'change_name_count': 0,
        'redeem_stats': {},
        'gacha_history': [],
        'gacha_pity_5': 0,
        'gacha_pity_4': 0,
        'gacha_guaranteed_5': False,
        'total_five_star': 0,
        'gacha_pity_5': 0,
        'gacha_pity_4': 0,
        'gacha_guaranteed_5': False,
        'monthly_jade_gained': 0,
        'total_jade_gained': 0,
        'monthly_ticket_gained': 0,
        'total_ticket_gained': 0,
        'birthday_reward_year': None,  # 记录已发生日奖励的年份
        'last_achievement_month': None,  # 记录已发成就奖励的月份（格式 "YYYY-MM"）
    }

    if not os.path.exists(path):
        return default_data

    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                raise json.JSONDecodeError("Empty file", content, 0)
            data = json.loads(content)
            # 补全缺失字段（合并默认值）
            for key, value in default_data.items():
                data.setdefault(key, value)
            return data
    except (json.JSONDecodeError, OSError) as e:
        # 备份损坏文件
        import shutil
        backup_path = path + '.corrupted'
        try:
            shutil.copy2(path, backup_path)
            print(f"[警告] 玩家数据文件 {path} 损坏，已备份至 {backup_path}")
        except Exception as backup_e:
            print(f"[错误] 无法备份损坏文件: {backup_e}")

        # 写入默认数据
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(default_data, f, indent=2)
        return default_data

def save_player_data(server_uid, data):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def load_player_mails(server_uid):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}_mails.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_player_mails(server_uid, mails):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}_mails.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(mails, f, indent=2)

def load_player_inventory(server_uid):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}_inv.json')
    default_inv = {
        'balance': 0,
        'credit': 0,
        'jade': 0,
        'owned_clothes': ['normal'],
        'star_rail_tickets': 0,
        'custom_items': {}   # 新增：用于存储自定义物品及其数量
    }
    if not os.path.exists(path):
        return default_inv

    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                raise json.JSONDecodeError("Empty file", content, 0)
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("Inventory data is not a dict")
            # 补全缺失字段
            for key in default_inv:
                if key not in data:
                    data[key] = default_inv[key]
            return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # 备份并重建
        import shutil
        backup_path = path + '.corrupted'
        try:
            shutil.copy2(path, backup_path)
            print(f"[警告] 库存文件 {path} 损坏，已备份至 {backup_path}")
        except Exception as backup_e:
            print(f"[错误] 无法备份损坏文件: {backup_e}")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(default_inv, f, indent=2)
        return default_inv

def save_player_inventory(server_uid, inv):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}_inv.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(inv, f, indent=2)

def load_server_codes():
    if not os.path.exists(SERVER_CODES_FILE):
        return {}
    with open(SERVER_CODES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_server_codes(codes):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(SERVER_CODES_FILE, 'w', encoding='utf-8') as f:
        json.dump(codes, f, indent=2)

def load_banned_areas():
    if not os.path.exists(BANNED_AREAS_FILE):
        return []
    with open(BANNED_AREAS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_banned_areas(areas):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(BANNED_AREAS_FILE, 'w', encoding='utf-8') as f:
        json.dump(areas, f, indent=2)

def load_banned_timezones():
    if not os.path.exists(BANNED_TIMEZONES_FILE):
        return []
    with open(BANNED_TIMEZONES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_banned_timezones(tzones):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(BANNED_TIMEZONES_FILE, 'w', encoding='utf-8') as f:
        json.dump(tzones, f, indent=2)

BANNED_IPS_FILE = os.path.join(SERVER_DATA, 'banned_ips.json')

def load_banned_ips():
    if not os.path.exists(BANNED_IPS_FILE):
        return []
    with open(BANNED_IPS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_banned_ips(ips):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(BANNED_IPS_FILE, 'w', encoding='utf-8') as f:
        json.dump(ips, f, indent=2)

def load_server_config():
    if not os.path.exists(SERVER_CONFIG_FILE):
        return {'server_name': '流萤服务器', 'ai_enabled': True, 'chat_enabled': True, 'common_mute': False,
                'max_players': 10, 'access_mode': 'none'}
    with open(SERVER_CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
        config.setdefault('server_name', '流萤服务器')
        config.setdefault('ai_enabled', True)
        config.setdefault('chat_enabled', True)
        config.setdefault('common_mute', False)
        # ===== 新增两行 =====
        config.setdefault('max_players', 10)
        config.setdefault('access_mode', 'none')
        return config

def load_whitelist():
    if not os.path.exists(WHITELIST_FILE):
        return []
    with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_whitelist(data):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        return []
    with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_blacklist(data):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def save_server_config(config):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(SERVER_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

def set_maintenance(enabled, message=None):
    """设置维护模式状态，并保存到配置文件"""
    global maintenance_mode, maintenance_message
    with maintenance_lock:
        maintenance_mode = enabled
        if enabled:
            maintenance_message = message if message is not None else ""
        else:
            maintenance_message = ""
        config = load_server_config()
        config['maintenance'] = {
            'enabled': maintenance_mode,
            'message': maintenance_message
        }
        save_server_config(config)

def load_maintenance_state():
    """从配置文件加载维护模式状态"""
    global maintenance_mode, maintenance_message
    config = load_server_config()
    maint = config.get('maintenance', {})
    maintenance_mode = maint.get('enabled', False)
    maintenance_message = maint.get('message', "")

# ==================== 辅助函数 ====================
def parse_time_spec(spec):
    """
    解析时间规格，支持：
    - 时长格式（单位可任意组合）：
        年：Y 或 yr
        月：M
        日：D 或 d
        时：H 或 hr
        分：m 或 min
        秒：s 或 sec
        示例：1Y2M3D4H5m6s 或 1yr 2mo ... (但mo未启用，可用M)
    - 纯数字：视为天数
    - 绝对日期：2026-12-31 或 2026-12-31 23:59:59
    返回 (timedelta, None) 或 (None, datetime) 或 (None, None)
    """
    if not spec:
        return None, None
    spec = spec.strip()

    # 1. 尝试绝对日期时间 (YYYY-MM-DD HH:MM:SS)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})$', spec)
    if match:
        y, m, d, h, min, sec = map(int, match.groups())
        try:
            dt = SERVER_TIMEZONE.localize(datetime(y, m, d, h, min, sec))
            return None, dt
        except:
            pass
    # 2. 绝对日期 (YYYY-MM-DD)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', spec)
    if match:
        y, m, d = map(int, match.groups())
        try:
            dt = SERVER_TIMEZONE.localize(datetime(y, m, d, 23, 59, 59))
            return None, dt
        except:
            pass

    # 3. 时长格式（支持大小写区分版和纯小写备选版）
    # 定义所有支持的符号（严格区分大小写）
    units = {
        'Y': 'years',
        'yr': 'years',
        'M': 'months',
        'D': 'days',
        'd': 'days',
        'H': 'hours',
        'hr': 'hours',
        'm': 'minutes',
        'min': 'minutes',
        's': 'seconds',
        'sec': 'seconds'
    }
    # 按长度降序排列，避免 `yr` 被 `y` 误匹配
    unit_pattern = '|'.join(sorted(units.keys(), key=len, reverse=True))
    pattern = re.compile(r'(\d+)(' + unit_pattern + r')')
    matches = pattern.findall(spec)

    if not matches:
        # 纯数字作为天数
        if spec.isdigit():
            return timedelta(days=int(spec)), None
        return None, None

    years = months = days = hours = minutes = seconds = 0
    for num_str, unit in matches:
        num = int(num_str)
        if unit in ('Y', 'yr'):
            years += num
        elif unit == 'M':
            months += num
        elif unit in ('D', 'd'):
            days += num
        elif unit in ('H', 'hr'):
            hours += num
        elif unit in ('m', 'min'):
            minutes += num
        elif unit in ('s', 'sec'):
            seconds += num
        else:
            return None, None

    # 累加为 timedelta（1年=365天，1月=30天）
    total_days = years * 365 + months * 30 + days
    delta = timedelta(days=total_days, hours=hours, minutes=minutes, seconds=seconds)
    return delta, None

def format_ban_until(ban_until_dt):
    if not ban_until_dt:
        return "永久封禁"
    now = datetime.now(SERVER_TIMEZONE)
    if now >= ban_until_dt:
        return "已过期"
    delta = ban_until_dt - now
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if days > 0:
        return f"剩余 {days} 天 {hours} 小时，解封时间：{ban_until_dt.strftime('UTC%z %Y-%m-%d %H:%M:%S')}"
    elif hours > 0:
        return f"剩余 {hours} 小时 {minutes} 分钟，解封时间：{ban_until_dt.strftime('UTC%z %Y-%m-%d %H:%M:%S')}"
    else:
        return f"剩余 {minutes} 分钟，解封时间：{ban_until_dt.strftime('UTC%z %Y-%m-%d %H:%M:%S')}"

# ==================== 全局状态 ====================
clients = {}
player_by_server_uid = {}
server_lock = threading.Lock()

def kick_all_players_for_maintenance(reason_message):
    """踢出所有在线玩家（管理员除外），发送维护原因，并清理全局客户端字典"""
    with server_lock:
        to_remove = []
        for client, player in list(clients.items()):
            # 管理员保留
            if player.get('permission') == 'admin':
                # 可选：发送通知给管理员，但不踢出
                try:
                    TCPHandler.send_to(client, {
                        'type': 'system_message',
                        'data': {'text': f'【维护模式】服务器已进入维护模式，普通玩家已被踢出。你作为管理员仍可正常操作。'}
                    })
                except:
                    pass
                continue
            try:
                TCPHandler.send_to(client, {'type': 'kick', 'data': {'reason': reason_message}})
                time.sleep(0.1)
                client.close()
            except:
                pass
            to_remove.append((client, player['server_uid']))
        # 清理非管理员玩家
        for client, server_uid in to_remove:
            if client in clients:
                del clients[client]
            if server_uid in player_by_server_uid:
                del player_by_server_uid[server_uid]

class LogEmitter(QObject):
    log_signal = pyqtSignal(str)

class TCPHandler(socketserver.BaseRequestHandler):
    @staticmethod
    def _is_valid_nickname(name):
        import unicodedata
        stripped = name.strip()
        if not stripped:
            return False
        # 检查不可见字符
        for ch in stripped:
            if unicodedata.category(ch).startswith('C'):
                return False
            if ord(ch) < 0x20:
                return False
        # 检查是否以“开拓者”开头且不等于“开拓者”
        if stripped.startswith("开拓者") and stripped != "开拓者":
            return False
        # 只允许中英文、数字、下划线（长度1-16）
        if not re.match(r'^[\w\u4e00-\u9fff]{1,16}$', stripped):
            return False
        return True

    def _load_gacha_config(self):
        config_path = os.path.join(SERVER_DATA, 'gacha_config.json')
        # 新格式默认配置（增加 expiry 字段）
        default_config = {
            "name": "角色活动跃迁",
            "description": "限定角色UP",
            "icon": "✦",
            "banner": "img/pool_banner.png",
            "cost_type": "star_rail_ticket",
            "cost_per_pull": 1,
            "expiry": None,  # 卡池过期时间（ISO格式字符串），None 表示永久有效
            "rules": {
                "base_5star_rate": 0.006,
                "base_4star_rate": 0.051,
                "pity_hard_5star": 80,
                "pity_hard_4star": 10,
                "guarantee_up_5star": True
            },
            "five_star_items": [
                {"id": "26_Newyear", "weight": 50, "up": True, "amount": 1},
            ],
            "four_star_items": [
                {"id": "jade", "weight": 50, "amount": 10},
                {"id": "cake", "weight": 20, "amount": 1},
                {"id": "star_rail_ticket", "weight": 50, "amount": 1}
            ],
            "three_star_items": [
                {"id": "credit", "weight": 70, "amount": 100},
                {"id": "none", "weight": 70, "amount": 0}
            ]
        }

        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                # 补全缺失字段（包括 expiry）
                for key, val in default_config.items():
                    if key not in config:
                        config[key] = val
                # 确保规则字段完整
                for key in default_config["rules"]:
                    if key not in config.get("rules", {}):
                        config.setdefault("rules", {})[key] = default_config["rules"][key]
                return config
            except Exception as e:
                print(f"[配置加载错误] {e}，使用默认配置")
                return default_config
        else:
            # 不存在则创建默认配置
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            return default_config

    def _do_gacha(self, server_uid, count):
        config = self._load_gacha_config()
        rules = config['rules']
        pity5 = self._get_pity(server_uid, 'gacha_pity_5')
        pity4 = self._get_pity(server_uid, 'gacha_pity_4')
        guaranteed5 = self._get_guaranteed(server_uid)

        results = []
        for _ in range(count):
            star = 3
            # 五星硬保底
            if pity5 >= rules['pity_hard_5star'] - 1:
                star = 5
                # 若大保底未触发则强制 UP，但大保底逻辑由 guaranteed 控制
                # 后面根据 guaranteed 决定是否强制 up
            elif pity4 >= rules['pity_hard_4star'] - 1:
                star = 4
            else:
                # 随机概率
                rand = random.random()
                base_5 = rules.get('base_5star_rate', 0.006)
                base_4 = rules.get('base_4star_rate', 0.051)
                if rand < base_5:
                    star = 5
                elif rand < base_5 + base_4:
                    star = 4
                else:
                    star = 3

            # 根据星级选择物品池
            if star == 5:
                items_pool = config['five_star_items']
                is_up_pool = True  # 五星池可能有 up 标记
            elif star == 4:
                items_pool = config['four_star_items']
                is_up_pool = False
            else:
                items_pool = config['three_star_items']
                is_up_pool = False

            # 权重随机选择一个物品
            total_weight = sum(item['weight'] for item in items_pool)
            rand_weight = random.randint(1, total_weight)
            cumulative = 0
            selected = items_pool[0]
            for item in items_pool:
                cumulative += item['weight']
                if rand_weight <= cumulative:
                    selected = item
                    break

            # 判断是否为 UP（仅五星且标记 up=True 且未触发大保底）
            is_up = False
            is_guaranteed = False
            if star == 5 and selected.get('up', False):
                if guaranteed5:
                    is_up = True
                    is_guaranteed = True
                    # 大保底触发后清除标志（由前端控制？但后端也需要重置）
                    guaranteed5 = False
                else:
                    # 普通五星，随机是否 UP（按照物品池的 up 标记，但这里直接使用 selected 的 up 字段）
                    # 实际我们应使用一个概率？传统是直接出 UP，这里我们简单些：只要 selected['up']==True 就视为 UP
                    # 但如果没有大保底，出 UP 概率为 50%（由权重决定）
                    # 因为权重已包含 up 和 non-up 物品，所以直接使用 selected['up']
                    is_up = selected.get('up', False)
                    if is_up:
                        # 出 UP 时，大保底不会被消耗（除非触发大保底）
                        pass
                    else:
                        # 出非UP五星，大保底标志不变
                        pass
            else:
                # 非五星，up 不考虑
                pass

            # 构造结果物品
            item_type = selected.get('id')  # 如 'ruyi', 'jade', 'credit' 等
            amount = selected.get('amount', 1)
            # 将 id 映射为实际存储的 type（用于库存处理）
            # 如果是时装，type='clothes'，id=具体时装id
            # 如果是通用物品，type=item_type，id=None
            # 我们需要统一处理：
            if item_type in AVAILABLE_CLOTHES:
                real_type = 'clothes'
                real_id = item_type
            elif item_type in ('cake', 'credit', 'jade', 'star_rail_ticket'):
                real_type = item_type
                real_id = None
            else:
                # 自定义物品（可能由服务器提供）
                real_type = 'custom'
                real_id = item_type

            result_item = {
                'type': real_type,
                'id': real_id,
                'amount': amount,
                'rarity': star,
                'isUp': is_up,
                'isGuaranteed': is_guaranteed
            }
            # 添加物品名称（方便前端）
            result_item['name'] = self._get_item_name_by_id(real_type, real_id)
            results.append(result_item)

            # 更新保底计数
            if star == 5:
                pity5 = 0
                pity4 = 0
            elif star == 4:
                pity4 = 0
                pity5 += 1
            else:
                pity4 += 1
                pity5 += 1

        # 保存更新后的保底数据
        pdata = load_player_data(server_uid)
        pdata['gacha_pity_5'] = pity5
        pdata['gacha_pity_4'] = pity4
        pdata['gacha_guaranteed_5'] = guaranteed5
        save_player_data(server_uid, pdata)

        return results

    def _get_pity(self, server_uid, key):
        pdata = load_player_data(server_uid)
        return pdata.get(key, 0)

    def _get_guaranteed(self, server_uid):
        pdata = load_player_data(server_uid)
        return pdata.get('gacha_guaranteed_5', False)

    def _get_item_name_by_id(self, real_type, real_id):
        """根据类型和ID返回可读名称"""
        if real_type == 'clothes':
            return AVAILABLE_CLOTHES.get(real_id, {}).get('name', real_id)
        elif real_type == 'custom':
            # 从自定义物品列表查找
            custom_items = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
            for ci in custom_items:
                if ci.get('id') == real_id:
                    return ci.get('name', real_id)
            return real_id
        else:
            # 内置物品
            name_map = {
                'cake': '橡木蛋糕卷',
                'credit': '信用点',
                'jade': '星琼',
                'star_rail_ticket': '星轨专票'
            }
            return name_map.get(real_type, real_type)

    def _check_and_grant_achievement(self, server_uid):
        """检查玩家是否达成月度四个进度 100%，若是则发放 10 专票并标记"""
        import calendar
        pdata = load_player_data(server_uid)
        if not pdata:
            return

        now = datetime.now(SERVER_TIMEZONE)
        current_month = now.strftime('%Y-%m')
        days_in_month = calendar.monthrange(now.year, now.month)[1]

        # 定义四个目标
        credit_target = 7200 * days_in_month
        sign_target = days_in_month
        jade_target = 1600
        cake_target = 72

        credit_gained = pdata.get('monthly_credit_gained', 0)
        sign_days = pdata.get('monthly_sign_days', 0)
        jade_gained = pdata.get('monthly_jade_gained', 0)
        cake_gained = pdata.get('monthly_cake_gained', 0)

        # 检查是否全部达标（>= 目标）
        if (credit_gained >= credit_target and
                sign_days >= sign_target and
                jade_gained >= jade_target and
                cake_gained >= cake_target):

            # 检查是否已发放
            if pdata.get('last_achievement_month') == current_month:
                return  # 已发放

            # 发放 10 张星铁专票
            items = [{"type": "star_rail_ticket", "amount": 10}]
            mail = {
                "id": str(uuid.uuid4()),
                "title": "🎉 月度成就达成！",
                "content": "恭喜你！本月所有养成进度均已达成 100%，获得 10 张星铁专票作为奖励！",
                "sender": "系统",
                "timestamp": now.isoformat(),
                "read": False,
                "claimed": False,
                "items": items,
            }
            mails = load_player_mails(server_uid)
            mails.append(mail)
            save_player_mails(server_uid, mails)

            # 标记已发放
            pdata['last_achievement_month'] = current_month
            save_player_data(server_uid, pdata)

            # 如果玩家在线，推送邮箱更新
            with server_lock:
                player = player_by_server_uid.get(server_uid)
            if player:
                self.send_to(player['client'], {'type': 'mails_data', 'data': {'mails': mails}})
                self.send_to(player['client'],
                             {'type': 'system_message', 'data': {'text': '🎉 恭喜你达成月度全成就！请查看邮箱领取奖励。'}})

    def _add_item_to_inventory(self, inv, item, pdata=None, skip_ticket_stat=False):
        """
        将物品添加到库存，并更新统计（如果提供 pdata）。

        :param inv: 库存字典（会被修改）
        :param item: 物品字典，包含 type, id, amount 等
        :param pdata: 玩家数据字典（可选），若提供则更新星琼/专票统计
        :param skip_ticket_stat: 是否跳过专票统计（用于兑换码场景）
        """
        # 忽略 'none' 物品（谢谢参与）
        if item.get('type') == 'custom' and item.get('id') == 'none':
            return

        itype = item['type']
        amount = item.get('amount', 0)

        if itype == 'cake':
            inv['balance'] = inv.get('balance', 0) + amount
        elif itype == 'credit':
            inv['credit'] = inv.get('credit', 0) + amount
            if inv['credit'] > 99999999:
                inv['credit'] = 99999999
        elif itype == 'jade':
            inv['jade'] = inv.get('jade', 0) + amount
            # ★ 星琼统计（所有场景都统计）
            if pdata:
                pdata['monthly_jade_gained'] = pdata.get('monthly_jade_gained', 0) + amount
                pdata['total_jade_gained'] = pdata.get('total_jade_gained', 0) + amount
        elif itype == 'star_rail_ticket':
            inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) + amount
            # ★ 专票统计（除兑换码外）
            if pdata and not skip_ticket_stat:
                pdata['monthly_ticket_gained'] = pdata.get('monthly_ticket_gained', 0) + amount
                pdata['total_ticket_gained'] = pdata.get('total_ticket_gained', 0) + amount
        elif itype == 'clothes':
            cid = item.get('id')
            if cid and cid not in inv.get('owned_clothes', ['normal']):
                inv.setdefault('owned_clothes', ['normal']).append(cid)
        elif itype == 'custom':
            custom_items = inv.setdefault('custom_items', {})
            custom_items[item['id']] = custom_items.get(item['id'], 0) + amount

    def handle(self):
        global clients, player_by_server_uid
        peername = self.client_address
        log_emitter = self.server.log_emitter
        # 不再输出 [连接] 日志，改为登录成功或失败时输出

        buffer = b''
        player = None
        while True:
            try:
                data = self.request.recv(1024)
                if not data:
                    break
                buffer += data
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not line:
                        continue
                    try:
                        line_str = line.decode('utf-8')
                    except UnicodeDecodeError:
                        log_emitter.log_signal.emit(f"[解码错误] {peername}: 非UTF-8数据")
                        continue
                    try:
                        msg = json.loads(line_str)
                    except json.JSONDecodeError as e:
                        log_emitter.log_signal.emit(f"[JSON错误] {peername}: {e} - 原始数据: {line_str[:200]}")
                        continue
                    player = self.process_message(msg, player)
            except ConnectionResetError as e:
                # 不输出 [连接断开] 日志，因为可能未登录
                break
            except ConnectionAbortedError as e:
                break
            except Exception as e:
                log_emitter.log_signal.emit(f"[未处理异常] {peername}: {e}")
                traceback.print_exc()
                break

        if player:
            server_uid = player.get('server_uid')
            with server_lock:
                if server_uid in player_by_server_uid:
                    del player_by_server_uid[server_uid]
                if self.request in clients:
                    del clients[self.request]
            log_emitter.log_signal.emit(f"[断开] {player['name']} 离开了服务器")
            self.broadcast_system(f"{player['name']} 离开了服务器")

    def process_message(self, msg, player):
        mtype = msg['type']
        data = msg.get('data', {})
        if mtype == 'status':
            config = load_server_config()
            max_players = config.get('max_players', 10)
            with server_lock:
                online = len(clients)
            self.send_json({
                'type': 'status_response',
                'data': {
                    'online': online,
                    'max': max_players
                }
            })
            return None

        if player is not None and hasattr(self.server, 'last_activity'):
            self.server.last_activity[player['server_uid']] = time.time()
        if not isinstance(data, dict):
            self.send_json({'type': 'login_fail', 'data': {'reason': '数据格式错误，请更新客户端'}})
            return None
        if mtype == 'login':
            data = msg.get('data', {})
            if not isinstance(data, dict):
                self.send_json({'type': 'login_fail', 'data': {'reason': '数据格式错误，请更新客户端'}})
                self.server.log_emitter.log_signal.emit(
                    f"[登录失败] {self.client_address[0]}:{self.client_address[1]} 数据格式错误")
                return None

            userid = data.get('userid')
            real_uid = data.get('uid')  # 例如 "user-808180249" 或 "admin-808180249"
            name = data.get('name')
            country = data.get('country', '')
            client_version = data.get('version')
            timezone = data.get('timezone')

            # 版本校验
            if not client_version or not isinstance(client_version, str):
                self.send_json({'type': 'login_fail', 'data': {'reason': '未知版本号，请更新客户端至最新版'}})
                return None

            if not ALLOW_ALL_VERSIONS and not versions_compatible(client_version, SERVER_VERSION):
                c_num, _ = parse_version(client_version)
                s_num, _ = parse_version(SERVER_VERSION)
                if c_num and s_num and c_num > s_num:
                    reason = f'客户端版本过高（{client_version}），请更新服务端至匹配版本'
                else:
                    reason = f'客户端版本过低（{client_version}），请更新客户端至 {SERVER_VERSION}'
                self.send_json({'type': 'login_fail', 'data': {'reason': reason}})
                return None

            # ---- 提前判断管理员（用于维护模式豁免） ----
            real_uid = data.get('uid')
            is_admin_login = False
            if real_uid and real_uid.lower().startswith('admin'):
                is_admin_login = True
            else:
                # 如果 UID 不是 admin 开头，但玩家文件中可能已被提升为管理员
                # 尝试通过 userid 或 real_uid 查找玩家文件
                userid = data.get('userid')
                # 先按 server_uid 格式查找（userid-uid_digits）
                uid_digits = None
                if real_uid and '-' in real_uid:
                    parts = real_uid.split('-', 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        uid_digits = parts[1]
                if userid and uid_digits:
                    possible_server_uid = f"{userid}-{uid_digits}"
                    main_path = os.path.join(PLAYERS_DIR, f'{possible_server_uid}.json')
                    if os.path.exists(main_path):
                        try:
                            with open(main_path, 'r', encoding='utf-8') as f:
                                pdata = json.load(f)
                            if pdata.get('permission') == 'admin':
                                is_admin_login = True
                        except:
                            pass
                # 如果还没找到，按 uid 字段查找
                if not is_admin_login and real_uid:
                    for filename in os.listdir(PLAYERS_DIR):
                        if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith(
                                '_mails.json'):
                            continue
                        filepath = os.path.join(PLAYERS_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                pdata = json.load(f)
                            if pdata.get('uid') == real_uid and pdata.get('permission') == 'admin':
                                is_admin_login = True
                                break
                        except:
                            continue

            # 维护模式（管理员可豁免）
            with maintenance_lock:
                if maintenance_mode and not is_admin_login:
                    if maintenance_message and maintenance_message.strip():
                        msg_text = f"服务器正在维护\n{maintenance_message}"
                    else:
                        msg_text = "服务器正在维护。"
                    self.send_json({'type': 'login_fail', 'data': {'reason': msg_text}})
                    return None

            # 地区封禁
            if country:
                banned_areas = load_banned_areas()
                if country.upper() in [a.upper() for a in banned_areas]:
                    self.send_json(
                        {'type': 'login_fail', 'data': {'reason': f'当前的国家/地区 ({country}) 不支持连接此服务器。'}})
                    return None

            # 时区封禁
            if timezone:
                banned_timezones = load_banned_timezones()
                if timezone in banned_timezones:
                    self.send_json({'type': 'login_fail', 'data': {'reason': f'你的时区 ({timezone}) 已被服务器封禁。'}})
                    return None

            # ---------- 提取数字部分 ----------
            # 从 real_uid 中提取数字部分（例如 "user-808180249" -> "808180249"）
            uid_digits = None
            if real_uid and '-' in real_uid:
                parts = real_uid.split('-', 1)
                if len(parts) == 2 and parts[1].isdigit():
                    uid_digits = parts[1]
            if not uid_digits:
                self.send_json({'type': 'login_fail', 'data': {'reason': '无效的UID格式，请联系管理员'}})
                return None

            # 生成 server_uid（文件名） = userid + '-' + uid_digits
            # 注意：如果 userid 为空，则 fallback 为 "user-" + uid_digits（兼容旧客户端）
            if userid:
                server_uid = f"{userid}-{uid_digits}"
            else:
                server_uid = f"user-{uid_digits}"  # 临时方案

            # ---------- 查找已有玩家数据 ----------
            existing_data = None
            existing_file_uid = None  # 当前文件名对应的 uid（用于判断是否需要迁移）

            # 先按 server_uid 文件名查找（新格式）
            main_path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
            if os.path.exists(main_path):
                try:
                    with open(main_path, 'r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                    existing_file_uid = server_uid
                except:
                    pass

            # 如果没找到，按 uid 字段查找（可能文件名不同）
            if existing_data is None:
                for filename in os.listdir(PLAYERS_DIR):
                    if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith(
                            '_mails.json'):
                        continue
                    filepath = os.path.join(PLAYERS_DIR, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            pdata = json.load(f)
                    except:
                        continue
                    # 匹配 uid 字段
                    if pdata.get('uid') == real_uid:
                        existing_data = pdata
                        existing_file_uid = filename[:-5]
                        break

            # 如果仍未找到，尝试按 userid 查找（旧账号兼容）
            if existing_data is None and userid:
                for filename in os.listdir(PLAYERS_DIR):
                    if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith(
                            '_mails.json'):
                        continue
                    filepath = os.path.join(PLAYERS_DIR, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            pdata = json.load(f)
                    except:
                        continue
                    if pdata.get('userid') == userid:
                        # 检查 uid 是否匹配（应一致）
                        if pdata.get('uid') != real_uid:
                            # 不一致，可能是冲突，报错
                            self.send_json({'type': 'login_fail', 'data': {'reason': '账号数据异常，请联系管理员'}})
                            return None
                        existing_data = pdata
                        existing_file_uid = filename[:-5]
                        break

            # ---------- 处理迁移（如果文件名不匹配 server_uid） ----------
            if existing_data is not None and existing_file_uid != server_uid:
                # 需要迁移：将 existing_file_uid 重命名为 server_uid
                def log_cb_migrate(msg):
                    self.server.log_emitter.log_signal.emit(f"[迁移] {msg}")

                log_cb_migrate(f"开始迁移玩家 {existing_file_uid} -> {server_uid}")
                success, err = rename_player_files(existing_file_uid, server_uid, log_cb_migrate)
                if success:
                    existing_file_uid = server_uid
                    log_cb_migrate("迁移成功")
                else:
                    log_cb_migrate(f"迁移失败: {err}")
                    self.send_json({'type': 'login_fail', 'data': {'reason': '数据迁移失败，请联系管理员'}})
                    return None

            # 如果找到了已有数据，确保 data 中的 uid 与 real_uid 一致（可能因 op 改变过）
            if existing_data is not None:
                pdata = existing_data
                # ★ 删除 uid 覆盖逻辑，服务端存储的 uid 才是权威数据
                # if pdata.get('uid') != real_uid:
                #     pdata['uid'] = real_uid
                #     save_player_data(server_uid, pdata)

                # 更新 timezone（如需）
                if timezone and pdata.get('timezone') != timezone:
                    pdata['timezone'] = timezone
                    save_player_data(server_uid, pdata)
            else:
                # ---------- 创建新玩家 ----------
                if not real_uid:
                    self.send_json({'type': 'login_fail', 'data': {'reason': '缺少用户UID，请重新登录'}})
                    return None
                # 判断权限
                is_admin = real_uid.lower().startswith('admin')
                # 确保 uid 字段正确（保留前缀）
                uid_field = real_uid  # 客户端传来的已经是 user-xxx 或 admin-xxx
                # 生成默认名称
                default_name = f"开拓者-{real_uid}" if real_uid else f"开拓者-{userid}" if userid else "开拓者"

                pdata = {
                    'name': default_name,
                    'userid': userid,
                    'uid': uid_field,
                    'permission': 'admin' if is_admin else 'user',
                    'banned': False,
                    'banned_until': None,
                    'redeemed_codes': [],
                    'timezone': timezone,
                    'muted_until': None,
                    'first_login': True,
                    'initial_mail_sent': False,
                    'total_interactions': 0,
                    'total_open_count': 0,
                    'total_sign_days': 0,
                    'total_affection_up': 0,
                    'total_sleep_count': 0,
                    'total_chat_count': 0,
                    'monthly_interactions': 0,
                    'monthly_open_count': 0,
                    'monthly_sign_days': 0,
                    'monthly_affection_up': 0,
                    'monthly_sleep_count': 0,
                    'monthly_chat_count': 0,
                    'monthly_credit_gained': 0,
                    'monthly_cake_gained': 0,
                    'current_month': None,
                }
                save_player_data(server_uid, pdata)
                save_player_inventory(server_uid,
                                      {'balance': 0, 'credit': 0, 'owned_clothes': ['normal'], 'star_rail_tickets': 0})
                save_player_mails(server_uid, [])
                self.server.log_emitter.log_signal.emit(f"[新玩家] 创建 {server_uid}")

            # 后面继续原有登录流程，但需要确保 server_uid 已正确
            # ...（后续代码与原有登录流程相同，但将 existing_server_uid 改为 server_uid）
            # 以下为复用原有代码，但已更改变量名
            # 注意：以下代码从 "封禁检查" 开始沿用，需调整变量名
            # 由于我们已定义 server_uid 和 pdata，后续直接使用即可
            # 但原有代码中使用了 existing_server_uid 等，需统一
            # 此处我们直接从封禁检查开始重写，以避免混乱

            # ---------- 封禁检查 ----------
            banned = pdata.get('banned', False)
            banned_until_str = pdata.get('banned_until')
            if banned:
                if banned_until_str:
                    try:
                        until_dt = datetime.fromisoformat(banned_until_str)
                        if until_dt.tzinfo is None:
                            until_dt = SERVER_TIMEZONE.localize(until_dt)
                        now = datetime.now(SERVER_TIMEZONE)
                        if now >= until_dt:
                            pdata['banned'] = False
                            pdata['banned_until'] = None
                            save_player_data(server_uid, pdata)
                            banned = False
                        else:
                            ban_info = format_ban_until(until_dt)
                            self.send_json({'type': 'login_fail', 'data': {'reason': f'你已被封禁，{ban_info}'}})
                            return None
                    except Exception as e:
                        print(f"[登录封禁] 解析错误: {e}")
                        self.send_json({'type': 'login_fail', 'data': {'reason': '你已被永久封禁。'}})
                        return None
                else:
                    self.send_json({'type': 'login_fail', 'data': {'reason': '你已被永久封禁。'}})
                    return None

            # ---------- 管理员豁免检查 ----------
            is_admin = pdata.get('permission') == 'admin'

            # ---- IP 封禁检查（管理员豁免） ----
            banned_ips = load_banned_ips()
            if self.client_address[0] in banned_ips and not is_admin:
                self.send_json({'type': 'login_fail', 'data': {'reason': '你的IP已被服务器封禁'}})
                self.server.log_emitter.log_signal.emit(
                    f"[登录失败] {real_uid} IP被封禁: {self.client_address[0]}"
                )
                return None

            # ---------- 人数上限检查（管理员豁免） ----------
            if not is_admin:
                config = load_server_config()
                max_players = config.get('max_players', 10)
                with server_lock:
                    current_players = len(clients)
                if current_players >= max_players:
                    self.send_json({'type': 'login_fail', 'data': {'reason': '拒绝连接：人数已达上限'}})
                    return None

            # ---------- 访问控制（白名单/黑名单），管理员豁免 ----------
            if not is_admin:
                config = load_server_config()
                access_mode = config.get('access_mode', 'none')
                if access_mode == 'whitelist':
                    whitelist = load_whitelist()
                    self.server.log_emitter.log_signal.emit(
                        f"[白名单检查] server_uid={server_uid}, uid={pdata.get('uid')}, 白名单列表={whitelist}")
                    if server_uid not in whitelist and pdata.get('uid') not in whitelist:
                        self.server.log_emitter.log_signal.emit(f"[白名单] 拒绝连接: {server_uid}")
                        self.send_json({'type': 'login_fail', 'data': {'reason': '你不在白名单中，拒绝连接'}})
                        return None

                elif access_mode == 'blacklist':
                    blacklist = load_blacklist()
                    self.server.log_emitter.log_signal.emit(
                        f"[黑名单检查] server_uid={server_uid}, uid={pdata.get('uid')}, 黑名单列表={blacklist}")
                    if server_uid in blacklist or pdata.get('uid') in blacklist:
                        self.server.log_emitter.log_signal.emit(f"[黑名单] 拒绝连接: {server_uid}")
                        self.send_json({'type': 'login_fail', 'data': {'reason': '服务器拒绝连接，你已被列入黑名单！'}})
                        return None

            # ---------- 防止重复登录 ----------
            with server_lock:
                old_player = player_by_server_uid.get(server_uid)
                if old_player:
                    old_client = old_player['client']
                    try:
                        TCPHandler.send_to(old_client, {'type': 'kick', 'data': {'reason': '账号已在其他客户端登录'}})
                        time.sleep(0.1)
                        old_client.close()
                    except:
                        pass
                    if old_client in clients:
                        del clients[old_client]
                    del player_by_server_uid[server_uid]
                if server_uid in player_by_server_uid:
                    self.send_json({'type': 'login_fail', 'data': {'reason': '该账号已在其他客户端登录'}})
                    return None

                # 更新 player 对象
                server_name = pdata.get('name')
                if not server_name and name:
                    server_name = name
                    pdata['name'] = server_name
                    save_player_data(server_uid, pdata)

                muted_until = pdata.get('muted_until')
                if muted_until:
                    try:
                        dt = datetime.fromisoformat(muted_until)
                        if dt.tzinfo is None:
                            dt = SERVER_TIMEZONE.localize(dt)
                            pdata['muted_until'] = dt.isoformat()
                            save_player_data(server_uid, pdata)
                        muted_until = dt.isoformat()
                    except Exception:
                        muted_until = None

                player = {
                    'userid': userid,
                    'uid': pdata['uid'],
                    'server_uid': server_uid,
                    'name': server_name,
                    'permission': pdata['permission'],
                    'client': self.request,
                    'country': country,
                    'timezone': timezone,
                    'muted_until': muted_until,
                }
                clients[self.request] = player
                player_by_server_uid[server_uid] = player

            # ---------- 加载库存和签到状态 ----------
            inv = load_player_inventory(server_uid)
            player['inventory'] = inv
            player['last_sign_date'] = inv.get('last_sign_date')

            now = datetime.now(SERVER_TIMEZONE)
            if now.hour < 3:
                sign_today_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            else:
                sign_today_str = now.strftime('%Y-%m-%d')
            signed_today = (inv.get('last_sign_date') == sign_today_str)

            stats = {
                'total_interactions': pdata.get('total_interactions', 0),
                'total_open_count': pdata.get('total_open_count', 0),
                'total_sign_days': pdata.get('total_sign_days', 0),
                'total_affection_up': pdata.get('total_affection_up', 0),
                'total_sleep_count': pdata.get('total_sleep_count', 0),
                'total_chat_count': pdata.get('total_chat_count', 0),
                'monthly_interactions': pdata.get('monthly_interactions', 0),
                'monthly_open_count': pdata.get('monthly_open_count', 0),
                'monthly_sign_days': pdata.get('monthly_sign_days', 0),
                'monthly_affection_up': pdata.get('monthly_affection_up', 0),
                'monthly_sleep_count': pdata.get('monthly_sleep_count', 0),
                'monthly_chat_count': pdata.get('monthly_chat_count', 0),
                'monthly_credit_gained': pdata.get('monthly_credit_gained', 0),
                'monthly_cake_gained': pdata.get('monthly_cake_gained', 0),
                'current_month': pdata.get('current_month', None),
                # ★ 新增
                'monthly_jade_gained': pdata.get('monthly_jade_gained', 0),
                'total_jade_gained': pdata.get('total_jade_gained', 0),
                'monthly_ticket_gained': pdata.get('monthly_ticket_gained', 0),
                'total_ticket_gained': pdata.get('total_ticket_gained', 0),
            }

            # 在 login 分支末尾，构造 data 字典前
            ai_enabled = load_server_config().get('ai_enabled', True)
            chat_enabled = load_server_config().get('chat_enabled', True)
            common_mute = load_server_config().get('common_mute', False)
            offset_hours = int(now.utcoffset().total_seconds() / 3600)
            config = load_server_config()
            server_display_name = config.get('server_name', '流萤服务器')
            history = load_chat_history()
            recent = history[-50:] if len(history) > 50 else history

            # ---------- 新增：加载自定义菜单和物品 ----------
            custom_menu = self.server.load_custom_menu()
            custom_items = self.server.load_custom_items()
            # ------------------------------------------

            # 在 login 分支末尾，找到 self.send_json 调用，添加一行
            self.send_json({
                'type': 'login_ok',
                'data': {
                    'server_uid': server_uid,
                    'uid': pdata['uid'],
                    'permission': player['permission'],
                    'inventory': inv,
                    'signed_today': signed_today,
                    'redeemed_codes': pdata.get('redeemed_codes', []),
                    'server_display_name': server_display_name,
                    'server_timezone_offset': offset_hours,
                    'chat_history': recent,
                    'server_name': server_name,
                    'server_version': SERVER_VERSION,
                    'stats': stats,
                    'muted_until': muted_until,
                    'ai_enabled': ai_enabled,
                    'chat_enabled': chat_enabled,
                    'common_mute': common_mute,
                    'require_rename': pdata.get('first_login', False),
                    'custom_menu': custom_menu,
                    'custom_items': custom_items,
                    'gacha_history': pdata.get('gacha_history', []),
                }
            })

            # ★ 新增：登录成功日志（简洁版）
            self.server.log_emitter.log_signal.emit(
                f"[登录] {player['name']} (UID: {player['uid']}) 从 {self.client_address[0]}:{self.client_address[1]} 进入"
            )

            self.server.log_emitter.log_signal.emit(f"{player['name']} 进入了服务器 (UID: {player['uid']})")
            if hasattr(self.server, 'last_activity'):
                self.server.last_activity[server_uid] = time.time()
            if not pdata.get('first_login', False):
                self.broadcast_system(f"{player['name']} 进入了服务器")
            return player





        elif mtype == 'exchange' and player:

            amount = data.get('amount', 1)

            if amount <= 0:
                self.send_json({'type': 'exchange_result', 'data': {'success': False, 'reason': '数量无效'}})

                return player

            with player_file_lock:

                inv = player['inventory']

                cost = amount * 160

                if inv.get('jade', 0) < cost:
                    self.send_json({'type': 'exchange_result', 'data': {'success': False, 'reason': '星琼不足'}})

                    return player

                inv['jade'] = inv.get('jade', 0) - cost

                inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) + amount

                # ★ 统计专票获取（兑换获得的专票计入统计）

                pdata = load_player_data(player['server_uid'])

                pdata['monthly_ticket_gained'] = pdata.get('monthly_ticket_gained', 0) + amount

                pdata['total_ticket_gained'] = pdata.get('total_ticket_gained', 0) + amount

                save_player_data(player['server_uid'], pdata)

                save_player_inventory(player['server_uid'], inv)

                self.send_json({

                    'type': 'exchange_result',

                    'data': {

                        'success': True,

                        'tickets': inv['star_rail_tickets'],

                        'jade': inv['jade'],

                        'pity': pdata.get('gacha_pity_5', 0),

                        'guaranteed': pdata.get('gacha_guaranteed_5', False)

                    }

                })

            return player


        elif mtype == 'gacha' and player:

            count = data.get('count', 1)

            config = self._load_gacha_config()

            expiry = config.get('expiry')

            if expiry is not None:

                try:

                    expiry_dt = datetime.fromisoformat(expiry)

                    if expiry_dt.tzinfo is None:
                        expiry_dt = SERVER_TIMEZONE.localize(expiry_dt)

                    now = datetime.now(SERVER_TIMEZONE)

                    if now > expiry_dt:
                        self.send_json({

                            'type': 'gacha_result',

                            'data': {'success': False, 'reason': '卡池已经结束'}

                        })

                        return player

                except Exception as e:

                    print(f"[卡池过期检查] 解析失败: {e}")

            inv = player['inventory']

            tickets = inv.get('star_rail_tickets', 0)

            if count not in (1, 10):
                self.send_json({'type': 'gacha_result', 'data': {'success': False, 'reason': '只支持单抽或十连'}})

                return player

            if tickets < count:
                self.send_json(
                    {'type': 'gacha_result', 'data': {'success': False, 'reason': f'专票不足，需要 {count} 张'}})

                return player

            with player_file_lock:

                inv = player['inventory']

                tickets = inv.get('star_rail_tickets', 0)

                if tickets < count:
                    self.send_json(
                        {'type': 'gacha_result', 'data': {'success': False, 'reason': f'专票不足，需要 {count} 张'}})

                    return player

                results = self._do_gacha(player['server_uid'], count)

                inv['star_rail_tickets'] = tickets - count

                pdata = load_player_data(player['server_uid'])

                total_five = pdata.get('total_five_star', 0)

                final_add_items = []

                converted_info = []

                for idx, item in enumerate(results):

                    if item['rarity'] == 5:

                        if total_five >= 1:

                            final_add_items.append({'type': 'star_rail_ticket', 'amount': 5})

                            converted_info.append(f"第{idx + 1}个五星已转换为5张专票")

                            item['converted'] = True

                        else:

                            final_add_items.append(item)

                        total_five += 1

                    else:

                        final_add_items.append(item)

                pdata['total_five_star'] = total_five

                # ---- 添加物品到库存 & 统计 ----

                for item in final_add_items:

                    self._add_item_to_inventory(inv, item)

                    # ★ 统计

                    itype = item['type']

                    if itype == 'jade':

                        pdata['monthly_jade_gained'] = pdata.get('monthly_jade_gained', 0) + item.get('amount', 0)

                        pdata['total_jade_gained'] = pdata.get('total_jade_gained', 0) + item.get('amount', 0)

                    elif itype == 'star_rail_ticket':

                        pdata['monthly_ticket_gained'] = pdata.get('monthly_ticket_gained', 0) + item.get('amount', 0)

                        pdata['total_ticket_gained'] = pdata.get('total_ticket_gained', 0) + item.get('amount', 0)

                    # clothes/custom 不统计

                save_player_inventory(player['server_uid'], inv)

                # 更新抽卡历史（保留30天）

                history = pdata.get('gacha_history', [])

                now = datetime.now(SERVER_TIMEZONE)

                cutoff = now - timedelta(days=30)

                history = [h for h in history if datetime.fromisoformat(h['time']) >= cutoff]

                for item in results:
                    history.append({

                        'name': item['name'],

                        'rarity': item['rarity'],

                        'time': now.isoformat(),

                        'isUp': item.get('isUp', False),

                        'isGuaranteed': item.get('isGuaranteed', False)

                    })

                pdata['gacha_history'] = history

                save_player_data(player['server_uid'], pdata)

                extra_info = '；'.join(converted_info) if converted_info else ''

                self.send_json({

                    'type': 'gacha_result',

                    'data': {

                        'success': True,

                        'results': results,

                        'pity': pdata.get('gacha_pity_5', 0),

                        'guaranteed': pdata.get('gacha_guaranteed_5', False),

                        'extra_info': extra_info,

                        'tickets': inv['star_rail_tickets'],

                        'jade': inv.get('jade', 0),

                    }

                })

            return player


        elif mtype == 'request_birthday_mail' and player:

            display_name = data.get('display_name', player['name'])

            server_uid = player['server_uid']

            pdata = load_player_data(server_uid)

            if not pdata:
                self.send_json(
                    {'type': 'birthday_mail_result', 'data': {'success': False, 'message': '玩家数据不存在'}})

                return player

            # 检查是否已祝福（服务端记录）

            current_year = datetime.now(SERVER_TIMEZONE).year

            if pdata.get('birthday_reward_year') == current_year:
                self.send_json({'type': 'birthday_mail_result', 'data': {'success': False, 'message': '今年已祝福过'}})

                return player

            # 构造邮件：发放 320 星琼

            items = [{"type": "jade", "amount": 320}]

            mail_content = f"{display_name}，生日快乐！"

            new_mail = {

                "id": str(uuid.uuid4()),

                "title": "流萤的祝福",

                "content": mail_content,

                "sender": "系统",

                "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),

                "read": False,

                "claimed": False,

                "items": items,

            }

            mails = load_player_mails(server_uid)

            mails.append(new_mail)

            save_player_mails(server_uid, mails)

            # 记录祝福年份

            pdata['birthday_reward_year'] = current_year

            save_player_data(server_uid, pdata)

            self.send_json({'type': 'birthday_mail_result', 'data': {'success': True, 'message': '生日邮件已发送'}})

            self.send_to(player['client'], {'type': 'mails_data', 'data': {'mails': mails}})

            return player

        elif mtype == 'send_mail' and player:
            target_uid = data.get('target_uid')
            title = data.get('title', '')
            content = data.get('content', '')
            items = data.get('items', [])
            if not target_uid or not title or not items:
                self.send_json({'type': 'send_mail_result', 'data': {'success': False, 'reason': '参数不完整'}})
                return player
            # 权限检查：只能给自己或管理员可以给他人发邮件
            if target_uid != player['server_uid'] and player['permission'] != 'admin':
                self.send_json({'type': 'send_mail_result', 'data': {'success': False, 'reason': '无权限'}})
                return player
            # 加载目标玩家邮件
            mails = load_player_mails(target_uid)
            mail = {
                "id": str(uuid.uuid4()),
                "title": title,
                "content": content,
                "sender": player['name'],
                "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                "read": False,
                "claimed": False,
                "items": items
            }
            mails.append(mail)
            save_player_mails(target_uid, mails)
            self.send_json({'type': 'send_mail_result', 'data': {'success': True}})
            return player

        # ---------- 聊天消息处理（禁言检查） ----------
        elif mtype == 'chat' and player:
            text = data['text'].strip()
            if text.startswith('/'):
                self.handle_command(text, player)
            else:
                # ---- 禁言检查（原有） ----
                muted_until = player.get('muted_until')
                if muted_until:
                    try:
                        muted_dt = datetime.fromisoformat(muted_until)
                        if muted_dt > datetime.now(SERVER_TIMEZONE):
                            remaining = muted_dt - datetime.now(SERVER_TIMEZONE)
                            days = remaining.days
                            hours = remaining.seconds // 3600
                            minutes = (remaining.seconds % 3600) // 60
                            if days > 0:
                                msg = f"你已被禁言，剩余 {days} 天 {hours} 小时"
                            elif hours > 0:
                                msg = f"你已被禁言，剩余 {hours} 小时 {minutes} 分钟"
                            else:
                                msg = f"你已被禁言，剩余 {minutes} 分钟"
                            self.send_json({'type': 'system_message', 'data': {'text': msg}})
                            return player
                        else:
                            pdata = load_player_data(player['server_uid'])
                            if pdata:
                                pdata['muted_until'] = None
                                save_player_data(player['server_uid'], pdata)
                                player['muted_until'] = None
                    except:
                        pass

                # ★★★ 新增：聊天开关检查 ★★★
                config = load_server_config()
                if config.get('common_mute', False) and player['permission'] != 'admin':
                    self.send_json({'type': 'system_message', 'data': {'text': '❌ 全体禁言中，无法发送消息'}})
                    return player
                if not config.get('chat_enabled', True):
                    self.send_json({'type': 'system_message', 'data': {'text': '❌ 聊天功能已被管理员关闭'}})
                    return player

                # 正常聊天逻辑
                msg_id = str(uuid.uuid4())
                chat_msg = {
                    'msg_id': msg_id,  # 新增
                    'from_name': player['name'],
                    'text': text,
                    'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
                    'is_system': False
                }
                save_chat_history(chat_msg)
                # 广播时带上 msg_id
                self.broadcast_chat(player['name'], text, sender_player=player, msg_id=msg_id)
                if '@' in text:
                    self.handle_mentions(text, player)
                return player



        elif mtype == 'get_rename_info' and player:

            pdata = load_player_data(player['server_uid'])

            if pdata:

                last_time_str = pdata.get('last_change_name_time')

                remaining = 1  # 1表示可用

                next_time_str = ""

                if last_time_str:

                    try:

                        last_time = datetime.fromisoformat(last_time_str)

                        if last_time.tzinfo is None:
                            last_time = SERVER_TIMEZONE.localize(last_time)

                        now = datetime.now(SERVER_TIMEZONE)

                        if (now - last_time).total_seconds() < 30 * 24 * 3600:
                            remaining = 0

                            next_time = last_time + timedelta(days=30)

                            next_time_str = next_time.strftime(
                                '%Y-%m-%d %H:%M:%S') + f" (UTC{next_time.strftime('%z')[:3]}:{next_time.strftime('%z')[3:]})"

                    except:

                        pass

                self.send_json({

                    'type': 'rename_info',

                    'data': {'remaining': remaining, 'next_time': next_time_str}

                })

            return player

        elif mtype == 'command' and player:
            self.handle_command(data['command'], player)
            return player


        elif mtype == 'get_player_list' and player:

            now = time.time()

            with server_lock:

                lst = []

                for p in clients.values():

                    entry = {'name': p['name'], 'id': p['userid']}

                    if player['permission'] == 'admin':
                        entry['uid'] = p['server_uid']

                    # 计算延迟（最后活动时间与当前时间的差值）

                    last = self.server.last_activity.get(p['server_uid'], now)

                    diff = now - last

                    if diff < 10:  # 10秒内活跃视为有效，否则不添加（客户端显示"未知"）

                        entry['latency'] = int(diff * 1000)  # 转换为毫秒

                    lst.append(entry)

            self.send_json({'type': 'player_list', 'data': {'players': lst}})

            return player

        elif mtype == 'check_name' and player:
            new_name = data.get('new_name', '').strip()
            if not new_name:
                self.send_json({'type': 'check_name_result', 'data': {'available': False, 'reason': '昵称不能为空'}})
                return player
            # 格式校验（复用 _is_valid_nickname）
            if not self._is_valid_nickname(new_name):
                reason = "此昵称不合法" if new_name.startswith("开拓者-") and new_name != "开拓者" else "昵称不合法"
                self.send_json({'type': 'check_name_result', 'data': {'available': False, 'reason': reason}})
                return player
            # 检查在线重名（排除自己）
            with server_lock:
                if any(p['name'] == new_name and p.get('server_uid') != player['server_uid'] for p in clients.values()):
                    self.send_json({'type': 'check_name_result', 'data': {'available': False, 'reason': '昵称已占用！'}})
                    return player
            # 检查离线重名（排除自己）
            server_uid = player['server_uid']
            for filename in os.listdir(PLAYERS_DIR):
                if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                    continue
                if filename == f'{server_uid}.json':
                    continue
                filepath = os.path.join(PLAYERS_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        pdata = json.load(f)
                    if pdata.get('name') == new_name:
                        self.send_json(
                            {'type': 'check_name_result', 'data': {'available': False, 'reason': '昵称已占用！'}})
                        return player
                except:
                    continue
            self.send_json({'type': 'check_name_result', 'data': {'available': True, 'reason': ''}})
            return player

        elif mtype == 'save_inventory' and player:
            inv = data.get('inventory', {})
            save_player_inventory(player['server_uid'], inv)
            player['inventory'] = inv
            return player





        elif mtype == 'change_name' and player:

            new_name = data['new_name'].strip()

            # ---- 基础验证 ----

            if not new_name or len(new_name) > 16:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '名字不能为空且不超过16字符'}})

                return player

            if not re.match(r'^[\w\u4e00-\u9fff]+$', new_name):
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '名字只能包含中英文、数字和下划线'}})

                return player

            if new_name == player['name']:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': True, 'message': f'你已改名为 {new_name}'}})

                return player

            # ---------- 检查是否重名（在线 + 离线） ----------

            server_uid = player['server_uid']

            # 在线检查

            with server_lock:

                if any(p['name'] == new_name and p != player for p in clients.values()):
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '昵称已占用！'}})

                    return player

            # 离线文件检查（排除自己）

            for filename in os.listdir(PLAYERS_DIR):

                if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                    continue

                if filename == f'{server_uid}.json':
                    continue

                filepath = os.path.join(PLAYERS_DIR, filename)

                try:

                    with open(filepath, 'r', encoding='utf-8') as f:

                        data = json.load(f)

                    if data.get('name') == new_name:
                        self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '昵称已占用！'}})

                        return player

                except:

                    continue

            # ---------- 昵称合法性校验（添加） ----------

            if not self._is_valid_nickname(new_name):
                self.send_json({'type': 'command_result',

                                'data': {'success': False,

                                         'message': '此昵称不合法'}})

                return player

            # ---------- 加载玩家数据 ----------

            pdata = load_player_data(server_uid)

            if not pdata:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '玩家数据不存在'}})

                return player

            # ---------- 30天冷却检查（首次登录跳过） ----------

            if not pdata.get('first_login', False):

                last_time_str = pdata.get('last_change_name_time')

                if last_time_str:

                    try:

                        last_time = datetime.fromisoformat(last_time_str)

                        if last_time.tzinfo is None:
                            last_time = SERVER_TIMEZONE.localize(last_time)

                        now = datetime.now(SERVER_TIMEZONE)

                        if (now - last_time).total_seconds() < 30 * 24 * 3600:
                            next_time = last_time + timedelta(days=30)

                            time_str = next_time.strftime(

                                '%Y-%m-%d %H:%M:%S') + f" (UTC{next_time.strftime('%z')[:3]}:{next_time.strftime('%z')[3:]})"

                            self.send_json({'type': 'command_result',

                                            'data': {'success': False,

                                                     'message': f'冷却期未到！下次可修改时间为 {time_str}'}})

                            return player

                    except Exception as e:

                        print(f"[改名] 解析上次改名时间失败: {e}，允许修改")

                        pass

            # ---------- 保存首次登录标志 ----------

            is_first_login = pdata.get('first_login', False)

            # ---------- 执行改名 ----------

            with server_lock:

                old_name = player['name']

                player['name'] = new_name

            # 更新玩家数据

            pdata['name'] = new_name

            pdata['last_change_name_time'] = datetime.now(SERVER_TIMEZONE).isoformat()

            pdata['change_name_count'] = pdata.get('change_name_count', 0) + 1

            # 如果是首次登录，发送初始奖励邮件

            if is_first_login and not pdata.get('initial_mail_sent', False):
                items = [

                    {'type': 'credit', 'amount': 1000},

                    {'type': 'cake', 'amount': 5}

                ]

                mail_content = "欢迎加入流萤服务器！这是你的初始奖励。"

                new_mail = {

                    "id": str(uuid.uuid4()),

                    "title": "🎉 欢迎加入！",

                    "content": mail_content,

                    "sender": "系统",

                    "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),

                    "read": False,

                    "claimed": False,

                    "items": items,

                }

                mails = load_player_mails(server_uid)

                mails.append(new_mail)

                save_player_mails(server_uid, mails)

                pdata['initial_mail_sent'] = True

            # 清除首次登录标志

            if is_first_login:
                pdata['first_login'] = False

            save_player_data(server_uid, pdata)

            # ---------- 更新聊天历史中的名字 ----------

            history = load_chat_history()

            updated = False

            for msg in history:

                if msg.get('from_name') == old_name:
                    msg['from_name'] = new_name

                    updated = True

            if updated:
                with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(history, f, indent=2)

            self.server.log_emitter.log_signal.emit(f"{old_name} 改名为 {new_name} (UID: {player['uid']})")

            # ---------- 广播消息（区分首次登录/普通改名） ----------

            if is_first_login:

                self.broadcast_system(f"🎉 欢迎新玩家 {new_name} 加入服务器！")

            else:

                self.broadcast_system(f"{old_name} 改名为 {new_name}")

            self.send_json(

                {'type': 'command_result', 'data': {'success': True, 'message': f'改名成功！你现在的名字是 {new_name}'}})

            self.send_player_list_to_all()

            return player

        elif mtype == 'get_mails' and player:
            mails = load_player_mails(player['server_uid'])
            self.send_json({'type': 'mails_data', 'data': {'mails': mails}})
            return player


        elif mtype == 'get_chat_history' and player:

            history = load_chat_history()

            # 过滤掉已撤回的消息

            history = [msg for msg in history if not msg.get('revoked', False)]

            recent = history[-200:] if len(history) > 200 else history

            self.send_json({'type': 'chat_history_data', 'data': {'history': recent}})

            return player





        elif mtype == 'claim_mail' and player:

            mail_id = data['mail_id']

            mails = load_player_mails(player['server_uid'])

            claimed = False

            pdata = load_player_data(player['server_uid'])

            if not pdata:
                self.send_json({'type': 'claim_mail_result', 'data': {'success': False, 'reason': '玩家数据不存在'}})

                return player

            for mail in mails:

                if mail['id'] == mail_id and not mail.get('claimed'):

                    mail['claimed'] = True

                    claimed = True

                    inv = player['inventory']

                    credit_gained = 0

                    cake_gained = 0

                    jade_gained = 0

                    ticket_gained = 0

                    for item in mail['items']:

                        itype = item['type']

                        amount = item.get('amount', 0)

                        if itype == 'cake':

                            inv['balance'] = inv.get('balance', 0) + amount

                            cake_gained += amount

                        elif itype == 'credit':

                            inv['credit'] = inv.get('credit', 0) + amount

                            if inv['credit'] > 99999999:
                                inv['credit'] = 99999999

                            credit_gained += amount

                        elif itype == 'jade':

                            inv['jade'] = inv.get('jade', 0) + amount

                            jade_gained += amount

                        elif itype == 'star_rail_ticket':

                            inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) + amount

                            ticket_gained += amount

                        elif itype == 'clothes':

                            cid = item['id']

                            if cid and cid not in inv.get('owned_clothes', []):
                                inv.setdefault('owned_clothes', ['normal']).append(cid)

                        elif itype == 'custom':

                            custom_items = inv.setdefault('custom_items', {})

                            custom_items[item['id']] = custom_items.get(item['id'], 0) + amount

                            stats = pdata.setdefault('redeem_stats', {})

                            stats[item['id']] = stats.get(item['id'], 0) + amount

                    # ---- 统计更新（只对非兑换码邮件） ----

                    if 'code' not in mail:

                        pdata['monthly_credit_gained'] = pdata.get('monthly_credit_gained', 0) + credit_gained

                        pdata['monthly_cake_gained'] = pdata.get('monthly_cake_gained', 0) + cake_gained

                        # ★ 新增

                        pdata['monthly_jade_gained'] = pdata.get('monthly_jade_gained', 0) + jade_gained

                        pdata['total_jade_gained'] = pdata.get('total_jade_gained', 0) + jade_gained

                        pdata['monthly_ticket_gained'] = pdata.get('monthly_ticket_gained', 0) + ticket_gained

                        pdata['total_ticket_gained'] = pdata.get('total_ticket_gained', 0) + ticket_gained

                        # redeem_stats

                        stats = pdata.setdefault('redeem_stats', {})

                        for item in mail['items']:

                            itype = item['type']

                            if itype == 'clothes':

                                cid = item.get('id')

                                if cid:
                                    stats[cid] = stats.get(cid, 0) + 1

                            elif itype == 'custom':

                                pass  # 已处理

                            else:

                                amount = item.get('amount', 1)

                                stats[itype] = stats.get(itype, 0) + amount

                        save_player_data(player['server_uid'], pdata)

                        # 推送统计更新

                        stats_data = {

                            'monthly_credit_gained': pdata.get('monthly_credit_gained', 0),

                            'monthly_cake_gained': pdata.get('monthly_cake_gained', 0),

                            'monthly_jade_gained': pdata.get('monthly_jade_gained', 0),

                            'monthly_ticket_gained': pdata.get('monthly_ticket_gained', 0),

                        }

                        self.send_to(player['client'], {'type': 'stats_data', 'data': {'stats': stats_data}})

                    player['inventory'] = inv

                    save_player_inventory(player['server_uid'], inv)

                    save_player_mails(player['server_uid'], mails)

                    self.send_to(player['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})

                    break

            if claimed:

                self.send_json({'type': 'claim_mail_result', 'data': {'success': True}})

            else:

                self.send_json(
                    {'type': 'claim_mail_result', 'data': {'success': False, 'reason': '邮件不存在或已领取'}})

            return player




        elif mtype == 'redeem_code' and player:

            code = data['code']

            codes = load_server_codes()

            if code not in codes:
                self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '无效的兑换码'}})

                return player

            cinfo = codes[code]

            if cinfo.get('revoked'):
                self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '该兑换码已被撤销'}})

                return player

            target_spec = cinfo.get('target_user')

            if target_spec:

                if target_spec.strip() != '@a':

                    allowed = [t.strip() for t in target_spec.split(',')]

                    if player['name'] not in allowed and player['uid'] not in allowed and player[
                        'server_uid'] not in allowed:
                        self.send_json(
                            {'type': 'redeem_result', 'data': {'success': False, 'message': '该兑换码不属于你'}})

                        return player

            expire_time = cinfo.get('expire_time')

            if expire_time:

                try:

                    if datetime.now(SERVER_TIMEZONE) > datetime.fromisoformat(expire_time):
                        self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '兑换码已过期'}})

                        return player

                except:

                    pass

            if player['server_uid'] in cinfo.get('used_by', []):
                self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '你已经使用过该兑换码'}})

                return player

            inv = player['inventory']

            pdata = load_player_data(player['server_uid'])  # 加载玩家数据用于统计

            try:

                for item in cinfo.get('items', []):

                    itype = item.get('type', '')

                    amount = item.get('amount', 0)

                    if itype == 'cake':

                        inv['balance'] = inv.get('balance', 0) + amount

                    elif itype == 'credit':

                        inv['credit'] = inv.get('credit', 0) + amount

                        if inv['credit'] > 99999999:
                            inv['credit'] = 99999999

                    elif itype == 'clothes':

                        cid = item.get('id')

                        if cid and cid not in inv.get('owned_clothes', []):
                            inv.setdefault('owned_clothes', ['normal']).append(cid)

                    elif itype == 'star_rail_ticket':

                        inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) + amount

                        # ★ 专票不统计（兑换码获得的专票不计）

                    elif itype == 'jade':

                        inv['jade'] = inv.get('jade', 0) + amount

                        # ★ 星琼统计

                        if pdata:
                            pdata['monthly_jade_gained'] = pdata.get('monthly_jade_gained', 0) + amount

                            pdata['total_jade_gained'] = pdata.get('total_jade_gained', 0) + amount

            except Exception as e:

                self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': f'兑换失败: {str(e)}'}})

                return player

            cinfo.setdefault('used_by', []).append(player['server_uid'])

            save_server_codes(codes)

            save_player_inventory(player['server_uid'], inv)

            # 更新 redeem_stats 和 redeemed_codes

            if pdata:

                if code not in pdata.get('redeemed_codes', []):
                    pdata.setdefault('redeemed_codes', []).append(code)

                stats = pdata.setdefault('redeem_stats', {})

                for item in cinfo.get('items', []):

                    itype = item.get('type')

                    if itype == 'clothes':

                        cid = item.get('id')

                        if cid:
                            stats[cid] = stats.get(cid, 0) + 1

                    else:

                        amount = item.get('amount', 1)

                        stats[itype] = stats.get(itype, 0) + amount

                save_player_data(player['server_uid'], pdata)

            # 生成物品描述，发送邮件

            items_desc = []

            for item in cinfo.get('items', []):

                if item['type'] == 'cake':

                    items_desc.append(f"橡木蛋糕卷 x{item['amount']}")

                elif item['type'] == 'credit':

                    items_desc.append(f"信用点 x{item['amount']}")

                elif item['type'] == 'clothes':

                    cid = item['id']

                    name = AVAILABLE_CLOTHES.get(cid, {}).get('name', cid)

                    items_desc.append(f"时装 [{name}]")

                elif item['type'] == 'star_rail_ticket':

                    items_desc.append(f"星铁专票 x{item['amount']}")

                elif item['type'] == 'jade':

                    items_desc.append(f"星琼 x{item['amount']}")

            mail_content = "你成功兑换了以下物品：\n" + "\n".join(items_desc)

            mails = load_player_mails(player['server_uid'])

            new_mail = {

                "id": str(uuid.uuid4()),

                "title": f"兑换码兑换成功！（{code}）",

                "content": mail_content,

                "sender": "系统",

                "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),

                "read": False,

                "claimed": False,

                "items": cinfo['items'],

                "code": code  # 标记兑换码，用于统计排除

            }

            mails.append(new_mail)

            save_player_mails(player['server_uid'], mails)

            # 推送库存更新

            self.send_to(player['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})

            # 推送统计更新

            if pdata:
                stats_data = {

                    'monthly_jade_gained': pdata.get('monthly_jade_gained', 0),

                    'monthly_ticket_gained': pdata.get('monthly_ticket_gained', 0),

                }

                self.send_to(player['client'], {'type': 'stats_data', 'data': {'stats': stats_data}})

            self.send_json({'type': 'redeem_result', 'data': {'success': True, 'message': '兑换成功！物品已发送至邮箱'}})

            return player


        elif mtype == 'disconnect' and player:

            server_uid = player['server_uid']

            with server_lock:

                if player['client'] in clients:
                    del clients[player['client']]

                if server_uid in player_by_server_uid:
                    del player_by_server_uid[server_uid]

            # ★ 防止重复广播“离开了服务器”

            if not getattr(player, '_disconnected', False):
                player._disconnected = True

                self.broadcast_system(f"{player['name']} 离开了服务器")

                self.server.log_emitter.log_signal.emit(f"[断开] {player['name']} 主动退出")

            return None


        elif mtype == 'sign_in' and player:

            now = datetime.now(SERVER_TIMEZONE)

            if now.hour < 3:

                sign_date = (now - timedelta(days=1)).strftime('%Y-%m-%d')

            else:

                sign_date = now.strftime('%Y-%m-%d')

            last_sign = player.get('last_sign_date')

            if last_sign == sign_date:
                self.send_json({'type': 'sign_result', 'data': {'success': False, 'message': '今日已签到'}})

                return player

            # 更新信用点

            player['inventory']['credit'] = player['inventory'].get('credit', 0) + 7200

            if player['inventory']['credit'] > 99999999:
                player['inventory']['credit'] = 99999999

            player['last_sign_date'] = sign_date

            inv = player['inventory']

            inv['last_sign_date'] = sign_date

            save_player_inventory(player['server_uid'], inv)

            # ★ 增加月度签到天数统计

            pdata = load_player_data(player['server_uid'])

            pdata['monthly_sign_days'] = pdata.get('monthly_sign_days', 0) + 1

            save_player_data(player['server_uid'], pdata)

            self.send_json({

                'type': 'sign_result',

                'data': {

                    'success': True,

                    'message': '签到成功！获得 7200 信用点',

                    'last_sign_date': sign_date

                }

            })

            self.send_json({'type': 'inventory_update', 'data': {'inventory': inv}})

            return player

        # ===== 新增：统计数据更新（客户端上报） =====
        elif mtype == 'update_stats' and player:
            stats = data.get('stats', {})
            if stats:
                pdata = load_player_data(player['server_uid'])
                if pdata:
                    for key, value in stats.items():
                        if key in pdata:
                            pdata[key] = value
                    save_player_data(player['server_uid'], pdata)
                    # ★ 检查月度成就
                    self._check_and_grant_achievement(player['server_uid'])
                    self.send_json({'type': 'stats_update_ok', 'data': {'success': True}})
                else:
                    self.send_json({'type': 'stats_update_ok', 'data': {'success': False, 'reason': '玩家数据不存在'}})
            else:
                self.send_json({'type': 'stats_update_ok', 'data': {'success': False, 'reason': '统计数据为空'}})
            return player

        # ===== 新增：获取统计数据（客户端主动拉取） =====
        elif mtype == 'get_stats' and player:
            pdata = load_player_data(player['server_uid'])
            if pdata:
                stats = {
                    'total_interactions': pdata.get('total_interactions', 0),
                    'total_open_count': pdata.get('total_open_count', 0),
                    'total_sign_days': pdata.get('total_sign_days', 0),
                    'total_affection_up': pdata.get('total_affection_up', 0),
                    'total_sleep_count': pdata.get('total_sleep_count', 0),
                    'total_chat_count': pdata.get('total_chat_count', 0),
                    'monthly_interactions': pdata.get('monthly_interactions', 0),
                    'monthly_open_count': pdata.get('monthly_open_count', 0),
                    'monthly_sign_days': pdata.get('monthly_sign_days', 0),
                    'monthly_affection_up': pdata.get('monthly_affection_up', 0),
                    'monthly_sleep_count': pdata.get('monthly_sleep_count', 0),
                    'monthly_chat_count': pdata.get('monthly_chat_count', 0),
                    'monthly_credit_gained': pdata.get('monthly_credit_gained', 0),
                    'monthly_cake_gained': pdata.get('monthly_cake_gained', 0),
                    'current_month': pdata.get('current_month', None),
                    # ★ 新增
                    'monthly_jade_gained': pdata.get('monthly_jade_gained', 0),
                    'total_jade_gained': pdata.get('total_jade_gained', 0),
                    'monthly_ticket_gained': pdata.get('monthly_ticket_gained', 0),
                    'total_ticket_gained': pdata.get('total_ticket_gained', 0),
                }
                self.send_json({'type': 'stats_data', 'data': {'stats': stats}})
            else:
                self.send_json({'type': 'stats_data', 'data': {'stats': {}}})
            return player

    def send_player_list_to_all(self):
        for p in clients.values():
            with server_lock:
                lst = []
                for q in clients.values():
                    entry = {'name': q['name'], 'id': q['userid']}
                    if p['permission'] == 'admin':
                        entry['uid'] = q['server_uid']
                    lst.append(entry)
            self.send_to(p['client'], {'type': 'player_list', 'data': {'players': lst}})

    def _is_expire_format(self, s):
        """检查字符串是否符合有效期格式"""
        if not s:
            return False
        if s.isdigit():
            return True
        if re.match(r'^(\d+[YMD])+$', s.upper()):
            return True
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except:
            return False

    def _parse_items_spec(self, items_spec):
        """
        解析物品列表，返回 items 列表，失败返回 None。
        支持 cake, credit, ticket, star_rail_tickets, jade, 时装ID。
        """
        items = []
        for part in items_spec.split('&'):
            part = part.strip()
            if not part:
                continue
            if ':' in part:
                itype, qty = part.split(':', 1)
                try:
                    qty = int(qty)
                except:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'数量错误: {part}'}})
                    return None
                if qty < 1 or qty > 9999:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'数量必须在1-9999: {part}'}})
                    return None
            else:
                itype = part
                qty = 1

            # ★ 关键修改：增加 jade 和 star_rail_tickets
            if itype == 'cake':
                items.append({'type': 'cake', 'amount': qty})
            elif itype == 'credit':
                items.append({'type': 'credit', 'amount': qty})
            elif itype == 'ticket' or itype == 'star_rail_tickets':
                items.append({'type': 'star_rail_ticket', 'amount': qty})
            elif itype == 'jade':
                items.append({'type': 'jade', 'amount': qty})
            else:
                items.append({'type': 'clothes', 'id': itype})

        if not items:
            self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '未指定有效物品'}})
            return None
        return items

    def _get_player_name_by_uid(self, server_uid):
        """根据 server_uid 返回玩家名字"""
        with server_lock:
            if server_uid in player_by_server_uid:
                return player_by_server_uid[server_uid]['name']
        path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('name', server_uid)
            except:
                pass
        return server_uid

    def handle_command(self, cmd, player):
        parts = cmd.strip().split()
        if not parts:
            return
        cmd_name = parts[0].lower()

        # ---------- 禁止客户端使用的控制台专用命令 ----------
        console_only_commands = ['/restart', '/close', '/change', '/fix', '/all_versions']
        if cmd_name in console_only_commands:
            self.send_json(
                {'type': 'command_result', 'data': {'success': False, 'message': '此命令只能在服务器控制台执行'}})
            return

        # ---------- 禁止客户端执行时区和地区封禁 ----------
        if cmd_name in ['/ban', '/unban'] and len(parts) >= 3:
            sub_cmd = parts[1].lower()
            if sub_cmd in ['area', 'timezone']:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '此命令只能在服务器控制台执行'}})
                return

        # ---------- /help ----------
        if cmd_name == '/help':
            help_text = (
                "═══════════════════════════════════════════\n"
                "         流萤联机服务器 控制台帮助\n"
                "═══════════════════════════════════════════\n\n"
                "【服务器控制台专用命令】（仅在控制台有效）\n"
                "  /restart           重启服务器（断开所有玩家）\n"
                "  /close             关闭服务器（停止服务）\n"
                "  /change name <新名称>  修改服务器名称（最多15字符）\n"
                "  /fix on [消息]      开启维护模式并踢出所有玩家\n"
                "  /fix off            关闭维护模式\n"
                "  /All_Versions True/False  调试用，放行所有版本\n\n"
                "【常用管理命令】（客户端可用）\n"
                "  /give <用户> <物品id> <数量>\n"
                "      给指定玩家发放物品（邮件形式）\n"
                "      用户: @a(所有人) @s(自己) 或 玩家名\n"
                "      物品: cake, credit, ruyi(时装), ticket, jade, star_rail_tickets\n"
                "      示例: /give Gubwin credit 1000\n\n"
                "  /set <用户> <物品id> <数量>\n"
                "      直接设定玩家物品数量（覆盖）\n"
                "      示例: /set @s cake 50\n\n"
                "  /clear <用户> <物品或*>\n"
                "      清空指定玩家的物品（仅支持玩家名或@s）\n"
                "      物品: cake, credit, * (清空全部)\n"
                "      示例: /clear Gubwin cake\n\n"
                "  /revoke <用户> <物品:数量&物品...>\n"
                "      直接扣除玩家物品（允许负值，时装只能移除）\n"
                "      用户: @a(所有人) @s(自己) 或 玩家名\n"
                "      物品: cake, credit, ticket, 时装ID\n"
                "      示例: /revoke Gubwin cake:5&credit:1000\n"
                "      被扣除的玩家会收到邮件通知，不可对自己使用\n\n"
                "  /revoke code <兑换码> <用户>\n"
                "      撤销指定用户对兑换码的使用资格（撤回兑换码）\n"
                "      用户: 同 /revoke\n"
                "      示例: /revoke code GIFT2026 @a\n\n"
                "  /set newcode <兑换码> <物品列表>\n"
                "      创建新兑换码，物品列表用 & 分隔\n"
                "      格式: 物品:数量 (数量默认为1，上限9999)\n"
                "      示例: /set newcode 新春福利 cake:5&ruyi&credit:1000\n\n"
                "  /mute <用户> <时长>\n"
                "      禁言玩家，时长格式同封禁（如 30D 或 2026-12-31）\n"
                "      示例: /mute Gubwin 1D\n\n"
                "  /unmute <用户>\n"
                "      解除禁言\n"
                "      示例: /unmute Gubwin\n\n"
                "  /function AI on|off\n"
                "      启用或禁用服务器的 AI（在线对话）功能\n\n"
                "  /function Chat on|off\n"
                "      启用或禁用服务器的多人聊天功能\n\n"
                "【玩家管理】\n"
                "  /ban <玩家> [时长]         封禁玩家（时长如 30D, 2026-12-31）\n"
                "  /unban <玩家>              解封玩家\n"
                "  /op <玩家>                 提升为管理员\n"
                "  /deop <玩家>               取消管理员\n"
                "  /resetname <用户>          重置玩家的改名冷却时间\n\n"
                "【区域与时区封禁】\n"
                "  /ban area <国家代码>       封禁地区\n"
                "  /unban area <国家代码>     解封地区\n"
                "  /ban timezone <时区名>     封禁时区\n"
                "  /unban timezone <时区名>   解封时区\n\n"
                "【IP 封禁】（管理员专用）\n"
                "  /ban ip list              查看已封禁的 IP 列表\n"
                "  /ban ip <IP>              直接封禁指定 IP（支持 IPv4）\n"
                "  /ban ip <玩家名>          封禁在线玩家当前的 IP\n"
                "  /unban ip <IP>            解封指定 IP\n"
                "  ⚠️ 封禁 IP 后，所有使用该 IP 的非管理员玩家将被立即踢出。\n\n"
                "【访问控制】（管理员专用）\n"
                "  /maxplayers <人数>         设置服务器最大在线人数（默认10）\n"
                "  /access <whitelist|blacklist|none> <on|off>\n"
                "      开启/关闭白名单或黑名单模式，none 关闭所有限制\n"
                "  /whitelist on|off          快捷开启/关闭白名单模式\n"
                "  /blacklist on|off          快捷开启/关闭黑名单模式\n"
                "  /whitelist add|remove|list [uuid]\n"
                "      管理白名单（必须先开启白名单模式）\n"
                "  /blacklist add|remove|list [uuid]\n"
                "      管理黑名单（必须先开启黑名单模式）\n"
                "  ⚠️ 管理员拥有完全豁免权，不受上述限制\n\n"
                "【其他】\n"
                "  /kick <玩家>               踢出玩家（需在线）\n"
                "  /recall <消息ID>           撤回自己的消息（2分钟内）\n"
                "  /redeemstats               查看自己的兑换码净获得统计\n\n"
                "💡 提示：所有管理命令仅管理员可用。输入 /help 查看本帮助。"
            )
            self.send_json({'type': 'system_message', 'data': {'text': help_text}})
            self.server.log_emitter.log_signal.emit(f"[帮助] {player['name']} 查看了帮助")
            return

        # ---------- /fix 维护模式 ----------
        if cmd_name == '/fix':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '用法: /fix on [消息] 或 /fix off'}})
                return
            subcmd = parts[1].lower()
            if subcmd == 'on':
                if len(parts) > 2:
                    msg_raw = ' '.join(parts[2:])
                    if msg_raw.strip().lower() == 'none':
                        msg = ''
                    else:
                        msg = msg_raw
                else:
                    msg = ''
                set_maintenance(True, msg if msg else None)
                if msg and msg.strip():
                    reason_text = f"服务器正在维护\n{msg}"
                else:
                    reason_text = "服务器正在维护"
                kick_all_players_for_maintenance(reason_text)
                self.send_json({'type': 'command_result', 'data': {'success': True,
                                                                   'message': f'⚠️维护模式已开启，已踢出所有在线玩家。提示消息: {maintenance_message}'}})
                self.broadcast_system(f"⚠️服务器已进入维护模式，所有玩家已被踢出。")
            elif subcmd == 'off':
                set_maintenance(False, '')
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': '维护模式已关闭'}})
                self.broadcast_system(f"服务器维护模式已关闭，欢迎新玩家加入。")
            else:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '未知子命令，使用 /fix on 或 /fix off'}})
            return

        if cmd_name == '/resetname' and len(parts) == 2:
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            identifier = parts[1]
            server_uid, pdata = self.find_player_file_by_identifier(identifier)
            if server_uid is None or pdata is None:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {identifier}'}})
                return
            # 重置冷却时间（设为None表示从未改过名）
            pdata['last_change_name_time'] = None
            # 可重置次数或保留
            # pdata['change_name_count'] = 0
            save_player_data(server_uid, pdata)
            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': f'已重置 {identifier} 的改名冷却时间'}})
            self.server.log_emitter.log_signal.emit(f"[管理员] {player['name']} 重置了 {identifier} 的改名冷却")
            return

        # ---------- 时区封禁/解封 ----------
        if cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            tzname = parts[2]
            try:
                pytz.timezone(tzname)
            except:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'无效的时区名称: {tzname}'}})
                return
            banned_tz = load_banned_timezones()
            if tzname in banned_tz:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'时区 {tzname} 已在封禁列表中'}})
                return
            banned_tz.append(tzname)
            save_banned_timezones(banned_tz)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已封禁时区 {tzname}'}})
            with server_lock:
                to_kick = [p for p in clients.values() if p.get('timezone') == tzname]
            # 时区封禁踢出
            for p in to_kick:
                try:
                    TCPHandler.send_to(p['client'], {'type': 'kick', 'data': {
                        'reason': f'你的时区 {tzname} 已被服务器封禁，你将被踢出。'}})
                    time.sleep(0.1)
                    p['client'].close()
                except:
                    pass
                with server_lock:
                    if p['client'] in clients:
                        del clients[p['client']]
                    if p['server_uid'] in player_by_server_uid:
                        del player_by_server_uid[p['server_uid']]
            self.broadcast_system(f"时区 {tzname} 已被封禁")
            return

        if cmd_name == '/unban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            tzname = parts[2]
            banned_tz = load_banned_timezones()
            if tzname not in banned_tz:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'时区 {tzname} 不在封禁列表中'}})
                return
            banned_tz.remove(tzname)
            save_banned_timezones(banned_tz)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已解封时区 {tzname}'}})
            self.broadcast_system(f"时区 {tzname} 已被解封")
            return

        if cmd_name == '/change' and len(parts) >= 3 and parts[1].lower() == 'name':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            new_name = ' '.join(parts[2:]).strip()
            if not new_name or len(new_name) > 15:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '服务器名称不能为空且最多15个字符'}})
                return
            config = load_server_config()
            config['server_name'] = new_name
            save_server_config(config)
            # 不广播，只返回结果
            self.send_json({'type': 'command_result',
                            'data': {'success': True, 'message': f'服务器名称已更新为: {new_name}，重启后生效'}})
            return

        if cmd_name == '/maxplayers':
            if player['permission'] != 'admin':
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) != 2 or not parts[1].isdigit():
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '用法: /maxplayers <人数>'}})
                return
            new_limit = int(parts[1])
            if new_limit < 1:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '人数必须 ≥ 1'}})
                return
            config = load_server_config()
            config['max_players'] = new_limit
            save_server_config(config)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'最大人数已设为 {new_limit}，新连接立即生效'}})
            return

        if cmd_name == '/access':
            if player['permission'] != 'admin':
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3 or parts[1] not in ('whitelist', 'blacklist', 'none'):
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '用法: /access <whitelist|blacklist|none> <on|off>'}})
                return
            mode = parts[1]
            action = parts[2].lower()
            if mode == 'none':
                config = load_server_config()
                config['access_mode'] = 'none'
                save_server_config(config)
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': '访问控制已关闭（none模式）'}})
                return
            if action not in ('on', 'off'):
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '动作必须是 on 或 off'}})
                return
            config = load_server_config()
            if action == 'on':
                config['access_mode'] = mode
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'{mode} 模式已启用'}})
            else:
                config['access_mode'] = 'none'
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'{mode} 模式已关闭'}})
            save_server_config(config)
            return

        if cmd_name == '/whitelist':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '用法: /whitelist add|remove|list [uuid]'}})
                return
            subcmd = parts[1].lower()
            if subcmd == 'list':
                wl = load_whitelist()
                if wl:
                    lines = []
                    for uid in wl:
                        name = self._get_player_name_by_uid(uid)
                        lines.append(f"{name} ({uid})")
                    text = '白名单列表:\n' + '\n'.join(lines)
                else:
                    text = '白名单为空'
                self.send_json({'type': 'system_message', 'data': {'text': text}})
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': text}})
                return
            if len(parts) < 3:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '请提供玩家标识符（名字、userid 或 uid）'}})
                return
            identifier = parts[2]
            # 查找玩家文件或在线玩家，获取 server_uid
            server_uid, pdata = self.find_player_file_by_identifier(identifier)
            if server_uid is None:
                with server_lock:
                    for p in clients.values():
                        if p['name'] == identifier or p['userid'] == identifier or p['uid'] == identifier or p[
                            'server_uid'] == identifier:
                            server_uid = p['server_uid']
                            break
            if server_uid is None:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {identifier}'}})
                return
            wl = load_whitelist()
            if subcmd == 'add':
                if server_uid not in wl:
                    wl.append(server_uid)
                    save_whitelist(wl)
                    self.send_json({'type': 'command_result',
                                    'data': {'success': True, 'message': f'已添加 {server_uid} 到白名单'}})
                else:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'{server_uid} 已在白名单中'}})
            elif subcmd == 'remove':
                if server_uid in wl:
                    wl.remove(server_uid)
                    save_whitelist(wl)
                    self.send_json({'type': 'command_result',
                                    'data': {'success': True, 'message': f'已从白名单移除 {server_uid}'}})
                else:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'{server_uid} 不在白名单中'}})
            else:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '未知子命令，可用 add/remove/list'}})
            return

        if cmd_name == '/blacklist':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '用法: /blacklist add|remove|list [uuid]'}})
                return
            subcmd = parts[1].lower()
            if subcmd == 'list':
                bl = load_blacklist()
                if bl:
                    lines = []
                    for uid in bl:
                        name = self._get_player_name_by_uid(uid)
                        lines.append(f"{name} ({uid})")
                    text = '黑名单列表:\n' + '\n'.join(lines)
                else:
                    text = '黑名单为空'
                self.send_json({'type': 'system_message', 'data': {'text': text}})
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': text}})
                return
            if len(parts) < 3:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '请提供玩家标识符（名字、userid 或 uid）'}})
                return
            identifier = parts[2]
            server_uid, pdata = self.find_player_file_by_identifier(identifier)
            if server_uid is None:
                with server_lock:
                    for p in clients.values():
                        if p['name'] == identifier or p['userid'] == identifier or p['uid'] == identifier or p[
                            'server_uid'] == identifier:
                            server_uid = p['server_uid']
                            break
            if server_uid is None:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {identifier}'}})
                return
            bl = load_blacklist()
            if subcmd == 'add':
                if server_uid not in bl:
                    bl.append(server_uid)
                    save_blacklist(bl)
                    self.send_json({'type': 'command_result',
                                    'data': {'success': True, 'message': f'已添加 {server_uid} 到黑名单'}})
                else:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'{server_uid} 已在黑名单中'}})
            elif subcmd == 'remove':
                if server_uid in bl:
                    bl.remove(server_uid)
                    save_blacklist(bl)
                    self.send_json({'type': 'command_result',
                                    'data': {'success': True, 'message': f'已从黑名单移除 {server_uid}'}})
                else:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'{server_uid} 不在黑名单中'}})
            else:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '未知子命令，可用 add/remove/list'}})
            return

        # ---------- 地区封禁/解封 ----------
        if cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'area':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            area_code = parts[2].upper()
            banned_areas = load_banned_areas()
            if area_code in banned_areas:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'地区 {area_code} 已在封禁列表中'}})
                return
            banned_areas.append(area_code)
            save_banned_areas(banned_areas)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已封禁地区 {area_code}'}})
            with server_lock:
                to_kick = [p for p in clients.values() if p.get('country') == area_code]
            for p in to_kick:
                try:
                    self.send_to(p['client'], {'type': 'system_message',
                                               'data': {'text': f'你的地区 {area_code} 已被服务器封禁，你将被踢出。'}})
                    p['client'].close()
                except:
                    pass
                with server_lock:
                    if p['client'] in clients:
                        del clients[p['client']]
                    if p['server_uid'] in player_by_server_uid:
                        del player_by_server_uid[p['server_uid']]
            self.broadcast_system(f"地区 {area_code} 已被封禁")
            return

        if cmd_name == '/unban' and len(parts) >= 3 and parts[1].lower() == 'area':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            area_code = parts[2].upper()
            banned_areas = load_banned_areas()
            if area_code not in banned_areas:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'地区 {area_code} 不在封禁列表中'}})
                return
            banned_areas.remove(area_code)
            save_banned_areas(banned_areas)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已解封地区 {area_code}'}})
            self.broadcast_system(f"地区 {area_code} 已被解封")
            return

        # ---------- 玩家封禁 ----------
        if cmd_name == '/ban':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result', 'data': {'success': False,
                                                                   'message': '格式：/ban <标识> [时长] 或 /ban area <国家代码> 或 /ban timezone <时区>'}})
                return
            targets = parts[1].split(',')
            duration_str = parts[2] if len(parts) > 2 else None
            delta, until_dt = parse_time_spec(duration_str) if duration_str else (None, None)
            if until_dt:
                banned_until = until_dt.isoformat()
                if until_dt <= datetime.now(SERVER_TIMEZONE):
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': '指定的解封时间已过期'}})
                    return
            elif delta:
                banned_until = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
            else:
                banned_until = None

            for identifier in targets:
                identifier = identifier.strip()
                if not identifier:
                    continue
                with server_lock:
                    target = next((p for p in clients.values() if
                                   p['name'] == identifier or p['uid'] == identifier or p['server_uid'] == identifier),
                                  None)
                if target:
                    server_uid = target['server_uid']
                    pdata = load_player_data(server_uid)
                    if pdata:
                        pdata['banned'] = True
                        pdata['banned_until'] = banned_until
                        save_player_data(server_uid, pdata)
                    try:
                        reason = '你已被管理员封禁。'
                        if banned_until:
                            until_dt2 = datetime.fromisoformat(banned_until)
                            reason += f' 解封时间：{until_dt2.strftime("UTC%z %Y-%m-%d %H:%M:%S")}'
                        else:
                            reason += ' 永久封禁。'
                        self.send_to(target['client'], {'type': 'kick', 'data': {'reason': reason}})
                        time.sleep(0.1)
                        target['client'].close()
                    except Exception as e:
                        self.server.log_emitter.log_signal.emit(f"发送封禁消息失败: {e}")
                    with server_lock:
                        if target['client'] in clients:
                            del clients[target['client']]
                        if server_uid in player_by_server_uid:
                            del player_by_server_uid[server_uid]
                    self.broadcast_system(f"{identifier} 已被封禁")
                else:
                    server_uid, pdata = self.find_player_file_by_identifier(identifier)
                    if server_uid is None:
                        self.send_json({'type': 'command_result',
                                        'data': {'success': False, 'message': f'未找到玩家 {identifier} 的数据'}})
                        continue
                    pdata['banned'] = True
                    pdata['banned_until'] = banned_until
                    save_player_data(server_uid, pdata)
                    self.broadcast_system(f"{identifier} 已被封禁 (离线)")
            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': f'已封禁 {", ".join(targets)}'}})
            self.send_player_list_to_all()
            return

        # ---------- 玩家解封 ----------
        if cmd_name == '/unban':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result', 'data': {'success': False,
                                                                   'message': '格式：/unban <标识> 或 /unban area <国家代码> 或 /unban timezone <时区>'}})
                return
            targets = parts[1].split(',')
            for identifier in targets:
                identifier = identifier.strip()
                if not identifier:
                    continue
                server_uid, pdata = self.find_player_file_by_identifier(identifier)
                if server_uid is None:
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'未找到玩家 {identifier} 的数据'}})
                    continue
                if not pdata.get('banned'):
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'玩家 {identifier} 未被封禁(如果被禁可能是ip禁入!)'}})
                    continue
                pdata['banned'] = False
                pdata['banned_until'] = None
                save_player_data(server_uid, pdata)
                with server_lock:
                    target_player = player_by_server_uid.get(server_uid)
                if target_player:
                    try:
                        self.send_to(target_player['client'],
                                     {'type': 'system_message', 'data': {'text': '你已被管理员解封，请重新登录以生效。'}})
                        target_player['client'].close()
                    except:
                        pass
                    with server_lock:
                        if target_player['client'] in clients:
                            del clients[target_player['client']]
                        if server_uid in player_by_server_uid:
                            del player_by_server_uid[server_uid]
                self.broadcast_system(f"{identifier} 已被解封")
            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': f'已解封 {", ".join(targets)}'}})
            self.send_player_list_to_all()
            return

        if cmd_name == '/op' and len(parts) == 2:
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return

            identifier = parts[1]
            self.server.log_emitter.log_signal.emit(f"[OP] 管理员 {player['name']} 尝试提升 {identifier}")

            target = None
            with server_lock:
                for p in clients.values():
                    if p['name'] == identifier or p['userid'] == identifier or p['server_uid'] == identifier:
                        target = p
                        break

            if target:
                if target['permission'] == 'admin':
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'{target["name"]} 已经是管理员'}})
                    return
                pdata = load_player_data(target['server_uid'])
                if not pdata:
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'玩家 {identifier} 数据不存在'}})
                    return
                old_uid = pdata.get('uid', '')
                # 正确转换前缀，保留数字部分
                if old_uid.startswith('user-'):
                    new_uid = old_uid.replace('user-', 'admin-', 1)
                elif old_uid.startswith('admin-'):
                    new_uid = old_uid
                else:
                    new_uid = 'admin-' + old_uid
                pdata['uid'] = new_uid
                pdata['permission'] = 'admin'
                save_player_data(target['server_uid'], pdata)

                target['permission'] = 'admin'
                target['uid'] = new_uid

                # 发送 permission_changed，必须包含 uid
                self.send_to(target['client'], {
                    'type': 'permission_changed',
                    'data': {
                        'permission': 'admin',
                        'server_uid': target['server_uid'],
                        'uid': new_uid
                    }
                })
                self.broadcast_system(f"{identifier} 被提升为管理员")
                self.send_player_list_to_all()
                self.send_json(
                    {'type': 'command_result', 'data': {'success': True, 'message': f'已将 {identifier} 提升为管理员'}})
                self.server.log_emitter.log_signal.emit(f"[OP] 成功提升 {identifier} 为管理员")
                return

            # 离线玩家（同样修改 uid 前缀）
            server_uid, pdata = self.find_player_file_by_identifier(identifier)
            if server_uid is None:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'未找到玩家 {identifier} 的数据文件'}})
                return
            if pdata.get('permission') == 'admin':
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'玩家 {identifier} 已经是管理员'}})
                return
            old_uid = pdata.get('uid', '')
            if old_uid.startswith('user-'):
                new_uid = old_uid.replace('user-', 'admin-', 1)
            elif old_uid.startswith('admin-'):
                new_uid = old_uid
            else:
                new_uid = 'admin-' + old_uid
            pdata['uid'] = new_uid
            pdata['permission'] = 'admin'
            save_player_data(server_uid, pdata)
            self.broadcast_system(f"{identifier} 被提升为管理员")
            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': f'已离线提升 {identifier} 为管理员'}})
            self.server.log_emitter.log_signal.emit(f"[OP] 成功离线提升 {identifier} 为管理员")
            return

        if cmd_name == '/deop' and len(parts) == 2:
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return

            identifier = parts[1]
            self.server.log_emitter.log_signal.emit(
                f"[DEOP] 管理员 {player['name']} 尝试取消 {identifier} 的管理员资格")

            target = None
            with server_lock:
                for p in clients.values():
                    if p['name'] == identifier or p['userid'] == identifier or p['server_uid'] == identifier:
                        target = p
                        break

            if target:
                if target['permission'] == 'user':
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'{target["name"]} 已经是普通用户'}})
                    return
                pdata = load_player_data(target['server_uid'])
                if not pdata:
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'玩家 {identifier} 数据不存在'}})
                    return
                old_uid = pdata.get('uid', '')
                if old_uid.startswith('admin-'):
                    new_uid = old_uid.replace('admin-', 'user-', 1)
                elif old_uid.startswith('user-'):
                    new_uid = old_uid
                else:
                    new_uid = 'user-' + old_uid
                pdata['uid'] = new_uid
                pdata['permission'] = 'user'
                save_player_data(target['server_uid'], pdata)

                target['permission'] = 'user'
                target['uid'] = new_uid

                # 发送 permission_changed，必须包含 uid
                self.send_to(target['client'], {
                    'type': 'permission_changed',
                    'data': {
                        'permission': 'user',
                        'server_uid': target['server_uid'],
                        'uid': new_uid
                    }
                })
                self.broadcast_system(f"{identifier} 被取消管理员资格")
                self.send_player_list_to_all()
                self.send_json({'type': 'command_result',
                                'data': {'success': True, 'message': f'已取消 {identifier} 的管理员资格'}})
                self.server.log_emitter.log_signal.emit(f"[DEOP] 成功取消 {identifier} 的管理员资格")

                # 如果维护模式开启，踢出该玩家
                with maintenance_lock:
                    if maintenance_mode:
                        try:
                            self.send_to(target['client'], {
                                'type': 'kick',
                                'data': {'reason': '维护模式已开启，你已被解除管理员权限，无法继续连接。'}
                            })
                            time.sleep(0.1)
                            target['client'].close()
                        except:
                            pass
                        with server_lock:
                            if target['client'] in clients:
                                del clients[target['client']]
                            if target['server_uid'] in player_by_server_uid:
                                del player_by_server_uid[target['server_uid']]
                        self.server.log_emitter.log_signal.emit(f"[维护模式] 已踢出被 deop 的玩家 {target['name']}")
                return

            # 离线玩家
            server_uid, pdata = self.find_player_file_by_identifier(identifier)
            if server_uid is None:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'未找到玩家 {identifier} 的数据文件'}})
                return
            if pdata.get('permission') != 'admin':
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'玩家 {identifier} 不是管理员'}})
                return
            old_uid = pdata.get('uid', '')
            if old_uid.startswith('admin-'):
                new_uid = old_uid.replace('admin-', 'user-', 1)
            elif old_uid.startswith('user-'):
                new_uid = old_uid
            else:
                new_uid = 'user-' + old_uid
            pdata['uid'] = new_uid
            pdata['permission'] = 'user'
            save_player_data(server_uid, pdata)
            self.broadcast_system(f"{identifier} 被取消管理员资格")
            self.send_json({'type': 'command_result',
                            'data': {'success': True, 'message': f'已离线取消 {identifier} 的管理员资格'}})
            self.server.log_emitter.log_signal.emit(f"[DEOP] 成功离线取消 {identifier} 的管理员资格")
            return

        # ---------- /All_Versions ----------
        if cmd_name == '/all_versions' and len(parts) == 2:
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            global ALLOW_ALL_VERSIONS
            arg = parts[1].lower()
            if arg == 'true':
                ALLOW_ALL_VERSIONS = True
                self.send_json(
                    {'type': 'command_result', 'data': {'success': True, 'message': '已启用所有版本放行模式'}})
                self.broadcast_system("服务器已允许所有客户端版本连接（仅限调试）")
            elif arg == 'false':
                ALLOW_ALL_VERSIONS = False
                self.send_json(
                    {'type': 'command_result', 'data': {'success': True, 'message': '已关闭所有版本放行模式'}})
                self.broadcast_system("服务器已恢复版本校验")
            else:
                self.send_json({'type': 'command_result', 'data': {'success': False,
                                                                   'message': '用法: /All_Versions True 或 /All_Versions False'}})
            return

        # ---------- /give ----------
        if cmd_name == '/give':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '格式错误，请使用 /give <用户> <物品id> [数量]'}})
                return
            user_spec = parts[1]
            item_id = parts[2].lower()
            # 别名映射（时装）
            fashion_alias = {'ruyi': '26_Newyear'}
            if item_id in fashion_alias:
                item_id = fashion_alias[item_id]

            # 1️⃣ 校验物品
            if not self.is_valid_item(item_id):
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'未知物品ID：{item_id}'}})
                return

            # 2️⃣ 解析数量
            if len(parts) >= 4:
                try:
                    amount = int(parts[3])
                except:
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '数量必须为整数'}})
                    return
                if amount <= 0:
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '数量必须大于0'}})
                    return
            else:
                amount = 1
            # 时装强制数量为1
            if item_id not in ('cake', 'credit', 'ticket', 'star_rail_tickets',
                               'jade') and item_id in AVAILABLE_CLOTHES:
                amount = 1

            # 3️⃣ 获取目标玩家列表
            target_players = []
            if user_spec == '@a':
                with server_lock:
                    target_players = list(clients.values())
            elif user_spec == '@s':
                target_players = [player]
            else:
                found = False
                with server_lock:
                    for p in clients.values():
                        if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                            target_players.append(p)
                            found = True
                if not found:
                    server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                    if server_uid:
                        fake = {
                            'server_uid': server_uid,
                            'name': pdata.get('name', user_spec),
                            'userid': pdata.get('userid', ''),
                            'uid': pdata.get('uid', ''),
                            'client': None
                        }
                        target_players.append(fake)
                if not target_players:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到用户 {user_spec}'}})
                    return

            # 4️⃣ ★★★ 构造 items 列表（支持 jade 和 star_rail_tickets）★★★
            items = []
            if item_id == 'cake':
                items.append({'type': 'cake', 'amount': amount})
            elif item_id == 'jade':
                items.append({'type': 'jade', 'amount': amount})
            elif item_id == 'star_rail_tickets' or item_id == 'ticket':
                items.append({'type': 'star_rail_ticket', 'amount': amount})
            elif item_id == 'credit':
                items.append({'type': 'credit', 'amount': amount})
            elif item_id in AVAILABLE_CLOTHES:
                items.append({'type': 'clothes', 'id': item_id})
            else:
                # 自定义物品（如果有）
                custom_items = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
                item_info = next((it for it in custom_items if it.get('id') == item_id), None)
                if item_info:
                    items.append(
                        {'type': 'custom', 'id': item_id, 'amount': amount, 'name': item_info.get('name', item_id)})
                else:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未知物品ID：{item_id}'}})
                    return

            # 5️⃣ 生成物品描述（用于邮件正文）
            items_desc = []
            for item in items:
                if item['type'] == 'cake':
                    items_desc.append(f"橡木蛋糕卷 x{item['amount']}")
                elif item['type'] == 'jade':
                    items_desc.append(f"星琼 x{item['amount']}")
                elif item['type'] == 'star_rail_ticket':
                    items_desc.append(f"星铁专票 x{item['amount']}")
                elif item['type'] == 'credit':
                    items_desc.append(f"信用点 x{item['amount']}")
                elif item['type'] == 'clothes':
                    cid = item['id']
                    name = AVAILABLE_CLOTHES.get(cid, {}).get('name', cid)
                    items_desc.append(f"时装 [{name}]")
                elif item['type'] == 'custom':
                    items_desc.append(f"{item.get('name', item['id'])} x{item['amount']}")

            # 6️⃣ 发送邮件给每个目标
            for p in target_players:
                mails = load_player_mails(p['server_uid'])
                mail = {
                    "id": str(uuid.uuid4()),
                    "title": "管理员赠送物品",
                    "content": f"管理员 {player['name']} 赠送了你以下物品：\n" + "\n".join(items_desc),
                    "sender": "系统",
                    "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                    "read": False,
                    "claimed": False,
                    "items": items  # ← 必须传递非空列表
                }
                mails.append(mail)
                save_player_mails(p['server_uid'], mails)
                # 在线通知
                if p.get('client') and p['client'] in clients:
                    try:
                        self.send_to(p['client'], {'type': 'system_message',
                                                   'data': {'text': f'你收到了管理员赠送的物品，请查看邮箱。'}})
                    except:
                        pass
            self.send_json({'type': 'command_result',
                            'data': {'success': True, 'message': f'已向 {len(target_players)} 个玩家发送物品邮件'}})
            return

        # ---------- /set 处理（含 /set newcode） ----------
        if cmd_name == '/set':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return

            # 先判断是否 /set newcode
            if len(parts) >= 3 and parts[1].lower() == 'newcode':
                if len(parts) < 4:
                    self.send_json({'type': 'command_result', 'data': {'success': False,
                                                                       'message': '格式: /set newcode <兑换码> <物品:数量&物品...> [有效期]'}})
                    return
                code = parts[2]
                args = parts[3:]
                expire_str = None
                if args:
                    delta, dt = parse_time_spec(args[-1])
                    if delta is not None or dt is not None:
                        expire_str = args[-1]
                        items_spec = ' '.join(args[:-1])
                    else:
                        items_spec = ' '.join(args)
                else:
                    items_spec = ''

                items = []
                for part in items_spec.split('&'):
                    part = part.strip()
                    if not part:
                        continue
                    if ':' in part:
                        itype, qty = part.split(':', 1)
                        try:
                            qty = int(qty)
                        except:
                            self.send_json({'type': 'command_result',
                                            'data': {'success': False, 'message': f'数量错误: {part}'}})
                            return
                        if qty < 1 or qty > 9999:
                            self.send_json({'type': 'command_result',
                                            'data': {'success': False, 'message': f'数量必须在1-9999: {part}'}})
                            return
                    else:
                        itype = part
                        qty = 1
                    if itype == 'cake':
                        items.append({'type': 'cake', 'amount': qty})
                    elif itype == 'credit':
                        items.append({'type': 'credit', 'amount': qty})
                    elif itype == 'ticket':
                        items.append({'type': 'star_rail_ticket', 'amount': qty})
                    else:
                        if itype not in AVAILABLE_CLOTHES:
                            self.send_json({'type': 'command_result',
                                            'data': {'success': False, 'message': f'无效的时装ID: {itype}'}})
                            return
                        items.append({'type': 'clothes', 'id': itype})
                if not items:
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '未指定有效物品'}})
                    return

                expire_time = None
                if expire_str:
                    delta, until_dt = parse_time_spec(expire_str)
                    if until_dt:
                        expire_time = until_dt.isoformat()
                    elif delta:
                        expire_time = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
                    else:
                        self.send_json({'type': 'command_result',
                                        'data': {'success': False, 'message': f'无效的有效期格式: {expire_str}'}})
                        return

                codes = load_server_codes()
                if code in codes:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'兑换码 {code} 已存在'}})
                    return
                codes[code] = {
                    "max_uses": 0,
                    "used_count": 0,
                    "used_by": [],
                    "created_by": player['name'],
                    "created_time": datetime.now(SERVER_TIMEZONE).isoformat(),
                    "items": items,
                    "expire_time": expire_time,
                    "revoked": False,
                    "revoked_users": [],
                    "revoked_by": None,
                    "revoked_time": None
                }
                save_server_codes(codes)
                self.send_json(
                    {'type': 'command_result', 'data': {'success': True, 'message': f'兑换码 {code} 创建成功'}})
                return

            # 普通 /set <用户> <物品id> <数量>
            if len(parts) < 4:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '格式错误，请使用 /set <用户> <物品id> <数量>'}})
                return
            user_spec = parts[1]
            item_id = parts[2].lower()
            # 别名映射
            fashion_alias = {'ruyi': '26_Newyear'}
            if item_id in fashion_alias:
                item_id = fashion_alias[item_id]
            # 校验物品合法性
            if not self.is_valid_item(item_id):
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': f'未知物品ID：{item_id}'}})
                return

            try:
                amount = int(parts[3])
            except:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '数量必须为整数'}})
                return
            if amount < 0 or amount > 99999999:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '数量必须在 0-99999999 之间'}})
                return

            # 获取目标玩家
            target_players = []
            if user_spec == '@a':
                with server_lock:
                    target_players = list(clients.values())
            elif user_spec == '@s':
                target_players = [player]
            else:
                found = False
                with server_lock:
                    for p in clients.values():
                        if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                            target_players.append(p)
                            found = True
                if not found:
                    server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                    if server_uid:
                        fake = {
                            'server_uid': server_uid,
                            'name': pdata.get('name', user_spec),
                            'userid': pdata.get('userid', ''),
                            'uid': pdata.get('uid', ''),
                            'client': None
                        }
                        target_players.append(fake)
                if not target_players:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到用户 {user_spec}'}})
                    return

            is_fashion = item_id not in ['cake', 'credit', 'ticket', 'star_rail_tickets', 'jade']
            for p in target_players:
                # 读取旧库存
                old_inv = load_player_inventory(p['server_uid'])
                new_inv = old_inv.copy()

                if is_fashion:
                    if amount == 0:
                        if item_id in new_inv.get('owned_clothes', []):
                            new_inv['owned_clothes'].remove(item_id)
                    elif amount == 1:
                        if item_id not in new_inv.get('owned_clothes', []):
                            new_inv['owned_clothes'].append(item_id)
                    else:
                        self.send_json({'type': 'command_result',
                                        'data': {'success': False,
                                                 'message': f'时装只能设置为 0(移除) 或 1(添加)，当前为 {amount}'}})
                        return
                else:
                    if item_id == 'cake':
                        new_inv['balance'] = amount
                    elif item_id == 'credit':
                        new_inv['credit'] = amount
                    elif item_id == 'ticket' or item_id == 'star_rail_tickets':
                        new_inv['star_rail_tickets'] = amount
                    elif item_id == 'jade':
                        new_inv['jade'] = amount

                # ★ 统计差值（仅增加时统计，减少不统计）
                pdata = load_player_data(p['server_uid'])
                if not is_fashion:
                    if item_id == 'jade':
                        diff = new_inv.get('jade', 0) - old_inv.get('jade', 0)
                        if diff > 0:
                            pdata['monthly_jade_gained'] = pdata.get('monthly_jade_gained', 0) + diff
                            pdata['total_jade_gained'] = pdata.get('total_jade_gained', 0) + diff
                    elif item_id in ('ticket', 'star_rail_tickets'):
                        diff = new_inv.get('star_rail_tickets', 0) - old_inv.get('star_rail_tickets', 0)
                        if diff > 0:
                            pdata['monthly_ticket_gained'] = pdata.get('monthly_ticket_gained', 0) + diff
                            pdata['total_ticket_gained'] = pdata.get('total_ticket_gained', 0) + diff

                save_player_data(p['server_uid'], pdata)
                save_player_inventory(p['server_uid'], new_inv)

                if p.get('client') and p['client'] in clients:
                    p['inventory'] = new_inv
                    try:
                        self.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': new_inv}})
                        # 推送统计更新
                        stats_data = {
                            'monthly_jade_gained': pdata.get('monthly_jade_gained', 0),
                            'monthly_ticket_gained': pdata.get('monthly_ticket_gained', 0),
                        }
                        self.send_to(p['client'], {'type': 'stats_data', 'data': {'stats': stats_data}})
                        if is_fashion:
                            self.send_to(p['client'], {'type': 'system_message',
                                                       'data': {
                                                           'text': f'管理员已{"添加" if amount == 1 else "移除"}你的时装 [{item_id}]'}})
                        else:
                            self.send_to(p['client'], {'type': 'system_message',
                                                       'data': {'text': f'管理员已设置你的 {item_id} 为 {amount}'}})
                    except:
                        pass

            self.send_json({'type': 'command_result',
                            'data': {'success': True, 'message': f'已处理 {len(target_players)} 个玩家的 {item_id}'}})
            return

        # ---------- /clear [用户] [物品或*] ----------
        if cmd_name == '/clear':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '格式: /clear <用户> <物品或*>'}})
                return
            user_spec = parts[1]
            item_spec = parts[2].lower()

            # 仅允许清空 cake、credit 或 *（全部）
            if item_spec not in ('cake', 'credit', '*'):
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '可清空的物品: cake, credit, *'}})
                return

            # 目标玩家（不支持 @a，仅具体用户或 @s）
            target_players = []
            if user_spec == '@s':
                target_players = [player]
            else:
                found = False
                with server_lock:
                    for p in clients.values():
                        if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                            target_players.append(p)
                            found = True
                            break
                if not found:
                    server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                    if server_uid:
                        fake = {
                            'server_uid': server_uid,
                            'name': pdata.get('name', user_spec),
                            'userid': pdata.get('userid', ''),
                            'uid': pdata.get('uid', ''),
                            'client': None
                        }
                        target_players.append(fake)
                if not target_players:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到用户 {user_spec}'}})
                    return

            # 执行清空
            for p in target_players:
                inv = load_player_inventory(p['server_uid'])
                if item_spec == '*':
                    inv['balance'] = 0
                    inv['credit'] = 0
                elif item_spec == 'cake':
                    inv['balance'] = 0
                elif item_spec == 'credit':
                    inv['credit'] = 0
                save_player_inventory(p['server_uid'], inv)
                if p.get('client') and p['client'] in clients:
                    p['inventory'] = inv
                    try:
                        self.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                        self.send_to(p['client'],
                                     {'type': 'system_message', 'data': {'text': f'管理员已清空你的 {item_spec}'}})
                    except:
                        pass
            self.send_json({'type': 'command_result', 'data': {'success': True,
                                                               'message': f'已清空 {item_spec}，影响 {len(target_players)} 个玩家'}})
            return

        # ---------- 新增：/revoke 物品扣除（不带 code），含邮件通知与统计更新 ----------
        if cmd_name == '/revoke' and not (len(parts) >= 3 and parts[1].lower() == 'code'):
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '格式: /revoke <用户> <物品:数量&物品...>'}})
                return

            user_spec = parts[1]
            items_spec = ' '.join(parts[2:])

            # 解析物品列表
            items = []
            for part in items_spec.split('&'):
                part = part.strip()
                if not part:
                    continue
                if ':' in part:
                    itype, qty = part.split(':', 1)
                    try:
                        qty = int(qty)
                    except:
                        self.send_json({'type': 'command_result',
                                        'data': {'success': False, 'message': f'数量错误: {part}'}})
                        return
                else:
                    itype = part
                    qty = 1
                if itype not in ('cake', 'credit', 'ticket') and itype not in AVAILABLE_CLOTHES:
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': f'未知物品: {itype}'}})
                    return
                items.append({'type': itype, 'amount': qty})

            if not items:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '未指定有效物品'}})
                return

            # 获取目标玩家
            target_players = []
            if user_spec == '@a':
                with server_lock:
                    target_players = list(clients.values())
            elif user_spec == '@s':
                target_players = [player]
            else:
                found = False
                with server_lock:
                    for p in clients.values():
                        if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                            target_players.append(p)
                            found = True
                            break
                if not found:
                    server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                    if server_uid:
                        fake = {
                            'server_uid': server_uid,
                            'name': pdata.get('name', user_spec),
                            'userid': pdata.get('userid', ''),
                            'uid': pdata.get('uid', ''),
                            'client': None
                        }
                        target_players.append(fake)
                if not target_players:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到用户 {user_spec}'}})
                    return

            # 执行扣除并更新统计
            for p in target_players:
                inv = load_player_inventory(p['server_uid'])
                pdata = load_player_data(p['server_uid'])  # 加载玩家数据用于统计
                if pdata is None:
                    pdata = {}  # 如果不存在，创建空字典（但通常存在）

                items_desc = []
                # 处理每个物品
                for item in items:
                    itype = item['type']
                    amount = item['amount']
                    if itype == 'cake':
                        inv['balance'] = inv.get('balance', 0) - amount
                        items_desc.append(f"橡木蛋糕卷 x{amount}")
                        # 更新统计：月度蛋糕减少，redeem_stats 减少
                        pdata['monthly_cake_gained'] = pdata.get('monthly_cake_gained', 0) - amount
                        stats = pdata.setdefault('redeem_stats', {})
                        stats['cake'] = stats.get('cake', 0) - amount
                    elif itype == 'credit':
                        inv['credit'] = inv.get('credit', 0) - amount
                        if inv['credit'] < -99999999:
                            inv['credit'] = -99999999
                        items_desc.append(f"信用点 x{amount}")
                        pdata['monthly_credit_gained'] = pdata.get('monthly_credit_gained', 0) - amount
                        stats = pdata.setdefault('redeem_stats', {})
                        stats['credit'] = stats.get('credit', 0) - amount
                    elif itype == 'ticket':
                        inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) - amount
                        items_desc.append(f"星铁专票 x{amount}")
                        # 专票统计（如果需要在 redeem_stats 中体现）
                        stats = pdata.setdefault('redeem_stats', {})
                        stats['star_rail_ticket'] = stats.get('star_rail_ticket', 0) - amount
                    else:  # 时装
                        if amount > 0:
                            owned = inv.get('owned_clothes', ['normal'])
                            if itype in owned:
                                owned.remove(itype)
                                inv['owned_clothes'] = owned
                                clothes_name = AVAILABLE_CLOTHES.get(itype, {}).get('name', itype)
                                items_desc.append(f"时装 [{clothes_name}]")
                                # 时装在 redeem_stats 中记录
                                stats = pdata.setdefault('redeem_stats', {})
                                stats[itype] = stats.get(itype, 0) - 1
                            else:
                                items_desc.append(f"时装 [{itype}] (未拥有，无法扣除)")
                        else:
                            items_desc.append(f"时装 [{itype}] (忽略)")

                # 保存库存和玩家数据（统计）
                save_player_inventory(p['server_uid'], inv)
                if pdata:
                    save_player_data(p['server_uid'], pdata)

                # 发送邮件通知
                if items_desc:
                    mail_content = f"管理员已从您的账户中扣除以下物品：\n" + "\n".join(items_desc)
                else:
                    mail_content = "管理员执行了物品扣除操作，但未影响您的任何物品。"
                notif_mail = {
                    "id": str(uuid.uuid4()),
                    "title": "物品扣除通知",
                    "content": mail_content,
                    "sender": "系统",
                    "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                    "read": False,
                    "claimed": False,
                    "items": [],
                    "code": None
                }
                mails = load_player_mails(p['server_uid'])
                mails.append(notif_mail)
                save_player_mails(p['server_uid'], mails)

                # 如果玩家在线，推送更新
                if p.get('client') and p['client'] in clients:
                    p['inventory'] = inv
                    try:
                        self.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                        # 推送统计更新
                        stats_data = {
                            'monthly_credit_gained': pdata.get('monthly_credit_gained', 0),
                            'monthly_cake_gained': pdata.get('monthly_cake_gained', 0),
                        }
                        self.send_to(p['client'], {'type': 'stats_data', 'data': {'stats': stats_data}})
                        self.send_to(p['client'], {'type': 'system_message',
                                                   'data': {'text': f'管理员已从你身上扣除物品，请查看邮箱获取详情。'}})
                    except:
                        pass

            self.send_json({'type': 'command_result',
                            'data': {'success': True,
                                     'message': f'已对 {len(target_players)} 个玩家执行物品扣除，并发送了通知邮件，已更新统计。'}})
            return

        if cmd_name == '/revoke' and len(parts) >= 3 and parts[1].lower() == 'code':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 4:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '格式: /revoke code <兑换码> <用户>'}})
                return
            code = parts[2]
            user_spec = parts[3]

            codes = load_server_codes()
            if code not in codes:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'兑换码 {code} 不存在'}})
                return
            cinfo = codes[code]
            if cinfo.get('revoked'):
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '该兑换码已被撤销'}})
                return

            # ---------- 获取目标用户列表 ----------
            target_list = []
            if user_spec == '@a':
                with server_lock:
                    target_list = list(clients.values())
            elif user_spec == '@s':
                target_list = [player]
            else:
                found = False
                with server_lock:
                    for p in clients.values():
                        if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                            target_list.append(p)
                            found = True
                if not found:
                    server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                    if server_uid:
                        fake = {
                            'server_uid': server_uid,
                            'name': pdata.get('name', user_spec),
                            'userid': pdata.get('userid', ''),
                            'uid': pdata.get('uid', ''),
                            'client': None
                        }
                        target_list.append(fake)
                if not target_list:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到用户 {user_spec}'}})
                    return

            revoked_count = 0

            for p in target_list:
                suid = p['server_uid']
                if suid in cinfo.get('revoked_users', []):
                    continue

                pdata = load_player_data(suid)
                if not pdata:
                    continue

                inv = load_player_inventory(suid)
                mails = load_player_mails(suid)

                # ---- 1. 超宽松匹配邮件 ----
                mails_to_remove = []
                self.server.log_emitter.log_signal.emit(f"[撤销] 开始处理用户 {suid}，共有 {len(mails)} 封邮件")
                for idx, mail in enumerate(mails):
                    title = mail.get('title', '')
                    content = mail.get('content', '')
                    mail_code = mail.get('code', '')
                    self.server.log_emitter.log_signal.emit(
                        f"[撤销] 邮件 {idx}: 标题='{title}', code字段='{mail_code}'")
                    # 条件1：code字段直接匹配
                    if mail_code == code:
                        self.server.log_emitter.log_signal.emit(f"[撤销] 条件1命中（code字段）")
                        mails_to_remove.append(mail)
                        continue
                    # 条件2：标题中包含兑换码字符串（无论括号）
                    if code in title:
                        self.server.log_emitter.log_signal.emit(f"[撤销] 条件2命中（标题包含）")
                        mails_to_remove.append(mail)
                        continue
                    # 条件3：标题以"兑换码兑换成功"开头且内容中包含兑换码
                    if title.startswith('兑换码兑换成功') and code in content:
                        self.server.log_emitter.log_signal.emit(f"[撤销] 条件3命中（内容包含）")
                        mails_to_remove.append(mail)
                        continue
                    # 条件4：使用正则提取标题中括号内的字符串，匹配code
                    import re
                    match = re.search(r'[（(](\w+)[）)]', title)
                    if match and match.group(1) == code:
                        self.server.log_emitter.log_signal.emit(f"[撤销] 条件4命中（括号提取）")
                        mails_to_remove.append(mail)
                        continue

                # ---- 2. 如果仍然没找到邮件，但用户 redeemed_codes 中有该码，则强制删除所有标题含有"兑换码兑换成功"的邮件（防止遗留） ----
                if not mails_to_remove and code in pdata.get('redeemed_codes', []):
                    self.server.log_emitter.log_signal.emit(
                        f"[撤销] 未匹配到邮件，但用户已使用该码，强制删除所有兑换码成功邮件")
                    for mail in mails:
                        title = mail.get('title', '')
                        if '兑换码兑换成功' in title:
                            mails_to_remove.append(mail)
                            self.server.log_emitter.log_signal.emit(f"[撤销] 强制删除邮件: {title}")

                # ---- 3. 处理找到的邮件 ----
                if mails_to_remove:
                    self.server.log_emitter.log_signal.emit(f"[撤销] 共找到 {len(mails_to_remove)} 封相关邮件")
                    # 区分已领取和未领取
                    claimed_mails = [m for m in mails_to_remove if m.get('claimed', False)]
                    unclaimed_mails = [m for m in mails_to_remove if not m.get('claimed', False)]

                    # 处理已领取的邮件：扣除库存
                    for mail in claimed_mails:
                        for item in mail.get('items', []):
                            itype = item.get('type')
                            if itype == 'cake':
                                inv['balance'] = max(0, inv.get('balance', 0) - item.get('amount', 0))
                            elif itype == 'credit':
                                inv['credit'] = max(0, inv.get('credit', 0) - item.get('amount', 0))
                            elif itype == 'clothes':
                                cid = item.get('id')
                                if cid and cid in inv.get('owned_clothes', []):
                                    inv['owned_clothes'].remove(cid)
                            elif itype == 'star_rail_ticket':
                                inv['star_rail_tickets'] = max(0,
                                                               inv.get('star_rail_tickets', 0) - item.get('amount', 0))

                        # 更新 redeem_stats 和月度统计
                        stats = pdata.setdefault('redeem_stats', {})
                        for item in mail.get('items', []):
                            itype = item.get('type')
                            if itype == 'clothes':
                                cid = item.get('id')
                                if cid:
                                    stats[cid] = stats.get(cid, 0) - 1
                            else:
                                amount = item.get('amount', 1)
                                stats[itype] = stats.get(itype, 0) - amount
                        for item in mail.get('items', []):
                            itype = item.get('type')
                            if itype == 'credit':
                                pdata['monthly_credit_gained'] = pdata.get('monthly_credit_gained', 0) - item.get(
                                    'amount', 0)
                            elif itype == 'cake':
                                pdata['monthly_cake_gained'] = pdata.get('monthly_cake_gained', 0) - item.get('amount',
                                                                                                              0)

                    # 删除所有找到的邮件（无论是否已领取）
                    for mail in mails_to_remove:
                        mails.remove(mail)
                        self.server.log_emitter.log_signal.emit(f"[撤销] 已删除邮件: {mail.get('title')}")

                    # 保存库存和邮件
                    save_player_inventory(suid, inv)
                    save_player_mails(suid, mails)

                    # 从 redeemed_codes 中移除该兑换码
                    if code in pdata.get('redeemed_codes', []):
                        pdata['redeemed_codes'] = [c for c in pdata['redeemed_codes'] if c != code]
                        self.server.log_emitter.log_signal.emit(f"[撤销] 从 redeemed_codes 移除 {code}")
                    save_player_data(suid, pdata)

                    # 标记该用户已撤销
                    cinfo.setdefault('revoked_users', []).append(suid)
                    revoked_count += 1

                    # 添加撤回通知邮件
                    if claimed_mails:
                        content_detail = "已领取的邮件已被撤回，相关物品已从您的库存中扣除。"
                    else:
                        content_detail = "未领取的邮件已被撤回，无物品扣除。"
                    if unclaimed_mails and claimed_mails:
                        content_detail = "部分邮件已领取（已扣除物品），部分未领取（直接撤回）。"
                    notif_mail = {
                        "id": str(uuid.uuid4()),
                        "title": f"兑换码撤回通知（{code}）",
                        "content": f"管理员已撤销兑换码 {code}。{content_detail}",
                        "sender": "系统",
                        "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                        "read": False,
                        "claimed": False,
                        "items": [],
                        "code": code
                    }
                    mails.append(notif_mail)
                    save_player_mails(suid, mails)
                    self.server.log_emitter.log_signal.emit(f"[撤销] 已添加撤回通知邮件")

                    # 如果玩家在线，推送更新
                    if p.get('client') and p['client'] in clients:
                        p['inventory'] = inv
                        try:
                            self.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                            self.send_to(p['client'], {
                                'type': 'system_message',
                                'data': {'text': f'兑换码 {code} 已被管理员撤销，请查看邮箱获取详情。'}
                            })
                            stats_data = {
                                'monthly_credit_gained': pdata.get('monthly_credit_gained', 0),
                                'monthly_cake_gained': pdata.get('monthly_cake_gained', 0),
                            }
                            self.send_to(p['client'], {'type': 'stats_data', 'data': {'stats': stats_data}})
                        except:
                            pass

                else:
                    # ---- 4. 完全没有匹配到邮件，但 redeemed_codes 中有该码，仍按已领取处理（扣除库存，但不删除邮件，因为没找到） ----
                    if code in pdata.get('redeemed_codes', []):
                        self.server.log_emitter.log_signal.emit(
                            f"[撤销] 未找到任何相关邮件，但用户已使用，直接扣除库存（视为已领取）")
                        for item in cinfo.get('items', []):
                            itype = item.get('type')
                            if itype == 'cake':
                                inv['balance'] = max(0, inv.get('balance', 0) - item.get('amount', 0))
                            elif itype == 'credit':
                                inv['credit'] = max(0, inv.get('credit', 0) - item.get('amount', 0))
                            elif itype == 'clothes':
                                cid = item.get('id')
                                if cid and cid in inv.get('owned_clothes', []):
                                    inv['owned_clothes'].remove(cid)
                            elif itype == 'star_rail_ticket':
                                inv['star_rail_tickets'] = max(0,
                                                               inv.get('star_rail_tickets', 0) - item.get('amount', 0))
                        stats = pdata.setdefault('redeem_stats', {})
                        for item in cinfo.get('items', []):
                            itype = item.get('type')
                            if itype == 'clothes':
                                cid = item.get('id')
                                if cid:
                                    stats[cid] = stats.get(cid, 0) - 1
                            else:
                                amount = item.get('amount', 1)
                                stats[itype] = stats.get(itype, 0) - amount
                        for item in cinfo.get('items', []):
                            itype = item.get('type')
                            if itype == 'credit':
                                pdata['monthly_credit_gained'] = pdata.get('monthly_credit_gained', 0) - item.get(
                                    'amount', 0)
                            elif itype == 'cake':
                                pdata['monthly_cake_gained'] = pdata.get('monthly_cake_gained', 0) - item.get('amount',
                                                                                                              0)
                        pdata['redeemed_codes'] = [c for c in pdata.get('redeemed_codes', []) if c != code]
                        save_player_inventory(suid, inv)
                        save_player_data(suid, pdata)

                        cinfo.setdefault('revoked_users', []).append(suid)
                        revoked_count += 1

                        # 添加通知邮件（告知已扣除）
                        notif_mail = {
                            "id": str(uuid.uuid4()),
                            "title": f"兑换码撤回通知（{code}）",
                            "content": f"管理员已撤销兑换码 {code}。由于未找到相关邮件，已直接扣除您库存中的对应物品（如有）。",
                            "sender": "系统",
                            "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                            "read": False,
                            "claimed": False,
                            "items": [],
                            "code": code
                        }
                        mails.append(notif_mail)
                        save_player_mails(suid, mails)

                        if p.get('client') and p['client'] in clients:
                            p['inventory'] = inv
                            try:
                                self.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                                self.send_to(p['client'], {
                                    'type': 'system_message',
                                    'data': {'text': f'兑换码 {code} 已被撤销，相关物品已扣除。'}
                                })
                            except:
                                pass

            # ---- 保存兑换码信息 ----
            save_server_codes(codes)

            # ---- 返回结果 ----
            self.send_json({
                'type': 'command_result',
                'data': {
                    'success': True,
                    'message': f'已撤销 {revoked_count} 个用户对兑换码 {code} 的使用资格。'
                }
            })
            return

        # ---------- 全体禁言 ----------
        if (cmd_name == '/mute' or cmd_name == 'mute') and len(parts) == 2 and parts[1].lower() == 'common':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            config = load_server_config()
            config['common_mute'] = True
            save_server_config(config)
            self.broadcast({
                'type': 'common_mute_status',
                'data': {'enabled': True}
            })
            self.broadcast_system("管理员已开启全体禁言")
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': '全体禁言已开启'}})
            return

        if (cmd_name == '/unmute' or cmd_name == 'unmute') and len(parts) == 2 and parts[1].lower() == 'common':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            config = load_server_config()
            config['common_mute'] = False
            save_server_config(config)
            self.broadcast({
                'type': 'common_mute_status',
                'data': {'enabled': False}
            })
            self.broadcast_system("管理员已解除全体禁言")
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': '全体禁言已解除'}})
            return

        # ---------- /mute 禁言用户 ----------
        if cmd_name == '/mute' or cmd_name == 'mute':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '格式: /mute <用户> <时长>'}})
                return
            target_spec = parts[1]
            duration_str = parts[2]
            delta, until_dt = parse_time_spec(duration_str)
            if until_dt:
                muted_until = until_dt.isoformat()
                if until_dt <= datetime.now(SERVER_TIMEZONE):
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': '指定的解禁时间已过期'}})
                    return
            elif delta:
                muted_until = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
            else:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '无效的时间格式'}})
                return

            target_player = None
            with server_lock:
                for p in clients.values():
                    if p['name'] == target_spec or p['userid'] == target_spec or p['server_uid'] == target_spec:
                        target_player = p
                        break
            if target_player:
                if target_player['permission'] == 'admin':
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '不能禁言管理员'}})
                    return
                pdata = load_player_data(target_player['server_uid'])
                if pdata:
                    pdata['muted_until'] = muted_until
                    save_player_data(target_player['server_uid'], pdata)
                    target_player['muted_until'] = muted_until
                # 禁言逻辑内，发送 mute_status 后
                try:
                    self.send_to(target_player['client'], {
                        'type': 'mute_status',
                        'data': {'muted_until': muted_until}
                    })
                    self.server.log_emitter.log_signal.emit(
                        f"[禁言] 已向 {target_player['name']} 发送禁言状态: {muted_until}")
                except Exception as e:
                    self.server.log_emitter.log_signal.emit(f"[禁言] 发送禁言状态失败: {e}")

                # ---- 新增：额外发送一条系统消息（确保用户看到） ----
                try:
                    self.send_to(target_player['client'], {
                        'type': 'system_message',
                        'data': {
                            'text': f"你已被管理员禁言，解封时间：{until_dt.strftime('%Y-%m-%d %H:%M:%S') if until_dt else (datetime.now(SERVER_TIMEZONE) + delta).strftime('%Y-%m-%d %H:%M:%S')}"}
                    })
                except Exception as e:
                    self.server.log_emitter.log_signal.emit(f"[禁言] 发送系统消息失败: {e}")
                # 构造带时区的时间字符串
                if until_dt:
                    time_str = until_dt.strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
                else:
                    time_str = (datetime.now(SERVER_TIMEZONE) + delta).strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
                # ---- 添加调试日志 ----
                self.server.log_emitter.log_signal.emit(
                    f"[DEBUG] until_dt.tzinfo = {until_dt.tzinfo if until_dt else 'None'}")
                self.server.log_emitter.log_signal.emit(f"[DEBUG] time_str = {time_str}")
                # ------------------------
                self.broadcast_system(f"{target_player['name']} 已被禁言，禁言时间：{time_str}")
            else:
                # 离线玩家
                server_uid, pdata = self.find_player_file_by_identifier(target_spec)
                if server_uid is None:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {target_spec}'}})
                    return
                if pdata.get('permission') == 'admin':
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '不能禁言管理员'}})
                    return
                pdata['muted_until'] = muted_until
                save_player_data(server_uid, pdata)
                if until_dt:
                    time_str = until_dt.strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
                else:
                    time_str = (datetime.now(SERVER_TIMEZONE) + delta).strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
                # ---- 添加调试日志 ----
                self.server.log_emitter.log_signal.emit(
                    f"[DEBUG] until_dt.tzinfo = {until_dt.tzinfo if until_dt else 'None'}")
                self.server.log_emitter.log_signal.emit(f"[DEBUG] time_str = {time_str}")
                # ------------------------
                self.broadcast_system(f"{target_player['name']} 已被禁言，禁言时间：{time_str}")

            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已禁言 {target_spec}'}})
            return

        # ---------- /unmute 解除禁言 ----------
        if cmd_name == '/unmute' or cmd_name == 'unmute':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '格式: /unmute <用户>'}})
                return
            target_spec = parts[1]

            target_player = None
            with server_lock:
                for p in clients.values():
                    if p['name'] == target_spec or p['userid'] == target_spec or p['server_uid'] == target_spec:
                        target_player = p
                        break
            if target_player:
                pdata = load_player_data(target_player['server_uid'])
                if pdata:
                    pdata['muted_until'] = None
                    save_player_data(target_player['server_uid'], pdata)
                    target_player['muted_until'] = None
                try:
                    self.send_to(target_player['client'], {
                        'type': 'mute_status',
                        'data': {'muted_until': None}
                    })
                    self.server.log_emitter.log_signal.emit(
                        f"[禁言] 已向 {target_player['name']} 发送解除禁言消息 (muted_until=None)"
                    )
                except Exception as e:
                    self.server.log_emitter.log_signal.emit(
                        f"[禁言] 发送解除消息失败: {e}"
                    )
            else:
                # 离线玩家
                server_uid, pdata = self.find_player_file_by_identifier(target_spec)
                if server_uid is None:
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {target_spec}'}})
                    return
                pdata['muted_until'] = None
                save_player_data(server_uid, pdata)
                self.broadcast_system(f"{target_spec} 已被解除禁言（离线）")

            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': f'已解除 {target_spec} 的禁言'}})
            return

        # ---------- /function AI 开关（原有） ----------
        if cmd_name == '/function' and len(parts) >= 2 and parts[1].lower() == 'ai':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '格式: /function AI on|off'}})
                return
            state = parts[2].lower()
            if state not in ('on', 'off'):
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '参数必须为 on 或 off'}})
                return
            config = load_server_config()
            config['ai_enabled'] = (state == 'on')
            save_server_config(config)
            self.broadcast({
                'type': 'system_message',
                'data': {'text': f"AI 功能已{'启用' if state == 'on' else '禁用'}"}
            })
            self.broadcast({
                'type': 'ai_status',
                'data': {'enabled': (state == 'on')}
            })
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'AI 功能已{state}'}})
            return

        # ---------- /function Chat 开关（新增） ----------
        if cmd_name == '/function' and len(parts) >= 2 and parts[1].lower() == 'chat':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 3:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '格式: /function Chat on|off'}})
                return
            state = parts[2].lower()
            if state not in ('on', 'off'):
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '参数必须为 on 或 off'}})
                return
            config = load_server_config()
            config['chat_enabled'] = (state == 'on')
            save_server_config(config)
            # 广播系统消息
            self.broadcast_system(f"聊天功能已{'启用' if state == 'on' else '禁用'}")
            # 广播结构化状态
            self.broadcast({
                'type': 'chat_status',
                'data': {'enabled': (state == 'on')}
            })
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'聊天功能已{state}'}})
            return

        if cmd_name == '/redeemstats':
            # 显示自己的 redeem_stats
            pdata = load_player_data(player['server_uid'])
            if pdata:
                stats = pdata.get('redeem_stats', {})
                text = "兑换码净获得统计：\n" + "\n".join(f"{k}: {v}" for k, v in stats.items())
                self.send_json({'type': 'system_message', 'data': {'text': text}})
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': text}})
            else:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '未找到数据'}})
            return

        if cmd_name == '/recall':
            if len(parts) < 2:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '用法: /recall <消息ID>'}})
                return

            msg_id = parts[1]

            # 加载聊天历史
            history = load_chat_history()
            target_msg = None
            for msg in history:
                if msg.get('msg_id') == msg_id:
                    target_msg = msg
                    break

            if not target_msg:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '消息不存在或已被撤回'}})
                return

            # 权限检查：管理员可以撤回任何人的消息，普通用户只能撤回自己的
            if player['permission'] != 'admin' and target_msg.get('from_name') != player['name']:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '无权撤回他人消息'}})
                return

            # 时间检查：普通用户限2分钟，管理员不受限
            try:
                msg_time = datetime.strptime(target_msg['time'], '%Y-%m-%d %H:%M:%S')
                msg_time = SERVER_TIMEZONE.localize(msg_time)
                if (datetime.now(SERVER_TIMEZONE) - msg_time).total_seconds() > 120 and player['permission'] != 'admin':
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': '超过2分钟，无法撤回'}})
                    return
            except Exception as e:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': f'时间解析失败: {e}'}})
                return

            # 标记消息为已撤回
            target_msg['revoked'] = True
            with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2, ensure_ascii=False)

            # 广播撤回通知（包含撤回者名字）
            self.broadcast({
                'type': 'recall_notification',
                'data': {
                    'msg_id': msg_id,
                    'sender': player['name']  # 撤回者的名字
                }
            })

            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': '撤回成功'}})
            return

        # ---------- 未知命令 ----------
        self.send_json({'type': 'command_result',
                        'data': {'success': False, 'message': f'未知指令 {cmd_name} 或该命令未完全实现'}})

    def is_valid_item(self, item_id):
        if item_id in ('cake', 'credit', 'ticket', 'star_rail_tickets', 'jade'):
            return True
        if item_id in AVAILABLE_CLOTHES:
            return True
        custom_items = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
        return any(it.get('id') == item_id for it in custom_items)

    def find_player_file_by_identifier(self, identifier):
        if not os.path.isdir(PLAYERS_DIR):
            return None, None
        identifier = str(identifier).strip()
        for filename in os.listdir(PLAYERS_DIR):
            if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                continue
            filepath = os.path.join(PLAYERS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except:
                continue
            server_uid = filename[:-5]
            if (server_uid == identifier or
                    data.get('name') == identifier or
                    data.get('userid') == identifier or
                    data.get('uid') == identifier):
                return server_uid, data
        return None, None

    def broadcast(self, msg):
        with server_lock:
            for client in list(clients.keys()):
                try: self.send_to(client, msg)
                except: pass

    def broadcast_chat(self, from_name, text, sender_player=None, msg_id=None):
        msg_base = {
            'type': 'chat_broadcast',
            'data': {
                'from_name': from_name,
                'text': text,
                'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
                'is_system': False,
                'msg_id': msg_id  # 新增
            }
        }
        with server_lock:
            for client, player in clients.items():
                msg = msg_base.copy()
                msg['data']['is_self'] = (player == sender_player)
                try:
                    self.send_to(client, msg)
                except:
                    pass

    def broadcast_system(self, text):
        self.broadcast({
            'type': 'chat_broadcast',
            'data': {
                'from_name': '系统',
                'text': text,
                'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
                'is_system': True
            }
        })

    def handle_mentions(self, text, sender):
        import re
        if re.search(r'@(所有人|all)', text, re.IGNORECASE):
            if sender['permission'] == 'admin':
                with server_lock:
                    for p in clients.values():
                        if p != sender:
                            try:
                                self.send_to(p['client'], {
                                    'type': 'mention',
                                    'data': {'from_name': sender['name'], 'all': True}
                                })
                            except:
                                pass
                self.broadcast_system(f"{sender['name']} @了所有人")
            else:
                self.send_json({'type': 'system_message', 'data': {'text': '只有管理员可以使用@所有人功能'}})
            return
        names = re.findall(r'@([\w\u4e00-\u9fff]+)', text)
        for name in names:
            with server_lock:
                target = next((p for p in clients.values() if p['name'] == name), None)
            if target and target != sender:
                try:
                    self.send_to(target['client'], {'type': 'mention', 'data': {'from_name': sender['name']}})
                except:
                    pass

    def send_json(self, msg):
        try:
            data = (json.dumps(msg, ensure_ascii=False) + '\n').encode()
            self.request.sendall(data)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    @staticmethod
    def send_to(client, msg):
        try:
            data = (json.dumps(msg, ensure_ascii=False) + '\n').encode()
            client.sendall(data)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    def __init__(self, server_address, RequestHandlerClass, log_emitter):
        self.log_emitter = log_emitter
        super().__init__(server_address, RequestHandlerClass)
        self.custom_menu_file = CUSTOM_MENU_FILE
        self.custom_items_file = CUSTOM_ITEMS_FILE
        self.last_activity = {}

    def load_custom_menu(self):
        if os.path.exists(self.custom_menu_file):
            try:
                with open(self.custom_menu_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except:
                pass
        return []

    def load_custom_items(self):
        if os.path.exists(self.custom_items_file):
            try:
                with open(self.custom_items_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except:
                pass
        return []


class ContentBridge(QObject):
    dataChanged = pyqtSignal()

    def __init__(self, window):
        super().__init__()
        self.window = window
        print(f"[Bridge] 初始化，window = {window}")

    @pyqtSlot(str, str, result=bool)
    def uploadResource(self, menu_id, file_data_json):
        """
        接收前端上传的文件列表（JSON），打包为ZIP并保存到 server_data/webroot/resources/<menu_id>.zip
        若传入单个 .zip 文件，则直接保存（不解压），完美保留目录结构。
        """
        import base64
        import zipfile
        import io
        import os
        import json

        try:
            files = json.loads(file_data_json)
            if not isinstance(files, list) or len(files) == 0:
                return False

            # 如果只有一个文件且以 .zip 结尾，直接保存为 ZIP（无需重新打包）
            if len(files) == 1:
                f = files[0]
                name = f.get('name', '')
                if name.lower().endswith('.zip'):
                    content_b64 = f.get('content')
                    if content_b64:
                        zip_data = base64.b64decode(content_b64)
                        resources_dir = os.path.join(SERVER_DATA, 'webroot', 'resources')
                        os.makedirs(resources_dir, exist_ok=True)
                        zip_path = os.path.join(resources_dir, f"{menu_id}.zip")
                        with open(zip_path, 'wb') as zf:
                            zf.write(zip_data)
                        # 自动更新菜单
                        if self._update_menu_resource(menu_id, f'/resources/{menu_id}.zip'):
                            self.window.save_files()
                            self.dataChanged.emit()
                        return True

            # 否则（多个文件或非ZIP）按原来的方式打包
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    name = f.get('name')
                    content_b64 = f.get('content')
                    if not name or not content_b64:
                        continue
                    content = base64.b64decode(content_b64)
                    zf.writestr(name, content)

            zip_data = zip_buffer.getvalue()
            resources_dir = os.path.join(SERVER_DATA, 'webroot', 'resources')
            os.makedirs(resources_dir, exist_ok=True)
            zip_path = os.path.join(resources_dir, f"{menu_id}.zip")
            with open(zip_path, 'wb') as zf:
                zf.write(zip_data)

            if self._update_menu_resource(menu_id, f'/resources/{menu_id}.zip'):
                self.window.save_files()
                self.dataChanged.emit()
            return True

        except Exception as e:
            print(f"[uploadResource] 错误: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _update_menu_resource(self, menu_id, resource_url):
        """更新菜单的 resource_url 和 resource_required"""

        def update(arr):
            for item in arr:
                if item.get('id') == menu_id:
                    item['resource_url'] = resource_url
                    item['resource_required'] = True
                    return True
                if 'children' in item:
                    if update(item['children']):
                        return True
            return False

        return update(self.window.menu_data)

    @pyqtSlot(result=str)
    def getGachaConfig(self):
        try:
            path = os.path.join(SERVER_DATA, 'gacha_config.json')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        # 尝试解析，确保是合法 JSON
                        json.loads(content)
                        return content
        except Exception as e:
            print(f"[getGachaConfig] 读取失败: {e}")
        # 任何错误都返回空对象
        return "{}"

    @pyqtSlot(str, result=bool)
    def saveGachaConfig(self, config_json):
        try:
            path = os.path.join(SERVER_DATA, 'gacha_config.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(config_json)
            return True
        except:
            return False

    @pyqtSlot(result=bool)
    def ping(self):
        print("[Bridge] ping() 被调用")
        try:
            print(f"[Bridge] self.window = {self.window}")
            return True
        except Exception as e:
            print(f"[Bridge] ping 异常: {e}")
            return False

    @pyqtSlot()
    def saveToDisk(self):
        """静默保存配置到磁盘，不弹出消息框"""
        self.window.save_files()

    @pyqtSlot(result=str)
    def getMenus(self):
        try:
            return json.dumps(self.window.menu_data)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @pyqtSlot(result=str)
    def getItems(self):
        try:
            return json.dumps(self.window.items_data)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @pyqtSlot(str, str, str, str, str, bool, str, str, str, str, result=bool)
    def saveMenu(self, menu_id, label, type_, data, tooltip, resource_required, resource_url, icon_type, icon, expiry):
        try:
            print(f"[saveMenu] 开始保存菜单: id={menu_id}, label={label}, expiry={expiry}")
            # ----- 解析有效期 -----
            expiry_iso = None
            if expiry and expiry.strip().lower() not in ('永久', 'permanent', ''):
                delta, until_dt = parse_time_spec(expiry)
                if until_dt:
                    expiry_iso = until_dt.isoformat()
                elif delta:
                    expiry_iso = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
            print(f"[saveMenu] 解析后的 expiry_iso = {expiry_iso}")

            # ----- 查找或创建菜单项 -----
            def find_node(arr, target_id):
                for item in arr:
                    if item.get('id') == target_id:
                        return item
                    if 'children' in item:
                        found = find_node(item['children'], target_id)
                        if found:
                            return found
                return None

            # ----- 唯一性检查（gacha） -----
            if type_ == 'gacha':
                def has_other_gacha(arr, exclude_id):
                    for item in arr:
                        if item.get('id') != exclude_id and item.get('type') == 'gacha':
                            return True
                        if 'children' in item and has_other_gacha(item['children'], exclude_id):
                            return True
                    return False

                if has_other_gacha(self.window.menu_data, menu_id):
                    print("[saveMenu] 拒绝保存：已存在其他 gacha 菜单项")
                    return False

            existing = find_node(self.window.menu_data, menu_id)
            if existing:
                print(f"[saveMenu] 找到现有菜单，更新字段")
                existing['label'] = label
                existing['type'] = type_
                existing['data'] = data
                existing['tooltip'] = tooltip
                existing['resource_required'] = resource_required
                existing['resource_url'] = resource_url
                existing['icon_type'] = icon_type
                existing['icon'] = icon
                existing['expiry'] = expiry_iso
            else:
                print(f"[saveMenu] 未找到现有菜单，创建新菜单")
                new_menu = {
                    'id': menu_id,
                    'label': label,
                    'type': type_,
                    'data': data,
                    'tooltip': tooltip,
                    'resource_required': resource_required,
                    'resource_url': resource_url,
                    'icon_type': icon_type,
                    'icon': icon,
                    'children': [],
                    'expiry': expiry_iso
                }
                self.window.menu_data.append(new_menu)

            # ★ 立即保存到磁盘
            print("[saveMenu] 准备调用 save_files()")
            self.window.save_files()
            print("[saveMenu] save_files() 调用完成")
            self.dataChanged.emit()
            return True

        except Exception as e:
            print(f"saveMenu error: {e}")
            import traceback
            traceback.print_exc()
            return False

    @pyqtSlot(str, str, str, str, int, result=bool)
    def saveItem(self, item_id, name, icon, description, count):
        try:
            print(f"[saveItem] 保存物品: id={item_id}, name={name}")
            if not item_id or item_id.strip() == '':
                item_id = f"custom_{uuid.uuid4().hex[:8]}"
            existing = next((it for it in self.window.items_data if it.get('id') == item_id), None)
            if existing:
                existing['name'] = name
                existing['icon'] = icon
                existing['description'] = description
                existing['count'] = count
            else:
                self.window.items_data.append({
                    'id': item_id,
                    'name': name,
                    'icon': icon,
                    'description': description,
                    'count': count
                })
            print("[saveItem] 准备调用 save_files()")
            self.window.save_files()
            print("[saveItem] save_files() 调用完成")
            self.dataChanged.emit()
            return True
        except Exception as e:
            print(f"saveItem error: {e}")
            import traceback
            traceback.print_exc()
            return False

    @pyqtSlot(str, result=bool)
    def deleteMenu(self, menu_id):
        try:
            def remove(arr):
                for i, item in enumerate(arr):
                    if item.get('id') == menu_id:
                        del arr[i]
                        return True
                    if 'children' in item and remove(item['children']):
                        return True
                return False
            if remove(self.window.menu_data):
                self.dataChanged.emit()
                return True
            return False
        except Exception as e:
            print(f"deleteMenu error: {e}")
            return False

    @pyqtSlot(str, result=bool)
    def deleteItem(self, item_id):
        try:
            for i, it in enumerate(self.window.items_data):
                if it.get('id') == item_id:
                    del self.window.items_data[i]
                    self.dataChanged.emit()
                    return True
            return False
        except Exception as e:
            print(f"deleteItem error: {e}")
            return False

    @pyqtSlot(str, str, result=bool)
    def addChildMenu(self, parent_id, child_data_json):
        try:
            child = json.loads(child_data_json)
            def find_parent(arr):
                for item in arr:
                    if item.get('id') == parent_id:
                        return item
                    if 'children' in item:
                        found = find_parent(item['children'])
                        if found:
                            return found
                return None
            parent = find_parent(self.window.menu_data)
            if parent:
                if 'children' not in parent:
                    parent['children'] = []
                parent['children'].append(child)
                self.dataChanged.emit()
                return True
            return False
        except Exception as e:
            print(f"addChildMenu error: {e}")
            return False

    @pyqtSlot(str, str, result=bool)
    def duplicateMenu(self, src_id, new_id):
        try:
            def find_copy(arr):
                for item in arr:
                    if item.get('id') == src_id:
                        copy = json.loads(json.dumps(item))
                        copy['id'] = new_id
                        copy['label'] += ' (副本)'
                        if 'children' in copy and len(copy['children']) > 0:
                            copy['children'] = []
                        return copy
                    if 'children' in item:
                        found = find_copy(item['children'])
                        if found:
                            return found
                return None
            cp = find_copy(self.window.menu_data)
            if not cp:
                return False
            def insert_after(arr):
                for i, item in enumerate(arr):
                    if item.get('id') == src_id:
                        arr.insert(i+1, cp)
                        return True
                    if 'children' in item and insert_after(item['children']):
                        return True
                return False
            if insert_after(self.window.menu_data):
                self.dataChanged.emit()
                return True
            return False
        except Exception as e:
            print(f"duplicateMenu error: {e}")
            return False

    @pyqtSlot(str, str, result=bool)
    def duplicateItem(self, src_id, new_id):
        try:
            src = next((it for it in self.window.items_data if it.get('id') == src_id), None)
            if not src:
                return False
            cp = json.loads(json.dumps(src))
            cp['id'] = new_id
            cp['name'] += ' (副本)'
            self.window.items_data.append(cp)
            self.dataChanged.emit()
            return True
        except Exception as e:
            print(f"duplicateItem error: {e}")
            return False

    # ---------- 修复：调用窗口的方法 ----------
    @pyqtSlot()
    def hotUpdate(self):
        self.window.hot_update()

    @pyqtSlot()
    def publish(self):
        self.window.publish()

    @pyqtSlot()
    def refresh(self):
        self.window.load_all_data()
        self.dataChanged.emit()

    # ---------- 新增：关闭窗口 ----------
    @pyqtSlot()
    def close(self):
        self.window.close()

class ContentManagerWindow(QDialog):
    def __init__(self, server_window, parent=None):
        super().__init__(parent)
        self.server_window = server_window
        self.setWindowTitle("服务器内容管理")
        self.setMinimumSize(1100, 750)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)

        self.menu_data = []
        self.items_data = []
        self.load_all_data()

        self.bridge = ContentBridge(self)
        self.bridge.dataChanged.connect(self.on_data_changed)

        self.webview = QWebEngineView()
        # 启用本地文件访问
        settings = self.webview.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        # 捕获 JS 控制台输出
        self.webview.page().javaScriptConsoleMessage = lambda level, msg, line, source: print(f"[JS] {msg}")

        html_path = resource_path("html/index.html")
        if not os.path.exists(html_path):
            self.webview.setHtml("<h1 style='color:white;'>错误：未找到 html/index.html</h1>")
        else:
            # 测试用：先注释 setUrl，改用 setHtml 读取文件内容
            # self.webview.setUrl(QUrl.fromLocalFile(html_path))
            with open(html_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.webview.setHtml(content, QUrl.fromLocalFile(os.path.dirname(html_path) + '/'))

        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.webview.page().setWebChannel(self.channel)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.webview)
        self.setLayout(layout)

        self.webview.loadFinished.connect(self.on_load_finished)

    def on_load_finished(self):
        self.bridge.dataChanged.emit()
        # 增加重试机制，确保前端 initBridge 被调用
        self.webview.page().runJavaScript("""
            (function retryInit(attempt) {
                if (typeof initBridge === 'function') {
                    initBridge();
                } else if (attempt < 5) {
                    setTimeout(function() { retryInit(attempt + 1); }, 500);
                } else {
                    console.error('initBridge 未定义，多次重试失败');
                }
            })(0);
        """)

    def on_data_changed(self):
        pass

    def load_all_data(self):
        self.menu_data = self.load_json('custom_menu.json')
        self.items_data = self.load_json('custom_items.json')

    def load_json(self, filename):
        path = os.path.join(SERVER_DATA, filename)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return []
        return []

    def save_json(self, filename, data):
        path = os.path.join(SERVER_DATA, filename)
        print(f"[save_json] 准备写入 {path}")
        if os.path.exists(path):
            shutil.copy2(path, path + '.bak')
        tmp_path = path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
            print(f"[save_json] 成功写入 {path}")
        except Exception as e:
            print(f"[save_json] 写入失败: {e}")
            raise

    def save_files(self):
        """保存菜单和物品数据到文件（无界面提示）"""
        try:
            self.save_json('custom_menu.json', self.menu_data)
            self.save_json('custom_items.json', self.items_data)
            print("[save_files] 成功保存所有文件")
        except Exception as e:
            print(f"[save_files] 保存失败: {e}")
            import traceback
            traceback.print_exc()

    def publish(self):
        self.save_files()
        QMessageBox.information(self, "成功", "配置已保存至磁盘。\n服务端重启后对所有玩家生效。")

    def hot_update(self):
        self.save_files()
        try:
            if self.server_window and self.server_window.server:
                self.server_window.broadcast_custom_config()
                QMessageBox.information(self, "成功", "配置已保存并推送给所有在线玩家")
            else:
                QMessageBox.information(self, "成功", "配置已保存（服务器未运行，无法广播）")
        except Exception as e:
            QMessageBox.warning(self, "广播失败", f"热更新推送失败：{str(e)}\n配置已保存，新登录玩家将生效。")

    def closeEvent(self, event):
        self.save_files()
        event.accept()

# ==================== GUI 窗口 ====================
class ServerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.server = None
        self.server_thread = None
        self.init_ui()
        self.apply_style()
        # 加载保存的时区并应用
        saved_config = load_server_config()
        saved_tz = saved_config.get("timezone", "Asia/Shanghai")
        set_server_timezone(saved_tz)
        self.tz_combo.setCurrentText(saved_tz)
        # 打印当前服务器名称
        current_name = saved_config.get("server_name", "流萤服务器")
        self.append_log(f"当前服务器名称: {current_name}")
        # 加载维护模式状态
        load_maintenance_state()
        os.makedirs(SERVER_DATA, exist_ok=True)

        if maintenance_mode:
            msg = "⚠️ 维护模式已开启(可通过/fix off关闭。)"
            if maintenance_message:
                msg += f": {maintenance_message}"
            self.append_log(msg)

    def _get_player_name_by_uid(self, server_uid):
        with server_lock:
            if server_uid in player_by_server_uid:
                return player_by_server_uid[server_uid]['name']
        path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('name', server_uid)
            except:
                pass
        return server_uid

    def open_content_manager(self):
        if hasattr(self, '_content_manager') and self._content_manager and self._content_manager.isVisible():
            self._content_manager.raise_()
            self._content_manager.activateWindow()
            return
        self._content_manager = ContentManagerWindow(self)
        self._content_manager.show()

    def start_web_server(self):
        """启动轻量级 HTTP 服务器，自动分配空闲端口，根目录为 server_data/webroot"""
        import socketserver
        from http.server import SimpleHTTPRequestHandler
        import threading
        import socket

        # 尝试从 8080 开始找空闲端口
        port = 8080
        max_port = 8099
        while port <= max_port:
            try:
                test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_sock.settimeout(1)
                test_sock.bind(('0.0.0.0', port))
                test_sock.close()
                break
            except OSError:
                port += 1
        else:
            self.append_log("[Web服务] 未找到可用端口 (8080-8099 均被占用)")
            return

        # Web 根目录
        web_root = os.path.join(SERVER_DATA, 'webroot')
        os.makedirs(web_root, exist_ok=True)
        # 确保 resources 子目录存在（用于资源包）
        resources_dir = os.path.join(web_root, 'resources')
        os.makedirs(resources_dir, exist_ok=True)

        # 自定义 Handler，不改变当前工作目录，直接基于 web_root 提供文件
        class CustomHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                # 将目录设为 web_root，但不改变全局 os.getcwd()
                super().__init__(*args, directory=web_root, **kwargs)

            def do_GET(self):
                # 处理 API 请求（可扩展）
                if self.path.startswith('/api/'):
                    self.handle_api()
                    return
                # 否则提供静态文件（SimpleHTTPRequestHandler 会自动处理）
                return super().do_GET()

            def do_POST(self):
                if self.path.startswith('/api/'):
                    self.handle_api_post()
                    return
                self.send_error(404)

            def handle_api(self):
                # 示例 API，可扩展
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def handle_api_post(self):
                if self.path == '/api/echo':
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            # 重写 log_message 避免控制台输出（可选）
            def log_message(self, format, *args):
                pass

        # 创建服务器
        self.web_server = socketserver.TCPServer(('0.0.0.0', port), CustomHandler)
        self.web_server_port = port

        # 在后台线程运行
        self.web_thread = threading.Thread(target=self.web_server.serve_forever, daemon=True)
        self.web_thread.start()

        self.append_log(f"[Web服务] 已启动，端口 {port}，根目录 {web_root}")

        # 保存端口到配置
        config = load_server_config()
        config['web_port'] = port
        save_server_config(config)

    def stop_web_server(self):
        if hasattr(self, 'web_server') and self.web_server:
            self.web_server.shutdown()
            self.web_server.server_close()
            self.web_server = None
            self.append_log("[Web服务] 已停止")

    def find_player_file_by_identifier(self, identifier):
        # 直接复制 TCPHandler 中的实现
        if not os.path.isdir(PLAYERS_DIR):
            return None, None
        identifier = str(identifier).strip()
        for filename in os.listdir(PLAYERS_DIR):
            if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                continue
            filepath = os.path.join(PLAYERS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except:
                continue
            server_uid = filename[:-5]
            if (server_uid == identifier or
                    data.get('name') == identifier or
                    data.get('userid') == identifier or
                    data.get('uid') == identifier):
                return server_uid, data
        return None, None

    def init_ui(self):
        self.setWindowTitle(f"流萤联机服务器控制台 v{SERVER_VERSION}")
        self.setFixedSize(700, 600)
        icon_path = os.path.join(SERVER_DIR, "images", "bg.png")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        screen = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen - self.rect().center())
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit, 1)

        # 时区选择行（带应用按钮）
        tz_layout = QHBoxLayout()
        tz_label = QLabel("服务器时区:")
        self.tz_combo = QComboBox()
        all_timezones = sorted(pytz.common_timezones)
        self.tz_combo.addItems(all_timezones)
        apply_tz_btn = QPushButton("应用并重启")
        apply_tz_btn.clicked.connect(self.on_apply_timezone)
        tz_layout.addWidget(tz_label)
        tz_layout.addWidget(self.tz_combo)
        tz_layout.addWidget(apply_tz_btn)
        tz_layout.addStretch()
        layout.addLayout(tz_layout)

        cmd_layout = QHBoxLayout()
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText("输入命令 (/help获取用法)")

        completer_list = [
            '/help', '/stop', '/restart',
            '/fix on', '/fix off',
            '/ban area', '/unban area',
            '/ban timezone', '/unban timezone',
            '/ban ip list', '/ban ip add', '/ban ip', '/unban ip',
            '/op', '/deop', '/ban', '/unban', '/kick',
            '/All_Versions True', '/All_Versions False',
            '/give', '/set', '/clear', '/revoke', '/set newcode',
            '/revoke code',
            '/mute', '/unmute',
            '/mute common', '/unmute common',
            '/function AI on', '/function AI off',
            '/function Chat on', '/function Chat off',
            '/change name',
            '/resetname',
            '/maxplayers',
            '/access whitelist on', '/access whitelist off',
            '/access blacklist on', '/access blacklist off',
            '/access none',
            '/whitelist add', '/whitelist remove', '/whitelist list', '/whitelist on', '/whitelist off',
            '/blacklist add', '/blacklist remove', '/blacklist list', '/blacklist on', '/blacklist off'
        ]
        completer = QCompleter(completer_list)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.cmd_edit.setCompleter(completer)

        self.cmd_edit.returnPressed.connect(self.send_command)
        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self.send_command)
        cmd_layout.addWidget(self.cmd_edit)
        cmd_layout.addWidget(send_btn)
        layout.addLayout(cmd_layout)

        btn_layout = QHBoxLayout()
        # 在 btn_layout 中添加
        edit_content_btn = QPushButton("📝 管理内容")
        edit_content_btn.clicked.connect(self.open_content_manager)
        btn_layout.addWidget(edit_content_btn)
        self.start_btn = QPushButton("启动服务器")
        self.start_btn.clicked.connect(self.start_server)
        self.restart_btn = QPushButton("重启服务器")
        self.restart_btn.clicked.connect(self.restart_server)
        self.stop_btn = QPushButton("停止服务器")
        self.stop_btn.clicked.connect(self.stop_server)
        self.stop_btn.setEnabled(False)
        self.restart_btn.setEnabled(False)
        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.clicked.connect(self.check_update)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.restart_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.check_update_btn)
        layout.addLayout(btn_layout)


        self.log_emitter = LogEmitter()
        self.log_emitter.log_signal.connect(self.append_log)

    def broadcast_structured(self, msg):
        with server_lock:
            for client in list(clients.keys()):
                try:
                    TCPHandler.send_to(client, msg)
                except:
                    pass

    def exec_kick(self, identifier):
        if not self.server:
            self.append_log("服务器未启动，无法踢出玩家")
            return
        with server_lock:
            target = next((p for p in clients.values() if
                           p['name'] == identifier or
                           p['userid'] == identifier or
                           p['server_uid'] == identifier), None)
        if target:
            try:
                TCPHandler.send_to(target['client'], {
                    'type': 'kick',
                    'data': {'reason': '你已被管理员踢出服务器，可重新连接。'}
                })
                time.sleep(0.1)
                target['client'].close()
            except Exception as e:
                self.append_log(f"踢出玩家时出错: {e}")
            with server_lock:
                if target['client'] in clients:
                    del clients[target['client']]
                if target['server_uid'] in player_by_server_uid:
                    del player_by_server_uid[target['server_uid']]
            self.append_log(f"已踢出玩家 {identifier}")
            self.broadcast_to_all(f"{identifier} 被管理员踢出服务器")
            self.refresh_all_lists()
        else:
            self.append_log(f"未找到在线玩家 {identifier}")

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1E1E1E; border-radius: 10px; }
            QTextEdit { background-color: #2D2D2D; border:1px solid #555; border-radius:8px; color:#CCC; font-family:Consolas; font-size:13px; padding:5px; }
            QLineEdit { border:1px solid #555; border-radius:6px; padding:6px; background:#2D2D2D; color:#CCC; font-size:13px; }
            QPushButton { background-color:#4CAF50; color:white; border:none; border-radius:6px; padding:8px 16px; font-size:13px; }
            QPushButton:hover { background-color:#45A049; }
            QPushButton:disabled { background-color:#666; }
            QLabel { color: #E0E0E0; font-size:13px; }
            QComboBox { background-color:#2D2D2D; color:white; border:1px solid #555; border-radius:4px; padding:4px; }
            QComboBox::drop-down { border: none; }
            QComboBox::down-arrow { image: none; }
            QComboBox QAbstractItemView { color: white; background-color: #2D2D2D; selection-background-color: #4CAF50; }
        """)

    def append_log(self, text):
        timestamp = datetime.now(SERVER_TIMEZONE).strftime("[%H:%M:%S] ")
        self.log_edit.append(timestamp + text)
        log_file = os.path.join(SERVER_DATA, "server.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(timestamp + text + "\n")

    def on_apply_timezone(self):
        tzname = self.tz_combo.currentText()
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("确认修改时区")
        msg_box.setText(f"确定要将服务器时区修改为 {tzname} 并重启服务器吗？\n重启后所有玩家将被断开连接。")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStyleSheet("""
            QMessageBox { background-color: #2D2D2D; color: #E0E0E0; }
            QMessageBox QLabel { color: #E0E0E0; background-color: #2D2D2D; }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 4px; padding: 5px 10px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        reply = msg_box.exec_()
        if reply == QMessageBox.Yes:
            self.apply_timezone_and_restart(tzname)

    def apply_timezone_and_restart(self, tzname):
        if not set_server_timezone(tzname):
            self.append_log(f"无效时区 {tzname}，切换失败")
            return
        config = load_server_config()
        config["timezone"] = tzname
        save_server_config(config)
        self.append_log(f"服务器时区已切换为 {tzname}（已保存）")
        if self.server is None:
            self.append_log("服务器未启动，时区已更改，无需重启。")
        else:
            self.append_log("服务器正在运行，将重启以完全应用新时区...")
            self.restart_server()

    def start_server(self):
        try:
            self.server = ThreadedTCPServer((HOST, PORT), TCPHandler, self.log_emitter)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            # 启动 Web 服务器（如果尚未启动）
            if not hasattr(self, 'web_server') or self.web_server is None:
                self.start_web_server()
            self.append_log(f"服务器已启动，监听 {HOST}:{PORT}")
            self.append_log("提示：输入 /help 查看命令提示")
            self.start_btn.setEnabled(False)
            self.restart_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
            # ---------- 启动归档定时器 ----------
            self._schedule_archive()
        except Exception as e:
            self.append_log(f"启动失败: {e}")

    def _schedule_archive(self):
        """计算下一个凌晨4点，启动归档定时器"""
        def archive_and_broadcast():
            archive_chat_history()
            # 向所有在线玩家广播系统消息
            if self.server:
                with server_lock:
                    for client in list(clients.keys()):
                        try:
                            TCPHandler.send_to(client, {
                                'type': 'chat_broadcast',
                                'data': {
                                    'from_name': '系统',
                                    'text': '📅 聊天记录已清空，开始新的一天。',
                                    'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
                                    'is_system': True
                                }
                            })
                        except:
                            pass
            # 调度下一次
            self._schedule_archive()

        now = datetime.now(SERVER_TIMEZONE)
        # 下一个凌晨4点（原为3点，现改为4点）
        next_run = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        delay = (next_run - now).total_seconds()
        threading.Timer(delay, archive_and_broadcast).start()

    def stop_server(self):
        if self.server:
            with server_lock:
                for client in list(clients.keys()):
                    try:
                        TCPHandler.send_to(client, {'type': 'server_shutdown', 'data': {'reason': '服务器已停止'}})
                        time.sleep(0.1)  # 等待消息发送
                        client.close()
                    except:
                        pass
                clients.clear()
                player_by_server_uid.clear()
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.stop_web_server()
            self.append_log("服务器已停止")
            self.start_btn.setEnabled(True)
            self.restart_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)

    def restart_server(self):
        self.append_log("正在重启服务器...")
        self.stop_server()
        self.start_server()

    def send_command(self):
        cmd = self.cmd_edit.text().strip()
        if not cmd:
            return
        self.cmd_edit.clear()

        if cmd == '/help':
            self.show_help_dialog()
            return

        if cmd == '/stop':
            self.stop_server()
        elif cmd == '/restart':
            self.restart_server()
        elif cmd.startswith('/'):
            parts = cmd.split()
            if not parts:
                return
            cmd_name = parts[0].lower()

            # ---------- /fix 维护模式 ----------
            if cmd_name == '/fix' and len(parts) >= 2:
                subcmd = parts[1].lower()
                if subcmd == 'on':
                    msg = ' '.join(parts[2:]) if len(parts) > 2 else ''
                    set_maintenance(True, msg if msg else None)
                    if self.server:
                        if msg and msg.strip():
                            reason_text = f"服务器正在维护\n{msg}"
                        else:
                            reason_text = "服务器正在维护"
                        kick_all_players_for_maintenance(reason_text)
                        self.append_log(f"维护模式已开启，已踢出所有在线玩家，消息: {maintenance_message}")
                    else:
                        self.append_log(f"维护模式已开启（服务器未启动，无玩家被踢出），消息: {maintenance_message}")
                elif subcmd == 'off':
                    set_maintenance(False, '')
                    self.append_log("维护模式已关闭")
                else:
                    self.append_log("用法: /fix on [消息] 或 /fix off")
                return

            # ---------- 地区封禁/解封 ----------
            if cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'area':
                self.exec_ban_area(parts[2])
                return
            elif cmd_name == '/unban' and len(parts) >= 3 and parts[1].lower() == 'area':
                self.exec_unban_area(parts[2])
                return
            elif cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
                self.exec_ban_timezone(parts[2])
                return
            elif cmd_name == '/unban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
                self.exec_unban_timezone(parts[2])
                return

            # ========== IP 封禁命令（必须在通用 /ban 之前） ==========
            if cmd_name == '/ban' and len(parts) >= 2 and parts[1].lower() == 'ip':
                if len(parts) < 3:
                    self.append_log("用法: /ban ip list 或 /ban ip <IP> 或 /ban ip <玩家名>")
                    return

                subcmd = parts[2].lower()

                if subcmd == 'list':
                    ips = load_banned_ips()
                    if ips:
                        self.append_log("封禁IP列表:")
                        for ip in ips:
                            self.append_log(f"  {ip}")
                    else:
                        self.append_log("封禁IP列表为空")
                    return

                ip_pattern = r'^\d+\.\d+\.\d+\.\d+$'
                if re.match(ip_pattern, parts[2]):
                    ip = parts[2]
                    ips = load_banned_ips()
                    if ip in ips:
                        self.append_log(f"IP {ip} 已在封禁列表中")
                        return
                    ips.append(ip)
                    save_banned_ips(ips)
                    self.append_log(f"已封禁IP: {ip}")
                    # 踢出所有使用该IP的非管理员玩家
                    with server_lock:
                        to_kick = [p for p in clients.values() if
                                   p['client'].getpeername()[0] == ip and p['permission'] != 'admin']
                    for p in to_kick:
                        try:
                            TCPHandler.send_to(p['client'], {'type': 'kick', 'data': {'reason': '你的IP已被封禁'}})
                            time.sleep(0.1)  # 等待消息发送完成
                            p['client'].close()
                        except:
                            pass
                        with server_lock:
                            if p['client'] in clients:
                                del clients[p['client']]
                            if p['server_uid'] in player_by_server_uid:
                                del player_by_server_uid[p['server_uid']]
                    if to_kick:
                        self.append_log(f"已踢出 {len(to_kick)} 个使用该IP的玩家")
                    return

                # 视为玩家名，封禁其当前IP（玩家必须在线）
                user = parts[2]
                with server_lock:
                    target = next((p for p in clients.values() if
                                   p['name'] == user or p['userid'] == user or p['server_uid'] == user), None)
                if not target:
                    self.append_log(f"未找到在线玩家 {user}")
                    return
                ip = target['client'].getpeername()[0]
                ips = load_banned_ips()
                if ip in ips:
                    self.append_log(f"IP {ip} 已在封禁列表中")
                    return
                ips.append(ip)
                save_banned_ips(ips)
                try:
                    TCPHandler.send_to(target['client'], {'type': 'kick', 'data': {'reason': '你的IP已被封禁'}})
                    time.sleep(0.1)
                    target['client'].close()
                except:
                    pass
                with server_lock:
                    if target['client'] in clients:
                        del clients[target['client']]
                    if target['server_uid'] in player_by_server_uid:
                        del player_by_server_uid[target['server_uid']]
                self.append_log(f"已封禁玩家 {user} 的IP: {ip}")
                self.broadcast_to_all(f"{user} 的IP已被封禁")
                return

            if cmd_name == '/unban' and len(parts) >= 2 and parts[1].lower() == 'ip':
                if len(parts) < 3:
                    self.append_log("用法: /unban ip <IP>")
                    return
                ip = parts[2]
                ips = load_banned_ips()
                if ip in ips:
                    ips.remove(ip)
                    save_banned_ips(ips)
                    self.append_log(f"已解封IP: {ip}")
                else:
                    self.append_log(f"IP {ip} 不在封禁列表中")
                return

            # ---------- 通用封禁/解封 ----------
            if cmd_name == '/ban' and len(parts) >= 2:
                target = parts[1]
                duration = parts[2] if len(parts) > 2 else None
                self.exec_ban(target, duration)
                return

            if cmd_name == '/unban' and len(parts) == 2:
                self.exec_unban(parts[1])
                return

            # ---------- OP/DEOP ----------
            if cmd_name == '/op' and len(parts) == 2:
                self.exec_op(parts[1])
                return

            if cmd_name == '/deop' and len(parts) == 2:
                self.exec_deop(parts[1])
                return

            # ---------- 踢出玩家 ----------
            if cmd_name == '/kick' and len(parts) == 2:
                self.exec_kick(parts[1])
                return

            # ---------- /all_versions ----------
            if cmd_name == '/all_versions' and len(parts) == 2:
                global ALLOW_ALL_VERSIONS
                arg = parts[1].lower()
                if arg == 'true':
                    ALLOW_ALL_VERSIONS = True
                    self.append_log("✓ 已启用所有版本放行模式（/All_Versions True）")
                elif arg == 'false':
                    ALLOW_ALL_VERSIONS = False
                    self.append_log("✓ 已关闭所有版本放行模式（/All_Versions False）")
                else:
                    self.append_log("用法: /All_Versions True 或 /All_Versions False")
                return

            # ---------- /give ----------
            if cmd_name == '/give':
                self.exec_give(cmd)
                return

            # ---------- /change name ----------
            if cmd_name == '/change':
                if len(parts) >= 3 and parts[1].lower() == 'name':
                    new_name = ' '.join(parts[2:]).strip()
                    if not new_name or len(new_name) > 15:
                        self.append_log("错误：服务器名称不能为空且最多15个字符")
                    else:
                        config = load_server_config()
                        config['server_name'] = new_name
                        save_server_config(config)
                        self.append_log(f"服务器名称已更新为 '{new_name}'，重启后生效")
                        self.append_log(f"配置文件路径: {SERVER_CONFIG_FILE}")
                else:
                    self.append_log("用法: /change name <新服务器名称> (最多15个字符)")
                return

            # ---------- /resetname ----------
            if cmd_name == '/resetname':
                if len(parts) == 2:
                    identifier = parts[1]
                    server_uid = None
                    pdata = None
                    with server_lock:
                        for p in clients.values():
                            if p['name'] == identifier or p['userid'] == identifier or p['server_uid'] == identifier:
                                server_uid = p['server_uid']
                                break
                    if server_uid is None:
                        server_uid, pdata = self.find_player_file_by_identifier(identifier)
                    else:
                        pdata = load_player_data(server_uid)
                    if server_uid is None or pdata is None:
                        self.append_log(f"未找到玩家 {identifier}")
                    else:
                        pdata['change_name_count'] = 0
                        pdata['last_change_name_time'] = None
                        save_player_data(server_uid, pdata)
                        self.append_log(f"已重置 {identifier} 的改名次数")
                else:
                    self.append_log("用法: /resetname <用户>")
                return

            # ---------- /maxplayers ----------
            if cmd_name == '/maxplayers':
                self.exec_maxplayers(parts)
                return

            # ---------- /access ----------
            if cmd_name == '/access':
                self.exec_access(parts)
                return

            # ---------- /whitelist / /blacklist ----------
            if cmd_name == '/whitelist':
                self.exec_whitelist(parts)
                return

            if cmd_name == '/blacklist':
                self.exec_blacklist(parts)
                return

            # ---------- /set ----------
            if cmd_name == '/set':
                if len(parts) >= 3 and parts[1].lower() == 'newcode':
                    self.exec_set_newcode(cmd)
                else:
                    self.exec_set(cmd)
                return

            # ---------- /clear ----------
            if cmd_name == '/clear':
                self.exec_clear(cmd)
                return

            # ---------- /revoke ----------
            if cmd_name == '/revoke':
                if len(parts) >= 3 and parts[1].lower() == 'code':
                    self.exec_revoke_code(cmd)
                else:
                    self.exec_revoke(cmd)
                return

            # ---------- /mute / /unmute ----------
            if cmd_name == '/mute':
                self.exec_mute(parts)
                return

            if cmd_name == '/unmute':
                self.exec_unmute(parts)
                return

            # ---------- /function ----------
            if cmd_name == '/function' and len(parts) >= 2:
                if parts[1].lower() == 'ai':
                    self.exec_function_ai(parts)
                elif parts[1].lower() == 'chat':
                    self.exec_function_chat(parts)
                else:
                    self.append_log("用法: /function AI on|off 或 /function Chat on|off")
                return

            # ---------- 未知命令 ----------
            self.append_log(f"未知控制台指令: {cmd}")
        else:
            # 非命令前缀，当作聊天广播（如控制台发言）
            if self.server:
                with server_lock:
                    if clients:
                        log_msg = f"[控制台] {cmd}"
                        self.append_log(log_msg)
                        for client in clients.keys():
                            TCPHandler.send_to(client, {'type': 'chat_broadcast',
                                                        'data': {'from_name': '系统', 'text': log_msg,
                                                                 'is_system': True}})
                    else:
                        self.append_log("[控制台] 没有玩家在线")
            else:
                self.append_log("服务器未启动")

    def show_help_dialog(self):
        """显示控制台帮助对话框（支持滚轮滚动）"""
        help_text = (
            "═══════════════════════════════════════════\n"
            "         流萤联机服务器 控制台帮助\n"
            "═══════════════════════════════════════════\n\n"
            "【服务器控制台专用命令】（仅在控制台有效）\n"
            "  /restart           重启服务器（断开所有玩家）\n"
            "  /close             关闭服务器（停止服务）\n"
            "  /change name <新名称>  修改服务器名称（最多15字符）\n"
            "  /fix on [消息]      开启维护模式并踢出所有玩家\n"
            "  /fix off            关闭维护模式\n"
            "  /All_Versions True/False  调试用，放行所有版本\n\n"
            "【常用管理命令】（客户端可用）\n"
            "  /give <用户> <物品id> <数量>\n"
            "      给指定玩家发放物品（邮件形式）\n"
            "      用户: @a(所有人) @s(自己) 或 玩家名\n"
            "      物品: cake, credit, ruyi(时装), ticket, jade, star_rail_tickets\n"
            "      示例: /give Gubwin credit 1000\n\n"
            "  /set <用户> <物品id> <数量>\n"
            "      直接设定玩家物品数量（覆盖）\n"
            "      示例: /set @s cake 50\n\n"
            "  /clear <用户> <物品或*>\n"
            "      清空指定玩家的物品（仅支持玩家名或@s）\n"
            "      物品: cake, credit, * (清空全部)\n"
            "      示例: /clear Gubwin cake\n\n"
            "  /revoke <用户> <物品:数量&物品...>\n"
            "      直接扣除玩家物品（允许负值，时装只能移除）\n"
            "      用户: @a(所有人) @s(自己) 或 玩家名\n"
            "      物品: cake, credit, ticket, 时装ID\n"
            "      示例: /revoke Gubwin cake:5&credit:1000\n"
            "      被扣除的玩家会收到邮件通知，不可对自己使用\n\n"
            "  /revoke code <兑换码> <用户>\n"
            "      撤销指定用户对兑换码的使用资格（撤回兑换码）\n"
            "      用户: 同 /revoke\n"
            "      示例: /revoke code GIFT2026 @a\n\n"
            "  /set newcode <兑换码> <物品列表>\n"
            "      创建新兑换码，物品列表用 & 分隔\n"
            "      格式: 物品:数量 (数量默认为1，上限9999)\n"
            "      示例: /set newcode 新春福利 cake:5&ruyi&credit:1000\n\n"
            "  /mute <用户> <时长>\n"
            "      禁言玩家，时长格式同封禁（如 30D 或 2026-12-31）\n"
            "      示例: /mute Gubwin 1D\n\n"
            "  /unmute <用户>\n"
            "      解除禁言\n"
            "      示例: /unmute Gubwin\n\n"
            "  /function AI on|off\n"
            "      启用或禁用服务器的 AI（在线对话）功能\n\n"
            "  /function Chat on|off\n"
            "      启用或禁用服务器的多人聊天功能\n\n"
            "【玩家管理】\n"
            "  /ban <玩家> [时长]         封禁玩家（时长如 30D, 2026-12-31）\n"
            "  /unban <玩家>              解封玩家\n"
            "  /op <玩家>                 提升为管理员\n"
            "  /deop <玩家>               取消管理员\n"
            "  /resetname <用户>          重置玩家的改名冷却时间\n\n"
            "【区域与时区封禁】\n"
            "  /ban area <国家代码>       封禁地区\n"
            "  /unban area <国家代码>     解封地区\n"
            "  /ban timezone <时区名>     封禁时区\n"
            "  /unban timezone <时区名>   解封时区\n\n"
            "【IP 封禁】（管理员专用）\n"
            "  /ban ip list              查看已封禁的 IP 列表\n"
            "  /ban ip <IP>              直接封禁指定 IP（支持 IPv4）\n"
            "  /ban ip <玩家名>          封禁在线玩家当前的 IP\n"
            "  /unban ip <IP>            解封指定 IP\n"
            "  ⚠️ 封禁 IP 后，所有使用该 IP 的非管理员玩家将被立即踢出。\n\n"
            "【访问控制】（管理员专用）\n"
            "  /maxplayers <人数>         设置服务器最大在线人数（默认10）\n"
            "  /access <whitelist|blacklist|none> <on|off>\n"
            "      开启/关闭白名单或黑名单模式，none 关闭所有限制\n"
            "  /whitelist on|off          快捷开启/关闭白名单模式\n"
            "  /blacklist on|off          快捷开启/关闭黑名单模式\n"
            "  /whitelist add|remove|list [uuid]\n"
            "      管理白名单（必须先开启白名单模式）\n"
            "  /blacklist add|remove|list [uuid]\n"
            "      管理黑名单（必须先开启黑名单模式）\n"
            "  ⚠️ 管理员拥有完全豁免权，不受上述限制\n\n"
            "【其他】\n"
            "  /kick <玩家>               踢出玩家（需在线）\n"
            "  /recall <消息ID>           撤回自己的消息（2分钟内）\n"
            "  /redeemstats               查看自己的兑换码净获得统计\n\n"
            "💡 提示：所有管理命令仅管理员可用。输入 /help 查看本帮助。"
        )

        # 创建自定义对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("帮助")
        dialog.setMinimumSize(600, 450)  # 足够大以显示内容
        dialog.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QTextEdit {
                background-color: #2D2D2D;
                color: #E0E0E0;
                border: 1px solid #555555;
                border-radius: 6px;
                padding: 10px;
                font-family: Consolas, monospace;
                font-size: 13px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # 文本框（只读）
        text_edit = QTextEdit()
        text_edit.setPlainText(help_text)
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(QTextEdit.NoWrap)  # 不换行，水平滚动条会自动出现
        layout.addWidget(text_edit, 1)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignCenter)

        # 居中显示
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        dialog.exec_()

    def exec_function_chat(self, parts):
        if len(parts) < 3:
            self.append_log("用法: /function Chat on|off")
            return
        state = parts[2].lower()
        if state not in ('on', 'off'):
            self.append_log("参数必须为 on 或 off")
            return
        config = load_server_config()
        config['chat_enabled'] = (state == 'on')
        save_server_config(config)
        # 广播给所有玩家
        self.broadcast_to_all(f"聊天功能已{'启用' if state == 'on' else '禁用'}")
        # 广播结构化状态（以便客户端即时响应）
        self.broadcast_structured({
            'type': 'chat_status',
            'data': {'enabled': (state == 'on')}
        })
        self.append_log(f"聊天功能已{state}")

    # ---------- 管理功能实现 ----------
    def exec_op(self, identifier):
        """控制台提升管理员（修改 uid 前缀和 permission）"""
        with server_lock:
            target = next((p for p in clients.values() if
                           p['name'] == identifier or p['userid'] == identifier or p['server_uid'] == identifier), None)
        if target:
            if target['permission'] == 'admin':
                self.append_log(f"{target['name']} 已经是管理员")
                return
            pdata = load_player_data(target['server_uid'])
            if not pdata:
                self.append_log(f"玩家 {identifier} 数据不存在")
                return
            old_uid = pdata.get('uid', '')
            if old_uid.startswith('user-'):
                new_uid = old_uid.replace('user-', 'admin-', 1)
            elif old_uid.startswith('admin-'):
                new_uid = old_uid
            else:
                new_uid = 'admin-' + old_uid
            pdata['uid'] = new_uid
            pdata['permission'] = 'admin'
            save_player_data(target['server_uid'], pdata)

            target['permission'] = 'admin'
            target['uid'] = new_uid

            TCPHandler.send_to(target['client'], {'type': 'permission_changed',
                                                  'data': {'permission': 'admin', 'server_uid': target['server_uid']}})
            self.broadcast_to_all(f"{identifier} 被提升为管理员")
            self.refresh_all_lists()
            self.append_log(f"已将 {identifier} 提升为管理员")
            return

        if not self.server:
            self.append_log("服务器未启动，无法离线操作")
            return
        server_uid, pdata = self.find_player_file_by_identifier(identifier)
        if server_uid is None:
            self.append_log(f"未找到玩家 {identifier} 的数据文件")
            return
        if pdata.get('permission') == 'admin':
            self.append_log(f"玩家 {identifier} 已经是管理员")
            return
        old_uid = pdata.get('uid', '')
        if old_uid.startswith('user-'):
            new_uid = old_uid.replace('user-', 'admin-', 1)
        elif old_uid.startswith('admin-'):
            new_uid = old_uid
        else:
            new_uid = 'admin-' + old_uid
        pdata['uid'] = new_uid
        pdata['permission'] = 'admin'
        save_player_data(server_uid, pdata)
        self.broadcast_to_all(f"{identifier} 被提升为管理员")
        self.append_log(f"已离线提升 {identifier} 为管理员")

    def exec_deop(self, identifier):
        """控制台取消管理员（修改 uid 前缀和 permission）"""
        with server_lock:
            target = next((p for p in clients.values() if
                           p['name'] == identifier or p['userid'] == identifier or p['server_uid'] == identifier), None)
        if target:
            if target['permission'] == 'user':
                self.append_log(f"{target['name']} 已经是普通用户")
                return
            pdata = load_player_data(target['server_uid'])
            if not pdata:
                self.append_log(f"玩家 {identifier} 数据不存在")
                return
            old_uid = pdata.get('uid', '')
            if old_uid.startswith('admin-'):
                new_uid = old_uid.replace('admin-', 'user-', 1)
            elif old_uid.startswith('user-'):
                new_uid = old_uid
            else:
                new_uid = 'user-' + old_uid
            pdata['uid'] = new_uid
            pdata['permission'] = 'user'
            save_player_data(target['server_uid'], pdata)

            target['permission'] = 'user'
            target['uid'] = new_uid

            TCPHandler.send_to(target['client'], {'type': 'permission_changed',
                                                  'data': {'permission': 'user', 'server_uid': target['server_uid']}})
            self.broadcast_to_all(f"{identifier} 被取消管理员资格")
            self.refresh_all_lists()
            self.append_log(f"已取消 {identifier} 的管理员资格")
            return

        if not self.server:
            self.append_log("服务器未启动，无法离线操作")
            return
        server_uid, pdata = self.find_player_file_by_identifier(identifier)
        if server_uid is None:
            self.append_log(f"未找到玩家 {identifier} 的数据文件")
            return
        if pdata.get('permission') != 'admin':
            self.append_log(f"玩家 {identifier} 不是管理员")
            return
        old_uid = pdata.get('uid', '')
        if old_uid.startswith('admin-'):
            new_uid = old_uid.replace('admin-', 'user-', 1)
        elif old_uid.startswith('user-'):
            new_uid = old_uid
        else:
            new_uid = 'user-' + old_uid
        pdata['uid'] = new_uid
        pdata['permission'] = 'user'
        save_player_data(server_uid, pdata)
        self.broadcast_to_all(f"{identifier} 被取消管理员资格")
        self.append_log(f"已离线取消 {identifier} 的管理员资格")

    def exec_ban(self, identifier, time_spec=None):
        delta, until_dt = parse_time_spec(time_spec) if time_spec else (None, None)
        if until_dt:
            banned_until = until_dt.isoformat()
            if until_dt <= datetime.now(SERVER_TIMEZONE):
                self.append_log(f"错误：指定的解封时间 {until_dt.strftime('%Y-%m-%d %H:%M:%S')} 已过期")
                return
        elif delta:
            banned_until = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
        else:
            banned_until = None

        with server_lock:
            target = next((p for p in clients.values() if
                           p['name'] == identifier or p['uid'] == identifier or p['server_uid'] == identifier), None)
        if target:
            server_uid = target['server_uid']
            pdata = load_player_data(server_uid)
            if pdata:
                pdata['banned'] = True
                pdata['banned_until'] = banned_until
                save_player_data(server_uid, pdata)
            try:
                reason = '你已被管理员封禁。'
                if banned_until:
                    until_dt2 = datetime.fromisoformat(banned_until)
                    reason += f' 解封时间：{until_dt2.strftime("UTC%z %Y-%m-%d %H:%M:%S")}'
                else:
                    reason += ' 永久封禁。'
                TCPHandler.send_to(target['client'], {'type': 'kick', 'data': {'reason': reason}})
                time.sleep(0.1)
                target['client'].close()
            except Exception as e:
                self.append_log(f"发送封禁消息失败: {e}")
            with server_lock:
                if target['client'] in clients:
                    del clients[target['client']]
                if server_uid in player_by_server_uid:
                    del player_by_server_uid[server_uid]
            self.append_log(f"已封禁在线玩家 {identifier}")
            self.broadcast_to_all(f"{identifier} 已被封禁")
            return

        if not self.server:
            self.append_log("服务器未启动，无法离线操作")
            return
        server_uid, pdata = self.find_player_file_by_identifier(identifier)
        if server_uid is None:
            self.append_log(f"未找到玩家 {identifier} 的数据文件")
            return
        pdata['banned'] = True
        pdata['banned_until'] = banned_until
        save_player_data(server_uid, pdata)
        self.append_log(f"已封禁离线玩家 {identifier}")
        self.broadcast_to_all(f"{identifier} 已被封禁")

    def exec_unban(self, identifier):
        if not self.server:
            self.append_log("服务器未启动，无法解封")
            return
        server_uid, pdata = self.find_player_file_by_identifier(identifier)
        if server_uid is None:
            self.append_log(f"未找到玩家 {identifier} 的数据文件")
            return
        if not pdata.get('banned'):
            self.append_log(f"玩家 {identifier} 未被封禁")
            return
        pdata['banned'] = False
        pdata['banned_until'] = None
        save_player_data(server_uid, pdata)
        self.append_log(f"已解封 {identifier}")
        with server_lock:
            target_player = player_by_server_uid.get(server_uid)
        if target_player:
            try:
                TCPHandler.send_to(target_player['client'], {'type': 'system_message', 'data': {'text': '你已被管理员解封，请重新登录以生效。'}})
                target_player['client'].close()
            except:
                pass
            with server_lock:
                if target_player['client'] in clients:
                    del clients[target_player['client']]
                if server_uid in player_by_server_uid:
                    del player_by_server_uid[server_uid]
            self.append_log(f"已将在线玩家 {identifier} 踢出，请重新登录")
        self.broadcast_to_all(f"{identifier} 已被解封")

    def exec_ban_area(self, area_code):
        if not self.server:
            self.append_log("服务器未启动，无法操作")
            return
        area_code = area_code.upper()
        banned_areas = load_banned_areas()
        if area_code in banned_areas:
            self.append_log(f"地区 {area_code} 已在封禁列表中")
            return
        banned_areas.append(area_code)
        save_banned_areas(banned_areas)
        self.append_log(f"已封禁地区 {area_code}")
        with server_lock:
            to_kick = [p for p in clients.values() if p.get('country') == area_code]
        for p in to_kick:
            try:
                TCPHandler.send_to(p['client'],
                                   {'type': 'kick', 'data': {'reason': f'你的地区/时区已被封禁，你将被踢出。'}})
                time.sleep(0.1)
                p['client'].close()
            except:
                pass
            with server_lock:
                if p['client'] in clients:
                    del clients[p['client']]
                if p['server_uid'] in player_by_server_uid:
                    del player_by_server_uid[p['server_uid']]
        self.broadcast_to_all(f"地区 {area_code} 已被封禁")

    def exec_unban_area(self, area_code):
        if not self.server:
            self.append_log("服务器未启动，无法操作")
            return
        area_code = area_code.upper()
        banned_areas = load_banned_areas()
        if area_code not in banned_areas:
            self.append_log(f"地区 {area_code} 不在封禁列表中")
            return
        banned_areas.remove(area_code)
        save_banned_areas(banned_areas)
        self.append_log(f"已解封地区 {area_code}")
        self.broadcast_to_all(f"地区 {area_code} 已被解封")

    def exec_ban_timezone(self, tzname):
        if not self.server:
            self.append_log("服务器未启动，无法操作")
            return
        try:
            pytz.timezone(tzname)
        except:
            self.append_log(f"无效的时区名称: {tzname}")
            return
        banned_tz = load_banned_timezones()
        if tzname in banned_tz:
            self.append_log(f"时区 {tzname} 已在封禁列表中")
            return
        banned_tz.append(tzname)
        save_banned_timezones(banned_tz)
        self.append_log(f"已封禁时区 {tzname}")
        with server_lock:
            to_kick = [p for p in clients.values() if p.get('timezone') == tzname]
        for p in to_kick:
            try:
                TCPHandler.send_to(p['client'],
                                   {'type': 'kick', 'data': {'reason': f'你的地区/时区已被封禁，你将被踢出。'}})
                time.sleep(0.1)
                p['client'].close()
            except:
                pass
            with server_lock:
                if p['client'] in clients:
                    del clients[p['client']]
                if p['server_uid'] in player_by_server_uid:
                    del player_by_server_uid[p['server_uid']]
        self.broadcast_to_all(f"时区 {tzname} 已被封禁")

    def exec_unban_timezone(self, tzname):
        if not self.server:
            self.append_log("服务器未启动，无法操作")
            return
        banned_tz = load_banned_timezones()
        if tzname not in banned_tz:
            self.append_log(f"时区 {tzname} 不在封禁列表中")
            return
        banned_tz.remove(tzname)
        save_banned_timezones(banned_tz)
        self.append_log(f"已解封时区 {tzname}")
        self.broadcast_to_all(f"时区 {tzname} 已被解封")

    def exec_maxplayers(self, parts):
        """控制台执行 /maxplayers <人数>"""
        if len(parts) != 2 or not parts[1].isdigit():
            self.append_log("用法: /maxplayers <人数>")
            return
        new_limit = int(parts[1])
        if new_limit < 1:
            self.append_log("人数必须 ≥ 1")
            return
        config = load_server_config()
        config['max_players'] = new_limit
        save_server_config(config)
        self.append_log(f"最大人数已设为 {new_limit}，新连接立即生效")

    def exec_access(self, parts):
        """执行访问控制命令：/access <whitelist|blacklist|none> <on|off>"""
        if len(parts) < 3 or parts[1] not in ('whitelist', 'blacklist', 'none'):
            self.append_log("用法: /access <whitelist|blacklist|none> <on|off>")
            return
        mode = parts[1]
        action = parts[2].lower()
        config = load_server_config()

        if mode == 'none':
            config['access_mode'] = 'none'
            save_server_config(config)
            self.append_log("访问控制已关闭（none模式）")
            return

        if action not in ('on', 'off'):
            self.append_log("动作必须是 on 或 off")
            return

        if action == 'on':
            config['access_mode'] = mode
            self.append_log(f"{mode} 模式已启用")
            kicked = []
            with server_lock:
                for client, player in list(clients.items()):
                    if player.get('permission') == 'admin':
                        continue
                    server_uid = player['server_uid']
                    uid = player.get('uid', '')
                    allow = False
                    if mode == 'whitelist':
                        wl = load_whitelist()
                        if server_uid in wl or uid in wl:
                            allow = True
                    elif mode == 'blacklist':
                        bl = load_blacklist()
                        if server_uid not in bl and uid not in bl:
                            allow = True
                    if not allow:
                        try:
                            TCPHandler.send_to(client, {'type': 'kick',
                                                        'data': {'reason': f'服务器已启用{mode}模式，你不在允许列表中'}})
                            time.sleep(0.1)  # 等待消息发送完成
                            client.close()
                        except:
                            pass
                        if client in clients:
                            del clients[client]
                        if server_uid in player_by_server_uid:
                            del player_by_server_uid[server_uid]
                        kicked.append(player['name'])
            if kicked:
                self.append_log(f"已踢出不符合条件的玩家: {', '.join(kicked)}")
            else:
                self.append_log("没有需要踢出的玩家")
        else:
            config['access_mode'] = 'none'
            self.append_log(f"{mode} 模式已关闭")
        save_server_config(config)

    def exec_whitelist(self, parts):
        """执行白名单管理命令：/whitelist on|off|add|remove|list [uuid]"""
        if len(parts) < 2:
            self.append_log("用法: /whitelist on|off|add|remove|list [uuid]")
            return
        subcmd = parts[1].lower()

        # 处理 on/off 开关（直接调用 exec_access 以复用踢出逻辑）
        if subcmd == 'on':
            self.exec_access(['/access', 'whitelist', 'on'])
            return
        elif subcmd == 'off':
            self.exec_access(['/access', 'whitelist', 'off'])
            return

        # 以下命令（add/remove/list）要求先开启白名单模式
        config = load_server_config()
        if config.get('access_mode') != 'whitelist':
            self.append_log("错误：白名单模式未开启，请先执行 /whitelist on")
            return

        wl = load_whitelist()
        if subcmd == 'list':
            if wl:
                lines = []
                for uid in wl:
                    name = self._get_player_name_by_uid(uid)
                    lines.append(f"{name} ({uid})")
                self.append_log("白名单列表:\n" + "\n".join(lines))
            else:
                self.append_log("白名单为空")
            return

        if len(parts) < 3:
            self.append_log("请提供玩家标识符（名字、userid 或 uid）")
            return
        identifier = parts[2]
        server_uid, pdata = self.find_player_file_by_identifier(identifier)
        if server_uid is None:
            with server_lock:
                for p in clients.values():
                    if p['name'] == identifier or p['userid'] == identifier or p['uid'] == identifier or p[
                        'server_uid'] == identifier:
                        server_uid = p['server_uid']
                        break
        if server_uid is None:
            self.append_log(f"未找到玩家 {identifier}")
            return

        if subcmd == 'add':
            if server_uid not in wl:
                wl.append(server_uid)
                save_whitelist(wl)
                self.append_log(f"已添加 {server_uid} 到白名单")
            else:
                self.append_log(f"{server_uid} 已在白名单中")
        elif subcmd == 'remove':
            if server_uid in wl:
                wl.remove(server_uid)
                save_whitelist(wl)
                self.append_log(f"已从白名单移除 {server_uid}")
            else:
                self.append_log(f"{server_uid} 不在白名单中")
        else:
            self.append_log("未知子命令，可用 on/off/add/remove/list")

    def exec_blacklist(self, parts):
        """执行黑名单管理命令：/blacklist on|off|add|remove|list [uuid]"""
        if len(parts) < 2:
            self.append_log("用法: /blacklist on|off|add|remove|list [uuid]")
            return
        subcmd = parts[1].lower()

        # 处理 on/off 开关（直接调用 exec_access）
        if subcmd == 'on':
            self.exec_access(['/access', 'blacklist', 'on'])
            return
        elif subcmd == 'off':
            self.exec_access(['/access', 'blacklist', 'off'])
            return

        # 以下命令（add/remove/list）要求先开启黑名单模式
        config = load_server_config()
        if config.get('access_mode') != 'blacklist':
            self.append_log("错误：黑名单模式未开启，请先执行 /blacklist on")
            return

        bl = load_blacklist()
        if subcmd == 'list':
            if bl:
                lines = []
                for uid in bl:
                    name = self._get_player_name_by_uid(uid)
                    lines.append(f"{name} ({uid})")
                self.append_log("黑名单列表:\n" + "\n".join(lines))
            else:
                self.append_log("黑名单为空")
            return

        if len(parts) < 3:
            self.append_log("请提供玩家标识符（名字、userid 或 uid）")
            return
        identifier = parts[2]
        server_uid, pdata = self.find_player_file_by_identifier(identifier)
        if server_uid is None:
            with server_lock:
                for p in clients.values():
                    if p['name'] == identifier or p['userid'] == identifier or p['uid'] == identifier or p[
                        'server_uid'] == identifier:
                        server_uid = p['server_uid']
                        break
        if server_uid is None:
            self.append_log(f"未找到玩家 {identifier}")
            return

        if subcmd == 'add':
            if server_uid not in bl:
                bl.append(server_uid)
                save_blacklist(bl)
                self.append_log(f"已添加 {server_uid} 到黑名单")
            else:
                self.append_log(f"{server_uid} 已在黑名单中")
        elif subcmd == 'remove':
            if server_uid in bl:
                bl.remove(server_uid)
                save_blacklist(bl)
                self.append_log(f"已从黑名单移除 {server_uid}")
            else:
                self.append_log(f"{server_uid} 不在黑名单中")
        else:
            self.append_log("未知子命令，可用 on/off/add/remove/list")

    def exec_give(self, cmd):
        """控制台执行 /give <用户> <物品id> [数量]（支持 jade 和 star_rail_tickets）"""
        parts = cmd.split()
        if len(parts) < 3:
            self.append_log("用法: /give <用户> <物品id> [数量] (数量默认为1，时装自动为1)")
            return
        user_spec = parts[1]
        item_id = parts[2].lower()

        # 别名映射（仅限已知时装）
        fashion_alias = {'ruyi': '26_Newyear'}
        if item_id in fashion_alias:
            item_id = fashion_alias[item_id]

        # ★★★ 物品合法性校验（扩展支持 jade 和 star_rail_tickets）★★★
        def is_valid_item(iid):
            if iid in ('cake', 'credit', 'ticket', 'star_rail_tickets', 'jade'):
                return True
            if iid in AVAILABLE_CLOTHES:
                return True
            # 自定义物品
            if hasattr(self, 'server') and self.server:
                custom_items = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
                return any(it.get('id') == iid for it in custom_items)
            return False

        if not is_valid_item(item_id):
            self.append_log(f"未知物品ID：{item_id}，请使用 cake/credit/ticket/star_rail_tickets/jade 或已注册的时装ID")
            return

        amount = 1
        if len(parts) >= 4:
            try:
                amount = int(parts[3])
            except:
                self.append_log("数量必须为整数")
                return
            if amount <= 0:
                self.append_log("数量必须大于0")
                return
        # 时装强制数量为1
        if item_id not in ('cake', 'credit', 'ticket', 'star_rail_tickets', 'jade'):
            amount = 1

        # 确定目标玩家列表（支持 @a，不支持 @s）
        target_players = []
        if user_spec == '@a':
            with server_lock:
                target_players = list(clients.values())
        else:
            # 在线玩家
            with server_lock:
                for p in clients.values():
                    if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                        target_players.append(p)
                        break
            # 如果没找到，尝试离线
            if not target_players:
                server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                if server_uid:
                    fake = {
                        'server_uid': server_uid,
                        'name': pdata.get('name', user_spec),
                        'userid': pdata.get('userid', ''),
                        'uid': pdata.get('uid', ''),
                        'client': None
                    }
                    target_players.append(fake)
            if not target_players:
                self.append_log(f"未找到用户 {user_spec}")
                return

        # 构造物品
        items = []
        if item_id == 'cake':
            items.append({'type': 'cake', 'amount': amount})
        elif item_id == 'credit':
            items.append({'type': 'credit', 'amount': amount})
        elif item_id == 'ticket' or item_id == 'star_rail_tickets':
            items.append({'type': 'star_rail_ticket', 'amount': amount})
        elif item_id == 'jade':
            items.append({'type': 'jade', 'amount': amount})
        else:
            # 时装或自定义
            if item_id in AVAILABLE_CLOTHES:
                items.append({'type': 'clothes', 'id': item_id})
            else:
                # 尝试自定义物品
                if hasattr(self, 'server') and self.server:
                    custom_items = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
                    item_info = next((it for it in custom_items if it.get('id') == item_id), None)
                    if item_info:
                        items.append(
                            {'type': 'custom', 'id': item_id, 'amount': amount, 'name': item_info.get('name', item_id)})
                    else:
                        self.append_log(f"未知物品: {item_id}")
                        return
                else:
                    self.append_log(f"未知物品: {item_id}")
                    return

        # 生成物品描述
        items_desc = []
        for item in items:
            if item['type'] == 'cake':
                items_desc.append(f"橡木蛋糕卷 x{item['amount']}")
            elif item['type'] == 'credit':
                items_desc.append(f"信用点 x{item['amount']}")
            elif item['type'] == 'star_rail_ticket':
                items_desc.append(f"星铁专票 x{item['amount']}")
            elif item['type'] == 'jade':
                items_desc.append(f"星琼 x{item['amount']}")
            elif item['type'] == 'clothes':
                cid = item['id']
                name = AVAILABLE_CLOTHES.get(cid, {}).get('name', cid)
                items_desc.append(f"时装 [{name}]")
            elif item['type'] == 'custom':
                items_desc.append(f"{item.get('name', item['id'])} x{item['amount']}")

        # 发送邮件给每个目标
        for p in target_players:
            mails = load_player_mails(p['server_uid'])
            mail = {
                "id": str(uuid.uuid4()),
                "title": "管理员赠送物品",
                "content": f"管理员 (控制台) 赠送了你以下物品：\n" + "\n".join(items_desc),
                "sender": "系统",
                "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                "read": False,
                "claimed": False,
                "items": items
            }
            mails.append(mail)
            save_player_mails(p['server_uid'], mails)
            # 如果在线，通知
            if p.get('client') and p['client'] in clients:
                try:
                    TCPHandler.send_to(p['client'], {'type': 'system_message',
                                                     'data': {'text': f'你收到了管理员赠送的物品，请查看邮箱。'}})
                except:
                    pass
        self.append_log(f"已向 {len(target_players)} 个玩家发送物品邮件")

    def exec_set(self, cmd):
        """控制台执行 /set <用户> <物品id> <数量>（支持时装和校验）"""
        parts = cmd.split()
        if len(parts) < 4:
            self.append_log("用法: /set <用户> <物品id> <数量>")
            return
        user_spec = parts[1]
        item_id = parts[2].lower()

        # 别名映射
        fashion_alias = {'ruyi': '26_Newyear'}
        if item_id in fashion_alias:
            item_id = fashion_alias[item_id]

        # ★★★ 物品合法性校验
        def is_valid_item(iid):
            if iid in ('cake', 'credit', 'ticket'):
                return True
            return iid in AVAILABLE_CLOTHES

        if not is_valid_item(item_id):
            self.append_log(f"未知物品ID：{item_id}")
            return

        try:
            amount = int(parts[3])
        except:
            self.append_log("数量必须为整数")
            return
        if amount < 0 or amount > 99999999:
            self.append_log("数量必须在 0-99999999 之间")
            return

        # 确定目标玩家（支持 @a，不支持 @s）
        target_players = []
        if user_spec == '@a':
            with server_lock:
                target_players = list(clients.values())
        else:
            with server_lock:
                for p in clients.values():
                    if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                        target_players.append(p)
                        break
            if not target_players:
                server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                if server_uid:
                    fake = {
                        'server_uid': server_uid,
                        'name': pdata.get('name', user_spec),
                        'userid': pdata.get('userid', ''),
                        'uid': pdata.get('uid', ''),
                        'client': None
                    }
                    target_players.append(fake)
            if not target_players:
                self.append_log(f"未找到用户 {user_spec}")
                return

        is_fashion = item_id not in ['cake', 'credit', 'ticket']
        for p in target_players:
            inv = load_player_inventory(p['server_uid'])
            if is_fashion:
                if amount == 0:
                    if item_id in inv.get('owned_clothes', []):
                        inv['owned_clothes'].remove(item_id)
                        self.append_log(f"已移除玩家 {p['name']} 的时装 {item_id}")
                elif amount == 1:
                    if item_id not in inv.get('owned_clothes', []):
                        inv['owned_clothes'].append(item_id)
                        self.append_log(f"已添加玩家 {p['name']} 的时装 {item_id}")
                else:
                    self.append_log(f"时装只能设置为 0(移除) 或 1(添加)，当前: {amount}")
                    return
            else:
                if item_id == 'cake':
                    inv['balance'] = amount
                elif item_id == 'credit':
                    inv['credit'] = amount
                elif item_id == 'ticket':
                    inv['star_rail_tickets'] = amount
            save_player_inventory(p['server_uid'], inv)
            if p.get('client') and p['client'] in clients:
                p['inventory'] = inv
                try:
                    TCPHandler.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                    if is_fashion:
                        TCPHandler.send_to(p['client'], {'type': 'system_message',
                                                         'data': {
                                                             'text': f'管理员已{"添加" if amount == 1 else "移除"}你的时装 [{item_id}]'}})
                    else:
                        TCPHandler.send_to(p['client'], {'type': 'system_message',
                                                         'data': {'text': f'管理员已设置你的 {item_id} 为 {amount}'}})
                except:
                    pass
        self.append_log(f"已处理 {len(target_players)} 个玩家的 {item_id}")

    def exec_clear(self, cmd):
        """控制台执行 /clear <用户> <物品或*>"""
        parts = cmd.split()
        if len(parts) < 3:
            self.append_log("用法: /clear <用户> <物品或*>")
            return
        user_spec = parts[1]
        item_spec = parts[2].lower()

        # 不支持 @a，只支持具体玩家名或ID（控制台不需要 @s）
        target_players = []
        with server_lock:
            for p in clients.values():
                if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                    target_players.append(p)
                    break
        if not target_players:
            server_uid, pdata = self.find_player_file_by_identifier(user_spec)
            if server_uid:
                fake = {
                    'server_uid': server_uid,
                    'name': pdata.get('name', user_spec),
                    'userid': pdata.get('userid', ''),
                    'uid': pdata.get('uid', ''),
                    'client': None
                }
                target_players.append(fake)
        if not target_players:
            self.append_log(f"未找到用户 {user_spec}")
            return

        for p in target_players:
            inv = load_player_inventory(p['server_uid'])
            if item_spec == '*':
                inv['balance'] = 0
                inv['credit'] = 0
            elif item_spec == 'cake':
                inv['balance'] = 0
            elif item_spec == 'credit':
                inv['credit'] = 0
            else:
                self.append_log(f"未知物品: {item_spec}")
                return
            save_player_inventory(p['server_uid'], inv)
            if p.get('client') and p['client'] in clients:
                p['inventory'] = inv
                try:
                    TCPHandler.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                    TCPHandler.send_to(p['client'], {'type': 'system_message',
                                                     'data': {'text': f'管理员已清空你的 {item_spec}'}})
                except:
                    pass
        self.append_log(f"已清空 {item_spec}，影响 {len(target_players)} 个玩家")

    def exec_revoke(self, cmd):
        """控制台执行 /revoke <用户> <物品:数量&物品...>（扣除物品）"""
        parts = cmd.split()
        if len(parts) < 3:
            self.append_log("用法: /revoke <用户> <物品:数量&物品...>")
            return
        user_spec = parts[1]
        items_spec = ' '.join(parts[2:])

        # 解析物品列表
        items = []
        for part in items_spec.split('&'):
            part = part.strip()
            if not part:
                continue
            if ':' in part:
                itype, qty = part.split(':', 1)
                try:
                    qty = int(qty)
                except:
                    self.append_log(f"数量错误: {part}")
                    return
            else:
                itype = part
                qty = 1
            if itype not in ('cake', 'credit', 'ticket') and itype not in AVAILABLE_CLOTHES:
                self.append_log(f"未知物品: {itype}")
                return
            items.append({'type': itype, 'amount': qty})

        if not items:
            self.append_log("未指定有效物品")
            return

        # 获取目标玩家（控制台不支持 @s，但支持 @a 和具体用户）
        target_players = []
        if user_spec == '@a':
            with server_lock:
                target_players = list(clients.values())
        elif user_spec == '@s':
            self.append_log("控制台不支持 @s，请使用具体用户名或 @a")
            return
        else:
            # 在线玩家
            found = False
            with server_lock:
                for p in clients.values():
                    if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                        target_players.append(p)
                        found = True
                        break
            if not found:
                # 离线查找
                server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                if server_uid:
                    fake = {
                        'server_uid': server_uid,
                        'name': pdata.get('name', user_spec),
                        'userid': pdata.get('userid', ''),
                        'uid': pdata.get('uid', ''),
                        'client': None
                    }
                    target_players.append(fake)
            if not target_players:
                self.append_log(f"未找到用户 {user_spec}")
                return

        # 执行扣除并发送邮件（逻辑同服务端 handle_command 中的 /revoke）
        for p in target_players:
            inv = load_player_inventory(p['server_uid'])
            items_desc = []
            for item in items:
                itype = item['type']
                amount = item['amount']
                if itype == 'cake':
                    inv['balance'] = inv.get('balance', 0) - amount
                    items_desc.append(f"橡木蛋糕卷 x{amount}")
                elif itype == 'credit':
                    inv['credit'] = inv.get('credit', 0) - amount
                    if inv['credit'] < -99999999:
                        inv['credit'] = -99999999
                    items_desc.append(f"信用点 x{amount}")
                elif itype == 'ticket':
                    inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) - amount
                    items_desc.append(f"星铁专票 x{amount}")
                else:  # 时装
                    if amount > 0:
                        owned = inv.get('owned_clothes', ['normal'])
                        if itype in owned:
                            owned.remove(itype)
                            inv['owned_clothes'] = owned
                            clothes_name = AVAILABLE_CLOTHES.get(itype, {}).get('name', itype)
                            items_desc.append(f"时装 [{clothes_name}]")
            save_player_inventory(p['server_uid'], inv)

            # 发送邮件通知
            if items_desc:
                mail_content = f"管理员已从您的账户中扣除以下物品：\n" + "\n".join(items_desc)
            else:
                mail_content = "管理员执行了物品扣除操作，但未影响您的任何物品。"
            notif_mail = {
                "id": str(uuid.uuid4()),
                "title": "物品扣除通知",
                "content": mail_content,
                "sender": "系统",
                "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                "read": False,
                "claimed": False,
                "items": [],
                "code": None
            }
            mails = load_player_mails(p['server_uid'])
            mails.append(notif_mail)
            save_player_mails(p['server_uid'], mails)

            # 在线推送
            if p.get('client') and p['client'] in clients:
                p['inventory'] = inv
                try:
                    TCPHandler.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                    TCPHandler.send_to(p['client'], {'type': 'system_message',
                                                     'data': {'text': f'管理员已从你身上扣除物品，请查看邮箱获取详情。'}})
                except:
                    pass

        self.append_log(f"已对 {len(target_players)} 个玩家执行物品扣除，并发送了通知邮件")

    def exec_revoke_code(self, cmd):
        """控制台执行 /revoke code <兑换码> <用户>（完整撤回逻辑）"""
        parts = cmd.split()
        if len(parts) < 4:
            self.append_log("用法: /revoke code <兑换码> <用户>")
            return
        code = parts[2]
        user_spec = parts[3]

        codes = load_server_codes()
        if code not in codes:
            self.append_log(f"兑换码 {code} 不存在")
            return
        cinfo = codes[code]
        if cinfo.get('revoked'):
            self.append_log("该兑换码已被撤销")
            return

        # 获取目标用户（支持 @a，不支持 @s，但控制台可酌情处理）
        target_list = []
        if user_spec == '@a':
            with server_lock:
                target_list = list(clients.values())
        elif user_spec == '@s':
            # 控制台没有当前玩家，因此不支持 @s，提示错误
            self.append_log("控制台不支持 @s，请指定具体用户名或 @a")
            return
        else:
            with server_lock:
                for p in clients.values():
                    if p['name'] == user_spec or p['userid'] == user_spec or p['server_uid'] == user_spec:
                        target_list.append(p)
                        break
            if not target_list:
                server_uid, pdata = self.find_player_file_by_identifier(user_spec)
                if server_uid:
                    fake = {
                        'server_uid': server_uid,
                        'name': pdata.get('name', user_spec),
                        'userid': pdata.get('userid', ''),
                        'uid': pdata.get('uid', ''),
                        'client': None
                    }
                    target_list.append(fake)
            if not target_list:
                self.append_log(f"未找到用户 {user_spec}")
                return

        revoked_count = 0

        for p in target_list:
            suid = p['server_uid']
            if suid in cinfo.get('revoked_users', []):
                continue

            pdata = load_player_data(suid)
            if not pdata:
                continue

            inv = load_player_inventory(suid)
            mails = load_player_mails(suid)

            # ---- 1. 超宽松匹配邮件 ----
            mails_to_remove = []
            self.append_log(f"[撤销] 处理用户 {suid}，共 {len(mails)} 封邮件")
            for idx, mail in enumerate(mails):
                title = mail.get('title', '')
                content = mail.get('content', '')
                mail_code = mail.get('code', '')
                self.append_log(f"[撤销] 邮件 {idx}: 标题='{title}', code字段='{mail_code}'")
                # 条件1：code字段直接匹配
                if mail_code == code:
                    self.append_log(f"[撤销] 条件1命中（code字段）")
                    mails_to_remove.append(mail)
                    continue
                # 条件2：标题中包含兑换码字符串
                if code in title:
                    self.append_log(f"[撤销] 条件2命中（标题包含）")
                    mails_to_remove.append(mail)
                    continue
                # 条件3：标题以"兑换码兑换成功"开头且内容中包含兑换码
                if title.startswith('兑换码兑换成功') and code in content:
                    self.append_log(f"[撤销] 条件3命中（内容包含）")
                    mails_to_remove.append(mail)
                    continue
                # 条件4：正则提取括号内字符串匹配
                import re
                match = re.search(r'[（(](\w+)[）)]', title)
                if match and match.group(1) == code:
                    self.append_log(f"[撤销] 条件4命中（括号提取）")
                    mails_to_remove.append(mail)
                    continue

            # ---- 2. 如果仍然没找到邮件，但 redeemed_codes 中有该码，强制删除所有标题含"兑换码兑换成功"的邮件 ----
            if not mails_to_remove and code in pdata.get('redeemed_codes', []):
                self.append_log(f"[撤销] 未匹配到邮件，但用户已使用该码，强制删除所有兑换码成功邮件")
                for mail in mails:
                    if '兑换码兑换成功' in mail.get('title', ''):
                        mails_to_remove.append(mail)
                        self.append_log(f"[撤销] 强制删除邮件: {mail.get('title')}")

            # ---- 3. 处理找到的邮件 ----
            if mails_to_remove:
                self.append_log(f"[撤销] 共找到 {len(mails_to_remove)} 封相关邮件")
                claimed_mails = [m for m in mails_to_remove if m.get('claimed', False)]
                unclaimed_mails = [m for m in mails_to_remove if not m.get('claimed', False)]

                # 处理已领取邮件：扣除库存
                for mail in claimed_mails:
                    for item in mail.get('items', []):
                        itype = item.get('type')
                        if itype == 'cake':
                            inv['balance'] = max(0, inv.get('balance', 0) - item.get('amount', 0))
                        elif itype == 'credit':
                            inv['credit'] = max(0, inv.get('credit', 0) - item.get('amount', 0))
                        elif itype == 'clothes':
                            cid = item.get('id')
                            if cid and cid in inv.get('owned_clothes', []):
                                inv['owned_clothes'].remove(cid)
                        elif itype == 'star_rail_ticket':
                            inv['star_rail_tickets'] = max(0, inv.get('star_rail_tickets', 0) - item.get('amount', 0))

                    stats = pdata.setdefault('redeem_stats', {})
                    for item in mail.get('items', []):
                        itype = item.get('type')
                        if itype == 'clothes':
                            cid = item.get('id')
                            if cid:
                                stats[cid] = stats.get(cid, 0) - 1
                        else:
                            amount = item.get('amount', 1)
                            stats[itype] = stats.get(itype, 0) - amount
                    for item in mail.get('items', []):
                        itype = item.get('type')
                        if itype == 'credit':
                            pdata['monthly_credit_gained'] = pdata.get('monthly_credit_gained', 0) - item.get('amount',
                                                                                                              0)
                        elif itype == 'cake':
                            pdata['monthly_cake_gained'] = pdata.get('monthly_cake_gained', 0) - item.get('amount', 0)

                # 删除所有找到的邮件
                for mail in mails_to_remove:
                    mails.remove(mail)
                    self.append_log(f"[撤销] 已删除邮件: {mail.get('title')}")

                save_player_inventory(suid, inv)
                save_player_mails(suid, mails)

                # 从 redeemed_codes 移除
                if code in pdata.get('redeemed_codes', []):
                    pdata['redeemed_codes'] = [c for c in pdata['redeemed_codes'] if c != code]
                    self.append_log(f"[撤销] 从 redeemed_codes 移除 {code}")
                save_player_data(suid, pdata)

                cinfo.setdefault('revoked_users', []).append(suid)
                revoked_count += 1

                # 添加撤回通知邮件
                if claimed_mails:
                    content_detail = "已领取的邮件已被撤回，相关物品已从您的库存中扣除。"
                else:
                    content_detail = "未领取的邮件已被撤回，无物品扣除。"
                if unclaimed_mails and claimed_mails:
                    content_detail = "部分邮件已领取（已扣除物品），部分未领取（直接撤回）。"
                notif_mail = {
                    "id": str(uuid.uuid4()),
                    "title": f"兑换码撤回通知（{code}）",
                    "content": f"管理员已撤销兑换码 {code}。{content_detail}",
                    "sender": "系统",
                    "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                    "read": False,
                    "claimed": False,
                    "items": [],
                    "code": code
                }
                mails.append(notif_mail)
                save_player_mails(suid, mails)
                self.append_log(f"[撤销] 已添加撤回通知邮件")

                # 如果玩家在线，推送更新
                if p.get('client') and p['client'] in clients:
                    p['inventory'] = inv
                    try:
                        TCPHandler.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                        TCPHandler.send_to(p['client'], {
                            'type': 'system_message',
                            'data': {'text': f'兑换码 {code} 已被管理员撤销，请查看邮箱获取详情。'}
                        })
                        stats_data = {
                            'monthly_credit_gained': pdata.get('monthly_credit_gained', 0),
                            'monthly_cake_gained': pdata.get('monthly_cake_gained', 0),
                        }
                        TCPHandler.send_to(p['client'], {'type': 'stats_data', 'data': {'stats': stats_data}})
                    except:
                        pass

            else:
                # ---- 4. 完全没有匹配到邮件，但 redeemed_codes 中有该码，按已领取处理（扣除库存） ----
                if code in pdata.get('redeemed_codes', []):
                    self.append_log(f"[撤销] 未找到任何相关邮件，但用户已使用，直接扣除库存（视为已领取）")
                    for item in cinfo.get('items', []):
                        itype = item.get('type')
                        if itype == 'cake':
                            inv['balance'] = max(0, inv.get('balance', 0) - item.get('amount', 0))
                        elif itype == 'credit':
                            inv['credit'] = max(0, inv.get('credit', 0) - item.get('amount', 0))
                        elif itype == 'clothes':
                            cid = item.get('id')
                            if cid and cid in inv.get('owned_clothes', []):
                                inv['owned_clothes'].remove(cid)
                        elif itype == 'star_rail_ticket':
                            inv['star_rail_tickets'] = max(0, inv.get('star_rail_tickets', 0) - item.get('amount', 0))
                    stats = pdata.setdefault('redeem_stats', {})
                    for item in cinfo.get('items', []):
                        itype = item.get('type')
                        if itype == 'clothes':
                            cid = item.get('id')
                            if cid:
                                stats[cid] = stats.get(cid, 0) - 1
                        else:
                            amount = item.get('amount', 1)
                            stats[itype] = stats.get(itype, 0) - amount
                    for item in cinfo.get('items', []):
                        itype = item.get('type')
                        if itype == 'credit':
                            pdata['monthly_credit_gained'] = pdata.get('monthly_credit_gained', 0) - item.get('amount',
                                                                                                              0)
                        elif itype == 'cake':
                            pdata['monthly_cake_gained'] = pdata.get('monthly_cake_gained', 0) - item.get('amount', 0)
                    pdata['redeemed_codes'] = [c for c in pdata.get('redeemed_codes', []) if c != code]
                    save_player_inventory(suid, inv)
                    save_player_data(suid, pdata)

                    cinfo.setdefault('revoked_users', []).append(suid)
                    revoked_count += 1

                    notif_mail = {
                        "id": str(uuid.uuid4()),
                        "title": f"兑换码撤回通知（{code}）",
                        "content": f"管理员已撤销兑换码 {code}。由于未找到相关邮件，已直接扣除您库存中的对应物品（如有）。",
                        "sender": "系统",
                        "timestamp": datetime.now(SERVER_TIMEZONE).isoformat(),
                        "read": False,
                        "claimed": False,
                        "items": [],
                        "code": code
                    }
                    mails.append(notif_mail)
                    save_player_mails(suid, mails)

                    if p.get('client') and p['client'] in clients:
                        p['inventory'] = inv
                        try:
                            TCPHandler.send_to(p['client'], {'type': 'inventory_update', 'data': {'inventory': inv}})
                            TCPHandler.send_to(p['client'], {
                                'type': 'system_message',
                                'data': {'text': f'兑换码 {code} 已被撤销，相关物品已扣除。'}
                            })
                        except:
                            pass

        save_server_codes(codes)
        self.append_log(f"已撤销 {revoked_count} 个用户对兑换码 {code} 的使用资格。")

    def exec_set_newcode(self, cmd):
        """控制台执行 /set newcode <兑换码> <物品列表>（含物品校验）"""
        parts = cmd.split()
        if len(parts) < 4:
            self.append_log("用法: /set newcode <兑换码> <物品:数量&物品:数量...>")
            return
        code = parts[2]
        items_spec = ' '.join(parts[3:])

        # ★★★ 解析并校验每个物品
        items = []
        for part in items_spec.split('&'):
            part = part.strip()
            if not part:
                continue
            if ':' in part:
                itype, qty = part.split(':', 1)
                try:
                    qty = int(qty)
                except:
                    self.append_log(f"数量错误: {part}")
                    return
                if qty < 1 or qty > 9999:
                    self.append_log(f"数量必须在1-9999: {part}")
                    return
            else:
                itype = part
                qty = 1
            # 物品类型校验
            if itype == 'cake':
                items.append({'type': 'cake', 'amount': qty})
            elif itype == 'credit':
                items.append({'type': 'credit', 'amount': qty})
            elif itype == 'ticket':
                items.append({'type': 'star_rail_ticket', 'amount': qty})
            else:
                if itype not in AVAILABLE_CLOTHES:
                    self.append_log(f"无效的时装ID: {itype}")
                    return
                items.append({'type': 'clothes', 'id': itype})
        if not items:
            self.append_log("未指定有效物品")
            return

        codes = load_server_codes()
        if code in codes:
            self.append_log(f"兑换码 {code} 已存在")
            return
        codes[code] = {
            "max_uses": 0,
            "used_count": 0,
            "used_by": [],
            "created_by": "控制台",
            "created_time": datetime.now(SERVER_TIMEZONE).isoformat(),
            "items": items,
            "expire_time": None,
            "revoked": False,
            "revoked_users": [],
            "revoked_by": None,
            "revoked_time": None
        }
        save_server_codes(codes)
        self.append_log(f"兑换码 {code} 创建成功")

    def exec_mute(self, parts):
        # 检查是否为全体禁言
        if len(parts) == 2 and parts[1].lower() == 'common':
            if not self.server:
                self.append_log("服务器未启动，无法操作")
                return
            config = load_server_config()
            config['common_mute'] = True
            save_server_config(config)
            # 广播给所有玩家
            self.broadcast_structured({
                'type': 'common_mute_status',
                'data': {'enabled': True}
            })
            self.broadcast_to_all("管理员已开启全体禁言")
            self.append_log("全体禁言已开启")
            return

        if len(parts) < 3:
            self.append_log("用法: /mute <用户> <时长>")
            return

        target = parts[1]
        duration = parts[2]
        delta, until_dt = parse_time_spec(duration)
        if until_dt:
            muted_until = until_dt.isoformat()
            if until_dt <= datetime.now(SERVER_TIMEZONE):
                self.append_log("指定的解禁时间已过期")
                return
        elif delta:
            muted_until = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
        else:
            self.append_log("无效的时间格式")
            return

        # 查找在线玩家
        target_player = None
        with server_lock:
            for p in clients.values():
                if p['name'] == target or p['userid'] == target or p['server_uid'] == target:
                    target_player = p
                    break
        if target_player:
            if target_player['permission'] == 'admin':
                self.append_log("不能禁言管理员")
                return
            pdata = load_player_data(target_player['server_uid'])
            if pdata:
                pdata['muted_until'] = muted_until
                save_player_data(target_player['server_uid'], pdata)
            try:
                TCPHandler.send_to(target_player['client'], {
                    'type': 'mute_status',
                    'data': {'muted_until': muted_until}
                })
            except:
                pass
            # ---------- 修改点：广播时间带 UTC ----------
            if until_dt:
                time_str = until_dt.strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
            else:
                time_str = (datetime.now(SERVER_TIMEZONE) + delta).strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
            self.broadcast_to_all(f"{target_player['name']} 已被禁言，解禁时间：{time_str}")
            self.append_log(f"已禁言 {target}")
        else:
            # 离线禁言
            server_uid, pdata = self.find_player_file_by_identifier(target)
            if server_uid is None:
                self.append_log(f"未找到玩家 {target}")
                return
            if pdata.get('permission') == 'admin':
                self.append_log("不能禁言管理员")
                return
            pdata['muted_until'] = muted_until
            save_player_data(server_uid, pdata)
            # ---------- 修改点：离线广播也带 UTC ----------
            if until_dt:
                time_str = until_dt.strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
            else:
                time_str = (datetime.now(SERVER_TIMEZONE) + delta).strftime('%Y-%m-%d %H:%M:%S (UTC%z)')
            self.broadcast_to_all(f"{target} 已被禁言（离线），解禁时间：{time_str}")
            self.append_log(f"已禁言离线玩家 {target}")

    def exec_unmute(self, parts):
        if len(parts) == 2 and parts[1].lower() == 'common':
            if not self.server:
                self.append_log("服务器未启动，无法操作")
                return
            config = load_server_config()
            config['common_mute'] = False
            save_server_config(config)
            self.broadcast_structured({
                'type': 'common_mute_status',
                'data': {'enabled': False}
            })
            self.broadcast_to_all("管理员已解除全体禁言")
            self.append_log("全体禁言已解除")
            return

        if len(parts) < 2:
            self.append_log("用法: /unmute <用户>")
            return
        target = parts[1]

        target_player = None
        with server_lock:
            for p in clients.values():
                if p['name'] == target or p['userid'] == target or p['server_uid'] == target:
                    target_player = p
                    break
        if target_player:
            pdata = load_player_data(target_player['server_uid'])
            if pdata:
                pdata['muted_until'] = None
                save_player_data(target_player['server_uid'], pdata)
            try:
                TCPHandler.send_to(target_player['client'], {
                    'type': 'mute_status',
                    'data': {'muted_until': None}
                })
            except:
                pass
            self.broadcast_to_all(f"{target_player['name']} 已被解除禁言")
            self.append_log(f"已解除 {target} 的禁言")
        else:
            # 离线解禁
            server_uid, pdata = self.find_player_file_by_identifier(target)
            if server_uid is None:
                self.append_log(f"未找到玩家 {target}")
                return
            pdata['muted_until'] = None
            save_player_data(server_uid, pdata)
            self.broadcast_to_all(f"{target} 已被解除禁言（离线）")
            self.append_log(f"已解除离线玩家 {target} 的禁言")

    def exec_function_ai(self, parts):
        if len(parts) < 3:
            self.append_log("用法: /function AI on|off")
            return
        state = parts[2].lower()
        if state not in ('on', 'off'):
            self.append_log("参数必须为 on 或 off")
            return
        config = load_server_config()
        config['ai_enabled'] = (state == 'on')
        save_server_config(config)
        self.broadcast_to_all(f"AI 功能已{'启用' if state == 'on' else '禁用'}")
        # ★ 新增：广播 ai_status 结构体
        self.broadcast_structured({
            'type': 'ai_status',
            'data': {'enabled': (state == 'on')}
        })
        self.append_log(f"AI 功能已{state}")

    def broadcast_to_all(self, text):
        with server_lock:
            clients_copy = list(clients.keys())
        for client in clients_copy:
            try:
                TCPHandler.send_to(client, {'type': 'chat_broadcast', 'data': {'from_name': '系统', 'text': text, 'is_system': True}})
            except: pass

    def refresh_all_lists(self):
        with server_lock:
            players = list(clients.values())
        for p in players:
            lst = []
            for q in players:
                entry = {'name': q['name'], 'id': q['userid']}
                if p['permission'] == 'admin':
                    entry['uid'] = q['server_uid']
                lst.append(entry)
            try:
                TCPHandler.send_to(p['client'], {'type': 'player_list', 'data': {'players': lst}})
            except: pass

    def find_player_file_by_identifier(self, identifier):
        if not os.path.isdir(PLAYERS_DIR):
            return None, None
        identifier = str(identifier).strip()
        for filename in os.listdir(PLAYERS_DIR):
            if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                continue
            filepath = os.path.join(PLAYERS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except:
                continue
            server_uid = filename[:-5]
            if (server_uid == identifier or
                data.get('name') == identifier or
                data.get('userid') == identifier or
                data.get('uid') == identifier):
                return server_uid, data
        return None, None

    def check_update(self):
        url = "https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online-Server/releases"
        webbrowser.open(url)
        self.append_log(f"已打开浏览器: {url}")

    def load_json(self, filename):
        """加载 server_data 目录下的 JSON 文件，返回列表或字典"""
        path = os.path.join(SERVER_DATA, filename)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return []
        return []

    def broadcast_custom_config(self):
        """广播自定义配置给所有在线玩家（热更新）"""
        global server_lock, clients  # ← 必须声明全局变量
        if not self.server:
            return
        menu = self.load_json('custom_menu.json')
        items = self.load_json('custom_items.json')
        msg = {
            'type': 'update_custom_config',
            'data': {
                'custom_menu': menu,
                'custom_items': items
            }
        }
        with server_lock:
            for client in list(clients.keys()):
                try:
                    TCPHandler.send_to(client, msg)
                except:
                    pass
        self.append_log(f"[热更新] 已广播配置给 {len(clients)} 个在线玩家")

    def closeEvent(self, event):
        self.stop_server()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    font_path = os.path.join(SERVER_DIR, "font", "HarmonyOS_Sans_SC_Bold.ttf")
    if os.path.exists(font_path):
        font_id = QFontDatabase.addApplicationFont(font_path)
        if font_id != -1:
            font_families = QFontDatabase.applicationFontFamilies(font_id)
            if font_families:
                default_font = QFont(font_families[0], 10)
                app.setFont(default_font)
    window = ServerWindow()
    window.show()
    sys.exit(app.exec_())
