# server_gui.py
import hashlib
import gc
import os
import random
import re
import shutil
import sys
import json
import threading
import socketserver
import time
import traceback
import uuid
import webbrowser
from datetime import datetime, timedelta
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings, QWebEngineProfile
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
PM_HISTORY_DIR = os.path.join(SERVER_DATA, 'pm_history')  # 私信历史（每对玩家一个文件）
PM_HISTORY_LIMIT = 1000  # 每对玩家保留的私信条数上限
PM_OFFLINE_DIR = os.path.join(SERVER_DATA, 'pm_offline')  # 离线私信收件箱（每人一个文件）
PM_OFFLINE_LIMIT = 500  # 每人离线私信条数上限
SERVER_CODES_FILE = os.path.join(SERVER_DATA, 'redeem_codes.json')
BANNED_AREAS_FILE = os.path.join(SERVER_DATA, 'banned_areas.json')
BANNED_TIMEZONES_FILE = os.path.join(SERVER_DATA, 'banned_timezones.json')
SERVER_CONFIG_FILE = os.path.join(SERVER_DATA, 'server_config.json')
# 添加自定义配置路径
CUSTOM_MENU_FILE = os.path.join(SERVER_DATA, 'custom_menu.json')
SHOP_CONFIG_FILE = os.path.join(SERVER_DATA, 'shop_config.json')
CUSTOM_ITEMS_FILE = os.path.join(SERVER_DATA, 'custom_items.json')
WEB_RESOURCES_DIR = os.path.join(SERVER_DATA, 'web_resources')  # 存放抽卡HTML等
os.makedirs(WEB_RESOURCES_DIR, exist_ok=True)

WHITELIST_FILE = os.path.join(SERVER_DATA, 'whitelist.json')
BLACKLIST_FILE = os.path.join(SERVER_DATA, 'blacklist.json')

def _ensure_data_dirs():
    """确保所有数据目录存在（首次运行 / 用户清空 server_data 后也要能正常收发）。

    此前启动只建了 web_resources：全新目录下登录会因 players 目录不存在抛
    FileNotFoundError(WinError 3)，表现为「[未处理异常] ... server_data\\players」。
    """
    for _d in (SERVER_DATA, PLAYERS_DIR, PM_HISTORY_DIR, PM_OFFLINE_DIR, WEB_RESOURCES_DIR,
               os.path.join(SERVER_DATA, 'webroot'),
               os.path.join(SERVER_DATA, 'webroot', 'resources'),
               os.path.join(SERVER_DATA, 'webroot', 'gacha_images'),
               os.path.join(SERVER_DATA, 'webroot', 'custom_items')):
        try:
            os.makedirs(_d, exist_ok=True)
        except Exception:
            pass


def _player_files():
    """列出 players 下的玩家档案文件名（目录不存在时返回空表，绝不抛 WinError 3）"""
    try:
        return [fn for fn in os.listdir(PLAYERS_DIR)
                if fn.endswith('.json') and not fn.endswith('_inv.json')
                and not fn.endswith('_mails.json')]
    except OSError:
        return []


_ensure_data_dirs()

HOST = '0.0.0.0'
PORT = 20082
SERVER_VERSION = "4.7.0"

# 全局版本放行开关（默认关闭）
ALLOW_ALL_VERSIONS = False

player_file_lock = threading.Lock()
pm_history_lock = threading.Lock()  # 私信历史文件并发保护
pm_offline_lock = threading.Lock()  # 离线收件箱并发保护

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

def fit_4_3(w, h):
    """返回不小于 (w, h) 的最近 4:3 尺寸。

    只放大不缩小（保证原内容不被挤压）：先按高度算宽度，不够宽就按宽度反推高度。
    菜单、登录/注册窗口、桌宠本体窗口不做处理。
    """
    try:
        w, h = int(w), int(h)
    except Exception:
        return w, h
    W = max(w, int(round(h * 4.0 / 3.0)))
    H = int(round(W * 3.0 / 4.0))
    if H < h:
        H = h
        W = int(round(H * 4.0 / 3.0))
    return W, H

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
# ==================== 资源包加密与完整性（防篡改 / 防抓包复用） ====================
PACK_MAGIC = b'FLYENC1'                 # 加密资源包的文件头
PACK_KEY_SALT = b'FireflyPack-v1'       # 密钥盐（与服务器私密 pack_secret 一起派生）


def pack_key(menu_id, secret=b''):
    """资源包密钥（32 字节）= sha256(盐 + 服务器私密 secret + 菜单 id)。

    服务器私密 secret 存在 server_config.json 且只在登录时下发 —— 别人即便从
    /resources/xxx.zip 抓到文件，也只是乱码，拿不到密钥。
    """
    if isinstance(secret, str):
        secret = secret.encode('utf-8')
    return hashlib.sha256(PACK_KEY_SALT + b'|' + (secret or b'') + b'|' +
                          str(menu_id or '').encode('utf-8')).digest()


def pack_encrypt(data, key):
    """加密：8 字节随机 nonce + HMAC-SHA256 计数器密钥流异或（同样的 key 才能还原）"""
    if data is None:
        return b''
    nonce = os.urandom(8)
    n = len(data)
    if n == 0:
        return PACK_MAGIC + nonce
    blocks = (n + 31) // 32
    ks = b''.join(hashlib.sha256(key + nonce + i.to_bytes(8, 'big')).digest()
                  for i in range(blocks))[:n]
    body = (int.from_bytes(data, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(n, 'big')
    return PACK_MAGIC + nonce + body


def pack_decrypt(blob, key):
    """解密；不是本程序加密的（旧服务器/普通 zip）原样返回，保持兼容"""
    if not blob or not blob.startswith(PACK_MAGIC):
        return blob
    nonce = blob[len(PACK_MAGIC):len(PACK_MAGIC) + 8]
    data = blob[len(PACK_MAGIC) + 8:]
    n = len(data)
    if n == 0:
        return b''
    blocks = (n + 31) // 32
    ks = b''.join(hashlib.sha256(key + nonce + i.to_bytes(8, 'big')).digest()
                  for i in range(blocks))[:n]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(n, 'big')


def pack_manifest(zip_path):
    """资源包内每个文件的 sha256 清单（客户端据此校验本地解压结果）"""
    man = {}
    try:
        import zipfile as _zip
        with _zip.ZipFile(zip_path, 'r') as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = str(info.filename).replace(chr(92), '/').lstrip('/')
                if not name or '..' in name.split('/'):
                    continue
                man[name] = hashlib.sha256(z.read(info)).hexdigest()
    except Exception as e:
        print(f"[资源包] 生成清单失败 {zip_path}: {e}")
        return {}
    return man


def pack_manifest_sig(files, key):
    """给清单签名（HMAC），客户端先验签再校验文件 —— 中间人改包会被发现"""
    import hmac as _hmac
    payload = json.dumps(files, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return _hmac.new(key, payload, hashlib.sha256).hexdigest()


def load_pack_secret():
    """服务器私密密钥（首次运行随机生成，存 server_config.json）。

    只通过登录包下发给客户端，用于解密资源包 / 校验清单签名 ——
    别人抓到的 .zip 与清单都是乱码或验不过。
    """
    cfg_path = os.path.join(SERVER_DATA, 'server_config.json')
    try:
        cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f) or {}
        secret = str(cfg.get('pack_secret') or '')
        if len(secret) < 32:
            secret = os.urandom(32).hex()
            cfg['pack_secret'] = secret
            try:
                os.makedirs(SERVER_DATA, exist_ok=True)
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                print("[安全] 已生成服务器资源密钥 pack_secret")
            except Exception as e:
                print(f"[安全] pack_secret 写入失败: {e}")
        return secret
    except Exception as e:
        print(f"[安全] 读取 pack_secret 失败: {e}")
        return ''


def load_json_safe(path, default):
    """容错加载 JSON：文件损坏时备份为 .corrupted 并返回默认值，避免整体崩溃"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"[警告] 配置文件损坏: {path} ({e})")
        try:
            shutil.copy2(path, path + '.corrupted')
        except Exception:
            pass
        # 防丢失：save_json 每次写盘前都会留一份 .bak，这里用它恢复
        try:
            _bak = path + '.bak'
            if os.path.exists(_bak):
                with open(_bak, 'r', encoding='utf-8') as f:
                    _data = json.load(f)
                print(f"[恢复] 已用备份恢复: {_bak}", flush=True)
                try:
                    shutil.copy2(_bak, path)
                except Exception:
                    pass
                return _data
        except Exception as _be:
            print(f"[恢复] 备份同样不可用: {_be}")
        return default

def save_json_atomic(path, data):
    """原子写入 JSON：先写临时文件再替换，避免写一半损坏"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)

_chat_history_lock = threading.Lock()

def load_chat_history():
    if not os.path.exists(CHAT_HISTORY_FILE):
        return []
    try:
        with open(CHAT_HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[警告] 聊天历史文件损坏或不可读: {e}")
        return []

def save_chat_history(msg):
    os.makedirs(SERVER_DATA, exist_ok=True)
    with _chat_history_lock:
        history = load_chat_history()
        history.append(msg)
        if len(history) > 500:
            history = history[-500:]
        save_json_atomic(CHAT_HISTORY_FILE, history)

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
        'muted_perm': False,          # 服内永久禁言
        'global_muted_until': None,   # 全局禁言（同时封禁私信）
        'global_muted_perm': False,   # 全局永久禁言
        'remarks': {},                # 独立备注存档 {server_uid: remark}，跨陌生人/拉黑/删友保留
        'destroy_pending_until': None,  # 注销冷却期截止时间（期间可取消）
        'destroy_requests': [],         # 最近的注销申请时间（每周最多 3 次）
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
        'friends': {},  # 好友表：{server_uid: {'remark': ''}}
        'stranger_sent': {},  # 已发送私信、等待回复的陌生人：{server_uid: {'time': ...}}
        'blocked': [],  # 拉黑名单：[server_uid, ...]（拉黑后双方都无法向对方发私信）
        # 会话状态（跟着账号走）：特别关注 / 消息免打扰 / 置顶 / 不再显示
        'chat_prefs': {'focus': [], 'muted': [], 'pinned': [], 'hidden': []},
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
    save_json_atomic(path, data)

def _pm_pair_file(uid_a, uid_b):
    """私信历史按无序 uid 对命名，双方共用同一文件"""
    a, b = sorted([str(uid_a), str(uid_b)])
    return os.path.join(PM_HISTORY_DIR, f'{a}__{b}.json')

def load_pm_history(uid_a, uid_b):
    """读取两名玩家之间的私信历史（时间正序）"""
    data = load_json_safe(_pm_pair_file(uid_a, uid_b), [])
    return data if isinstance(data, list) else []

def append_pm_history(from_uid, from_name, to_uid, text, time_str, msg_id, from_perm='user'):
    """追加一条私信到双方共用历史文件（加锁 + 原子写，超限裁剪最旧）"""
    os.makedirs(PM_HISTORY_DIR, exist_ok=True)
    with pm_history_lock:
        path = _pm_pair_file(from_uid, to_uid)
        history = load_json_safe(path, [])
        if not isinstance(history, list):
            history = []
        history.append({'id': msg_id, 'from_uid': from_uid, 'from_name': from_name,
                        'from_perm': from_perm, 'text': text, 'time': time_str})
        if len(history) > PM_HISTORY_LIMIT:
            history = history[-PM_HISTORY_LIMIT:]
        save_json_atomic(path, history)

def _pm_offline_file(server_uid):
    return os.path.join(PM_OFFLINE_DIR, f'{server_uid}.json')

def queue_pm_offline(to_uid, msg):
    """把私信放入对方离线收件箱（加锁 + 原子写，超限裁剪最旧）"""
    os.makedirs(PM_OFFLINE_DIR, exist_ok=True)
    with pm_offline_lock:
        path = _pm_offline_file(to_uid)
        inbox = load_json_safe(path, [])
        if not isinstance(inbox, list):
            inbox = []
        inbox.append(msg)
        if len(inbox) > PM_OFFLINE_LIMIT:
            inbox = inbox[-PM_OFFLINE_LIMIT:]
        save_json_atomic(path, inbox)

def take_pm_offline(server_uid):
    """取出并清空某玩家的离线收件箱（登录时调用）"""
    os.makedirs(PM_OFFLINE_DIR, exist_ok=True)
    with pm_offline_lock:
        path = _pm_offline_file(server_uid)
        inbox = load_json_safe(path, [])
        if not isinstance(inbox, list):
            inbox = []
        if inbox:
            save_json_atomic(path, [])
    return inbox

def load_pm_offline(server_uid):
    """只读查看离线收件箱（不清空）"""
    data = load_json_safe(_pm_offline_file(server_uid), [])
    return data if isinstance(data, list) else []

def load_player_mails(server_uid):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}_mails.json')
    return load_json_safe(path, [])

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
    return load_json_safe(SERVER_CODES_FILE, {})

def save_server_codes(codes):
    save_json_atomic(SERVER_CODES_FILE, codes)

def load_banned_areas():
    return load_json_safe(BANNED_AREAS_FILE, [])

def save_banned_areas(areas):
    save_json_atomic(BANNED_AREAS_FILE, areas)

def load_banned_timezones():
    return load_json_safe(BANNED_TIMEZONES_FILE, [])

def save_banned_timezones(tzones):
    save_json_atomic(BANNED_TIMEZONES_FILE, tzones)

BANNED_IPS_FILE = os.path.join(SERVER_DATA, 'banned_ips.json')

def load_banned_ips():
    return load_json_safe(BANNED_IPS_FILE, [])

def save_banned_ips(ips):
    save_json_atomic(BANNED_IPS_FILE, ips)

def load_server_config():
    config = load_json_safe(SERVER_CONFIG_FILE, {})
    if not isinstance(config, dict):
        config = {}
    config.setdefault('server_name', '流萤服务器')
    config.setdefault('ai_enabled', True)
    config.setdefault('chat_enabled', True)
    config.setdefault('common_mute', False)
    # ===== 新增两行 =====
    config.setdefault('max_players', 10)
    config.setdefault('access_mode', 'none')
    return config

def load_whitelist():
    return load_json_safe(WHITELIST_FILE, [])

def save_whitelist(data):
    save_json_atomic(WHITELIST_FILE, data)

def load_blacklist():
    return load_json_safe(BLACKLIST_FILE, [])

def save_blacklist(data):
    save_json_atomic(BLACKLIST_FILE, data)

def save_server_config(config):
    save_json_atomic(SERVER_CONFIG_FILE, config)

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

# ==================== 好友/私信系统 ====================
FRIENDS_LIMIT = 100  # 每位玩家好友数量上限

def _lookup_player_summary(server_uid):
    """查询玩家摘要（当前昵称 + 是否在线）：在线优先，其次读玩家档案"""
    with server_lock:
        op = player_by_server_uid.get(server_uid)
        if op:
            return {'name': op['name'], 'online': True,
                    'permission': op.get('permission', 'user')}
    path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {'name': data.get('name') or server_uid, 'online': False,
                    'permission': data.get('permission', 'user')}
        except Exception:
            pass
    return {'name': server_uid, 'online': False, 'permission': 'user'}

CHAT_PREF_KEYS = ('focus', 'muted', 'pinned', 'hidden')
CHAT_FOCUS_LIMIT = 3

def sanitize_chat_prefs(prefs):
    """规范化会话状态：去重、仅保留字符串、特别关注最多 3 个、不可对服内聊天免打扰/关注"""
    result = {key: [] for key in CHAT_PREF_KEYS}
    if not isinstance(prefs, dict):
        return result
    for key in CHAT_PREF_KEYS:
        seen = []
        for uid in (prefs.get(key) or []):
            if not isinstance(uid, str) or not uid:
                continue
            if key in ('focus', 'muted') and uid == 'server':
                continue
            if key not in seen:
                seen.append(uid)
        result[key] = seen[:CHAT_FOCUS_LIMIT] if key == 'focus' else seen
    return result

# ==================== 禁言 / 私信封禁 状态 ====================

# ==================== 账号销毁 ====================
DESTROYED_FILE = os.path.join(SERVER_DATA, 'destroyed_accounts.json')


def load_destroyed_accounts():
    data = load_json_safe(DESTROYED_FILE, {})
    return data if isinstance(data, dict) else {}


def save_destroyed_accounts(data):
    try:
        with open(DESTROYED_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[销号] 保存失败: {e}")


_destroy_in_progress = set()
_destroy_progress_lock = threading.Lock()


def destroy_in_progress(server_uid):
    with _destroy_progress_lock:
        return server_uid in _destroy_in_progress


def begin_destroy(server_uid):
    """标记账号注销处理中；已在处理则返回 False（重复请求提示"正在处理"）"""
    with _destroy_progress_lock:
        if server_uid in _destroy_in_progress:
            return False
        _destroy_in_progress.add(server_uid)
        return True


def end_destroy(server_uid):
    with _destroy_progress_lock:
        _destroy_in_progress.discard(server_uid)


def destroy_phrase_ok(text):
    """注销确认短语判定（宽松）：接受中英文两套写法，忽略大小写与所有空白"""
    t = ''.join((text or '').split())
    if t in ('我确认', '我确定注销并了解结果'):
        return True
    tl = t.lower().rstrip('.。')
    return tl in ('iconfirm',
                  'iconfirmthecancellationandacknowledgetheconsequences')


def destroyed_users_payload():
    """已注销名单：{uid: 显示名}（下发给客户端用于拦截 @/私信/拉黑）"""
    return {uid: f"账号已注销#{info.get('destroy_id', '000000')}"
            for uid, info in load_destroyed_accounts().items()}


def is_account_destroyed(server_uid):
    return server_uid in load_destroyed_accounts()


def get_destroyed_display(server_uid):
    """已注销账号的聊天显示名：账号已注销#6位注销编号"""
    info = load_destroyed_accounts().get(server_uid)
    if not info:
        return None
    return f"账号已注销#{info.get('destroy_id', '000000')}"


def destroy_account(server_uid, notice='你的账户已被注销！'):
    """销毁账号（带防重）：正在处理时返回 None（不重复执行）。"""
    if not begin_destroy(server_uid):
        return None
    try:
        return _destroy_account_inner(server_uid, notice)
    finally:
        end_destroy(server_uid)


def _destroy_account_inner(server_uid, notice='你的账户已被注销！'):
    """销毁账号：删除档案/背包/邮件/离线私信，匿名化聊天记录，清理他人好友与等待列表。
    返回 6 位注销编号（编号专属此人，其历史消息永久显示 账号已注销#编号）。
    若该用户在线：先弹窗提示 notice，再踢出服务器（强制注销也必须先离线）。"""
    pdata = load_player_data(server_uid) or {}
    destroyed = load_destroyed_accounts()
    if server_uid in destroyed:
        return destroyed[server_uid].get('destroy_id', '000000')
    while True:
        destroy_id = f"{random.randint(0, 999999):06d}"
        if not any(str(v.get('destroy_id')) == destroy_id for v in destroyed.values()):
            break
    display = f"账号已注销#{destroy_id}"
    destroyed[server_uid] = {'destroy_id': destroy_id,
                             'name': pdata.get('name') or server_uid,
                             'destroyed_at': datetime.now(SERVER_TIMEZONE).isoformat()}
    save_destroyed_accounts(destroyed)

    # 删除档案/背包/邮件/离线私信
    for suffix in ('', '_inv', '_mails'):
        try:
            p = os.path.join(PLAYERS_DIR, f'{server_uid}{suffix}.json')
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        p = os.path.join(PM_OFFLINE_DIR, f'{server_uid}.json')
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass

    # 匿名化公共聊天历史（此人发过的消息昵称显示 账号已注销#编号）
    try:
        if os.path.exists(CHAT_HISTORY_FILE):
            with open(CHAT_HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            changed = False
            for m in history:
                if not isinstance(m, dict):
                    continue
                if m.get('from_uid') == server_uid:
                    if m.get('from_name') != display:
                        m['from_name'] = display
                        changed = True
                elif not m.get('from_uid') and m.get('sender') == pdata.get('name'):
                    if m.get('sender') != display:
                        m['sender'] = display
                        changed = True
            if changed:
                with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(history, f, ensure_ascii=False)
    except Exception as e:
        print(f"[销号] 匿名化聊天历史失败: {e}")

    # 匿名化私信历史
    try:
        if os.path.isdir(PM_HISTORY_DIR):
            for fn in os.listdir(PM_HISTORY_DIR):
                if not fn.endswith('.json'):
                    continue
                fp = os.path.join(PM_HISTORY_DIR, fn)
                # 与并发的私信追加互斥，避免读到半截文件或覆盖并发写入
                with pm_history_lock:
                    msgs = load_json_safe(fp, [])
                    if not isinstance(msgs, list):
                        continue
                    changed = False
                    for m in msgs:
                        if isinstance(m, dict) and m.get('from_uid') == server_uid and m.get('from_name') != display:
                            m['from_name'] = display
                            changed = True
                    if changed:
                        with open(fp, 'w', encoding='utf-8') as f:
                            json.dump(msgs, f, ensure_ascii=False)
    except Exception as e:
        print(f"[销号] 匿名化私信历史失败: {e}")

    # 从其他玩家档案清理：好友/等待回复/拉黑/会话状态（陌生人板块自动删除该用户）
    try:
        if os.path.isdir(PLAYERS_DIR):
            for fn in os.listdir(PLAYERS_DIR):
                if (not fn.endswith('.json') or fn.endswith('_inv.json')
                        or fn.endswith('_mails.json') or fn[:-5] == server_uid):
                    continue
                fp = os.path.join(PLAYERS_DIR, fn)
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        other = json.load(f)
                except Exception:
                    continue
                changed = False
                for key in ('friends', 'stranger_sent'):
                    d = other.get(key)
                    if isinstance(d, dict) and server_uid in d:
                        d.pop(server_uid, None)
                        changed = True
                blocked = other.get('blocked')
                if isinstance(blocked, list) and server_uid in blocked:
                    blocked.remove(server_uid)
                    changed = True
                prefs = other.get('chat_prefs')
                if isinstance(prefs, dict):
                    for k in ('focus', 'muted', 'pinned', 'hidden'):
                        lst = prefs.get(k)
                        if isinstance(lst, list) and server_uid in lst:
                            lst.remove(server_uid)
                            changed = True
                if changed:
                    save_player_data(fn[:-5], other)
    except Exception as e:
        print(f"[销号] 清理他人档案失败: {e}")

    # 在线则先弹窗提示、再踢出（确保注销时用户离线）
    with server_lock:
        tp = player_by_server_uid.get(server_uid)
    if tp is not None:
        try:
            TCPHandler.send_to(tp['client'], {'type': 'account_destroyed',
                                              'data': {'text': notice}})
            time.sleep(0.4)
            TCPHandler.send_to(tp['client'], {'type': 'kick',
                                              'data': {'reason': notice}})
            time.sleep(0.1)
            tp['client'].close()
        except Exception:
            pass
        with server_lock:
            if tp['client'] in clients:
                del clients[tp['client']]
            player_by_server_uid.pop(server_uid, None)
    broadcast_player_list()
    # 立即广播最新注销名单：其他客户端据此拦截 @/私信/拉黑，并更新会话显示
    # 注意：这里是模块级函数，没有 TCPHandler.broadcast，需自行遍历客户端发送
    try:
        payload = {'type': 'destroyed_users_data',
                   'data': {'destroyed_users': destroyed_users_payload()}}
        with server_lock:
            targets = list(clients.values())
        for _tp in targets:
            try:
                TCPHandler.send_to(_tp['client'], payload)
            except Exception:
                pass
    except Exception:
        pass
    return destroy_id


def _account_punished(pdata):
    """是否存在禁言/封禁等惩罚"""
    active, _, _ = get_mute_info(pdata, 'server')
    if active:
        return True
    if pdata.get('banned'):
        return True
    bu = pdata.get('banned_until')
    if bu:
        try:
            bt = datetime.fromisoformat(bu)
            if bt.tzinfo is None:
                bt = SERVER_TIMEZONE.localize(bt)
            if bt > datetime.now(SERVER_TIMEZONE):
                return True
        except Exception:
            pass
    return False


def check_destroy_pending(server_uid, tp=None):
    """检查一个玩家的注销冷却：到期且无惩罚 → 自动销毁；到期但受罚 → 取消注销。
    返回 'destroyed' / 'cancelled' / 'pending' / 'none'"""
    pdata = load_player_data(server_uid) or {}
    until = pdata.get('destroy_pending_until')
    if not until:
        return 'none'
    try:
        dt = datetime.fromisoformat(until)
    except Exception:
        return 'none'
    if dt.tzinfo is None:
        dt = SERVER_TIMEZONE.localize(dt)
    if datetime.now(SERVER_TIMEZONE) < dt:
        return 'pending'
    if _account_punished(pdata):
        pdata['destroy_pending_until'] = None
        save_player_data(server_uid, pdata)
        if tp is not None:
            try:
                TCPHandler.send_to(tp['client'], {
                    'type': 'account_destroy_status',
                    'data': {'state': 'cancelled',
                             'reason': '冷却期内受到禁言/封禁等惩罚，注销已取消'}})
            except Exception:
                pass
        return 'cancelled'
    if destroy_account(server_uid, '你的账户已在规定时间内注销') is None:
        return 'processing'
    return 'destroyed'


def destroy_pending_watcher():
    """每 5 秒检查在线玩家注销冷却期；每 30 秒额外扫描离线档案。
    到期自动销号（自动化，无需控制台确认）；冷却期内受到惩罚则取消注销。"""
    tick = 0
    while True:
        time.sleep(5)
        tick += 1
        try:
            with server_lock:
                online_snapshot = {uid: p for uid, p in player_by_server_uid.items()}
        except Exception:
            continue
        processed = set()
        # --- 在线玩家（每 5 秒） ---
        for server_uid, tp in online_snapshot.items():
            try:
                result = check_destroy_pending(server_uid, tp)
                processed.add(server_uid)
                if result in ('destroyed', 'cancelled'):
                    print(f"[销号] {server_uid} 冷却期处理结果: {result}")
            except Exception as e:
                print(f"[销号] 处理失败: {e}")
        # --- 离线玩家（每 30 秒） ---
        if tick % 6 == 0 and os.path.isdir(PLAYERS_DIR):
            try:
                for fn in os.listdir(PLAYERS_DIR):
                    if (not fn.endswith('.json') or fn.endswith('_inv.json')
                            or fn.endswith('_mails.json')):
                        continue
                    server_uid = fn[:-5]
                    if server_uid in processed:
                        continue
                    result = check_destroy_pending(server_uid, None)
                    if result in ('destroyed', 'cancelled'):
                        print(f"[销号] 离线 {server_uid} 冷却期处理结果: {result}")
            except Exception as e:
                print(f"[销号] 离线扫描失败: {e}")


def get_mute_info(state, scope='server'):
    """检查禁言状态。

    scope='server'：服内禁言（全局禁言同样封禁服内聊天）
    scope='pm'    ：私信（仅全局禁言生效）
    返回 (active, until_iso_or_None, permanent)
    """
    now = datetime.now(SERVER_TIMEZONE)
    if scope == 'pm':
        candidates = [('global_muted_until', 'global_muted_perm')]
    else:
        candidates = [('global_muted_until', 'global_muted_perm'),
                      ('muted_until', 'muted_perm')]
    best_dt = None
    for until_key, perm_key in candidates:
        if state.get(perm_key):
            return True, None, True
        raw = state.get(until_key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = SERVER_TIMEZONE.localize(dt)
        if dt > now and (best_dt is None or dt > best_dt):
            best_dt = dt
    if best_dt is not None:
        return True, best_dt.isoformat(), False
    return False, None, False


def format_mute_until(dt):
    """把解除时间格式化为 UTC+8 6:00 形式；dt 为 None 表示永久"""
    if dt is None:
        return '永久'
    try:
        off = dt.utcoffset()
        hours = int(off.total_seconds() // 3600) if off is not None else 0
    except Exception:
        hours = 0
    sign = '+' if hours >= 0 else '-'
    return f"UTC{sign}{abs(hours)} {dt.hour}:{dt.minute:02d}"


def pm_ban_payload(state):
    """私信封禁（全局禁言）状态，供客户端显示禁令横幅"""
    active, until_iso, perm = get_mute_info(state, 'pm')
    if not active:
        return {'banned': False}
    if perm or not until_iso:
        return {'banned': True, 'permanent': True, 'until_text': '永久'}
    try:
        dt = datetime.fromisoformat(until_iso)
    except Exception:
        dt = None
    return {'banned': True, 'permanent': False,
            'until_text': format_mute_until(dt), 'until': until_iso}


def server_mute_payload(state):
    """服内禁言状态（不含全局禁言），供客户端右键菜单显示/解除"""
    now = datetime.now(SERVER_TIMEZONE)
    if state.get('muted_perm'):
        return {'muted': True, 'permanent': True, 'until_text': '永久'}
    raw = state.get('muted_until')
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = SERVER_TIMEZONE.localize(dt)
            if dt > now:
                return {'muted': True, 'permanent': False,
                        'until_text': format_mute_until(dt), 'until': dt.isoformat()}
        except Exception:
            pass
    return {'muted': False}


def _lookup_server_mute(server_uid):
    """查询某玩家的服内禁言状态（在线优先，其次读档案）"""
    if not server_uid:
        return {'muted': False}
    with server_lock:
        op = player_by_server_uid.get(server_uid)
    if op is not None:
        return server_mute_payload(op)
    return server_mute_payload(load_player_data(server_uid) or {})


def _lookup_pm_ban(server_uid):
    """查询某玩家的私信封禁状态（在线优先，其次读档案）"""
    if not server_uid:
        return {'banned': False}
    with server_lock:
        op = player_by_server_uid.get(server_uid)
    if op is not None:
        return pm_ban_payload(op)
    return pm_ban_payload(load_player_data(server_uid) or {})


def _lookup_remark(pdata, uid):
    """读取请求方对某玩家的备注（独立存档优先，旧的好友内嵌备注兜底）"""
    if not pdata or not uid:
        return ''
    remarks = pdata.get('remarks')
    if isinstance(remarks, dict) and remarks.get(uid):
        return str(remarks.get(uid))[:20]
    info = (pdata.get('friends') or {}).get(uid)
    if isinstance(info, dict) and info.get('remark'):
        return str(info['remark'])[:20]
    return ''


def resolve_player_ref(identifier):
    """按 名字/userid/server_uid 查找玩家：先在线，再档案。
    返回 (online_player_or_None, server_uid_or_None, pdata_or_None, name)"""
    identifier = str(identifier).strip()
    with server_lock:
        for p in clients.values():
            if p['name'] == identifier or p['userid'] == identifier or p['server_uid'] == identifier:
                return p, p['server_uid'], load_player_data(p['server_uid']), p['name']
    if os.path.isdir(PLAYERS_DIR):
        for filename in _player_files():
            if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                continue
            filepath = os.path.join(PLAYERS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue
            server_uid = filename[:-5]
            if (server_uid == identifier or data.get('name') == identifier or
                    data.get('userid') == identifier or data.get('uid') == identifier):
                return None, server_uid, data, data.get('name') or server_uid
    return None, None, None, identifier


def _sync_player_mute_fields(player, pdata):
    if player is None:
        return
    player['muted_until'] = pdata.get('muted_until')
    player['muted_perm'] = bool(pdata.get('muted_perm'))
    player['global_muted_until'] = pdata.get('global_muted_until')
    player['global_muted_perm'] = bool(pdata.get('global_muted_perm'))


def _push_mute_status(player, pdata):
    if player is None:
        return
    try:
        TCPHandler.send_to(player['client'], {'type': 'mute_status', 'data': {
            'muted_until': pdata.get('muted_until'),
            'muted_perm': bool(pdata.get('muted_perm')),
            'pm_ban': pm_ban_payload(pdata)}})
    except Exception:
        pass


def broadcast_player_list():
    """向所有在线客户端推送最新在线名单（含权限与私信封禁状态）"""
    with server_lock:
        targets = list(clients.values())
    for p in targets:
        with server_lock:
            lst = [{'name': q['name'], 'id': q['userid'], 'uid': q['server_uid'],
                    'permission': q.get('permission', 'user'),
                    'pm_ban': pm_ban_payload(q),
                    'mute_server': server_mute_payload(q)} for q in clients.values()]
        try:
            TCPHandler.send_to(p['client'], {'type': 'player_list', 'data': {'players': lst}})
        except Exception:
            pass


def apply_mute_state(identifier, scope, mute_until, mute_perm):
    """设置禁言。scope='global' 全局禁言（同时封私信）/ 其他=服内禁言。
    返回 (ok, message, name, online)"""
    tp, server_uid, pdata, name = resolve_player_ref(identifier)
    if not server_uid or not pdata:
        return False, f'未找到玩家 {identifier}', identifier, False
    if pdata.get('permission') == 'admin' or (tp is not None and tp.get('permission') == 'admin'):
        return False, '不能禁言管理员', name, tp is not None
    if scope == 'global':
        # 全局禁言 = 服内聊天 + 私信 一起禁；解除全局禁言时也一并解除，
        # 避免"解了全局还剩服内"导致客户端输入框仍处于禁言锁定状态
        pdata['global_muted_until'] = mute_until
        pdata['global_muted_perm'] = bool(mute_perm)
        pdata['muted_until'] = mute_until
        pdata['muted_perm'] = bool(mute_perm)
    else:
        pdata['muted_until'] = mute_until
        pdata['muted_perm'] = bool(mute_perm)
    save_player_data(server_uid, pdata)
    _sync_player_mute_fields(tp, pdata)
    _push_mute_status(tp, pdata)
    broadcast_player_list()
    return True, 'ok', name, tp is not None


def clear_mute_state(identifier, scope):
    """解除禁言。scope='global' 解除全局禁言（含私信封禁）/ 其他解除服内禁言"""
    tp, server_uid, pdata, name = resolve_player_ref(identifier)
    if not server_uid or not pdata:
        return False, f'未找到玩家 {identifier}', identifier, False
    if scope == 'global':
        # 与 apply_mute_state 对应：全局禁言解除时一并解除服内禁言
        pdata['global_muted_until'] = None
        pdata['global_muted_perm'] = False
        pdata['muted_until'] = None
        pdata['muted_perm'] = False
    else:
        pdata['muted_until'] = None
        pdata['muted_perm'] = False
    save_player_data(server_uid, pdata)
    _sync_player_mute_fields(tp, pdata)
    _push_mute_status(tp, pdata)
    broadcast_player_list()
    return True, 'ok', name, tp is not None


def build_friends_data(pdata):
    """好友列表 + 等待回复 + 拉黑名单 + 会话状态（统一构造，登录/变更时下发）"""
    friends, pending = build_social_payload(pdata)
    destroyed_users = destroyed_users_payload()
    return {'friends': friends, 'pending': pending,
            'blocked': pdata.get('blocked', []) or [],
            'destroyed_users': destroyed_users,
            'destroy_pending_until': pdata.get('destroy_pending_until'),
            'self_pm_ban': pm_ban_payload(pdata),
            'self_mute_server': server_mute_payload(pdata),
            'chat_prefs': sanitize_chat_prefs(pdata.get('chat_prefs'))}

def build_social_payload(pdata):
    """构建好友列表与等待回复的陌生人列表（用于下发客户端）"""
    friends = []
    for uid, info in (pdata.get('friends') or {}).items():
        if not isinstance(uid, str) or not uid:
            continue
        if is_account_destroyed(uid):
            continue  # 已注销账号不出现在好友/私信名单中
        remark = _lookup_remark(pdata, uid)
        summary = _lookup_player_summary(uid)
        friends.append({'uid': uid, 'name': summary['name'],
                        'remark': remark or '', 'online': summary['online'],
                        'permission': summary.get('permission', 'user'),
                        'pm_ban': _lookup_pm_ban(uid),
                        'mute_server': _lookup_server_mute(uid)})
    pending = []
    for uid, info in (pdata.get('stranger_sent') or {}).items():
        if not isinstance(uid, str) or not uid:
            continue
        if is_account_destroyed(uid):
            continue  # 已注销账号不出现在陌生人名单中
        summary = _lookup_player_summary(uid)
        t = info.get('time', '') if isinstance(info, dict) else ''
        pending.append({'uid': uid, 'name': summary['name'], 'time': t,
                        'remark': _lookup_remark(pdata, uid),
                        'permission': summary.get('permission', 'user'),
                        'pm_ban': _lookup_pm_ban(uid),
                        'mute_server': _lookup_server_mute(uid)})
    return friends, pending

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
    # 单位表：只要不与另一单位冲突就大小写都收（D/d=天、H/h=小时、Y/y=年、S/s=秒），
    # 并收下中文单位（天/日/小时/时/分/分钟/月/年/秒）。
    # 仍区分大小写的只有一对：'M'=月、'm'=分钟（两个都常用，不能混）。
    units = {
        'Y': 'years', 'y': 'years', 'yr': 'years', 'yrs': 'years', 'year': 'years', 'years': 'years', '年': 'years',
        'M': 'months', 'mo': 'months', 'mon': 'months', 'month': 'months', 'months': 'months', '月': 'months',
        'D': 'days', 'd': 'days', 'day': 'days', 'days': 'days', '天': 'days', '日': 'days',
        'H': 'hours', 'h': 'hours', 'hr': 'hours', 'hrs': 'hours', 'hour': 'hours', 'hours': 'hours',
        '小时': 'hours', '时': 'hours',
        'm': 'minutes', 'min': 'minutes', 'mins': 'minutes', 'minute': 'minutes', 'minutes': 'minutes',
        '分': 'minutes', '分钟': 'minutes',
        's': 'seconds', 'sec': 'seconds', 'secs': 'seconds', 'second': 'seconds', 'seconds': 'seconds', '秒': 'seconds',
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
        kind = units.get(unit)
        if kind == 'years':
            years += num
        elif kind == 'months':
            months += num
        elif kind == 'days':
            days += num
        elif kind == 'hours':
            hours += num
        elif kind == 'minutes':
            minutes += num
        elif kind == 'seconds':
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
    # 锁内只做快照，网络发送放到锁外，避免阻塞所有工作线程
    with server_lock:
        snapshot = [(client, dict(player)) for client, player in clients.items()]
    to_remove = []
    for client, player in snapshot:
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
            client.close()
        except:
            pass
        to_remove.append((client, player['server_uid']))
    # 清理非管理员玩家
    with server_lock:
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
        # 不允许冒充已注销账号的显示名（账号已注销#编号 / 开拓者-user-… 之类）
        if stripped.startswith("账号已注销"):
            return False
        # 只允许中英文、数字、下划线（长度1-16）
        if not re.match(r'^[\w\u4e00-\u9fff]{1,16}$', stripped):
            return False
        return True

    def _save_gacha_config_file(self, cfg):
        path = os.path.join(SERVER_DATA, 'gacha_config.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _broadcast_gacha_config(self, cfg):
        """广播卡池配置给所有在线客户端（热更新）"""
        with server_lock:
            for client in list(clients.keys()):
                try:
                    TCPHandler.send_to(client, {'type': 'gacha_config', 'data': {'config': cfg}})
                except Exception:
                    pass

    def window_ref_shop_goods(self):
        """登录下发商店商品（enabled 项；由内容管理的商店管理维护）"""
        try:
            bridge = getattr(self.server, 'bridge', None) if hasattr(self, 'server') else None
            if bridge is not None and hasattr(bridge, '_load_shop_config'):
                goods = bridge._load_shop_config()
                return [g for g in goods if g.get('enabled')]
        except Exception:
            pass
        # 服务端主窗口有 bridge 属性；兜底直接读文件
        try:
            if os.path.exists(SHOP_CONFIG_FILE):
                with open(SHOP_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                return [g for g in (data.get('goods') or [])
                        if isinstance(g, dict) and g.get('enabled')]
        except Exception:
            pass
        return []

    def _resource_sigs(self):
        """每个资源包 zip 的签名（mtime:size），用于客户端版本握手"""
        out = {}
        try:
            rdir = os.path.join(SERVER_DATA, 'webroot', 'resources')
            if os.path.isdir(rdir):
                for fn in os.listdir(rdir):
                    if not fn.endswith('.zip'):
                        continue
                    p = os.path.join(rdir, fn)
                    try:
                        st = os.stat(p)
                        out[fn[:-4]] = f'{int(st.st_mtime)}:{st.st_size}'
                    except Exception:
                        pass
        except Exception as e:
            print(f"[资源签名] 计算失败: {e}")
        return out

    def _gacha_pity_map(self, pdata):
        """{pool_id: {pity, guaranteed}}：客户端卡池页面切换时显示各池保底"""
        try:
            cfg = self._load_gacha_config()
            out = {}
            for p in (cfg.get('pools') or []):
                if not isinstance(p, dict):
                    continue
                k5, k4, kg = pool_pity_keys(p.get('id'))
                out[p.get('id')] = {'pity': int(pdata.get(k5, 0) or 0),
                                    'guaranteed': bool(pdata.get(kg, False))}
            return out
        except Exception:
            return {}

    def _load_gacha_config(self):
        config_path = os.path.join(SERVER_DATA, 'gacha_config.json')
        # 新格式默认配置（增加 expiry 字段）
        default_config = {
            "name": "新卡池",
            "description": "限定角色UP",
            "icon": "✦",
            "banner": "img/pool_banner.png",
            "cost_type": "star_rail_ticket",
            "cost_per_pull": 1,
            "multi_pull": 10,
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
                # 确保规则字段完整（规则不是字典时整体回退默认）
                if not isinstance(config.get("rules"), dict):
                    config["rules"] = dict(default_config["rules"])
                for key in default_config["rules"]:
                    if key not in config["rules"]:
                        config["rules"][key] = default_config["rules"][key]
                # 物品池必须是列表，否则回退默认，避免结算时崩溃
                for key in ("five_star_items", "four_star_items", "three_star_items"):
                    if not isinstance(config.get(key), list):
                        print(f"[配置加载] {key} 类型异常，回退默认")
                        config[key] = default_config[key]
                # 多卡池规范化（无 pools 时用顶层字段兼容成单卡池）
                _raw_dump = json.dumps(config, ensure_ascii=False, sort_keys=True)
                config = normalize_gacha_pools(config, default_config)
                if json.dumps(config, ensure_ascii=False, sort_keys=True) != _raw_dump:
                    # 相对有效期（30D/2h…）在这里被换算成绝对时间：落盘一次，
                    # 否则每次重启都会从"现在"重新计时（卡池永远不过期）
                    try:
                        with open(config_path, 'w', encoding='utf-8') as f:
                            json.dump(config, f, indent=2, ensure_ascii=False)
                        print("[配置加载] 卡池配置已规范化并落盘（相对有效期→绝对时间）")
                    except Exception as _e:
                        print(f"[配置加载] 规范化结果落盘失败: {_e}")
                return config
            except Exception as e:
                print(f"[配置加载错误] {e}，使用默认配置")
                return default_config
        else:
            # 不存在则创建默认配置
            default_config = normalize_gacha_pools(default_config, default_config)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            return default_config

    @staticmethod
    def _normalize_pool(pool):
        """把物品池规范为 [{id, weight, amount, up?}]：
        兼容纯字符串写法，丢弃缺少 id 的条目。"""
        out = []
        if not isinstance(pool, list):
            return out
        for entry in pool:
            if isinstance(entry, str):
                entry = {'id': entry, 'weight': 1}
            if not isinstance(entry, dict):
                continue
            item_id = entry.get('id')
            if not isinstance(item_id, str) or not item_id.strip():
                continue
            try:
                weight = int(entry.get('weight', 1) or 0)
            except Exception:
                weight = 0
            try:
                amount = int(entry.get('amount', 0) or 0)
            except Exception:
                amount = 0
            norm = dict(entry)
            norm['id'] = item_id.strip()
            norm['weight'] = max(0, weight)
            norm['amount'] = amount
            out.append(norm)
        return out

    def _do_gacha(self, server_uid, count, pool_id=None):
        config = self._load_gacha_config()
        pool = get_gacha_pool(config, pool_id)
        if pool is None:
            # 传入的是未规范化的旧结构（顶层字段）：按顶层字段兜底
            pool = {'id': 'default', 'rules': config.get('rules') or {},
                    'five_star_items': config.get('five_star_items') or [],
                    'four_star_items': config.get('four_star_items') or [],
                    'three_star_items': config.get('three_star_items') or []}
        _pity5_key, _pity4_key, _guar_key = pool_pity_keys(pool.get('id'))
        rules = pool.get('rules') or {}
        # 规则缺失时用服务端默认值兜底，避免 KeyError 打断整个抽卡
        _default_rules = {'base_5star_rate': 0.006, 'base_4star_rate': 0.051,
                          'pity_hard_5star': 80, 'pity_hard_4star': 10,
                          'guarantee_up_5star': True}
        for _k, _v in _default_rules.items():
            rules.setdefault(_k, _v)
        # 物品池规范 + 空池兜底（空池配置不应让抽卡直接崩）
        _safe_pool = [{'id': 'none', 'weight': 1, 'amount': 0}]
        _pools = {
            5: self._normalize_pool(pool.get('five_star_items')) or list(_safe_pool),
            4: self._normalize_pool(pool.get('four_star_items')) or list(_safe_pool),
            3: self._normalize_pool(pool.get('three_star_items')) or list(_safe_pool),
        }
        pity5 = self._get_pity(server_uid, _pity5_key)
        pity4 = self._get_pity(server_uid, _pity4_key)
        guaranteed5 = load_player_data(server_uid).get(_guar_key, False)

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

            # 根据星级选择物品池（空池已兜底）
            items_pool = _pools.get(star) or _safe_pool

            # 权重随机选择一个物品；总权重为 0 时退化为等概率
            total_weight = sum(item.get('weight', 0) for item in items_pool)
            if total_weight > 0:
                rand_weight = random.randint(1, total_weight)
                cumulative = 0
                selected = items_pool[0]
                for item in items_pool:
                    cumulative += item.get('weight', 0)
                    if rand_weight <= cumulative:
                        selected = item
                        break
            else:
                selected = random.choice(items_pool)
            if selected.get('id') in (None, ''):
                selected = _safe_pool[0]

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
                        # 出非UP五星：下次五星必为 UP（大保底）
                        guaranteed5 = True
            else:
                if star == 5:
                    # 五星但物品未标记 up：同样进入大保底
                    guaranteed5 = True

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
                # 自定义物品/时装（由管理页面创建）
                _ci_list = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
                _ci = next((it for it in _ci_list if it.get('id') == item_type), None)
                if _ci and (_ci.get('type') or 'item') == 'clothes':
                    real_type = 'clothes'
                    real_id = item_type
                else:
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
        pdata[_pity5_key] = pity5
        pdata[_pity4_key] = pity4
        pdata[_guar_key] = guaranteed5
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
            _n = AVAILABLE_CLOTHES.get(real_id, {}).get('name')
            if _n:
                return _n
            _ci_list = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
            for _ci in _ci_list:
                if _ci.get('id') == real_id:
                    return _ci.get('name', real_id)
            return real_id
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
        _ensure_data_dirs()   # 数据目录缺失自愈（首次运行/被清理后）
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
                # 校验归属：顶号场景下该映射可能已被新会话占用，不能误删
                if player_by_server_uid.get(server_uid) is player:
                    del player_by_server_uid[server_uid]
                if clients.get(self.request) is player:
                    del clients[self.request]
            log_emitter.log_signal.emit(f"[断开] {player['name']} 离开了服务器")
            self.broadcast_system(f"{player['name']} 离开了服务器")

    def process_message(self, msg, player):
        mtype = msg.get('type', '')
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
            return player

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
                    for filename in _player_files():
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
                for filename in _player_files():
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
                for filename in _player_files():
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
                # 通过 load_player_data 重新读取并补全缺失字段（防止旧文件/手工编辑缺 uid 等键导致 KeyError 断连）
                pdata = load_player_data(server_uid)
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
                # 生成默认名称：注销用户 + 随机 10 位数字
                # （不使用客户端传来的原昵称；也不使用 UID——UID 会重复使用，会撞名）
                _rand_digits = ''.join(random.choice('0123456789') for _ in range(10))
                default_name = f"注销用户{_rand_digits}"

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
                    'muted_perm': False,
                    'global_muted_until': None,
                    'global_muted_perm': False,
                    'remarks': {},
                    'destroy_pending_until': None,
                    'destroy_requests': [],
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
                # 已注销的 uid 禁止再次登录：注销身份必须永久生效
                # （此前"重新登录即从注销名单移除"会让该账号重新出现在他人的发起聊天/私信里，
                #   导致"仍可向注销账户发起聊天"的问题。注册身份已销毁，如需继续玩请新建本地账号。）
                if is_account_destroyed(server_uid):
                    self.send_json({'type': 'login_fail', 'data': {
                        'reason': '该账号已注销，无法登录。如需继续游玩请新建本地账号'}})
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
                    'muted_perm': bool(pdata.get('muted_perm')),
                    'global_muted_until': pdata.get('global_muted_until'),
                    'global_muted_perm': bool(pdata.get('global_muted_perm')),
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
                    # 资源包密钥种子：客户端用它解密资源包、校验清单签名（不落盘、每次登录都发）
                    'pack_secret': load_pack_secret(),
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
                    'muted_perm': bool(pdata.get('muted_perm')),
                    'pm_ban': pm_ban_payload(pdata),
                    'ai_enabled': ai_enabled,
                    'chat_enabled': chat_enabled,
                    'common_mute': common_mute,
                    'require_rename': pdata.get('first_login', False),
                    'custom_menu': custom_menu,
                    'custom_items': custom_items,
                    # 资源包签名：客户端据此判断本地缓存是否过期（过期自动重下）
                    'resource_sigs': self._resource_sigs(),
                    'shop_goods': self.window_ref_shop_goods(),
                    'gacha_history': pdata.get('gacha_history', []),
                    'gacha_config': self._load_gacha_config(),
                    'gacha_pity': self._gacha_pity_map(pdata),
                }
            })

            # 下发好友列表、等待回复的陌生人、拉黑名单与会话状态
            try:
                self.send_json({'type': 'friends_data', 'data': build_friends_data(pdata)})
            except Exception as e:
                print(f"[好友] 下发好友数据失败: {e}")

            # 送达离线期间收到的私信
            try:
                offline_msgs = take_pm_offline(server_uid)
                for om in offline_msgs:
                    self.send_json({'type': 'pm_recv', 'data': om})
                if offline_msgs:
                    print(f"[私信] 向 {player['name']} 送达 {len(offline_msgs)} 条离线私信")
            except Exception as e:
                print(f"[私信] 送达离线私信失败: {e}")

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





        elif mtype == 'request_shop_goods' and player:
            # 客户端主动拉取商店商品（不依赖广播）
            try:
                _goods = self.window_ref_shop_goods()
                print(f"[商店] 收到商店数据请求（{player.get('name')}），"
                      f"已响应 {len(_goods)} 件商品", flush=True)
                self.send_json({'type': 'shop_goods',
                                'data': {'goods': _goods}})
            except Exception as e:
                print(f"[商店] 主动拉取失败: {e}")
                self.send_json({'type': 'shop_goods', 'data': {'goods': []}})
            return player

        elif mtype == 'request_gacha_config' and player:
            # 客户端主动拉取卡池配置（打开卡池页发现没有可下发的池时自愈用）
            try:
                _raw = load_json_safe(os.path.join(SERVER_DATA, 'gacha_config.json'), {})
                _gc = normalize_gacha_pools(dict(_raw or {}), GACHA_MIN_DEFAULTS)
                print(f"[卡池] 收到卡池配置请求（{player.get('name')}），"
                      f"已响应 {len(_gc.get('pools') or [])} 个池", flush=True)
                self.send_json({'type': 'gacha_config', 'data': {'config': _gc}})
            except Exception as e:
                print(f"[卡池] 主动拉取失败: {e}")
                self.send_json({'type': 'gacha_config', 'data': {'config': {}}})
            return player

        elif mtype == 'buy_shop_item' and player:
            # 商店统一结算：商品必须存在且上架中，价格以商店配置为准（下架/移除后一律拒绝）
            _sid = data.get('id')
            try:
                _qty = int(data.get('qty') or 1)
            except Exception:
                _qty = 1
            if _qty <= 0 or _qty > 9999:
                _qty = 1
            _goods = []
            try:
                if os.path.exists(SHOP_CONFIG_FILE):
                    with open(SHOP_CONFIG_FILE, 'r', encoding='utf-8') as _f:
                        _goods = (json.load(_f) or {}).get('goods') or []
            except Exception:
                _goods = []
            _entry = next((g for g in _goods
                           if isinstance(g, dict) and g.get('id') == _sid), None)
            if _entry is None:
                self.send_json({'type': 'buy_shop_result',
                                'data': {'success': False, 'reason': '该商品已下架或移除'}})
                return player
            if not _entry.get('enabled'):
                self.send_json({'type': 'buy_shop_result',
                                'data': {'success': False, 'reason': '该商品已下架'}})
                return player
            try:
                _price = max(0, int(_entry.get('price') or 0))
            except Exception:
                _price = 0
            _cur = _entry.get('currency') or 'credit'
            _total = _price * _qty
            with player_file_lock:
                _inv = player['inventory']
                if _inv.get(_cur, 0) < _total:
                    self.send_json({'type': 'buy_shop_result',
                                    'data': {'success': False, 'reason': '余额不足'}})
                    return player
                _inv[_cur] = _inv.get(_cur, 0) - _total
                _itype = _entry.get('item_type') or ('custom' if not _entry.get('builtin')
                                                     else _entry['id'])
                if _itype == 'cake' or _sid in ('cake', 'balance'):
                    _inv['balance'] = _inv.get('balance', 0) + _qty
                elif _itype == 'star_rail_ticket' or _sid in ('star_rail_ticket', 'ticket'):
                    _inv['star_rail_tickets'] = _inv.get('star_rail_tickets', 0) + _qty
                elif _itype == 'clothes':
                    _owned = _inv.setdefault('owned_clothes', ['normal'])
                    if _sid not in _owned:
                        _owned.append(_sid)
                else:
                    _ci = _inv.setdefault('custom_items', {})
                    _ci[_sid] = _ci.get(_sid, 0) + _qty
                save_player_inventory(player['server_uid'], _inv)
                self.send_json({'type': 'buy_shop_result',
                                'data': {'success': True, 'id': _sid, 'qty': _qty,
                                         'name': _entry.get('name') or _sid,
                                         'inventory': _inv}})
            return player

        elif mtype == 'buy_custom_item' and player:

            item_id = data.get('id')

            try:
                qty = int(data.get('qty') or 1)
            except Exception:
                qty = 1

            if qty <= 0:
                qty = 1

            _items = self.server.load_custom_items() if hasattr(self.server, 'load_custom_items') else []
            _info = next((it for it in _items if it.get('id') == item_id), None)
            if not _info or not _info.get('shop'):
                self.send_json({'type': 'buy_item_result', 'data': {'success': False, 'reason': '该物品未上架'}})
                return player

            try:
                _price = max(0, int(_info.get('price') or 0))
            except Exception:
                _price = 0
            _cur = _info.get('currency') or 'credit'
            # 商店管理里设置的价格/货币为权威（管理员改价立即生效）
            try:
                if os.path.exists(SHOP_CONFIG_FILE):
                    with open(SHOP_CONFIG_FILE, 'r', encoding='utf-8') as _f:
                        _sc = json.load(_f) or {}
                    _g = next((x for x in (_sc.get('goods') or [])
                               if isinstance(x, dict) and x.get('id') == item_id), None)
                    if _g is not None:
                        if not _g.get('enabled'):
                            self.send_json({'type': 'buy_item_result',
                                            'data': {'success': False,
                                                     'reason': '该商品已下架'}})
                            return player
                        _price = max(0, int(_g.get('price') or 0))
                        _cur = _g.get('currency') or _cur
                    else:
                        # 物品已不在商店配置里（被移除）→ 不允许交易
                        self.send_json({'type': 'buy_item_result',
                                        'data': {'success': False,
                                                 'reason': '该商品已下架或移除'}})
                        return player
            except Exception:
                pass
            _total = _price * qty

            if (_info.get('type') or 'item') == 'clothes' and \
                    item_id in (player['inventory'].get('owned_clothes') or []):
                self.send_json({'type': 'buy_item_result',
                                'data': {'success': False, 'reason': '你已拥有该时装'}})
                return player

            with player_file_lock:
                _inv = player['inventory']
                if _inv.get(_cur, 0) < _total:
                    self.send_json({'type': 'buy_item_result',
                                    'data': {'success': False, 'reason': '余额不足'}})
                    return player
                _inv[_cur] = _inv.get(_cur, 0) - _total
                if (_info.get('type') or 'item') == 'clothes':
                    _owned = _inv.setdefault('owned_clothes', ['normal'])
                    if item_id not in _owned:
                        _owned.append(item_id)
                else:
                    _ci = _inv.setdefault('custom_items', {})
                    _ci[item_id] = _ci.get(item_id, 0) + qty
                save_player_inventory(player['server_uid'], _inv)
                self.send_json({'type': 'buy_item_result',
                                'data': {'success': True, 'id': item_id, 'qty': qty,
                                         'inventory': _inv}})
            return player

        elif mtype == 'exchange' and player:

            # 兑换响应里要带"当前卡池自己的保底"：先取一次保底字段名。
            # （此前这一步漏了 → UnboundLocalError: local variable '_pity5_key'，
            #   兑换 1600 星琼换 10 张专票时服务端抛未处理异常、客户端拿不到 exchange_result）
            try:
                _ex_cfg = self._load_gacha_config()
                _ex_pool = get_gacha_pool(_ex_cfg, None) or {}
                _pity5_key, _pity4_key, _guar_key = pool_pity_keys(_ex_pool.get('id') or 'default')
            except Exception as _ex_e:
                print(f"[兑换] 保底字段回退默认: {_ex_e}")
                _pity5_key, _pity4_key, _guar_key = pool_pity_keys('default')

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

                        # 返回当前卡池自己的保底（以前写死取默认池字段，切到 UP 池后计数会显示错）
                        'pity': pdata.get(_pity5_key, 0),

                        'guaranteed': pdata.get(_guar_key, False)

                    }

                })

            return player


        elif mtype == 'gacha_config_save' and player:
            # 页面内直接编辑卡池 → 保存（仅管理员）
            if player.get('permission') != 'admin':
                self.send_json({'type': 'gacha_config_save_result',
                                'data': {'success': False, 'reason': '只有管理员可以修改卡池'}})
                return player
            payload = data.get('config')
            if not isinstance(payload, dict):
                self.send_json({'type': 'gacha_config_save_result',
                                'data': {'success': False, 'reason': '数据格式错误'}})
                return player
            try:
                cfg = normalize_gacha_pools(payload, GACHA_MIN_DEFAULTS)
                self._save_gacha_config_file(cfg)
                self._broadcast_gacha_config(cfg)
                print(f"[卡池] 页面编辑已保存，共 {len(cfg.get('pools') or [])} 个卡池")
                self.send_json({'type': 'gacha_config_save_result',
                                'data': {'success': True, 'config': cfg}})
            except Exception as e:
                print(f"[卡池] 页面保存失败: {e}")
                self.send_json({'type': 'gacha_config_save_result',
                                'data': {'success': False, 'reason': str(e)}})
            return player

        elif mtype == 'gacha_image_upload' and player:
            # 页面内点击图片上传（仅管理员）
            import base64 as _b64
            import re as _re
            if player.get('permission') != 'admin':
                self.send_json({'type': 'gacha_image_result',
                                'data': {'success': False, 'reason': '只有管理员可以修改卡池'}})
                return player

            def _safe_name(s, default='x'):
                s = _re.sub(r'[^0-9A-Za-z_\-]', '', str(s or ''))[:40]
                return s or default

            pool_id = _safe_name(data.get('pool_id'), 'pool1')
            kind = _safe_name(data.get('kind'), 'banner')
            menu_id = _safe_name(data.get('menu_id'), 'm11')
            raw_b64 = (data.get('b64') or '').strip()
            try:
                if ',' in raw_b64[:64]:
                    raw_b64 = raw_b64.split(',', 1)[1]
                blob = _b64.b64decode(raw_b64)
                if not blob:
                    raise ValueError('空图片')
            except Exception as e:
                self.send_json({'type': 'gacha_image_result',
                                'data': {'success': False, 'pool_id': pool_id, 'kind': kind,
                                         'reason': f'图片数据无效: {e}'}})
                return player
            if len(blob) > 8 * 1024 * 1024:
                self.send_json({'type': 'gacha_image_result',
                                'data': {'success': False, 'pool_id': pool_id, 'kind': kind,
                                         'reason': '图片过大（上限 8MB）'}})
                return player
            try:
                dest_dir = os.path.join(SERVER_DATA, 'webroot', 'gacha_images', menu_id)
                os.makedirs(dest_dir, exist_ok=True)
                fname = f'{kind}__{pool_id}.png'
                with open(os.path.join(dest_dir, fname), 'wb') as f:
                    f.write(blob)
                url = f'/gacha_images/{menu_id}/{fname}'
                # 直接写进配置：封面写 banner，其余写入 ui.<kind>
                try:
                    cfg = self._load_gacha_config()
                    pool = get_gacha_pool(cfg, pool_id)
                    if pool is not None:
                        if kind == 'banner':
                            pool['banner'] = url
                        else:
                            ui = pool.get('ui')
                            if not isinstance(ui, dict):
                                ui = {}
                                pool['ui'] = ui
                            ui[kind] = url
                        cfg['active_pool'] = pool.get('id') or cfg.get('active_pool')
                        cfg = normalize_gacha_pools(cfg, GACHA_MIN_DEFAULTS)
                        self._save_gacha_config_file(cfg)
                        self._broadcast_gacha_config(cfg)
                except Exception as e:
                    print(f"[卡池] 图片写入配置失败: {e}")
                self.send_json({'type': 'gacha_image_result',
                                'data': {'success': True, 'url': url, 'pool_id': pool_id,
                                         'kind': kind}})
                print(f"[卡池] 已上传图片 {kind}（卡池 {pool_id}）→ {url}")
            except Exception as e:
                print(f"[卡池] 图片保存失败: {e}")
                self.send_json({'type': 'gacha_image_result',
                                'data': {'success': False, 'pool_id': pool_id, 'kind': kind,
                                         'reason': str(e)}})
            return player

        elif mtype == 'gacha' and player:

            count = data.get('count', 1)
            pool_id = data.get('pool_id') or None

            config = self._load_gacha_config()
            _pool = get_gacha_pool(config, pool_id) or {}
            _pool_id = _pool.get('id') or 'default'
            _pity5_key, _pity4_key, _guar_key = pool_pity_keys(_pool_id)

            expiry = _pool.get('expiry')

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

            _cost_type = (_pool.get('cost_type') or 'star_rail_ticket')
            _cost_key = gacha_cost_key(_cost_type)
            _cost_name = gacha_cost_name(_cost_type)
            # 卡池时段校验：未开始 / 已结束都明确拒绝
            _now_dt = datetime.now(SERVER_TIMEZONE)
            _start_dt = parse_pool_time(_pool.get('start_time'))
            _end_dt = parse_pool_time(_pool.get('end_time')) or parse_pool_time(_pool.get('expiry'))
            if not _pool.get('published'):
                self.send_json({'type': 'gacha_result',
                                'data': {'success': False,
                                         'reason': '该卡池尚未公开（配置完成后勾选公开并保存）'}})
                return player
            if _start_dt and _now_dt < _start_dt:
                self.send_json({'type': 'gacha_result',
                                'data': {'success': False,
                                         'reason': f"卡池尚未开启（{_start_dt.strftime('%Y-%m-%d %H:%M')} 开始）"}})
                return player
            if _end_dt and _now_dt >= _end_dt:
                self.send_json({'type': 'gacha_result',
                                'data': {'success': False,
                                         'reason': f"卡池已结束（{_end_dt.strftime('%Y-%m-%d %H:%M')} 结束）"}})
                return player
            try:
                _cost_per = int(_pool.get('cost_per_pull', 1) or 1)
            except Exception:
                _cost_per = 1
            try:
                _multi = int(_pool.get('multi_pull', 10) or 10)
            except Exception:
                _multi = 10
            if count not in (1, _multi):
                self.send_json({'type': 'gacha_result', 'data': {'success': False, 'reason': f'只支持单抽或 {_multi} 连'}})

                return player

            _inv_now = player.get('inventory') or {}
            if _inv_now.get(_cost_key, 0) < _cost_per * count:
                self.send_json(
                    {'type': 'gacha_result',
                     'data': {'success': False,
                              'reason': (f'{_cost_name}不足，需要 {_cost_per * count}，'
                                         f'当前 {_inv_now.get(_cost_key, 0)}')}})

                return player

            with player_file_lock:

                inv = player['inventory']

                tickets = inv.get('star_rail_tickets', 0)

                if inv.get(_cost_key, 0) < _cost_per * count:
                    self.send_json(
                        {'type': 'gacha_result',
                         'data': {'success': False,
                                  'reason': (f'{_cost_name}不足，需要 {_cost_per * count}，'
                                             f'当前 {inv.get(_cost_key, 0)}')}})

                    return player

                results = self._do_gacha(player['server_uid'], count, _pool_id)

                inv[_cost_key] = inv.get(_cost_key, 0) - _cost_per * count

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

                # 更新抽卡历史（保留30天；按玩家保存，因此天然汇总了所有卡池的记录）

                history = pdata.get('gacha_history', [])

                now = datetime.now(SERVER_TIMEZONE)

                cutoff = now - timedelta(days=30)

                def _hist_keep(h):
                    """30 天内才保留；时间无法解析的坏记录直接丢弃（不让它把抽卡流程带崩）"""
                    try:
                        t = datetime.fromisoformat(str((h or {}).get('time') or ''))
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=SERVER_TIMEZONE)
                        return t >= cutoff
                    except Exception:
                        return False

                history = [h for h in history if isinstance(h, dict) and _hist_keep(h)]

                _pool_name = _pool.get('name') or _pool_id

                _base_ms = int(now.timestamp() * 1000)

                for _idx, item in enumerate(results):
                    history.append({

                        'name': item['name'],

                        'rarity': item['rarity'],

                        # 统一成与客户端一致的本地时间格式，页面直接显示
                        'time': now.strftime('%Y-%m-%d %H:%M:%S'),

                        'isUp': item.get('isUp', False),

                        'isGuaranteed': item.get('isGuaranteed', False),

                        # 记录来自哪个卡池（页面「抽卡记录」要显示所有卡池的记录）
                        'pool': _pool_name,

                        'pool_id': _pool_id,

                        # 同一次十连里每条都唯一，便于页面去重
                        'id': '%d-%d' % (_base_ms, _idx)

                    })

                pdata['gacha_history'] = history

                save_player_data(player['server_uid'], pdata)

                extra_info = '；'.join(converted_info) if converted_info else ''

                self.send_json({

                    'type': 'gacha_result',

                    'data': {

                        'success': True,

                        'results': results,

                        'pity': pdata.get(_pity5_key, 0),

                        'guaranteed': pdata.get(_guar_key, False),

                        'pool_id': _pool_id,

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
            text = data.get('text', '').strip()
            if text.startswith('/'):
                self.handle_command(text, player)
            else:
                # ---- 已注销账号：永久全局禁言（档案已删；正常无法登录，此处兜底） ----
                if is_account_destroyed(player['server_uid']):
                    self.send_json({'type': 'system_message',
                                    'data': {'text': '❌ 你的账号已注销，无法发送消息'}})
                    return player
                # ---- 禁言检查（服内禁言 / 全局禁言，支持永久禁言） ----
                _active, _until_iso, _perm = get_mute_info(player, 'server')
                if _active:
                    if _perm:
                        msg = "你已被禁言（永久），无法发送消息"
                    else:
                        try:
                            muted_dt = datetime.fromisoformat(_until_iso)
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
                        except Exception:
                            msg = "你已被禁言"
                    self.send_json({'type': 'system_message', 'data': {'text': msg}})
                    return player
                # 清理已过期的服内禁言（永久禁言不清理）
                _pdata = load_player_data(player['server_uid'])
                if _pdata and not _pdata.get('muted_perm') and _pdata.get('muted_until'):
                    try:
                        if datetime.fromisoformat(_pdata['muted_until']) <= datetime.now(SERVER_TIMEZONE):
                            _pdata['muted_until'] = None
                            save_player_data(player['server_uid'], _pdata)
                            player['muted_until'] = None
                    except Exception:
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
                    'from_uid': player['server_uid'],
                    'from_perm': player['permission'],
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

                            next_time = last_time + timedelta(days=90)

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
            self.handle_command(data.get('command', ''), player)
            return player


        elif mtype == 'get_player_list' and player:

            now = time.time()

            with server_lock:

                lst = []

                for p in clients.values():

                    entry = {'name': p['name'], 'id': p['userid'], 'uid': p['server_uid'],
                             'permission': p.get('permission', 'user'),
                             'pm_ban': pm_ban_payload(p),
                             'mute_server': server_mute_payload(p)}

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
                reason = ("此昵称不合法"
                          if (new_name.startswith("开拓者-") and new_name != "开拓者")
                          or new_name.startswith("注销用户")
                          else "昵称不合法")
                self.send_json({'type': 'check_name_result', 'data': {'available': False, 'reason': reason}})
                return player
            # 检查在线重名（排除自己）
            with server_lock:
                if any(p['name'] == new_name and p.get('server_uid') != player['server_uid'] for p in clients.values()):
                    self.send_json({'type': 'check_name_result', 'data': {'available': False, 'reason': '昵称已占用！'}})
                    return player
            # 检查离线重名（排除自己）
            server_uid = player['server_uid']
            for filename in _player_files():
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

            new_name = data.get('new_name', '').strip()

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

            for filename in _player_files():

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
                            next_time = last_time + timedelta(days=90)

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

            mail_id = data.get('mail_id')

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

            code = data.get('code', '')

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

            if not player.get('_disconnected', False):
                player['_disconnected'] = True

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

        # ---------- 私信 ----------
        elif mtype == 'pm_send' and player:
            return self.handle_pm_send(data, player)

        # ---------- 设置好友备注 ----------
        elif mtype == 'friend_remark' and player:
            return self.handle_friend_remark(data, player)

        # ---------- 发起聊天：全部玩家名单 / 私信历史 ----------
        elif mtype == 'get_all_players' and player:
            return self.handle_get_all_players(player)

        elif mtype == 'get_pm_history' and player:
            return self.handle_get_pm_history(data, player)

        elif mtype == 'block_user' and player:
            return self.handle_block_user(data, player)

        elif mtype == 'friend_delete' and player:
            return self.handle_friend_delete(data, player)

        elif mtype == 'save_chat_prefs' and player:
            return self.handle_save_chat_prefs(data, player)
        elif mtype == 'account_destroy_request' and player:
            return self.handle_account_destroy_request(player)
        elif mtype == 'account_destroy_cancel' and player:
            return self.handle_account_destroy_cancel(player)

        # 兜底：未知消息类型也返回 player，避免已登录会话被覆写为 None（幽灵玩家占名额）
        return player

    def send_player_list_to_all(self):
        with server_lock:
            targets = list(clients.values())
        for p in targets:
            with server_lock:
                lst = []
                for q in clients.values():
                    entry = {'name': q['name'], 'id': q['userid'], 'uid': q['server_uid'],
                         'permission': q.get('permission', 'user'),
                         'pm_ban': pm_ban_payload(q),
                         'mute_server': server_mute_payload(q)}
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
                "  /mute <用户> [global] [时长]\n"
                "      禁言玩家；省略时长=永久禁言\n"
                "      默认仅服内禁言；加 global 为全局禁言（同时禁止私信）\n"
                "      时长格式同封禁（如 30D、1H、2026-12-31）\n"
                "      示例: /mute Gubwin 1D   /mute Gubwin global 7D   /mute Gubwin global\n\n"
                "  /unmute <用户> [global]\n"
                "      解除禁言；加 global 解除全局禁言（含私信封禁）\n"
                "      示例: /unmute Gubwin   /unmute Gubwin global\n\n"
                "  /function AI on|off\n"
                "      启用或禁用服务器的 AI（在线对话）功能\n\n"
                "  /function Chat on|off\n"
                "      启用或禁用服务器的多人聊天功能\n\n"
                "【玩家管理】\n"
                "  /ban <玩家> [时长]         封禁玩家（时长如 30D, 2026-12-31）\n"
                "  /unban <玩家>              解封玩家\n"
                "  /op <玩家>                 提升为管理员\n"
                "  /deop <玩家>               取消管理员\n"
                "  /resetname <用户>          重置玩家的改名冷却时间\n"
            "  /deleteaccount <用户>      注销账号（弹出确认窗口，须输入 我确认 / I confirm）\n"
            "      删除该账号全部档案与记录，其历史消息显示为 账号已注销#6位编号；在线则踢出\n\n"
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
                "  /recall <消息ID>           撤回消息（自己的2分钟内；管理员可撤回非管理员的消息）\n"
                "  私信/好友：右键玩家名字→私信；互发1条消息即成为好友（上限100），支持好友备注\n"
            "  私信禁令：被全局禁言的玩家无法收发私信，其聊天顶部会显示红色禁令横幅\n"
            "  管理员：右键用户可执行 服内禁言 / 全局禁言（及其解除），不可对管理员操作\n"
                "  /redeemstats [用户]        查看兑换码统计；控制台带用户名可查看该用户对话记录\n\n"
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

        if cmd_name == '/kick' and len(parts) == 2:
            # 1. 权限检查：仅管理员可使用
            if player['permission'] != 'admin':
                self.send_json({
                    'type': 'command_result',
                    'data': {'success': False, 'message': '只有管理员可以使用此命令'}
                })
                return

            target_spec = parts[1]
            with server_lock:
                # 2. 查找目标玩家（在线）
                target = next(
                    (p for p in clients.values() if
                     p['name'] == target_spec or
                     p['userid'] == target_spec or
                     p['server_uid'] == target_spec or
                     p.get('uid') == target_spec),
                    None
                )

            if not target:
                self.send_json({
                    'type': 'command_result',
                    'data': {'success': False, 'message': f'未找到玩家 {target_spec}'}
                })
                return

            # 3. ★ 先检查是否自己（包括自己是管理员）
            if target['server_uid'] == player['server_uid']:
                self.send_json({
                    'type': 'command_result',
                    'data': {'success': False, 'message': '无法踢出自己(包括其他管理员)'}
                })
                return

            # 4. 再检查是否为其他管理员
            if target.get('permission') == 'admin':
                self.send_json({
                    'type': 'command_result',
                    'data': {'success': False, 'message': '不能踢出管理员'}
                })
                return

            # 5. 执行踢出
            try:
                TCPHandler.send_to(target['client'], {
                    'type': 'kick',
                    'data': {'reason': '你已被管理员踢出服务器，可重新连接。'}
                })
                time.sleep(0.1)
                target['client'].close()
            except Exception as e:
                self.server.log_emitter.log_signal.emit(f"踢出玩家时出错: {e}")

            # 6. 清理全局状态
            with server_lock:
                if target['client'] in clients:
                    del clients[target['client']]
                if target['server_uid'] in player_by_server_uid:
                    del player_by_server_uid[target['server_uid']]

            # 7. 广播通知所有玩家
            self.broadcast_system(f"{target['name']} 被管理员踢出服务器")
            self.send_player_list_to_all()

            # 8. 返回成功消息
            self.send_json({
                'type': 'command_result',
                'data': {'success': True, 'message': f'已踢出玩家 {target_spec}'}
            })
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
                # 自影响防护：不能封禁自己（会影响输入者自身）
                if identifier.lower() in (str(player.get('name', '')).lower(),
                                          str(player.get('userid', '')).lower(),
                                          str(player.get('server_uid', '')).lower()):
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': '不能封禁自己（会影响你自己）'}})
                    return
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

            if target and target.get('server_uid') == player.get('server_uid'):
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '不能取消自己的管理员资格（会影响你自己）'}})
                return
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
            # 大小写不敏感地解析物品 id（自定义物品可能含大写，如 Oneiric_Shard）
            item_id = self.resolve_item_id(parts[2]) or parts[2].strip().lower()
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
                    if (item_info.get('type') or 'item') == 'clothes':
                        items.append({'type': 'clothes', 'id': item_id,
                                      'name': item_info.get('name', item_id)})
                    else:
                        items.append(
                            {'type': 'custom', 'id': item_id, 'amount': amount,
                             'name': item_info.get('name', item_id)})
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
                    name = item.get('name') or AVAILABLE_CLOTHES.get(cid, {}).get('name', cid)
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
            # 大小写不敏感解析（自定义物品可能含大写）
            item_id = self.resolve_item_id(parts[2]) or parts[2].strip().lower()
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

            # 按物品真实类型区分：内置货币/材料 | 时装（数量 0/1）| 自定义物品（数量任意）
            _builtin = ['cake', 'credit', 'ticket', 'star_rail_tickets', 'jade']
            is_fashion = False
            is_custom_item = False
            if item_id not in _builtin:
                try:
                    _ci = next((it for it in (self.server.load_custom_items() or [])
                                if it.get('id') == item_id), None)
                except Exception:
                    _ci = None
                if _ci is not None:
                    is_custom_item = (str(_ci.get('type') or 'item') != 'clothes')
                    is_fashion = not is_custom_item
                else:
                    is_fashion = True     # 内置时装
            for p in target_players:
                # 读取旧库存
                old_inv = load_player_inventory(p['server_uid'])
                new_inv = old_inv.copy()

                if is_custom_item:
                    # 自定义物品：直接设定数量（0 = 清空）
                    _items = new_inv.setdefault('custom_items', {})
                    if amount <= 0:
                        _items.pop(item_id, None)
                    else:
                        _items[item_id] = amount
                elif is_fashion:
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
            # 大小写不敏感（* 与内置名照旧；自定义物品 id 也允许清空）
            _raw_spec = parts[2].strip()
            item_spec = '*' if _raw_spec == '*' else (self.resolve_item_id(_raw_spec) or _raw_spec.lower())

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

        # ---------- /mute 禁言用户（/mute <用户> [global] [时长]，省略时长=永久） ----------
        if cmd_name == '/mute' or cmd_name == 'mute':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result',
                                'data': {'success': False,
                                         'message': '格式: /mute <用户> [global] [时长]（省略时长为永久）'}})
                return
            target_spec = parts[1]
            rest = parts[2:]
            scope = 'server'
            if rest and rest[0].lower() == 'global':
                scope = 'global'
                rest = rest[1:]
            duration_str = ' '.join(rest).strip()
            if duration_str:
                delta, until_dt = parse_time_spec(duration_str)
                if until_dt:
                    if until_dt <= datetime.now(SERVER_TIMEZONE):
                        self.send_json({'type': 'command_result',
                                        'data': {'success': False, 'message': '指定的解禁时间已过期'}})
                        return
                    mute_until = until_dt.isoformat()
                elif delta:
                    mute_until = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
                else:
                    self.send_json({'type': 'command_result',
                                    'data': {'success': False, 'message': '无效的时间格式'}})
                    return
                mute_perm = False
                time_str = format_mute_until(datetime.fromisoformat(mute_until))
            else:
                mute_until = None
                mute_perm = True
                time_str = '永久'
            ok, msg, name, online = apply_mute_state(target_spec, scope, mute_until, mute_perm)
            if not ok:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': msg}})
                return
            label = '全局禁言（含私信）' if scope == 'global' else '禁言'
            self.broadcast_system(f"{name} 已被{label}，解除时间：{time_str}")
            self.send_json({'type': 'command_result', 'data': {
                'success': True, 'message': f'已对 {name} 执行{label}，解除时间：{time_str}'}})
            return

        # ---------- /unmute 解除禁言（/unmute <用户> [global]） ----------
        if cmd_name == '/unmute' or cmd_name == 'unmute':
            if player['permission'] != 'admin':
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '格式: /unmute <用户> [global]'}})
                return
            target_spec = parts[1]
            scope = 'global' if len(parts) >= 3 and parts[2].lower() == 'global' else 'server'
            ok, msg, name, online = clear_mute_state(target_spec, scope)
            if not ok:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': msg}})
                return
            label = '全局禁言' if scope == 'global' else '禁言'
            self.broadcast_system(f"{name} 已被解除{label}")
            self.send_json({'type': 'command_result', 'data': {
                'success': True, 'message': f'已解除 {name} 的{label}'}})
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

            # 先查服内聊天历史，再查私信配对历史（私信撤回）
            history = load_chat_history()
            target_msg = None
            is_pm = False
            pm_pair_path = None
            for msg in history:
                if msg.get('msg_id') == msg_id:
                    target_msg = msg
                    break

            if not target_msg:
                target_msg, pm_pair_path = self._find_pm_by_id(msg_id)
                if target_msg is not None:
                    is_pm = True

            if not target_msg:
                self.send_json(
                    {'type': 'command_result', 'data': {'success': False, 'message': '消息不存在或已被撤回'}})
                return

            # ---------- 权限检查 ----------
            # 作者判定：优先 uid，兼容旧消息按名字
            author_uid = target_msg.get('from_uid')
            if author_uid:
                is_self_msg = (author_uid == player['server_uid'])
            else:
                is_self_msg = (target_msg.get('from_name') == player['name'])
            # 作者权限：以**当前档案**权限为准（而不是发送时记录的 from_perm）——
            # 例如某人在管理员期间发的消息、其后被降为普通用户，其他管理员仍可撤回。
            author_perm = None
            try:
                if author_uid:
                    _apdata = load_player_data(author_uid)
                    if _apdata.get('uid'):
                        author_perm = _apdata.get('permission')
                if not author_perm:
                    _asu, _apdata2 = self.find_player_file_by_identifier(target_msg.get('from_name', ''))
                    author_perm = (_apdata2 or {}).get('permission')
            except Exception:
                author_perm = None
            if not author_perm:
                author_perm = target_msg.get('from_perm') or 'user'

            if is_self_msg:
                # 自己的消息撤回时限：
                #   私信（好友/陌生人）：一律 2 分钟（管理员也受限）
                #   服内聊天：普通用户 5 分钟，管理员不限时
                limit = None
                over_text = ''
                if is_pm:
                    limit, over_text = 120, '超过2分钟，无法撤回'
                elif player['permission'] != 'admin':
                    limit, over_text = 300, '超过5分钟，无法撤回'
                if limit is not None:
                    try:
                        msg_time = datetime.strptime(target_msg['time'], '%Y-%m-%d %H:%M:%S')
                        msg_time = SERVER_TIMEZONE.localize(msg_time)
                        if (datetime.now(SERVER_TIMEZONE) - msg_time).total_seconds() > limit:
                            self.send_json(
                                {'type': 'command_result', 'data': {'success': False, 'message': over_text}})
                            return
                    except Exception as e:
                        self.send_json(
                            {'type': 'command_result', 'data': {'success': False, 'message': f'时间解析失败: {e}'}})
                        return
            else:
                # 他人的消息：私聊（好友/陌生人）任何人都不能撤回；
                # 服内聊天仅管理员可撤回（含其他管理员的消息）
                if is_pm:
                    self.send_json(
                        {'type': 'command_result',
                         'data': {'success': False, 'message': '私聊中无法撤回他人的消息'}})
                    return
                if player['permission'] != 'admin':
                    self.send_json(
                        {'type': 'command_result', 'data': {'success': False, 'message': '无权撤回他人消息'}})
                    return
                # 管理员可撤回任何时段的他人消息（含当前管理员；author_perm 按当前
                # 档案解析，所以"管理员期间发送、之后被降级"的消息也能正常撤回）

            # 撤回提示文案：管理员撤回**他人**的消息时标注 [管理员] 与原作者
            author_name = target_msg.get('from_name') or '对方'
            if is_self_msg or player['permission'] != 'admin':
                recall_notice = f"{player['name']}撤回了一条消息"
            else:
                recall_notice = f"{player['name']}[管理员]撤回了{author_name}一条消息"

            if is_pm:
                # 私信撤回：从配对历史中移除该条，并逐个通知在线的双方
                with pm_history_lock:
                    pm_history = load_json_safe(pm_pair_path, [])
                    if isinstance(pm_history, list):
                        pm_history = [m for m in pm_history
                                      if not (isinstance(m, dict) and m.get('id') == msg_id)]
                        save_json_atomic(pm_pair_path, pm_history)
                for uid in os.path.basename(pm_pair_path)[:-5].split('__'):
                    with server_lock:
                        tp = player_by_server_uid.get(uid)
                    if tp is not None:
                        self.send_to(tp['client'], {'type': 'recall_notification',
                                                    'data': {'msg_id': msg_id,
                                                             'sender': player['name'],
                                                             'text': recall_notice}})
            else:
                # 服内聊天：标记已撤回并广播
                target_msg['revoked'] = True
                with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)

                # 广播撤回通知（包含撤回者名字）
                self.broadcast({
                    'type': 'recall_notification',
                    'data': {
                        'msg_id': msg_id,
                        'sender': player['name'],  # 撤回者的名字
                        'text': recall_notice      # 服务端计算好的提示文案
                    }
                })

            self.send_json(
                {'type': 'command_result', 'data': {'success': True, 'message': '撤回成功'}})
            return

        # ---------- 未知命令 ----------
        self.send_json({'type': 'command_result',
                        'data': {'success': False, 'message': f'未知指令 {cmd_name} 或该命令未完全实现'}})

    def resolve_item_id(self, raw):
        """委托给服务器：把命令里写的物品 id 解析成规范 id（大小写不敏感）"""
        try:
            return self.server.resolve_item_id(raw)
        except Exception:
            return None

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
        for filename in _player_files():
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

    def _pm_reject(self, to_uid, local_id, reason, pm_ban=None):
        payload = {'to_uid': to_uid, 'ok': False, 'reason': reason, 'local_id': local_id}
        if pm_ban is not None:
            payload['pm_ban'] = pm_ban
        self.send_json({'type': 'pm_ack', 'data': payload})

    def handle_save_chat_prefs(self, data, player):
        """保存会话状态（特别关注/免打扰/置顶/不再显示）到玩家档案，并回发权威副本"""
        prefs_src = data.get('prefs') if isinstance(data.get('prefs'), dict) else data
        prefs = sanitize_chat_prefs(prefs_src)
        with player_file_lock:
            pdata = load_player_data(player['server_uid'])
            pdata['chat_prefs'] = prefs
            save_player_data(player['server_uid'], pdata)
        self.send_json({'type': 'chat_prefs_data', 'data': {'chat_prefs': prefs}})
        return player

    def handle_account_destroy_request(self, player):
        """联机销号申请：校验背包非负、无禁言/封禁惩罚 → 进入 7 天注销冷却期"""
        server_uid = player['server_uid']
        if is_account_destroyed(server_uid):
            self.send_json({'type': 'account_destroy_status',
                            'data': {'state': 'already_destroyed'}})
            return player
        pdata = load_player_data(server_uid) or {}
        # 已有冷却期或正在处理：重复请求提示"此请求正在被处理"
        cur = pdata.get('destroy_pending_until')
        if cur:
            try:
                cdt = datetime.fromisoformat(cur)
                if cdt.tzinfo is None:
                    cdt = SERVER_TIMEZONE.localize(cdt)
                if datetime.now(SERVER_TIMEZONE) < cdt:
                    self.send_json({'type': 'account_destroy_status',
                                    'data': {'state': 'processing',
                                             'reason': '此请求正在被处理'}})
                    return player
            except Exception:
                pass
        if destroy_in_progress(server_uid):
            self.send_json({'type': 'account_destroy_status',
                            'data': {'state': 'processing', 'reason': '此请求正在被处理'}})
            return player
        # 每周最多申请 3 次
        now_dt = datetime.now(SERVER_TIMEZONE)
        cutoff = now_dt - timedelta(days=7)
        recent = []
        for r in (pdata.get('destroy_requests') or []):
            try:
                dt = datetime.fromisoformat(r)
                if dt.tzinfo is None:
                    dt = SERVER_TIMEZONE.localize(dt)
                if dt > cutoff:
                    recent.append(r)
            except Exception:
                continue
        if len(recent) >= 3:
            self.send_json({'type': 'account_destroy_status',
                            'data': {'state': 'rejected',
                                     'reason': '本周注销申请次数已达上限（每周最多 3 次）'}})
            return player
        if _account_punished(pdata):
            self.send_json({'type': 'account_destroy_status',
                            'data': {'state': 'rejected',
                                     'reason': '存在禁言/封禁等惩罚，无法注销账号'}})
            return player
        inv = load_player_inventory(server_uid) or {}
        for key, val in inv.items():
            if isinstance(val, (int, float)) and val < 0:
                self.send_json({'type': 'account_destroy_status',
                                'data': {'state': 'rejected',
                                         'reason': f'背包中 {key} 数量为负数，无法注销账号'}})
                return player
        until = (now_dt + timedelta(days=7)).isoformat()
        pdata['destroy_pending_until'] = until
        pdata['destroy_requests'] = recent + [now_dt.isoformat()]
        save_player_data(server_uid, pdata)
        self.send_json({'type': 'account_destroy_status', 'data': {'state': 'pending', 'until': until}})
        self.server.log_emitter.log_signal.emit(
            f"[销号] {player['name']} 申请注销账号，冷却期至 {until}")
        return player

    def handle_account_destroy_cancel(self, player):
        pdata = load_player_data(player['server_uid']) or {}
        pdata['destroy_pending_until'] = None
        save_player_data(player['server_uid'], pdata)
        self.send_json({'type': 'account_destroy_status', 'data': {'state': 'cancelled'}})
        self.server.log_emitter.log_signal.emit(f"[销号] {player['name']} 取消了注销申请")
        return player

    def handle_friend_delete(self, data, player):
        """删除好友：双方解除好友关系并清空相关等待状态（之后需重新互发消息才能再次成为好友）"""
        to_uid = (data.get('target_uid') or '').strip()
        if not to_uid or to_uid == player['server_uid']:
            self.send_json({'type': 'friend_delete_result',
                            'data': {'ok': False, 'target_uid': to_uid, 'message': '参数错误'}})
            return player
        with player_file_lock:
            my_pdata = load_player_data(player['server_uid'])
            friends = my_pdata.setdefault('friends', {})
            if to_uid not in friends:
                self.send_json({'type': 'friend_delete_result',
                                'data': {'ok': False, 'target_uid': to_uid, 'message': '对方不是你的好友'}})
                return player
            friends.pop(to_uid, None)
            # 清掉可能残留的"等待回复"状态，保证删除后能重新发第一条
            my_pdata.setdefault('stranger_sent', {}).pop(to_uid, None)
            save_player_data(player['server_uid'], my_pdata)
            t_pdata = load_player_data(to_uid)
            if t_pdata.get('uid'):
                t_pdata.setdefault('friends', {}).pop(player['server_uid'], None)
                t_pdata.setdefault('stranger_sent', {}).pop(player['server_uid'], None)
                save_player_data(to_uid, t_pdata)
        self.send_json({'type': 'friend_delete_result',
                        'data': {'ok': True, 'target_uid': to_uid, 'message': '已删除好友'}})
        # 双方刷新好友列表
        my_now = load_player_data(player['server_uid'])
        self.send_json({'type': 'friends_data', 'data': build_friends_data(my_now)})
        with server_lock:
            tp = player_by_server_uid.get(to_uid)
        if tp is not None:
            try:
                t_now = load_player_data(to_uid)
                self.send_to(tp['client'], {'type': 'friends_data', 'data': build_friends_data(t_now)})
                self.send_to(tp['client'], {'type': 'system_message',
                                            'data': {'text': f'{player["name"]} 解除了与你的好友关系'}})
            except Exception:
                pass
        return player

    def handle_get_all_players(self, player):
        """下发所有加入过服务器的玩家（在线+离线），用于客户端"发起聊天"选择"""
        with server_lock:
            online_uids = set(player_by_server_uid.keys())
        my_pdata = load_player_data(player['server_uid'])
        result = []
        try:
            os.makedirs(PLAYERS_DIR, exist_ok=True)
            filenames = os.listdir(PLAYERS_DIR)
        except OSError:
            filenames = []
        for filename in filenames:
            if not filename.endswith('.json'):
                continue
            if filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                continue
            server_uid = filename[:-5]
            if not server_uid or server_uid == player['server_uid']:
                continue
            pdata = load_player_data(server_uid)
            if not pdata.get('uid'):
                continue  # 未完成注册的残留档案
            if pdata.get('banned'):
                continue  # 封禁玩家不参与私信
            if is_account_destroyed(server_uid):
                continue  # 已注销账号不出现在"发起聊天"名单中
            result.append({
                'uid': server_uid,
                'name': pdata.get('name') or server_uid,
                'id': pdata.get('userid', ''),
                'online': server_uid in online_uids,
                'remark': _lookup_remark(my_pdata, server_uid),
                'permission': pdata.get('permission', 'user'),
                'pm_ban': pm_ban_payload(pdata),
                'mute_server': server_mute_payload(pdata),
            })
        result.sort(key=lambda x: (0 if x['online'] else 1, str(x['name'])))
        self.send_json({'type': 'all_players', 'data': {'players': result}})
        return player

    def handle_get_pm_history(self, data, player):
        """按需下发与某位玩家的私信历史（打开会话时调用）"""
        peer_uid = (data.get('peer_uid') or '').strip()
        if not peer_uid or peer_uid == player['server_uid']:
            self.send_json({'type': 'pm_history_data',
                            'data': {'peer_uid': peer_uid, 'messages': [], 'peer_name': ''}})
            return player
        messages = load_pm_history(player['server_uid'], peer_uid)
        summary = _lookup_player_summary(peer_uid)
        with server_lock:
            online = peer_uid in player_by_server_uid
        my_pdata = load_player_data(player['server_uid'])
        self.send_json({'type': 'pm_history_data', 'data': {
            'peer_uid': peer_uid, 'messages': messages,
            'peer_name': summary['name'], 'online': online,
            'peer_remark': _lookup_remark(my_pdata, peer_uid),
            'peer_pm_ban': _lookup_pm_ban(peer_uid),
            'peer_mute_server': _lookup_server_mute(peer_uid)}})
        return player

    def handle_block_user(self, data, player):
        """拉黑/取消拉黑：拉黑后双方都无法向对方发送私信，被拉黑方的消息不再送达"""
        to_uid = (data.get('target_uid') or '').strip()
        block = bool(data.get('block'))
        if not to_uid or to_uid == player['server_uid']:
            self.send_json({'type': 'block_result',
                            'data': {'ok': False, 'target_uid': to_uid, 'message': '参数错误'}})
            return player
        if is_account_destroyed(to_uid):
            self.send_json({'type': 'block_result',
                            'data': {'ok': False, 'target_uid': to_uid, 'message': '对方账号已注销'}})
            return player
        with player_file_lock:
            my_pdata = load_player_data(player['server_uid'])
            blocked = my_pdata.setdefault('blocked', [])
            if block:
                if to_uid not in blocked:
                    blocked.append(to_uid)
            else:
                if to_uid in blocked:
                    blocked.remove(to_uid)
            save_player_data(player['server_uid'], my_pdata)
        self.send_json({'type': 'block_result',
                        'data': {'ok': True, 'target_uid': to_uid, 'blocked': block,
                                 'list': my_pdata.get('blocked', [])}})
        return player

    def _find_pm_by_id(self, msg_id):
        """在私信配对历史中按 id 查找消息，返回 (消息, 配对文件路径)"""
        try:
            os.makedirs(PM_HISTORY_DIR, exist_ok=True)
            filenames = os.listdir(PM_HISTORY_DIR)
        except OSError:
            return None, None
        for filename in filenames:
            if not filename.endswith('.json'):
                continue
            path = os.path.join(PM_HISTORY_DIR, filename)
            with pm_history_lock:
                pm_history = load_json_safe(path, [])
            if not isinstance(pm_history, list):
                continue
            for pm_msg in pm_history:
                if isinstance(pm_msg, dict) and pm_msg.get('id') == msg_id:
                    return pm_msg, path
        return None, None

    def handle_pm_send(self, data, player):
        """私信：陌生人只能发 1 条等待回复；互发即成为好友（上限各100）"""
        to_uid = (data.get('to_uid') or '').strip()
        text = (data.get('text') or '').strip()
        local_id = data.get('local_id')
        if not to_uid or not text:
            self._pm_reject(to_uid, local_id, '消息不能为空')
            return player
        if to_uid == player['server_uid']:
            self._pm_reject(to_uid, local_id, '不能给自己发私信')
            return player
        if is_account_destroyed(to_uid):
            self._pm_reject(to_uid, local_id, '对方账号已注销，私信已关闭')
            return player
        if is_account_destroyed(player['server_uid']):
            self._pm_reject(to_uid, local_id, '你的账号已注销，无法发送私信')
            return player

        # 禁言/开关检查：全局禁言会封禁私信（服内禁言不影响私信）
        _pm_active, _pm_until, _pm_perm = get_mute_info(player, 'pm')
        if _pm_active:
            self._pm_reject(to_uid, local_id, '你已被禁止私信，无法发送',
                            pm_ban=pm_ban_payload(player))
            return player
        config = load_server_config()
        if config.get('common_mute', False) and player['permission'] != 'admin':
            self._pm_reject(to_uid, local_id, '全体禁言中，无法发送私信')
            return player
        if not config.get('chat_enabled', True):
            self._pm_reject(to_uid, local_id, '聊天功能已被管理员关闭')
            return player

        with player_file_lock:
            my_pdata = load_player_data(player['server_uid'])
            # 拉黑检查：我拉黑了对方 / 对方拉黑了我（双向发送都会失败并提示）
            if to_uid in (my_pdata.get('blocked') or []):
                self._pm_reject(to_uid, local_id, '已拉黑：你已拉黑对方，无法发送')
                return player
            target_pdata = load_player_data(to_uid)
            if not target_pdata.get('uid'):
                self._pm_reject(to_uid, local_id, '玩家不存在')
                return player
            # 对方被全局禁言（禁止私信）：系统拒收，并回传其禁令信息供客户端显示横幅
            _t_ban = pm_ban_payload(target_pdata)
            if _t_ban.get('banned'):
                self._pm_reject(to_uid, local_id, '对方已被禁止私信，无法接收你的消息',
                                pm_ban=_t_ban)
                return player
            if player['server_uid'] in (target_pdata.get('blocked') or []):
                self._pm_reject(to_uid, local_id, '已拉黑：对方已将你拉黑')
                return player
            friends = my_pdata.setdefault('friends', {})
            stranger_sent = my_pdata.setdefault('stranger_sent', {})
            if to_uid in friends:
                is_friend = True
                friend_added = False
            else:
                if to_uid in stranger_sent:
                    # 已经发过第一条，等待对方回复
                    self._pm_reject(to_uid, local_id, '已发送过消息，等待对方回复')
                    return player
                t_friends = target_pdata.setdefault('friends', {})
                t_stranger_sent = target_pdata.setdefault('stranger_sent', {})
                if player['server_uid'] in t_stranger_sent:
                    # 对方先发来过消息：互发即成为好友
                    if len(friends) >= FRIENDS_LIMIT or len(t_friends) >= FRIENDS_LIMIT:
                        self._pm_reject(to_uid, local_id, '好友数量已达上限（100）')
                        return player
                    friends[to_uid] = {'remark': ''}
                    t_friends[player['server_uid']] = {'remark': ''}
                    stranger_sent.pop(to_uid, None)
                    t_stranger_sent.pop(player['server_uid'], None)
                    save_player_data(player['server_uid'], my_pdata)
                    save_player_data(to_uid, target_pdata)
                    is_friend = True
                    friend_added = True
                else:
                    # 首次联系：仅允许发送这 1 条，等待对方回复
                    stranger_sent[to_uid] = {'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}
                    save_player_data(player['server_uid'], my_pdata)
                    is_friend = False
                    friend_added = False

        with server_lock:
            target_player = player_by_server_uid.get(to_uid)
        msg_id = str(uuid.uuid4())
        time_str = datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
        # 持久化：好友/陌生人的私信都写入双方共用历史，供日后"发起聊天"回看
        append_pm_history(player['server_uid'], player['name'], to_uid, text, time_str, msg_id,
                          player['permission'])
        self.send_json({'type': 'pm_ack', 'data': {
            'to_uid': to_uid, 'ok': True, 'msg_id': msg_id, 'time': time_str,
            'is_friend': is_friend, 'friend_added': friend_added, 'text': text,
            'delivered': 'online' if target_player is not None else 'offline',
            'local_id': local_id}})
        pm_recv_payload = {
            'from_uid': player['server_uid'], 'from_name': player['name'],
            'text': text, 'time': time_str, 'msg_id': msg_id,
            'is_friend': is_friend, 'friend_added': friend_added}
        if target_player is not None:
            try:
                self.send_to(target_player['client'], {'type': 'pm_recv', 'data': pm_recv_payload})
            except Exception:
                pass
        else:
            # 对方不在线：进入离线收件箱，登录后送达
            queue_pm_offline(to_uid, pm_recv_payload)

        if friend_added:
            # 双方同步好友列表（对方离线时好友关系照常建立，其好友列表登录时下发）
            my_pdata_now = load_player_data(player['server_uid'])
            self.send_json({'type': 'friends_data', 'data': build_friends_data(my_pdata_now)})
            t_pdata_now = load_player_data(to_uid)
            try:
                if target_player is not None:
                    self.send_to(target_player['client'],
                                 {'type': 'friends_data', 'data': build_friends_data(t_pdata_now)})
                    self.send_to(target_player['client'], {'type': 'system_message',
                                                           'data': {'text': f'🎉 你与 {player["name"]} 已成为好友，现在可以互相发送私信了'}})
            except Exception:
                pass
            self.send_json({'type': 'system_message',
                            'data': {'text': f'🎉 你与 {target_pdata.get("name", to_uid)} 已成为好友，现在可以互相发送私信了'}})
        return player

    def handle_friend_remark(self, data, player):
        """设置好友备注（仅自己可见），最长20字"""
        to_uid = (data.get('target_uid') or '').strip()
        remark = (data.get('remark') or '').strip()[:20]
        if not to_uid:
            self.send_json({'type': 'friend_remark_result', 'data': {'ok': False, 'target_uid': to_uid, 'message': '参数错误'}})
            return player
        with player_file_lock:
            my_pdata = load_player_data(player['server_uid'])
            friends = my_pdata.setdefault('friends', {})
            if to_uid not in friends:
                self.send_json({'type': 'friend_remark_result',
                                'data': {'ok': False, 'target_uid': to_uid, 'message': '对方不是你的好友'}})
                return player
            # 备注不能和对方当前昵称相同（否则会话列表会显示成 名字(名字)）
            if remark:
                current_name = _lookup_player_summary(to_uid)['name']
                if remark == current_name:
                    self.send_json({'type': 'friend_remark_result',
                                    'data': {'ok': False, 'target_uid': to_uid,
                                             'message': '备注不能和对方的昵称相同'}})
                    return player
            friends[to_uid]['remark'] = remark
            # 备注同时写入独立存档：删除好友/成为陌生人/拉黑后依然保留
            remarks = my_pdata.setdefault('remarks', {})
            if remark:
                remarks[to_uid] = remark
            else:
                remarks.pop(to_uid, None)
            save_player_data(player['server_uid'], my_pdata)
        self.send_json({'type': 'friend_remark_result',
                        'data': {'ok': True, 'target_uid': to_uid, 'remark': remark, 'message': '备注已保存'}})
        return player

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
        if sender_player is not None:
            msg_base['data']['from_uid'] = sender_player.get('server_uid')
            msg_base['data']['from_perm'] = sender_player.get('permission', 'user')
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
    daemon_threads = True  # 未登录/卡住的连接线程随主进程退出，不阻塞 server_close()
    def __init__(self, server_address, RequestHandlerClass, log_emitter):
        self.log_emitter = log_emitter
        super().__init__(server_address, RequestHandlerClass)
        self.custom_menu_file = CUSTOM_MENU_FILE
        self.custom_items_file = CUSTOM_ITEMS_FILE
        self.last_activity = {}

    def load_custom_menu(self):
        data = []
        if os.path.exists(self.custom_menu_file):
            try:
                with open(self.custom_menu_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, list):
                        data = loaded
            except:
                pass

        # 抽卡菜单底层化：不再依赖服务端资源包（客户端内置基底），防止旧数据回流
        def _fix(items):
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                if it.get('type') == 'gacha':
                    it['resource_required'] = False
                _fix(it.get('children'))
        _fix(data)
        return data

    def resolve_item_id(self, raw):
        """把命令里写的物品 id 解析成规范 id（大小写不敏感；解析不了返回 None）。

        以前 /give、/set 等先 `parts[2].lower()` 再精确匹配 → 自定义物品 id 只要含大写字母
        （例如 Oneiric_Shard）就永远提示"未知物品ID"。现在：精确匹配优先，其次忽略大小写，
        内置名与别名（ticket / star_rail_ticket / ruyi 等）照旧。
        """
        if not raw:
            return None
        try:
            txt = str(raw).strip()
        except Exception:
            return None
        if not txt:
            return None
        low = txt.lower()
        alias = {'ruyi': '26_Newyear', 'ticket': 'ticket',
                 'star_rail_ticket': 'ticket', 'star_rail_tickets': 'star_rail_tickets',
                 'cake': 'cake', 'credit': 'credit', 'jade': 'jade', 'none': 'none'}
        try:
            _ci = self.load_custom_items()
        except Exception:
            _ci = []
        for it in (_ci or []):
            if it.get('id') == txt:
                return txt
        if low in alias:
            return alias[low]
        try:
            for cid in AVAILABLE_CLOTHES:
                if cid.lower() == low:
                    return cid
        except Exception:
            pass
        for it in (_ci or []):
            if str(it.get('id') or '').lower() == low:
                return it.get('id')
        return None

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


GACHA_MIN_DEFAULTS = {
    'name': '新卡池', 'description': '', 'icon': '✦',
    'banner': 'img/pool_banner.png', 'cost_type': 'star_rail_ticket',
    'cost_per_pull': 1, 'multi_pull': 10, 'expiry': None,
    'rules': {'base_5star_rate': 0.006, 'base_4star_rate': 0.051,
              'pity_hard_5star': 80, 'pity_hard_4star': 10,
              'guarantee_up_5star': True},
    'five_star_items': [{'id': '26_Newyear', 'weight': 50, 'amount': 1, 'up': True}],
    'four_star_items': [{'id': 'jade', 'weight': 50, 'amount': 10}],
    'three_star_items': [{'id': 'credit', 'weight': 70, 'amount': 100},
                         {'id': 'none', 'weight': 70, 'amount': 0}],
}


def normalize_gacha_pools(cfg, defaults):
    """把卡池配置规范为 cfg['pools']（列表）。没有 pools 时把顶层字段视为单个卡池，
    保证旧配置与旧代码路径继续可用。返回同一个 cfg。"""
    def _mk():
        p = {k: defaults.get(k) for k in (
            'name', 'description', 'icon', 'banner', 'cost_type',
            'cost_per_pull', 'multi_pull', 'expiry')}
        p['rules'] = dict(defaults.get('rules') or {})
        p['five_star_items'] = list(defaults.get('five_star_items') or [])
        p['four_star_items'] = list(defaults.get('four_star_items') or [])
        p['three_star_items'] = list(defaults.get('three_star_items') or [])
        p['id'] = 'default'
        p['menu_id'] = ''
        return p

    if not isinstance(cfg, dict):
        cfg = {}
    pools = cfg.get('pools')
    if not isinstance(pools, list) or not pools:
        p = _mk()
        for k in list(p.keys()):
            if k in ('id', 'menu_id'):
                continue
            if cfg.get(k) is not None:
                p[k] = cfg[k]
        if isinstance(cfg.get('rules'), dict):
            p['rules'] = dict(cfg['rules'])
        p['id'] = str(cfg.get('id') or 'default')
        p['menu_id'] = str(cfg.get('menu_id') or '')
        pools = [p]

    out, used = [], set()
    for i, raw in enumerate(pools):
        if not isinstance(raw, dict):
            continue
        p = _mk()
        p.update({k: v for k, v in raw.items() if v is not None})
        p['rules'] = dict(p.get('rules') or {})
        for rk, rv in (defaults.get('rules') or {}).items():
            p['rules'].setdefault(rk, rv)
        for pk in ('five_star_items', 'four_star_items', 'three_star_items'):
            if not isinstance(p.get(pk), list):
                p[pk] = list(defaults.get(pk) or [])
        try:
            p['cost_per_pull'] = max(1, int(p.get('cost_per_pull') or 1))
        except Exception:
            p['cost_per_pull'] = 1
        try:
            p['multi_pull'] = max(2, int(p.get('multi_pull') or 10))
        except Exception:
            p['multi_pull'] = 10
        pid = str(p.get('id') or f'pool{i + 1}')
        while pid in used:
            pid += '_'
        used.add(pid)
        p['id'] = pid
        p['menu_id'] = str(p.get('menu_id') or '')
        # 公开条件：已上传卡池封面与侧边封面（必须是真实上传的资源），且配置了抽卡道具
        _has_banner = str(p.get('banner') or '').startswith('/gacha_images/')
        _has_items = bool(p.get('five_star_items') or p.get('four_star_items')
                          or p.get('three_star_items'))
        if p.get('published') and not (_has_banner and _has_items):
            print(f"[卡池] {pid} 公开条件不足"
                  f"（封面={_has_banner} 道具={_has_items}），保持未公开")
            p['published'] = False
        # 卡池时段：空值统一为 None；有效期支持「永久 / 30D / 2h / 2026-12-31 / ISO」等写法，
        # 相对写法统一换算成绝对 end_time —— 页面倒计时才能正常显示（否则"30D"会一直显示"永久"）
        for _tk in ('start_time', 'end_time'):
            _v = p.get(_tk)
            if isinstance(_v, str) and not _v.strip():
                p[_tk] = None
        _res_end = resolve_pool_time(p.get('end_time')) or resolve_pool_time(p.get('expiry'))
        if _res_end:
            p['end_time'] = _res_end
            p['expiry'] = _res_end
        _res_start = resolve_pool_time(p.get('start_time'))
        if _res_start:
            p['start_time'] = _res_start
        out.append(p)
    if not out:
        out = [_mk()]
    cfg['pools'] = out
    if cfg.get('active_pool') not in [p['id'] for p in out]:
        cfg['active_pool'] = out[0]['id']
    # 顶层镜像当前卡池，兼容仍读顶层字段的旧路径
    active = next((p for p in out if p['id'] == cfg['active_pool']), out[0])
    for k in ('name', 'description', 'icon', 'banner', 'side_banner', 'published',
              'color', 'font', 'cost_type',
              'cost_per_pull', 'multi_pull', 'expiry', 'start_time', 'end_time', 'rules',
              'five_star_items', 'four_star_items', 'three_star_items'):
        if active.get(k) is not None:
            cfg[k] = active[k]
    return cfg


def get_gacha_pool(cfg, pool_id=None):
    """按 id 取卡池；id 缺失/无效时退回 active_pool，再退回第一个。"""
    pools = (cfg or {}).get('pools') or []
    if not pools:
        return None
    if pool_id:
        for p in pools:
            if p.get('id') == pool_id:
                return p
    for p in pools:
        if p.get('id') == (cfg or {}).get('active_pool'):
            return p
    return pools[0]


def gacha_cost_key(cost_type):
    """卡池消耗货币 → 库存字段名。
    注意：专票在库存里是复数 star_rail_tickets，而配置里常写物品 ID star_rail_ticket，
    两者不一致会导致"物品充足却提示不足"。"""
    k = str(cost_type or 'star_rail_ticket').strip()
    _alias = {
        'star_rail_ticket': 'star_rail_tickets',
        'star_rail_tickets': 'star_rail_tickets',
        'ticket': 'star_rail_tickets',
        'tickets': 'star_rail_tickets',
        '专票': 'star_rail_tickets',
        '星铁专票': 'star_rail_tickets',
        'jade': 'jade',
        '星琼': 'jade',
        'credit': 'credit',
        '信用点': 'credit',
    }
    return _alias.get(k, k)


def gacha_cost_name(cost_type):
    """消耗货币的中文名（用于提示文案）"""
    k = str(cost_type or 'star_rail_ticket').strip()
    _names = {
        'star_rail_ticket': '专票', 'star_rail_tickets': '专票',
        'ticket': '专票', 'tickets': '专票', '专票': '专票', '星铁专票': '专票',
        'jade': '星琼', '星琼': '星琼',
        'credit': '信用点', '信用点': '信用点',
        'balance': '蛋糕', 'cake': '蛋糕',
    }
    return _names.get(k, k)


def parse_pool_time(value):
    """解析卡池时间（支持 2026-10-10T21:45 / 带时区 ISO / 日期），失败返回 None"""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in ('none', 'null', '永久', 'permanent'):
            return None
        if ' ' in value and 'T' not in value:
            value = value.replace(' ', 'T', 1)
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        try:
            dt = SERVER_TIMEZONE.localize(dt)
        except Exception:
            pass
    return dt


_REL_UNIT_MINUTES = {
    'd': 1440, 'day': 1440, 'days': 1440, '天': 1440, '日': 1440,
    'h': 60, 'hr': 60, 'hrs': 60, 'hour': 60, 'hours': 60, '小时': 60, '时': 60,
    'm': 1, 'min': 1, 'mins': 1, 'minute': 1, 'minutes': 1, '分': 1, '分钟': 1,
    's': 1.0 / 60, 'sec': 1.0 / 60, 'secs': 1.0 / 60, 'second': 1.0 / 60,
    'seconds': 1.0 / 60, '秒': 1.0 / 60,
}


def parse_relative_minutes(text):
    """解析相对时长 → 总分钟数（解析不了返回 None）。

    大小写与中文都不敏感：30D / 30d / 30天 / 2H / 2h / 2小时 / 30m / 30分钟 /
    1D12H / 1天2小时；纯数字按"天"（与 parse_time_spec 一致）。
    注意 'M'（大写）= 月（按 30 天），'m' = 分钟 —— 这一对仍然区分大小写。
    """
    if text is None:
        return None
    v = str(text).strip()
    if not v:
        return None
    if v.isdigit():
        return int(v) * 1440
    # 必须是"纯相对写法"，避免把 2026-12-31 / ISO 里的数字误当相对时长
    if not re.fullmatch(r'(?:\d+\s*[A-Za-z\u4e00-\u9fff]+\s*)+', v):
        return None
    total = 0.0
    found = False
    for num, unit in re.findall(r'(\d+)\s*([A-Za-z\u4e00-\u9fff]+)', v):
        n = int(num)
        if unit in ('M', 'mo', 'mon', 'month', 'months', '月'):
            total += n * 30 * 1440
        else:
            k = _REL_UNIT_MINUTES.get(unit)
            if k is None:
                k = _REL_UNIT_MINUTES.get(unit.lower())
            if k is None:
                return None
            total += n * k
        found = True
    return total if found else None


def resolve_pool_time(value):
    """把卡池有效期规整为可解析的时间字符串（页面倒计时用 JS Date 解析）。

    - 相对写法（30D / 30d / 30天 / 2h / 2H / 2小时 / 30m / 30分钟 / 1D12H / 纯数字=天）
      → 换算成绝对时间 '%Y-%m-%d %H:%M:%S'；
      **大小写、中文都不敏感**："30D 无效、必须写 30d" 这类差异在这里统一掉；
    - 已是绝对时间（ISO / 日期）→ 原样返回（页面两种格式都能解析）；
    - 永久 / 空 / 无法解析 → None。
    """
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v or v.lower() in ('none', 'null', '永久', 'permanent', 'forever', '不限', '无限'):
            return None
        mins = parse_relative_minutes(v)
        if mins:
            dt = datetime.now(SERVER_TIMEZONE) + timedelta(minutes=mins)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
    dt = parse_pool_time(value)
    if dt is None:
        return None
    return str(value).strip()


def pool_pity_keys(pid):
    """保底存档字段。

    - 常驻/默认卡池（default）沿用最老的字段，老玩家进度不丢；
    - **其余（UP/限定）卡池共用一套字段**：这样"旧 UP 池下架、新 UP 池上架"时
      抽数与大小保底会自然继承（与崩铁/原神限定池共保底一致），
      不会因为换了池子就从 0 重新开始。
    """
    if not pid or pid == 'default':
        return ('gacha_pity_5', 'gacha_pity_4', 'gacha_guaranteed_5')
    return ('gacha_pity_5__up', 'gacha_pity_4__up', 'gacha_guaranteed_5__up')


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

    # ---------- 商店管理 ----------
    # 商店分区固定三种：美食 / 兑换 / 更多
    SHOP_CATEGORIES = ('美食', '兑换', '更多')

    @classmethod
    def normalize_shop_category(cls, entry):
        """把商品分区规范为 美食/兑换/更多三者之一。
        已是三者之一则保留；否则按物品类型推断（蛋糕类→美食、货币/票券→兑换、其余→更多）"""
        cat = (entry or {}).get('category')
        if cat in cls.SHOP_CATEGORIES:
            return cat
        eid = (entry or {}).get('id') or ''
        itype = (entry or {}).get('item_type') or ''
        if itype == 'cake' or eid in ('cake', 'balance'):
            return '美食'
        if itype == 'star_rail_ticket' or eid in ('star_rail_ticket', 'ticket',
                                                 'jade', 'credit'):
            return '兑换'
        return '更多'

    # 默认原版商品：只有 星铁专票 + 橡木蛋糕卷（其它原版/自定义物品在管理页手动加入）
    BUILTIN_SHOP_ITEMS = [
        {'id': 'cake', 'builtin': True, 'name': '橡木蛋糕卷', 'price': 3600,
         'currency': 'credit', 'item_type': 'cake', 'enabled': True, 'emoji': '🍰',
         'image': '', 'category': '美食'},
        {'id': 'star_rail_ticket', 'builtin': True, 'name': '星铁专票', 'price': 160,
         'currency': 'jade', 'item_type': 'star_rail_ticket', 'enabled': True, 'emoji': '🎫',
         'image': '', 'category': '兑换'},
    ]

    def _load_shop_config(self):
        goods = []
        if os.path.exists(SHOP_CONFIG_FILE):
            try:
                with open(SHOP_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                if isinstance(data.get('goods'), list):
                    goods = [g for g in data['goods'] if isinstance(g, dict) and g.get('id')]
            except Exception as e:
                print(f"[商店] 读取失败: {e}")
        # 内置商品兜底（首次或被删后自动补回）
        # 过滤历史上误加的“余额”商品；其余原版物品（蛋糕/星琼/信用点/专票）与自定义物品都允许
        goods = [g for g in goods if g.get('id') != 'balance']
        # 字段兜底：emoji 空则用物品表里的图标/默认；分区空则按类型推断
        items_map = {}
        try:
            for _it in (self.window.items_data or []):
                if isinstance(_it, dict) and _it.get('id'):
                    items_map[_it['id']] = _it
        except Exception:
            pass
        for g in goods:
            if not g.get('emoji'):
                it = items_map.get(g.get('id'))
                g['emoji'] = (it or {}).get('icon') or ('🍰' if g.get('id') == 'cake' else '🎁')
            g['category'] = self.normalize_shop_category(g)
        have = {g.get('id') for g in goods}
        for b in self.BUILTIN_SHOP_ITEMS:
            if b['id'] not in have:
                goods.insert(0, dict(b))
        return goods

    def _save_shop_config(self, goods):
        os.makedirs(SERVER_DATA, exist_ok=True)
        with open(SHOP_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'goods': goods}, f, ensure_ascii=False, indent=2)

    @pyqtSlot(result=str)
    def getShopGoods(self):
        try:
            return json.dumps({'goods': self._load_shop_config()}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({'goods': [], 'error': str(e)})

    @pyqtSlot(str, result=bool)
    def saveShopGoods(self, goods_json):
        """保存整个商店商品表（内容管理「商店管理」编辑后提交）"""
        try:
            data = json.loads(goods_json or '{}')
            goods = [g for g in (data.get('goods') or []) if isinstance(g, dict) and g.get('id')]
            for g in goods:
                try:
                    g['price'] = max(0, int(g.get('price') or 0))
                except Exception:
                    g['price'] = 0
                g['currency'] = g.get('currency') or 'credit'
                g['enabled'] = bool(g.get('enabled'))
                g['name'] = g.get('name') or g['id']
                g['emoji'] = g.get('emoji') or '🎁'
                g['category'] = self.normalize_shop_category(g)
            # 过滤历史上误加的“余额”商品；原版物品与自定义物品都可上架
            goods = [g for g in goods if g.get('id') != 'balance']
            # 内置商品兜底补回
            have = {g.get('id') for g in goods}
            for b in self.BUILTIN_SHOP_ITEMS:
                if b['id'] not in have:
                    goods.insert(0, dict(b))
            self._save_shop_config(goods)
            # 即时下发（只发上架商品，与登录下发保持一致）
            try:
                if getattr(self, 'window', None) is not None:
                    self.window.broadcast_structured(
                        {'type': 'shop_goods',
                         'data': {'goods': [g for g in goods if g.get('enabled')]}})
            except Exception:
                pass
            print(f"[商店] 已保存 {len(goods)} 个商品并下发")
            return True
        except Exception as e:
            print(f"[商店] 保存失败: {e}")
            return False

    def _shop_add_item(self, item_id, name, price=0, currency='credit',
                       item_type='custom', emoji='🎁', image='', category=''):
        """物品勾选「商店物品」时自动加入商店（已存在则只补全 emoji/图片/分区，不动价格）"""
        goods = self._load_shop_config()
        exist = next((g for g in goods if g.get('id') == item_id), None)
        if exist is not None:
            if emoji and not exist.get('emoji'):
                exist['emoji'] = emoji
            if image and not exist.get('image'):
                exist['image'] = image
            if category and not exist.get('category'):
                exist['category'] = category
            self._save_shop_config(goods)
            return
        goods.append({'id': item_id, 'builtin': False, 'name': name or item_id,
                      'price': price, 'currency': currency, 'enabled': True,
                      'item_type': item_type, 'emoji': emoji or '🎁',
                      'image': image or '',
                      'category': (category if category in self.SHOP_CATEGORIES else '更多')})
        self._save_shop_config(goods)

    def _shop_remove_item(self, item_id):
        goods = self._load_shop_config()
        goods = [g for g in goods if g.get('id') != item_id]
        self._save_shop_config(goods)
        # 物品删除 → 商店里这一项也立刻消失：主动广播一次，在线玩家的商店无需重开即刷新
        try:
            if getattr(self, 'window', None) is not None:
                self.window.broadcast_structured(
                    {'type': 'shop_goods',
                     'data': {'goods': [g for g in goods if g.get('enabled')]}})
        except Exception:
            pass

    @pyqtSlot(result=str)
    def getWebBase(self):
        """管理页面用本地文件加载，图片等资源需要服务端 HTTP 绝对地址"""
        try:
            port = getattr(self.window, 'web_server_port', None)
            if not port:
                try:
                    port = load_server_config().get('web_port')
                except Exception:
                    port = None
            if not port:
                return ''
            return f'http://127.0.0.1:{int(port)}'
        except Exception as e:
            print(f'[Web基地址] 获取失败: {e}')
            return ''

    @pyqtSlot(str, str, str, str, result=str)
    def uploadPoolImage(self, menu_id, pool_id, kind, b64):
        """管理页面里直接上传卡池图片（封面 banner / 右上角物品 res1、res2）
        写入 webroot/gacha_images 并更新配置、广播热更新，返回可访问 URL（失败返回空串）"""
        import base64
        import re as _re
        try:
            def _safe(s, d='x'):
                s = _re.sub(r'[^0-9A-Za-z_\-]', '', str(s or ''))[:40]
                return s or d

            menu_id = _safe(menu_id, 'm11')
            pool_id = _safe(pool_id, 'pool1')
            kind = _safe(kind, 'banner')
            raw = (b64 or '').strip()
            if not raw:
                return ''
            if ',' in raw[:64]:
                raw = raw.split(',', 1)[1]
            blob = base64.b64decode(raw)
            if not blob:
                return ''
            if len(blob) > 8 * 1024 * 1024:
                print('[卡池图片] 图片过大（上限 8MB）')
                return ''
            dest_dir = os.path.join(SERVER_DATA, 'webroot', 'gacha_images', menu_id)
            os.makedirs(dest_dir, exist_ok=True)
            # 版本化文件名：同名资源重复上传也会生成新文件（客户端/浏览器按 URL 即时刷新）
            fname = f'{kind}__{pool_id}.png'
            try:
                for old_fn in os.listdir(dest_dir):
                    if old_fn.startswith(f'{kind}__{pool_id}.') and old_fn != fname:
                        try:
                            os.remove(os.path.join(dest_dir, old_fn))
                        except Exception:
                            pass
            except Exception:
                pass
            fname = f'{kind}__{pool_id}_{uuid.uuid4().hex[:8]}.png'
            with open(os.path.join(dest_dir, fname), 'wb') as f:
                f.write(blob)
            url = f'/gacha_images/{menu_id}/{fname}'
            cfg_path = os.path.join(SERVER_DATA, 'gacha_config.json')
            cfg = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f) or {}
            cfg = normalize_gacha_pools(cfg, GACHA_MIN_DEFAULTS)
            pool = get_gacha_pool(cfg, pool_id)
            if pool is not None:
                if kind == 'banner':
                    pool['banner'] = url
                elif kind == 'side':
                    pool['side_banner'] = url
                else:
                    ui = pool.get('ui')
                    if not isinstance(ui, dict):
                        ui = {}
                        pool['ui'] = ui
                    ui[kind] = url
                cfg = normalize_gacha_pools(cfg, GACHA_MIN_DEFAULTS)
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                try:
                    if getattr(self, 'window', None) is not None:
                        self.window.broadcast_structured(
                            {'type': 'gacha_config', 'data': {'config': cfg}})
                except Exception:
                    pass
            print(f'[卡池图片] 已上传 {kind}（卡池 {pool_id}，菜单 {menu_id}）→ {url}')
            return url
        except Exception as e:
            print(f'[卡池图片] 上传失败: {e}')
            return ''

    @pyqtSlot(str, str, result=bool)
    def uploadGachaCover(self, menu_id, b64):
        """上传卡池封面：写入资源包内的 img/pool_banner.png，并同步配置与在线客户端"""
        import base64
        import zipfile
        try:
            menu_id = (menu_id or 'm11').strip() or 'm11'
            zip_path = os.path.join(SERVER_DATA, 'webroot', 'resources', f'{menu_id}.zip')
            if not os.path.exists(zip_path):
                print(f"[卡池封面] 资源包不存在: {zip_path}")
                return False
            image = base64.b64decode(b64)
            entries = {}
            with zipfile.ZipFile(zip_path) as zf:
                for n in zf.namelist():
                    if n.endswith('/') or n == 'img/pool_banner.png':
                        continue
                    entries[n] = zf.read(n)
            entries['img/pool_banner.png'] = image
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for n, data in entries.items():
                    zf.writestr(n, data)

            cfg_path = os.path.join(SERVER_DATA, 'gacha_config.json')
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            cfg['banner'] = 'img/pool_banner.png'
            # 多卡池：把所有归属该资源包的卡池封面一起指向新图
            _hit = 0
            for _p in (cfg.get('pools') or []):
                if not isinstance(_p, dict):
                    continue
                if (_p.get('menu_id') or '') == menu_id or not (cfg.get('pools')):
                    _p['banner'] = 'img/pool_banner.png'
                    _hit += 1
            if _hit == 0 and isinstance(cfg.get('pools'), list):
                # 没有卡池声明归属该资源包：退而更新当前启用卡池
                _act = cfg.get('active_pool')
                for _p in cfg['pools']:
                    if isinstance(_p, dict) and _p.get('id') == _act:
                        _p['banner'] = 'img/pool_banner.png'
                        break
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            try:
                if getattr(self, 'window', None) is not None:
                    self.window.broadcast_structured({'type': 'gacha_config', 'data': {'config': cfg}})
                    # 资源包内容变了：通知客户端失效本地缓存并重新下载
                    self.window.broadcast_structured(
                        {'type': 'gacha_resource_updated', 'data': {'menu_id': menu_id}})
            except Exception:
                pass
            print(f"[卡池封面] 已更新资源包 {menu_id} 的封面")
            return True
        except Exception as e:
            print(f"[卡池封面] 上传失败: {e}")
            return False

    @pyqtSlot(str, result=bool)
    def saveGachaConfig(self, config_json):
        try:
            path = os.path.join(SERVER_DATA, 'gacha_config.json')
            # 规范化后再落盘：保证 pools 结构完整，坏数据不会让卡池整体失效
            _minimal_defaults = GACHA_MIN_DEFAULTS
            try:
                data = json.loads(config_json)
                if not isinstance(data, dict):
                    data = {}
            except Exception as e:
                print(f"[卡池] 保存内容不是合法 JSON: {e}")
                return False
            data = normalize_gacha_pools(data, _minimal_defaults)
            config_json = json.dumps(data, ensure_ascii=False, indent=2)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(config_json)
            # 立即广播给在线客户端：卡池封面/标题/规则改动即时生效
            try:
                _win = getattr(self, 'window', None)
                if _win is not None:
                    _win.broadcast_structured({'type': 'gacha_config',
                                               'data': {'config': json.loads(config_json)}})
            except Exception as e:
                print(f"[卡池] 广播配置失败: {e}")
            # 卡池资源包（页面模板）有实质变化时，通知客户端重新下载，
            # 否则老客户端页面不认识多卡池接口
            try:
                _win = getattr(self, 'window', None)
                _data = json.loads(config_json)
                _mids = sorted({(p.get('menu_id') or '') for p in (_data.get('pools') or [])
                                if isinstance(p, dict) and p.get('menu_id')})
                if _win is not None and _mids:
                    if not hasattr(self, '_pack_sig'):
                        self._pack_sig = {}
                    for _mid in _mids:
                        _zp = os.path.join(SERVER_DATA, 'webroot', 'resources', f'{_mid}.zip')
                        try:
                            _st = os.stat(_zp)
                            _sig = f"{int(_st.st_mtime)}:{_st.st_size}"
                        except Exception:
                            continue
                        if self._pack_sig.get(_mid) != _sig:
                            self._pack_sig[_mid] = _sig
                            _win.broadcast_structured({'type': 'gacha_resource_updated',
                                                       'data': {'menu_id': _mid}})
                            print(f"[卡池] 资源包 {_mid} 有更新，已通知客户端重下")
            except Exception as e:
                print(f"[卡池] 资源更新广播失败: {e}")
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
                # 去掉微秒：Python 的 isoformat() 会产出 6 位小数秒（...09.471783+08:00），
                # 而 JS 的 new Date() 只认最多 3 位 → 页面解析失败会一直显示"永久"
                if until_dt:
                    expiry_iso = until_dt.replace(microsecond=0).isoformat()
                elif delta:
                    expiry_iso = (datetime.now(SERVER_TIMEZONE) + delta).replace(microsecond=0).isoformat()
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

    def _broadcast_custom_items(self):
        """把最新的自定义物品表下发给所有在线客户端（无需重启即可上架）"""
        try:
            if getattr(self, 'window', None) is not None:
                self.window.broadcast_structured({
                    'type': 'update_custom_config',
                    'data': {'custom_menu': self.window.menu_data,
                             'custom_items': self.window.items_data}})
        except Exception as e:
            print(f"[custom_items] 广播失败: {e}")

    def _custom_assets_dir(self):
        d = os.path.join(SERVER_DATA, 'webroot', 'custom_items')
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _decode_b64(raw):
        raw = (raw or '').strip()
        if not raw:
            return None
        if ',' in raw[:64]:
            raw = raw.split(',', 1)[1]
        import base64
        return base64.b64decode(raw)

    @pyqtSlot(str, result=str)
    def saveCustomItemEx(self, item_json):
        """保存物品并返回失败原因（成功返回空字符串）——供管理页面弹窗提示"""
        reason = self._save_custom_item(item_json)
        return '' if reason is True else str(reason)

    @pyqtSlot(str, result=bool)
    def saveCustomItem(self, item_json):
        """保存/更新自定义物品或时装（支持商店上架、图片、四尺寸时装 GIF）"""
        return self._save_custom_item(item_json) is True

    def _save_custom_item(self, item_json):
        """内部实现：成功返回 True，失败返回原因字符串"""
        try:
            data = json.loads(item_json or '{}')
            # 物品 id 允许自定义：输入带空格默认用下划线代替
            item_id = re.sub(r'\s+', '_', (data.get('id') or '').strip()) \
                or f"custom_{uuid.uuid4().hex[:8]}"
            # 物品 id 只能首次设定：编辑已有物品时不允许改 id（删除后 id 可复用）
            orig_id = re.sub(r'\s+', '_', (data.get('orig_id') or '').strip())
            if orig_id and orig_id != item_id:
                print(f"[物品] 拒绝修改 id：{orig_id} -> {item_id}")
                return '物品 ID 不能修改（删除后该 ID 才可复用）'
            # 不可以重复 id：新建（无 orig_id）时若 id 已存在则拒绝
            if not orig_id and any(it.get('id') == item_id for it in self.window.items_data):
                print(f"[物品] 拒绝重复 id：{item_id}")
                return f'物品 ID「{item_id}」已存在，请换一个'
            # 不允许与内置物品/内置时装撞 ID（否则 /give 与穿戴逻辑会歧义）
            _reserved = {'cake', 'credit', 'jade', 'star_rail_ticket', 'ticket', 'none',
                         'normal', '26_Newyear'}
            if item_id in _reserved:
                print(f"[saveCustomItem] 拒绝保留 ID: {item_id}")
                return f'物品 ID「{item_id}」是内置保留名（如 cake/credit/jade/normal 等），请换一个'
            existing = next((it for it in self.window.items_data if it.get('id') == item_id), None)
            rec = existing if existing is not None else {}
            rec['id'] = item_id
            rec['name'] = (data.get('name') or '未命名').strip()
            rec['icon'] = (data.get('icon') or '🎁').strip()
            rec['description'] = data.get('description') or ''
            try:
                rec['count'] = int(data.get('count') or 0)
            except Exception:
                rec['count'] = 0
            rec['type'] = 'clothes' if (data.get('type') == 'clothes') else 'item'
            rec['shop'] = bool(data.get('shop'))
            # 价格/货币优先在「商店管理」里设置；这里只在显式传入时才写
            # （管理页的物品表单已不再有价格/货币输入，编辑物品不会把它清零）
            if 'price' in data:
                try:
                    # 防止负价被用来刷货币：价格钳制为 >= 0
                    rec['price'] = max(0, int(data.get('price') or 0))
                except Exception:
                    rec['price'] = 0
            elif 'price' not in rec:
                rec['price'] = 0
            if data.get('currency'):
                rec['currency'] = data['currency']
            elif 'currency' not in rec:
                rec['currency'] = 'credit'

            assets = self._custom_assets_dir()
            img = self._decode_b64(data.get('image'))
            if img is not None:
                with open(os.path.join(assets, f'{item_id}.png'), 'wb') as f:
                    f.write(img)
                rec['image'] = f'/custom_items/{item_id}.png'
                try:
                    items_dir = os.path.join(SERVER_DATA, 'webroot', 'gacha_images', 'items')
                    os.makedirs(items_dir, exist_ok=True)
                    with open(os.path.join(items_dir, f'{item_id}.png'), 'wb') as f:
                        f.write(img)
                except Exception as e:
                    print(f"[物品] 统一图片目录写入失败: {e}")

            # 时装改回普通物品：清理残留的时装资源，避免级联遗漏
            if rec['type'] != 'clothes':
                _old_dir = os.path.join(assets, 'clothes', item_id)
                if os.path.isdir(_old_dir):
                    shutil.rmtree(_old_dir, ignore_errors=True)
                rec.pop('gifs', None)

            # 「加入卡池」：必须同时上传 抽到后的展示图（png，按物品 id 命名）
            rec['gacha'] = bool(data.get('gacha'))
            gacha_img = self._decode_b64(data.get('gacha_image'))
            if gacha_img is not None:
                items_dir = os.path.join(SERVER_DATA, 'webroot', 'gacha_images', 'items')
                os.makedirs(items_dir, exist_ok=True)
                with open(os.path.join(items_dir, f'{item_id}.png'), 'wb') as f:
                    f.write(gacha_img)
                rec['gacha_image'] = f'/gacha_images/items/{item_id}.png'
            if rec.get('gacha') and not rec.get('gacha_image') and not rec.get('image'):
                print(f"[物品] {item_id} 勾选了加入卡池但缺少物品图片/展示图，取消加入")
                rec['gacha'] = False

            if rec['type'] == 'clothes':
                gifs = data.get('gifs') or {}
                cdir = os.path.join(assets, 'clothes', item_id)
                os.makedirs(cdir, exist_ok=True)
                saved = dict(rec.get('gifs') or {})
                for key in ('Standby', 'Standby_Large', 'Standby_medium', 'Standby_small'):
                    blob = self._decode_b64(gifs.get(key))
                    if blob is None:
                        continue
                    with open(os.path.join(cdir, f'{key}.gif'), 'wb') as f:
                        f.write(blob)
                    saved[key] = f'/custom_items/clothes/{item_id}/{key}.gif'
                rec['gifs'] = saved

            # 版本号：客户端据此判断本地缓存的图片/时装是否需要重新下载
            try:
                rec['ver'] = int(datetime.now().timestamp())
            except Exception:
                rec['ver'] = 0
            if existing is None:
                self.window.items_data.append(rec)
            self.window.save_files()
            # 勾选「商店物品」只表示"允许上架"：真正的上架去管理页
            # 「商店管理 → 添加商店物品」里做（在那里设置价格与货币）。
            # 这里不再自动加入商店 —— 否则候选列表里看不到它、还会以 0 价直接开卖。
            if rec.get('shop'):
                print(f"[物品] {item_id} 已标记可上架，请到「商店管理 → 添加商店物品」上架并设价")
            self._broadcast_custom_items()
            self.dataChanged.emit()
            print(f"[saveCustomItem] 已保存 {item_id} type={rec['type']} shop={rec['shop']} price={rec['price']}")
            return True
        except Exception as e:
            print(f"saveCustomItem error: {e}")
            import traceback
            traceback.print_exc()
            return f'服务端保存异常：{e}'

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
                # 删完立即落盘（allow_empty=True：删到最后一个菜单也要保存，
                # 否则重启后被删的菜单又回来了）
                try:
                    self.window.save_files(allow_empty=True)
                except Exception as _e:
                    print(f"deleteMenu 保存失败: {_e}")
                self.dataChanged.emit()
                return True
            return False
        except Exception as e:
            print(f"deleteMenu error: {e}")
            return False

    @pyqtSlot(str, result=bool)
    def deleteItem(self, item_id):
        try:
            removed = False
            for i, it in enumerate(self.window.items_data):
                if it.get('id') == item_id:
                    del self.window.items_data[i]
                    removed = True
                    break
            if not removed:
                return False
            # 级联删除：物品图片与自定义时装资源
            try:
                assets = os.path.join(SERVER_DATA, 'webroot', 'custom_items')
                png = os.path.join(assets, f'{item_id}.png')
                if os.path.exists(png):
                    os.remove(png)
                cdir = os.path.join(assets, 'clothes', item_id)
                if os.path.isdir(cdir):
                    shutil.rmtree(cdir)
                item_png = os.path.join(SERVER_DATA, 'webroot', 'gacha_images',
                                        'items', f'{item_id}.png')
                if os.path.exists(item_png):
                    os.remove(item_png)
                try:
                    self._shop_remove_item(item_id)
                except Exception:
                    pass
            except Exception as e:
                print(f"[deleteItem] 清理资源失败: {e}")
            # 级联删除：从卡池星级物品池中移除该物品/时装
            try:
                cfg_path = os.path.join(SERVER_DATA, 'gacha_config.json')
                cfg = {}
                if os.path.exists(cfg_path):
                    with open(cfg_path, 'r', encoding='utf-8') as f:
                        cfg = json.load(f) or {}
                changed = False
                for key in ('five_star_items', 'four_star_items', 'three_star_items'):
                    arr = cfg.get(key)
                    if not isinstance(arr, list):
                        continue
                    keep = []
                    for e in arr:
                        eid = e.get('id') if isinstance(e, dict) else (e if isinstance(e, str) else None)
                        if eid == item_id:
                            changed = True
                            continue
                        keep.append(e)
                    cfg[key] = keep
                if changed:
                    with open(cfg_path, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    try:
                        self.window.broadcast_structured(
                            {'type': 'gacha_config', 'data': {'config': cfg}})
                    except Exception:
                        pass
                    print(f"[deleteItem] 已从卡池移除 {item_id}")
            except Exception as e:
                print(f"[deleteItem] 卡池级联失败: {e}")
            self.window.save_files(allow_empty=True)   # 删到一件不剩也要落盘
            self._broadcast_custom_items()
            self.dataChanged.emit()
            return True
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
        self.setMinimumSize(*fit_4_3(1100, 750))
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)

        self.menu_data = []
        self.items_data = []
        self.load_all_data()

        self.bridge = ContentBridge(self)
        self.bridge.dataChanged.connect(self.on_data_changed)

        # 启用磁盘缓存
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)

        self.webview = QWebEngineView()
        try:
            _p = getattr(Qt, 'PreventContextMenu', None)
            if _p is not None:
                self.webview.page().setContextMenuPolicy(_p)
        except Exception:
            pass
        settings = self.webview.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)

        # 设置背景色避免白屏
        self.webview.page().setBackgroundColor(QColor(30, 30, 30))

        self.webview.page().javaScriptConsoleMessage = lambda level, msg, line, source: print(f"[JS] {msg}")

        # 建立 QWebChannel（提前注册）
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.webview.page().setWebChannel(self.channel)

        # 使用 setHtml 快速加载
        html_path = resource_path("html/index.html")
        if os.path.exists(html_path):
            with open(html_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            base_url = QUrl.fromLocalFile(os.path.dirname(html_path) + '/')
            self.webview.setHtml(html_content, base_url)
        else:
            self.webview.setHtml("<h1 style='color:white;'>错误：未找到 html/index.html</h1>")

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

    def broadcast_structured(self, msg):
        """转发给服务器主窗口统一广播。

        ContentBridge 里所有"保存后广播"（商店/卡池配置/封面等）都是通过
        self.window.broadcast_structured(...) 发出的；而 self.window 是本窗口，
        之前没有这个方法 → 每次都抛 AttributeError 被 except 吞掉，
        客户端永远收不到热更新（表现为"必须退出重进才刷新"）。
        """
        try:
            sw = getattr(self, 'server_window', None)
            if sw is not None and hasattr(sw, 'broadcast_structured'):
                sw.broadcast_structured(msg)
                return
        except Exception as e:
            print(f"[广播] 转发失败: {e}")
        # 兜底：直接遍历在线客户端发送
        try:
            import main as _m
            with _m.server_lock:
                for client in list(_m.clients.keys()):
                    try:
                        _m.TCPHandler.send_to(client, msg)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[广播] 兜底发送失败: {e}")

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

    def save_files(self, allow_empty=False):
        """保存菜单和物品数据到文件（无界面提示）。

        保护：内存为空而磁盘非空时**拒绝覆盖**——防止管理页面加载失败/滞后把数据清空。
        例外：删除操作传 allow_empty=True —— 删到一件不剩也必须落盘，
        否则"删掉最后一个物品/菜单"不会写盘，重启后被删的又回来了（用户实测）。
        """
        try:
            for fname, data in (('custom_menu.json', self.menu_data),
                                ('custom_items.json', self.items_data)):
                if not data and not allow_empty:
                    path = os.path.join(SERVER_DATA, fname)
                    try:
                        if os.path.exists(path):
                            with open(path, 'r', encoding='utf-8') as f:
                                old_data = json.load(f)
                            if isinstance(old_data, list) and old_data:
                                print(f"[save_files] 拒绝用空数据覆盖 {fname}"
                                      f"（磁盘现有 {len(old_data)} 项）", flush=True)
                                continue
                    except Exception:
                        pass
                self.save_json(fname, data)
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
CONSOLE_COMMAND_SPECS = [
    ('/help', False, []),
    ('/stop', False, []),
    ('/restart', False, []),
    ('/close', False, []),
    ('/fix', False, [('on', 'off')]),
    ('/change', False, [('name',), '新名称']),
    ('/ban', False, ['ban_target', 'ban_value']),
    ('/unban', False, ['ban_target']),
    ('/op', False, ['user']),
    ('/deop', False, ['user']),
    ('/kick', False, ['online_user']),
    ('/give', False, ['target', ('cake', 'credit', 'ticket', 'jade', 'ruyi'), '数量']),
    ('/set', False, ['set_target', ('cake', 'credit', 'ticket', 'jade', 'ruyi'), '数量']),
    ('/clear', False, ['target', ('cake', 'credit', '*')]),
    ('/revoke', False, ['target', '物品:数量...']),
    ('/resetname', False, ['user']),
    ('/deleteaccount', False, ['user']),
    ('/maxplayers', False, ['人数']),
    ('/access', False, [('whitelist', 'blacklist', 'none'), ('on', 'off')]),
    ('/whitelist', False, [('on', 'off', 'add', 'remove', 'list')]),
    ('/blacklist', False, [('on', 'off', 'add', 'remove', 'list')]),
    ('/function', False, [('AI', 'Chat'), ('on', 'off')]),
    ('/all_versions', False, [('True', 'False')]),
    ('/mute', False, ['user', ('server', 'global'), 'duration']),
    ('/unmute', False, ['user', ('server', 'global')]),
    ('/redeemstats', False, ['user']),
]

ARG_PLACEHOLDER_HINTS = {
    'duration': '时长（1D/12H/30m/2026-12-31，留空=永久）',
    '数量': '数量',
    '人数': '人数',
    '物品:数量...': '物品:数量&物品:数量',
    '新名称': '新名称',
    'ban_value': '时长(1D)/IP/国家代码/时区名',
    '兑换码': '兑换码',
}

class SlashCompleter(QObject):
    """Minecraft 风格命令补全：白色已输入部分 + 灰色内联提示 + Tab 补齐 + 参数候选。

    参数候选按命令需要提供（provider 返回 (is_admin, users_fn)）：
      'user'        → 全部玩家名字（在线优先）
      'online_user' → 仅在线玩家名字
      'target'      → @a / @s + 全部玩家名字
      'ban_target'  → ip / area / timezone + 全部玩家名字
      'set_target'  → newcode / @a / @s + 全部玩家名字
      元组/列表      → 固定候选（如 server|global、on|off、物品名）
      其他字符串     → 仅灰色占位提示（不可补齐，如 时长/数量）
    """

    def __init__(self, edit, provider, specs):
        super().__init__(edit)
        self.edit = edit
        self.provider = provider
        self.specs = specs
        self._sel = 0
        self._typed = ''
        self._stage = None
        self._cands = []          # [(补全token, 显示文本)]
        self._static_hint = ''
        self._just_completed = False   # 刚用 TAB 补齐过（再按 TAB 循环切换）
        self._cycle_cands = []         # 刚补齐位置的候选列表
        self._cycle_token = ''         # 刚补齐的 token
        self._in_cycle = False         # 循环替换中（避免 refresh 重算候选位置）
        self._ime_composing = False   # 输入法组合中（此时不显示提示，避免与输入法候选窗重叠）
        self.allow_at_s = True         # @目标里是否给 '@s'（本人）：服务端控制台没有本人，置 False
        self._ime_seq = 0            # 输入法事件序号（用于延迟恢复提示）
        self._hint_lbl = QLabel(edit)
        self._hint_lbl.setStyleSheet('color: rgba(255,255,255,110); background:transparent;')
        self._hint_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint_lbl.hide()
        self._popup = QListWidget(edit.window())
        self._popup.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._popup.setFocusPolicy(Qt.NoFocus)
        self._popup.setStyleSheet(
            "QListWidget { background:#2D2D2D; color:#DDDDDD; border:1px solid #555;"
            " border-radius:6px; font-size:12px; }"
            "QListWidget::item { padding:3px 8px; }"
            "QListWidget::item:selected { background:#3A5A40; color:white; }")
        self._popup.hide()
        self._popup.itemClicked.connect(self._on_item_clicked)
        edit.installEventFilter(self)
        edit.textEdited.connect(self._on_user_edit)
        edit.cursorPositionChanged.connect(self.refresh)

    def _on_user_edit(self):
        """用户手动输入：清除 TAB 循环状态"""
        self._just_completed = False
        self._cycle_cands = []
        self.refresh()

    # ---------- 解析 ----------
    def _compute(self, text):
        """解析输入，返回 (candidates, typed, stage, static_hint)"""
        if not text.startswith('/'):
            return [], '', None, ''
        ends_space = text.endswith(' ')
        # 折叠末尾多余空格：多敲一个空格不应跳到下一个参数位
        parts = (text[1:].rstrip(' ').split(' ') if ends_space else text[1:].split(' '))
        try:
            is_admin, users_fn = self.provider()
        except Exception:
            is_admin, users_fn = False, (lambda online_only=False: [])
        if len(parts) == 1 and not ends_space:
            token = parts[0]
            out = []
            for cmd, admin_only, args in self.specs:
                if admin_only and not is_admin:
                    continue
                if cmd.startswith('/' + token):
                    out.append((cmd, self._cmd_display(cmd, args)))
            return out, token, 'cmd', ''
        cmd = '/' + parts[0]
        arg_spec = None
        for c, admin_only, args in self.specs:
            if c == cmd and (not admin_only or is_admin):
                arg_spec = args
                break
        if arg_spec is None:
            return [], '', None, ''
        arg_index = len(parts) if ends_space else len(parts) - 1   # '/mute ' → 第1个参数
        typed = '' if ends_space else parts[-1]
        if arg_index < 1 or arg_index - 1 >= len(arg_spec):
            return [], '', None, ''
        arg = arg_spec[arg_index - 1]
        if arg == 'user':
            cands = users_fn(False)
        elif arg == 'online_user':
            cands = users_fn(True)
        elif arg == 'target':
            cands = (['@a', '@s'] if getattr(self, 'allow_at_s', True) else ['@a']) + users_fn(False)
        elif arg == 'ban_target':
            cands = ['ip', 'area', 'timezone'] + users_fn(False)
        elif arg == 'set_target':
            cands = (['newcode', '@a', '@s'] if getattr(self, 'allow_at_s', True)
                     else ['newcode', '@a']) + users_fn(False)
        elif isinstance(arg, (list, tuple)):
            cands = list(arg)
        else:
            return [], typed, 'arg', ARG_PLACEHOLDER_HINTS.get(arg, str(arg))
        cands = [c for c in cands if c.lower().startswith(typed.lower())]
        return [(c, c) for c in cands][:8], typed, 'arg', ''

    def _cmd_display(self, cmd, args):
        ph = []
        for a in args:
            if isinstance(a, (list, tuple)):
                ph.append('[' + '|'.join(a) + ']')
            elif a == 'user':
                ph.append('<玩家>')
            elif a == 'online_user':
                ph.append('<在线玩家>')
            elif a == 'target':
                ph.append('<@a/@s/玩家>' if getattr(self, 'allow_at_s', True) else '<@a/玩家>')
            elif a == 'ban_target':
                ph.append('<玩家|ip|area|timezone>')
            elif a == 'duration':
                ph.append('[时长]')
            elif a == 'items':
                ph.append('<物品:数量...>')
            else:
                ph.append('<%s>' % ARG_PLACEHOLDER_HINTS.get(a, a))
        return cmd + (' ' + ' '.join(ph) if ph else '')

    def _cmd_args_hint(self, cmd):
        for c, _admin_only, args in self.specs:
            if c == cmd:
                disp = self._cmd_display(cmd, args)
                return disp[len(cmd):]
        return ''

    def _cmd_first_arg_preview(self, cmd):
        """第一个参数的第一个具体候选（用于灰色内联预览，如玩家名）；不可枚举则给占位提示"""
        for c, _admin_only, args in self.specs:
            if c == cmd:
                if not args:
                    return ''
                a = args[0]
                try:
                    _is_admin, users_fn = self.provider()
                except Exception:
                    return ''
                if a == 'user':
                    cands = users_fn(False)
                elif a == 'online_user':
                    cands = users_fn(True)
                elif a == 'target':
                    cands = (['@a', '@s'] if getattr(self, 'allow_at_s', True) else ['@a']) + users_fn(False)
                elif a == 'ban_target':
                    cands = ['ip', 'area', 'timezone'] + users_fn(False)
                elif a == 'set_target':
                    cands = (['newcode', '@a', '@s'] if getattr(self, 'allow_at_s', True)
                             else ['newcode', '@a']) + users_fn(False)
                elif isinstance(a, (list, tuple)):
                    cands = list(a)
                else:
                    return ARG_PLACEHOLDER_HINTS.get(a, str(a))
                return cands[0] if cands else ''
        return ''

    # ---------- 刷新 UI ----------
    def refresh(self):
        if getattr(self, '_in_cycle', False):
            return
        if getattr(self, '_ime_composing', False):
            # 输入法还在组合：不要弹出提示/候选（等上屏后再显示）
            self._hide_all()
            return
        e = self.edit
        if not e.text().startswith('/'):
            self._hide_all()
            return
        cands, typed, stage, static_hint = self._compute(e.text())
        self._cands, self._typed, self._stage, self._static_hint = cands, typed, stage, static_hint
        if self._sel >= max(len(cands), 1):
            self._sel = 0
        self._update_hint()
        self._update_popup()

    def _hide_all(self):
        self._cands, self._stage, self._static_hint = [], None, ''
        self._hint_lbl.hide()
        self._popup.hide()

    def _update_hint(self):
        e = self.edit
        hint = ''
        if self._cands:
            if self._sel >= len(self._cands):
                self._sel = 0
            token = self._cands[self._sel][0]
            if self._stage == 'cmd':
                # 命令词补全不含斜杠：/mu → te；并预览第一个参数的第一个候选（如玩家名）
                word = token[1:]
                if word.lower().startswith(self._typed.lower()):
                    hint = word[len(self._typed):]
                preview = self._cmd_first_arg_preview(token)
                if preview:
                    hint = (hint + ' ' + preview).strip() + ' ...'
                hint = hint.strip()
            elif token.lower().startswith(self._typed.lower()):
                hint = token[len(self._typed):]
        elif self._static_hint:
            hint = ' ' + self._static_hint
        if not hint or e.cursorPosition() != len(e.text()):
            self._hint_lbl.hide()
            return
        self._hint_lbl.setText(hint)
        self._hint_lbl.setFont(e.font())
        self._hint_lbl.adjustSize()
        fm = e.fontMetrics()
        try:
            opt = QStyleOptionFrame()
            e.initStyleOption(opt)
            cr = e.style().subElementRect(QStyle.SE_LineEditContents, opt, e)
            x = cr.x() + fm.horizontalAdvance(e.text()) + 2
        except Exception:
            x = fm.horizontalAdvance(e.text()) + 8
        if x < 0 or x + self._hint_lbl.width() > e.width() - 4:
            self._hint_lbl.hide()
            return
        self._hint_lbl.move(x, (e.height() - self._hint_lbl.height()) // 2)
        self._hint_lbl.show()

    def _update_popup(self):
        e = self.edit
        if getattr(self, '_ime_composing', False):
            self._popup.hide()
            return
        if not self._cands:
            self._popup.hide()
            return
        self._popup.clear()
        for token, disp in self._cands:
            it = QListWidgetItem(disp)
            it.setData(Qt.UserRole, token)
            self._popup.addItem(it)
        if self._sel >= len(self._cands):
            self._sel = 0
        self._popup.setCurrentRow(self._sel)
        self._popup.move(e.mapToGlobal(QPoint(0, e.height() + 2)))
        self._popup.setFixedWidth(max(e.width(), 300))
        self._popup.setFixedHeight(min(len(self._cands) * 24 + 10, 190))
        if not self._popup.isVisible():
            self._popup.show()
        self._popup.raise_()

    def _safe_hide_popup(self):
        try:
            self._popup.hide()
        except Exception:
            pass

    def _on_item_clicked(self, item):
        token = item.data(Qt.UserRole)
        self._apply(token)
        self._popup.hide()

    # ---------- 补齐 ----------
    def _complete(self):
        e = self.edit
        text = e.text()
        # 连续 TAB：在刚补齐的参数位上循环切换候选（server → global → server / 用户间切换）
        tail_space = self._cycle_token + ' '
        if (self._just_completed and self._cycle_cands
                and (text.endswith(tail_space) or text.endswith(self._cycle_token))):
            matches = [c for c, _ in self._cycle_cands]
            if self._cycle_token in matches and len(matches) > 1:
                idx = matches.index(self._cycle_token)
                self._sel = (idx + 1) % len(self._cycle_cands)
                new_token = self._cycle_cands[self._sel][0]
                strip_len = len(tail_space) if text.endswith(tail_space) else len(self._cycle_token)
                head = text[:len(text) - strip_len]
                keep_space = ' ' if text.endswith(tail_space) else ''
                self._in_cycle = True
                try:
                    e.setText(head + new_token + keep_space)
                    e.setCursorPosition(len(e.text()))
                finally:
                    self._in_cycle = False
                self._cycle_token = new_token
                self._just_completed = True
                # 重算到下一参数位：灰色提示/弹窗显示下一参数（如 server|global），
                # 避免刚补齐的名字又被灰色重复显示一遍（/mute 开拓者 开拓者）
                self.refresh()
                return True
        if not self._cands:
            return False
        if self._sel >= len(self._cands):
            self._sel = 0
        self._apply(self._cands[self._sel][0])
        self._popup.hide()
        return True

    def _apply(self, token):
        e = self.edit
        text = e.text()
        # 全程挂起 refresh（setText/setCursorPosition 都会触发信号重算候选）：
        # 先保住"当前参数位"的候选供 TAB 循环，再让 refresh 重算到下一参数位。
        # 注意：补齐后**不自动补空格**——空格由用户输入，作为"进入下一阶段提示"的信号
        self._in_cycle = True
        try:
            if self._stage == 'cmd':
                e.setText(token)
            elif text.endswith(' '):
                e.setText(text + token)
            else:
                e.setText(text[:len(text) - len(self._typed)] + token)
            e.setCursorPosition(len(e.text()))
        finally:
            self._in_cycle = False
        self._cycle_cands = list(self._cands)
        self._cycle_token = token
        self._just_completed = True
        self.refresh()

    # ---------- 事件 ----------
    def eventFilter(self, obj, ev):
        if obj is not self.edit:
            return False
        if ev.type() == QEvent.InputMethod:
            # 输入法：任何输入法事件都先让位 —— 部分输入法（"不显示拼音串"）在组合期间
            # preeditString() 为空，只看 preedit 会漏判，导致提示/候选窗与输入法候选窗重叠。
            self._ime_seq = getattr(self, '_ime_seq', 0) + 1
            _seq = self._ime_seq
            try:
                _pre = ev.preeditString()
            except Exception:
                _pre = ''
            self._hide_all()
            if _pre:
                self._ime_composing = True      # 组合中：保持隐藏
            else:
                self._ime_composing = False
                # 组合刚结束：稍等再恢复（避免输入法候选窗尚未关闭提示就弹回）
                def _resume(_seq=_seq):
                    try:
                        if getattr(self, '_ime_seq', 0) != _seq:
                            return          # 期间又来了输入法事件：继续让位
                        self.refresh()
                    except RuntimeError:
                        pass                # 控件已销毁
                QTimer.singleShot(180, _resume)
            return False
        if ev.type() == QEvent.FocusOut:
            # 延迟隐藏：让弹窗条目的点击先生效（带异常保护：对象可能已销毁）
            QTimer.singleShot(150, self._safe_hide_popup)
            return False
        if ev.type() != QEvent.KeyPress:
            return False
        key = ev.key()
        if key == Qt.Key_Tab:
            if self.edit.text().startswith('/'):
                # 有候选则补齐/切换；无候选（如时长占位提示）则留空——都不让焦点跳出输入框
                self._complete()
                return True
            return False
        if key in (Qt.Key_Down, Qt.Key_Up) and self._popup.isVisible() and self._cands:
            n = len(self._cands)
            self._sel = (self._sel + (1 if key == Qt.Key_Down else -1)) % n
            self._popup.setCurrentRow(self._sel)
            self._update_hint()
            return True
        if key == Qt.Key_Escape and self._popup.isVisible():
            self._popup.hide()
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._popup.hide()
        return False


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
        # 预构建内容管理窗口（隐藏）：真实页面在后台加载完毕，点击"内容管理"即时显示，
        # 修复首次打开页面空白 1-2 秒（原 about:blank 预热只暖引擎、不暖页面）
        try:
            self._content_manager = ContentManagerWindow(self)
        except Exception as e:
            self.append_log(f"[内容管理] 预构建失败，将在打开时重试: {e}")
            self._content_manager = None

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
        if hasattr(self, '_content_manager') and self._content_manager:
            if self._content_manager.isVisible():
                self._content_manager.raise_()
                self._content_manager.activateWindow()
            else:
                self._content_manager.show()
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
        _ensure_data_dirs()   # webroot/resources、gacha_images、custom_items、players 等一次建全
        resources_dir = os.path.join(web_root, 'resources')

        # 自定义 Handler，不改变当前工作目录，直接基于 web_root 提供文件
        class CustomHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                # 将目录设为 web_root，但不改变全局 os.getcwd()
                super().__init__(*args, directory=web_root, **kwargs)

            def _serve_pack(self):
                """资源包 / 清单：加密 + 签名下发。

                /resources/<menu_id>.zip       → 加密后的包（抓到也是乱码）
                /resources/<menu_id>.manifest  → {files:{路径:sha256}, sig:HMAC}
                返回 True 表示已处理（含 404）。
                """
                try:
                    import urllib.parse as _up
                    path = _up.unquote(str(self.path).split('?', 1)[0])
                    if not path.startswith('/resources/'):
                        return False
                    name = path[len('/resources/'):]
                    _secret = load_pack_secret()
                    if name.endswith('.manifest'):
                        mid = name[:-len('.manifest')].strip('/')
                        zp = os.path.join(web_root, 'resources', '%s.zip' % mid)
                        if not os.path.isfile(zp):
                            self.send_error(404)
                            return True
                        files = pack_manifest(zp)
                        body = json.dumps({'files': files,
                                           'sig': pack_manifest_sig(files, pack_key(mid, _secret))},
                                          ensure_ascii=False).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json; charset=utf-8')
                        self.send_header('Content-Length', str(len(body)))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(body)
                        return True
                    if name.endswith('.zip'):
                        mid = name[:-len('.zip')].strip('/')
                        zp = os.path.join(web_root, 'resources', '%s.zip' % mid)
                        if not os.path.isfile(zp):
                            self.send_error(404)
                            return True
                        with open(zp, 'rb') as f:
                            blob = f.read()
                        body = pack_encrypt(blob, pack_key(mid, _secret))
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(len(body)))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(body)
                        print(f"[Web] 资源包 {mid} 加密下发（{len(blob)} → {len(body)} 字节）")
                        return True
                    return False
                except Exception as e:
                    print(f"[Web] 资源包加密下发失败: {e}")
                    return False

            def do_GET(self):
                # 资源包 / 清单：加密 + 签名（别人抓到是乱码）
                if self._serve_pack():
                    return
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

        # 创建服务器（允许端口复用，快速重启不再报地址占用）
        class _ReusableWebTCPServer(socketserver.TCPServer):
            allow_reuse_address = True
        self.web_server = _ReusableWebTCPServer(('0.0.0.0', port), CustomHandler)
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

    def init_ui(self):
        self.setWindowTitle(f"流萤联机服务器控制台 v{SERVER_VERSION}")
        self.setFixedSize(*fit_4_3(700, 600))
        icon_path = os.path.join(SERVER_DIR, "images", "bg.png")
        if not os.path.isfile(icon_path):
            icon_path = resource_path(os.path.join("images", "bg.png"))
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

        # Minecraft 风格命令补全：灰色内联提示 + Tab 补齐 +
        # 参数候选（按命令需要提供 在线玩家 / 全部玩家档案 / 固定选项）
        self.cmd_completer = SlashCompleter(self.cmd_edit, self._console_completer_provider,
                                            CONSOLE_COMMAND_SPECS)
        # 服务端控制台没有"本人"：/give /set /clear 等目标候选里不再给 @s
        self.cmd_completer.allow_at_s = False

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
        """控制台踢出玩家（管理员专用）"""
        if not self.server:
            self.append_log("服务器未启动，无法踢出玩家")
            return

        target = None
        with server_lock:
            # 优先从 player_by_server_uid 查找（更可靠）
            for p in player_by_server_uid.values():
                if (p.get('name', '').strip() == identifier.strip() or
                        p.get('userid') == identifier or
                        p.get('server_uid') == identifier or
                        p.get('uid') == identifier):
                    target = p
                    break
            # 若未找到，再从 clients 中查找
            if not target:
                for p in clients.values():
                    if (p.get('name', '').strip() == identifier.strip() or
                            p.get('userid') == identifier or
                            p.get('server_uid') == identifier or
                            p.get('uid') == identifier):
                        target = p
                        break

        if not target:
            self.append_log(f"未找到在线玩家 '{identifier}'")
            return

        # 禁止踢出管理员
        if target.get('permission') == 'admin':
            self.append_log(f"不能踢出管理员 '{target.get('name')}'")
            return

        client = target.get('client')
        if not client:
            self.append_log(f"目标玩家 '{target.get('name')}' 的客户端已断开")
            return

        # 发送踢出消息并关闭连接
        try:
            TCPHandler.send_to(client, {
                'type': 'kick',
                'data': {'reason': '你已被管理员踢出服务器，可重新连接。'}
            })
            time.sleep(0.1)
            client.close()
        except Exception as e:
            self.append_log(f"踢出玩家时出错: {e}")

        # 清理全局状态
        with server_lock:
            if client in clients:
                del clients[client]
            if target.get('server_uid') in player_by_server_uid:
                del player_by_server_uid[target['server_uid']]

        self.append_log(f"已踢出玩家 {target.get('name')} (UID: {target.get('server_uid')})")
        self.broadcast_to_all(f"{target.get('name')} 被管理员踢出服务器")
        self.refresh_all_lists()

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
        # 取消旧定时器，避免重启后多条定时链叠加
        old_timer = getattr(self, '_archive_timer', None)
        if old_timer is not None:
            old_timer.cancel()
            self._archive_timer = None

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
        timer = threading.Timer(delay, archive_and_broadcast)
        timer.daemon = True  # 守护线程：关闭窗口后不阻止进程退出
        timer.start()
        self._archive_timer = timer

    def stop_server(self):
        # 停掉归档定时器
        old_timer = getattr(self, '_archive_timer', None)
        if old_timer is not None:
            old_timer.cancel()
            self._archive_timer = None
        if self.server:
            with server_lock:
                to_close = list(clients.keys())
                clients.clear()
                player_by_server_uid.clear()
            for client in to_close:
                try:
                    TCPHandler.send_to(client, {'type': 'server_shutdown', 'data': {'reason': '服务器已停止'}})
                    client.close()
                except:
                    pass
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

    def _console_completer_provider(self):
        """控制台补全数据源：玩家名字 = 在线 ∪ 全部档案（缓存 10 秒）"""
        online = []
        try:
            with server_lock:
                online = [p['name'] for p in clients.values() if p.get('name')]
        except Exception:
            online = []
        online_names = sorted(set(online))

        def users(online_only=False):
            if online_only:
                return list(online_names)
            try:
                now = time.time()
                cache = getattr(self, '_users_cache', None)
                cache_at = getattr(self, '_users_cache_at', 0)
                if cache is None or now - cache_at > 10:
                    names = set()
                    if os.path.isdir(PLAYERS_DIR):
                        for fn in os.listdir(PLAYERS_DIR):
                            if (not fn.endswith('.json') or fn.endswith('_inv.json')
                                    or fn.endswith('_mails.json')):
                                continue
                            try:
                                with open(os.path.join(PLAYERS_DIR, fn), 'r', encoding='utf-8') as f:
                                    data = json.load(f)
                                n = data.get('name')
                                if n:
                                    names.add(n)
                            except Exception:
                                continue
                    self._users_cache = names
                    self._users_cache_at = now
                else:
                    names = cache
                all_names = set(online_names) | set(names)
                return sorted(all_names)
            except Exception:
                return list(online_names)

        return True, users

    def exec_redeemstats(self, parts):
        """控制台 /redeemstats [用户]：居中窗口显示该用户的对话记录（可滚轮滑动）；无参数显示全服兑换统计"""
        target = parts[1] if len(parts) >= 2 else None
        lines = []
        if target:
            server_uid, pdata = self.find_player_file_by_identifier(target)
            if not server_uid:
                self.append_log(f"未找到玩家 {target}")
                return
            name = (pdata or {}).get('name') or server_uid
            lines.append(f"=== {name}（{server_uid}）的对话记录 ===")
            lines.append("")
            # 服内聊天记录
            try:
                history = load_chat_history()
            except Exception:
                history = []
            chat_lines = []
            for m in history:
                if not isinstance(m, dict):
                    continue
                if m.get('from_uid') == server_uid or (not m.get('from_uid') and m.get('from_name') == name):
                    t = m.get('time') or m.get('timestamp') or ''
                    chat_lines.append(f"[{t}] {name}: {m.get('text', '')}")
            lines.append(f"—— 服内聊天（{len(chat_lines)} 条）——")
            lines.extend(chat_lines or ["（无）"])
            lines.append("")
            # 私信记录
            pm_lines = []
            try:
                if os.path.isdir(PM_HISTORY_DIR):
                    for fn in os.listdir(PM_HISTORY_DIR):
                        if not fn.endswith('.json') or server_uid not in fn:
                            continue
                        msgs = load_json_safe(os.path.join(PM_HISTORY_DIR, fn), [])
                        if not isinstance(msgs, list):
                            continue
                        for m in msgs:
                            if not isinstance(m, dict):
                                continue
                            if m.get('from_uid') == server_uid:
                                pm_lines.append(f"[{m.get('time', '')}] {name} → 私信: {m.get('text', '')}")
            except Exception as e:
                pm_lines.append(f"（读取私信记录失败: {e}）")
            lines.append(f"—— 私信（{len(pm_lines)} 条）——")
            lines.extend(pm_lines or ["（无）"])
        else:
            lines.append("=== 全服兑换码净获得统计 ===")
            lines.append("")
            try:
                if os.path.isdir(PLAYERS_DIR):
                    for fn in os.listdir(PLAYERS_DIR):
                        if (not fn.endswith('.json') or fn.endswith('_inv.json')
                                or fn.endswith('_mails.json')):
                            continue
                        try:
                            with open(os.path.join(PLAYERS_DIR, fn), 'r', encoding='utf-8') as f:
                                d = json.load(f)
                        except Exception:
                            continue
                        stats = d.get('redeem_stats') or {}
                        if stats:
                            total = sum(v for v in stats.values() if isinstance(v, (int, float)))
                            lines.append(f"{d.get('name') or fn[:-5]}: {total}  "
                                         + ", ".join(f"{k}={v}" for k, v in stats.items()))
            except Exception as e:
                lines.append(f"（读取失败: {e}）")
            if len(lines) == 2:
                lines.append("（暂无数据）")

        self.append_log("\n".join(lines))
        # 居中窗口（QPlainTextEdit 原生支持滚轮上下滑动）
        dlg = QDialog(self)
        dlg.setWindowTitle("对话记录" if target else "兑换码统计")
        dlg.setMinimumSize(*fit_4_3(620, 460))
        dlg.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        dlg.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QPlainTextEdit { background-color: #2D2D2D; color: #E0E0E0; border: 1px solid #555;
                             border-radius: 6px; padding: 10px; font-size: 13px; }
            QPushButton { background-color: #4CAF50; color: white; border: none;
                          border-radius: 6px; padding: 8px 20px; font-size: 14px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(lines))
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(view, 1)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=Qt.AlignCenter)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dlg.move(screen_center - dlg.rect().center())
        dlg.exec_()

    def confirm_destroy_account(self, uid, name):
        """控制台注销二次确认：居中窗口，须输入 我确认 / I confirm"""
        dlg = QDialog(self)
        dlg.setWindowTitle("确认注销账号")
        dlg.setFixedSize(470, 250)
        dlg.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        dlg.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #E0E0E0; font-size: 13px; background: transparent; }
            QLineEdit { background: #2D2D2D; color: white; border: 1px solid #555555;
                        border-radius: 6px; padding: 8px; font-size: 13px; }
            QPushButton { background-color: #B03A3A; color: white; border: none;
                          border-radius: 8px; padding: 8px 16px; font-size: 14px; }
            QPushButton:hover { background-color: #C94848; }
        """)
        layout = QVBoxLayout(dlg)
        warn = QLabel(f"确定注销 {name} 的账号？此操作不可逆！")
        warn.setStyleSheet("color:#FF6B6B; font-weight:bold; font-size:14px;")
        layout.addWidget(warn)
        hint = QLabel("输入：我确认 或 I confirm（也接受：我确定注销并了解结果）")
        hint.setStyleSheet("color:#FFD700;")
        layout.addWidget(hint)
        edit = QLineEdit()
        layout.addWidget(edit)
        row = QHBoxLayout()
        row.addStretch()
        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")
        row.addWidget(ok_btn)
        row.addWidget(cancel_btn)
        layout.addLayout(row)

        def _ok():
            if not destroy_phrase_ok(edit.text()):
                QMessageBox.warning(dlg, "确认失败", "请输入：我确认 或 I confirm")
                return
            dlg.accept()

        ok_btn.clicked.connect(_ok)
        cancel_btn.clicked.connect(dlg.reject)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dlg.move(screen_center - dlg.rect().center())
        if dlg.exec_() != QDialog.Accepted:
            self.append_log("已取消销号")
            return
        # 防重：与客户端同时请求时，后到的一方提示"此请求正在被处理"
        if not begin_destroy(uid):
            self.append_log("此请求正在被处理")
            QMessageBox.warning(self, "提示", "此请求正在被处理")
            return
        try:
            time.sleep(1.5)  # 确定后等待 1.5 秒，期间重复请求会被拦截
            destroy_id = _destroy_account_inner(uid)
            self.append_log(f"✅ 已注销账号 {name}（注销编号 {destroy_id}），相关文件与记录已清除")
            QMessageBox.information(self, "注销完成", f"{name} 的账号已注销（编号 {destroy_id}）")
        except Exception as e:
            self.append_log(f"销号失败: {e}")
        finally:
            end_destroy(uid)

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
                    # 自影响防护：回环/本机/网络地址一律禁止封禁（一定不可以封 127.0.0.1 这类）
                    if (ip == '127.0.0.1' or ip.startswith('127.') or ip in ('0.0.0.0', '::1', 'localhost')
                            or ip.endswith('.0') or ip.endswith('.255')):
                        self.append_log(f"禁止封禁 {ip}（回环/本机/网络地址，会影响服务器自身）")
                        return

                    def _peer_ip_of(sock):
                        try:
                            return sock.getpeername()[0]
                        except OSError:
                            return None
                    with server_lock:
                        admin_ips = {_peer_ip_of(p['client']) for p in clients.values()
                                     if p.get('permission') == 'admin'}
                    if ip in admin_ips:
                        self.append_log(f"禁止封禁 {ip}（该 IP 上有管理员在线，会影响管理员）")
                        return
                    ips = load_banned_ips()
                    if ip in ips:
                        self.append_log(f"IP {ip} 已在封禁列表中")
                        return
                    ips.append(ip)
                    save_banned_ips(ips)
                    self.append_log(f"已封禁IP: {ip}")
                    # 踢出所有使用该IP的非管理员玩家
                    def _peer_ip(sock):
                        try:
                            return sock.getpeername()[0]
                        except OSError:
                            return None
                    with server_lock:
                        to_kick = [p for p in clients.values() if
                                   _peer_ip(p['client']) == ip and p['permission'] != 'admin']
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
                if target.get('permission') == 'admin':
                    self.append_log("不能封禁管理员的 IP（会影响管理员）")
                    return
                try:
                    ip = target['client'].getpeername()[0]
                except OSError:
                    self.append_log(f"玩家 {user} 的连接已断开，无法获取IP")
                    return
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
            if cmd_name == '/redeemstats':
                self.exec_redeemstats(parts)
                return

            if cmd_name == '/deleteaccount' and len(parts) == 2:
                _uid, _pdata = self.find_player_file_by_identifier(parts[1])
                _name = (_pdata or {}).get('name') or _uid
                if not _uid:
                    self.append_log(f"未找到玩家 {parts[1]}")
                    return
                if _pdata.get('permission') == 'admin':
                    self.append_log("不能销毁管理员账号")
                    return
                self.confirm_destroy_account(_uid, _name)
                return
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

            # ---------- /close 关闭服务器 ----------
            if cmd_name == '/close':
                self.stop_server()
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
            "  /mute <用户> [global] [时长]\n"
            "      禁言玩家；省略时长=永久禁言\n"
            "      默认仅服内禁言；加 global 为全局禁言（同时禁止私信）\n"
            "      时长格式同封禁（如 30D、1H、2026-12-31）\n"
            "      示例: /mute Gubwin 1D   /mute Gubwin global 7D   /mute Gubwin global\n\n"
            "  /unmute <用户> [global]\n"
            "      解除禁言；加 global 解除全局禁言（含私信封禁）\n"
            "      示例: /unmute Gubwin   /unmute Gubwin global\n\n"
            "  /function AI on|off\n"
            "      启用或禁用服务器的 AI（在线对话）功能\n\n"
            "  /function Chat on|off\n"
            "      启用或禁用服务器的多人聊天功能\n\n"
            "【玩家管理】\n"
            "  /ban <玩家> [时长]         封禁玩家（时长如 30D, 2026-12-31）\n"
            "  /unban <玩家>              解封玩家\n"
            "  /op <玩家>                 提升为管理员\n"
            "  /deop <玩家>               取消管理员\n"
            "  /resetname <用户>          重置玩家的改名冷却时间\n"
            "  /deleteaccount <用户>      注销账号（弹出确认窗口，须输入 我确认 / I confirm）\n"
            "      删除该账号全部档案与记录，其历史消息显示为 账号已注销#6位编号；在线则踢出\n\n"
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
            "  /recall <消息ID>           撤回消息（自己的2分钟内；管理员可撤回非管理员的消息）\n"
            "  私信/好友：右键玩家名字→私信；互发1条消息即成为好友（上限100），支持好友备注\n"
            "  私信禁令：被全局禁言的玩家无法收发私信，其聊天顶部会显示红色禁令横幅\n"
            "  管理员：右键用户可执行 服内禁言 / 全局禁言（及其解除），不可对管理员操作\n"
            "  /redeemstats [用户]        查看兑换码统计；控制台带用户名可查看该用户对话记录\n\n"
            "💡 提示：所有管理命令仅管理员可用。输入 /help 查看本帮助。"
        )

        # 创建自定义对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("帮助")
        dialog.setMinimumSize(*fit_4_3(600, 450))  # 足够大以显示内容
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
        # 大小写不敏感解析（自定义物品可能含大写）
        item_id = self.server.resolve_item_id(parts[2]) if getattr(self, 'server', None) else None
        item_id = item_id or parts[2].strip().lower()

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
        # 时装强制数量为1（与玩家端命令一致，自定义物品不受限）
        if item_id not in ('cake', 'credit', 'ticket', 'star_rail_tickets', 'jade') and item_id in AVAILABLE_CLOTHES:
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
        # 大小写不敏感解析；同时也认自定义物品（以前只认内置名 → 自定义物品报"未知物品ID"）
        _rid = self.server.resolve_item_id(parts[2]) if getattr(self, 'server', None) else None
        item_id = _rid or parts[2].strip().lower()

        # 别名映射
        fashion_alias = {'ruyi': '26_Newyear'}
        if item_id in fashion_alias:
            item_id = fashion_alias[item_id]

        # ★★★ 物品合法性校验（内置 + 自定义物品/时装）
        def is_valid_item(iid):
            if iid in ('cake', 'credit', 'ticket', 'jade', 'star_rail_tickets'):
                return True
            if iid in AVAILABLE_CLOTHES:
                return True
            try:
                return any(it.get('id') == iid for it in (self.server.load_custom_items() or []))
            except Exception:
                return False

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

        # 按物品"真实类型"区分，不能再用"非内置即时装"（用户实测：随便给个不存在的 id 也被当成了时装）
        _builtin = ['cake', 'credit', 'ticket', 'star_rail_tickets', 'jade']
        _custom_rec = None
        if item_id not in _builtin:
            try:
                _custom_rec = next((it for it in (self.server.load_custom_items() or [])
                                    if it.get('id') == item_id), None)
            except Exception:
                _custom_rec = None
        is_custom_item = bool(_custom_rec) and (str(_custom_rec.get('type') or 'item') != 'clothes')
        is_fashion = (item_id not in _builtin) and not is_custom_item
        # 双重校验：未知 id 直接拒绝（别静默当时装塞进衣柜）
        if is_fashion and item_id not in AVAILABLE_CLOTHES and not (
                _custom_rec and str(_custom_rec.get('type')) == 'clothes'):
            self.append_log(f"未知物品ID：{item_id}（已拒绝；不再自动当成时装）")
            return
        for p in target_players:
            inv = load_player_inventory(p['server_uid'])
            if is_custom_item:
                _items = inv.setdefault('custom_items', {})
                if amount <= 0:
                    _items.pop(item_id, None)
                    self.append_log(f"已清空玩家 {p['name']} 的物品 {item_id}")
                else:
                    _items[item_id] = amount
                    self.append_log(f"已设置玩家 {p['name']} 的物品 {item_id} 为 {amount}")
            elif is_fashion:
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
                elif item_id == 'ticket' or item_id == 'star_rail_tickets':
                    inv['star_rail_tickets'] = amount
                elif item_id == 'jade':
                    inv['jade'] = amount
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
        _raw = parts[2].strip()
        item_spec = '*' if _raw == '*' else ((self.server.resolve_item_id(_raw) if getattr(self, 'server', None)
                                              else None) or _raw.lower())

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

        if len(parts) < 2:
            self.append_log("用法: /mute <用户> [global] [时长]（省略时长为永久）")
            return

        target = parts[1]
        rest = parts[2:]
        scope = 'server'
        if rest and rest[0].lower() == 'global':
            scope = 'global'
            rest = rest[1:]
        duration_str = ' '.join(rest).strip()
        if duration_str:
            delta, until_dt = parse_time_spec(duration_str)
            if until_dt:
                if until_dt <= datetime.now(SERVER_TIMEZONE):
                    self.append_log("指定的解禁时间已过期")
                    return
                mute_until = until_dt.isoformat()
            elif delta:
                mute_until = (datetime.now(SERVER_TIMEZONE) + delta).isoformat()
            else:
                self.append_log("无效的时间格式")
                return
            mute_perm = False
            time_str = format_mute_until(datetime.fromisoformat(mute_until))
        else:
            mute_until = None
            mute_perm = True
            time_str = '永久'
        ok, msg, name, online = apply_mute_state(target, scope, mute_until, mute_perm)
        if not ok:
            self.append_log(msg)
            return
        label = '全局禁言（含私信）' if scope == 'global' else '禁言'
        self.broadcast_to_all(f"{name} 已被{label}，解除时间：{time_str}")
        self.append_log(f"已对 {name} 执行{label}，解除时间：{time_str}")

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
            self.append_log("用法: /unmute <用户> [global]")
            return
        target = parts[1]
        scope = 'global' if len(parts) >= 3 and parts[2].lower() == 'global' else 'server'
        ok, msg, name, online = clear_mute_state(target, scope)
        if not ok:
            self.append_log(msg)
            return
        label = '全局禁言' if scope == 'global' else '禁言'
        self.broadcast_to_all(f"{name} 已被解除{label}")
        self.append_log(f"已解除 {name} 的{label}")

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
                entry = {'name': q['name'], 'id': q['userid'], 'uid': q['server_uid'],
                         'permission': q.get('permission', 'user'),
                         'pm_ban': pm_ban_payload(q),
                         'mute_server': server_mute_payload(q)}
                lst.append(entry)
            try:
                TCPHandler.send_to(p['client'], {'type': 'player_list', 'data': {'players': lst}})
            except: pass

    def find_player_file_by_identifier(self, identifier):
        if not os.path.isdir(PLAYERS_DIR):
            return None, None
        identifier = str(identifier).strip()
        for filename in _player_files():
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
        # 卡池配置也一起推：否则客户端热更新后仍拿不到卡池 → 卡池页面显示"无卡池"
        try:
            _cfg = None
            # 桥在 ContentManagerWindow 上（ServerWindow 上拿不到，之前一直拿 None → 漏推）
            _cm = getattr(self, '_content_manager', None)
            _bridge = getattr(_cm, 'bridge', None) if _cm is not None else None
            if _bridge is not None and hasattr(_bridge, '_load_gacha_config'):
                _cfg = _bridge._load_gacha_config()
            if not isinstance(_cfg, dict) or not _cfg:
                _cfg = load_json_safe(os.path.join(SERVER_DATA, 'gacha_config.json'), {})
            if isinstance(_cfg, dict) and _cfg:
                with server_lock:
                    for client in list(clients.keys()):
                        try:
                            TCPHandler.send_to(client, {'type': 'gacha_config',
                                                        'data': {'config': _cfg}})
                        except Exception:
                            pass
        except Exception as e:
            print(f"[热更新] 卡池配置广播失败: {e}")
        # 商店商品也一起推：这样"热更新"一键就把 菜单/物品/商店/卡池 全推给在线玩家，
        # 不需要先跑进对应的管理页（用户要求"点击之后全内容热更新"）。
        try:
            _shop = load_json_safe(os.path.join(SERVER_DATA, 'shop_config.json'), {})
            _goods = _shop.get('goods') if isinstance(_shop, dict) else None
            if isinstance(_goods, list):
                with server_lock:
                    for client in list(clients.keys()):
                        try:
                            TCPHandler.send_to(client, {'type': 'shop_goods',
                                                        'data': {'goods': _goods}})
                        except Exception:
                            pass
                print(f"[热更新] 商店商品 {len(_goods)} 件已广播")
        except Exception as e:
            print(f"[热更新] 商店配置广播失败: {e}")
        self.append_log(f"[热更新] 已广播配置给 {len(clients)} 个在线玩家")

    def closeEvent(self, event):
        self.stop_server()
        event.accept()



def cleanup_stale_mei_dirs(max_age_seconds=900):
    """清理临时目录中残留的 PyInstaller 解压目录（_MEI*）。

    onefile 每次启动解压约 700MB，正常退出由引导器自动删除；但崩溃/被杀/
    解压失败会留下残留，累积会占满系统盘，导致之后启动时解压大文件
    （Qt5WebEngineCore.dll）失败（"Failed to extract ..." 弹窗，无法启动）。
    只删除：非本进程、无任何进程引用（环境变量/加载模块/打开文件/工作目录）、
    且修改时间超过 max_age_seconds 的目录。返回删除的目录数。"""
    if not getattr(sys, 'frozen', False) or not getattr(sys, '_MEIPASS', None):
        return 0
    try:
        import psutil
    except Exception:
        return 0
    try:
        meipass = os.path.abspath(sys._MEIPASS)
        temp_root = os.path.dirname(meipass)
        temp_nc = os.path.normcase(temp_root)
        mine = os.path.normcase(meipass)
        names = os.listdir(temp_root)
    except Exception:
        return 0

    # 收集所有进程引用到的 _MEI 目录（环境变量/工作目录/加载模块/打开的文件）
    # 按进程名粗筛：memory_maps/open_files 很昂贵，只详查本程序相关进程，
    # 避免启动时对系统全部进程逐一枚举造成卡顿与 CPU 占用
    in_use = set()
    name_keys = ('流萤', 'firefly', 'python')
    for p in psutil.process_iter(['pid', 'name']):
        try:
            pname = (p.info.get('name') or '').lower()
        except Exception:
            continue
        if not any(k in pname for k in name_keys):
            continue
        candidates = []
        try:
            candidates.append(p.cwd() or '')
        except Exception:
            pass
        try:
            for m in p.memory_maps():
                candidates.append(m[0])
        except Exception:
            pass
        try:
            for f in p.open_files():
                candidates.append(f[0])
        except Exception:
            pass
        try:
            home = p.environ().get('_PYI_APPLICATION_HOME_DIR')
            if home:
                candidates.append(home)
        except Exception:
            pass
        for s in candidates:
            s = os.path.normcase(os.path.abspath(s)) if s else ''
            if s.startswith(temp_nc + os.sep + '_mei'):
                in_use.add(s[len(temp_nc) + 1:].split(os.sep)[0])

    now = time.time()
    removed = 0
    for name in names:
        if not name.startswith('_MEI') or name.lower() in in_use:
            continue
        path = os.path.join(temp_root, name)
        if not os.path.isdir(path) or os.path.normcase(os.path.abspath(path)) == mine:
            continue
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
    if removed:
        try:
            print(f"[清理] 已清理 {removed} 个残留解压目录")
        except Exception:
            pass
    return removed

if __name__ == '__main__':
    # 防复发：后台清理残留的 _MEI 解压目录（累积会占满系统盘，导致解压失败）。
    # 延迟 6 秒再执行：避免与窗口初始化争抢 CPU 造成启动卡顿
    def _delayed_mei_cleanup():
        try:
            time.sleep(6)
        except Exception:
            pass
        try:
            cleanup_stale_mei_dirs()
        except Exception:
            pass
    try:
        threading.Thread(target=_delayed_mei_cleanup, daemon=True).start()
    except Exception:
        pass
    # 注销冷却期监视：到期自动销号（无需控制台确认）
    try:
        threading.Thread(target=destroy_pending_watcher, daemon=True).start()
    except Exception:
        pass
    app = QApplication(sys.argv)
    font_path = os.path.join(SERVER_DIR, "font", "HarmonyOS_Sans_SC_Bold.ttf")
    if not os.path.exists(font_path):
        font_path = resource_path(os.path.join("font", "HarmonyOS_Sans_SC_Bold.ttf"))
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
