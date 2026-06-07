# server_gui.py
import os
import random
import re
import sys
import json
import socket
import threading
import socketserver
import uuid
import webbrowser
from datetime import datetime, timedelta
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
import pytz

# ---------- 路径与时区配置 ----------
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DATA = os.path.join(SERVER_DIR, 'server_data')
CHAT_HISTORY_FILE = os.path.join(SERVER_DATA, 'chat_history.json')
PLAYERS_DIR = os.path.join(SERVER_DATA, 'players')
SERVER_CODES_FILE = os.path.join(SERVER_DATA, 'redeem_codes.json')
BANNED_AREAS_FILE = os.path.join(SERVER_DATA, 'banned_areas.json')
BANNED_TIMEZONES_FILE = os.path.join(SERVER_DATA, 'banned_timezones.json')
SERVER_CONFIG_FILE = os.path.join(SERVER_DATA, 'server_config.json')

HOST = '0.0.0.0'
PORT = 20082
SERVER_VERSION = "4.3.7"

# 服务器时区（全局，可动态修改）
SERVER_TIMEZONE = pytz.timezone('Asia/Shanghai')   # 东八区（默认）

# 维护模式状态
maintenance_mode = False
maintenance_message = "服务器正在维护中，暂时无法加入服务器。"
maintenance_lock = threading.Lock()

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
    检查客户端版本是否允许连接服务器
    规则：
      1. 数字部分必须完全一致
      2. 若客户端有后缀，则服务器必须有相同后缀
      3. 若客户端无后缀，则服务器可有任意后缀（或无）
    """
    c_num, c_suf = parse_version(client_ver)
    s_num, s_suf = parse_version(server_ver)
    if c_num is None or s_num is None:
        return False
    if c_num != s_num:
        return False
    if c_suf is not None:
        return c_suf == s_suf
    return True   # 客户端无后缀，服务端后缀任意

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

def load_player_data(server_uid):
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    path = os.path.join(PLAYERS_DIR, f'{server_uid}.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            data.setdefault('name', '')
            data.setdefault('permission', 'user')
            data.setdefault('banned', False)
            data.setdefault('banned_until', None)
            data.setdefault('redeemed_codes', [])
            data.setdefault('uid', '')
            data.setdefault('userid', '')
            data.setdefault('timezone', None)
            return data
    return None

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
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'balance': 0,
        'credit': 0,
        'owned_clothes': ['normal'],
        'star_rail_tickets': 0
    }

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

def load_server_config():
    if not os.path.exists(SERVER_CONFIG_FILE):
        return {}
    with open(SERVER_CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

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
            # 消息可以包含换行符，但存储原始内容（显示时会自动添加“服务器正在维护\n”前缀）
            maintenance_message = message if message is not None else ""
        else:
            maintenance_message = ""
        # 保存到配置
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
    - 时长格式：30D, 1Y6M15D, 999Y999M999D → 返回 (timedelta, None)
    - 纯数字：30 → 视为 30 天，返回 (timedelta, None)
    - 绝对日期：2026-12-31 → 返回 (None, datetime) 当天的 23:59:59
    - 绝对日期时间：2026-12-31 23:59:59 → 返回 (None, datetime)
    若无法解析返回 (None, None)
    """
    if not spec:
        return None, None
    spec = spec.strip()
    # 绝对日期时间 (YYYY-MM-DD HH:MM:SS)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})$', spec)
    if match:
        y, m, d, h, min, sec = map(int, match.groups())
        try:
            dt = SERVER_TIMEZONE.localize(datetime(y, m, d, h, min, sec))
            return None, dt
        except:
            pass
    # 绝对日期 (YYYY-MM-DD)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', spec)
    if match:
        y, m, d = map(int, match.groups())
        try:
            dt = SERVER_TIMEZONE.localize(datetime(y, m, d, 23, 59, 59))
            return None, dt
        except:
            pass
    # 时长格式 (例如 30D, 1Y2M3D)
    match = re.match(r'^(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?$', spec.upper())
    if match and any(match.groups()):
        years = int(match.group(1) or 0)
        months = int(match.group(2) or 0)
        days = int(match.group(3) or 0)
        total_days = years * 365 + months * 30 + days
        if total_days > 0:
            delta = timedelta(days=total_days)
            return delta, None
    # 纯数字作为天数
    if spec.isdigit():
        delta = timedelta(days=int(spec))
        return delta, None
    return None, None

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
    """踢出所有在线玩家，发送维护原因，并清理全局客户端字典"""
    with server_lock:
        for client, player in list(clients.items()):
            try:
                TCPHandler.send_to(client, {'type': 'server_shutdown', 'data': {'reason': reason_message}})
                client.close()
            except:
                pass
        clients.clear()
        player_by_server_uid.clear()

class LogEmitter(QObject):
    log_signal = pyqtSignal(str)

class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        global clients, player_by_server_uid
        peername = self.client_address
        log_emitter = self.server.log_emitter
        log_emitter.log_signal.emit(f"[连接] {peername}")

        player = None
        while True:
            try:
                data = self.request.recv(1024)
                if not data:
                    break
                for line in data.decode().split('\n'):
                    if not line: continue
                    msg = json.loads(line)
                    player = self.process_message(msg, player)
            except ConnectionResetError as e:
                log_emitter.log_signal.emit(f"[连接断开] {peername}: {e}")
                break
            except json.JSONDecodeError as e:
                log_emitter.log_signal.emit(f"[JSON错误] {peername}: {e}")
                break
            except Exception as e:
                log_emitter.log_signal.emit(f"[未处理异常] {peername}: {e}")
                import traceback
                traceback.print_exc()
                break

        if player:
            server_uid = player.get('server_uid')
            with server_lock:
                if server_uid in player_by_server_uid:
                    del player_by_server_uid[server_uid]
            log_emitter.log_signal.emit(f"[断开] {player['name']} 离开了服务器")
            self.broadcast_system(f"{player['name']} 离开了服务器")

    def process_message(self, msg, player):
        mtype = msg['type']
        data = msg.get('data', {})

        if mtype == 'login':
            userid = data['userid']
            real_uid = data['uid']
            name = data['name']
            country = data.get('country', '')
            client_version = data.get('version')
            timezone = data.get('timezone')

            # ---------- 版本校验 ----------
            if not client_version or not isinstance(client_version, str):
                self.send_json({'type': 'login_fail', 'data': {'reason': '未知版本号，请更新客户端至最新版'}})
                return None

            if not versions_compatible(client_version, SERVER_VERSION):
                c_num, _ = parse_version(client_version)
                s_num, _ = parse_version(SERVER_VERSION)
                if c_num and s_num and c_num > s_num:
                    reason = f'客户端版本过高（{client_version}），请更新服务端至匹配版本'
                else:
                    reason = f'客户端版本过低（{client_version}），请更新客户端至 {SERVER_VERSION}'
                self.send_json({'type': 'login_fail', 'data': {'reason': reason}})
                return None

            # ---------- 维护模式检查（新增） ----------
            with maintenance_lock:
                if maintenance_mode:
                    if maintenance_message and maintenance_message.strip():
                        msg_text = f"服务器正在维护\n{maintenance_message}"
                    else:
                        msg_text = "服务器正在维护。"
                    self.send_json({'type': 'login_fail', 'data': {'reason': msg_text}})
                    return None

            # ---------- 地区封禁检查 ----------
            if country:
                banned_areas = load_banned_areas()
                if country.upper() in [a.upper() for a in banned_areas]:
                    self.send_json({'type': 'login_fail', 'data': {'reason': f'当前的国家/地区 ({country}) 不支持连接此服务器。'}})
                    return None

            # ---------- 时区封禁检查 ----------
            if timezone:
                banned_timezones = load_banned_timezones()
                if timezone in banned_timezones:
                    self.send_json({'type': 'login_fail', 'data': {'reason': f'你的时区 ({timezone}) 已被服务器封禁。'}})
                    return None

            # ---------- 查找已有玩家数据 ----------
            existing_server_uid = None
            existing_data = None

            for filename in os.listdir(PLAYERS_DIR):
                if not filename.endswith('.json') or filename.endswith('_inv.json') or filename.endswith('_mails.json'):
                    continue
                filepath = os.path.join(PLAYERS_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        pdata = json.load(f)
                except:
                    continue
                if pdata.get('userid') == userid or pdata.get('uid') == real_uid:
                    existing_server_uid = filename[:-5]
                    existing_data = pdata
                    break

            # 封禁检查（已有账号）
            if existing_data:
                banned = existing_data.get('banned', False)
                banned_until_str = existing_data.get('banned_until')
                if banned:
                    if banned_until_str:
                        try:
                            until_dt = datetime.fromisoformat(banned_until_str)
                            if until_dt.tzinfo is None:
                                until_dt = SERVER_TIMEZONE.localize(until_dt)
                            now = datetime.now(SERVER_TIMEZONE)
                            if now >= until_dt:
                                existing_data['banned'] = False
                                existing_data['banned_until'] = None
                                save_player_data(existing_server_uid, existing_data)
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

            # 防止重复登录
            with server_lock:
                if existing_server_uid and existing_server_uid in player_by_server_uid:
                    self.send_json({'type': 'login_fail', 'data': {'reason': '该账号已在其他客户端登录'}})
                    return None

                if existing_server_uid:
                    server_uid = existing_server_uid
                    pdata = existing_data
                    if timezone and pdata.get('timezone') != timezone:
                        pdata['timezone'] = timezone
                        save_player_data(server_uid, pdata)
                else:
                    is_admin = (real_uid.lower().startswith('admin') if real_uid else False)
                    prefix = 'online-admin-' if is_admin else 'online-user-'
                    while True:
                        rand_num = str(random.randint(0, 10 ** 9 - 1)).zfill(9)
                        server_uid = prefix + rand_num
                        if not os.path.exists(os.path.join(PLAYERS_DIR, f'{server_uid}.json')):
                            break
                    pdata = {
                        'name': name,
                        'userid': userid,
                        'uid': real_uid,
                        'permission': 'admin' if is_admin else 'user',
                        'banned': False,
                        'banned_until': None,
                        'redeemed_codes': [],
                        'timezone': timezone
                    }
                    save_player_data(server_uid, pdata)

                server_name = pdata.get('name')
                if not server_name and name:
                    server_name = name
                    pdata['name'] = server_name
                    save_player_data(server_uid, pdata)

                player = {
                    'userid': userid,
                    'uid': real_uid,
                    'server_uid': server_uid,
                    'name': server_name,
                    'permission': pdata['permission'],
                    'client': self.request,
                    'country': country,
                    'timezone': timezone
                }
                clients[self.request] = player
                player_by_server_uid[server_uid] = player

            inv = load_player_inventory(server_uid)
            player['inventory'] = inv
            player['last_sign_date'] = inv.get('last_sign_date')

            # 签到日期判断（凌晨3点刷新）
            now = datetime.now(SERVER_TIMEZONE)
            if now.hour < 3:
                sign_today_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            else:
                sign_today_str = now.strftime('%Y-%m-%d')
            signed_today = (inv.get('last_sign_date') == sign_today_str)

            history = load_chat_history()
            recent = history[-50:] if len(history) > 50 else history

            self.send_json({
                'type': 'login_ok',
                'data': {
                    'server_uid': server_uid,
                    'permission': player['permission'],
                    'inventory': inv,
                    'signed_today': signed_today,
                    'redeemed_codes': pdata.get('redeemed_codes', []),
                    'server_name': server_name,
                    'chat_history': recent,
                    'server_version': SERVER_VERSION
                }
            })
            self.server.log_emitter.log_signal.emit(f"{player['name']} 进入了服务器 (UID: {server_uid})")
            self.broadcast_system(f"{player['name']} 进入了服务器")
            return player


        elif mtype == 'chat' and player:
            text = data['text'].strip()
            if text.startswith('/'):
                self.handle_command(text, player)
            else:
                chat_msg = {
                    'from_name': player['name'],
                    'text': text,
                    'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
                    'is_system': False
                }
                save_chat_history(chat_msg)
                # 传入发送者 player 对象
                self.broadcast_chat(player['name'], text, sender_player=player)
                if '@' in text:
                    self.handle_mentions(text, player)
            return player

        elif mtype == 'command' and player:
            self.handle_command(data['command'], player)
            return player

        elif mtype == 'get_player_list' and player:
            with server_lock:
                lst = []
                for p in clients.values():
                    entry = {'name': p['name'], 'id': p['userid']}
                    if player['permission'] == 'admin':
                        entry['uid'] = p['server_uid']
                    lst.append(entry)
            self.send_json({'type': 'player_list', 'data': {'players': lst}})
            return player

        elif mtype == 'save_inventory' and player:
            inv = data.get('inventory', {})
            save_player_inventory(player['server_uid'], inv)
            player['inventory'] = inv
            return player

        elif mtype == 'change_name' and player:
            new_name = data['new_name'].strip()
            if not new_name or len(new_name) > 16:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '名字不能为空且不超过16字符'}})
                return player
            if not re.match(r'^[\w\u4e00-\u9fff]+$', new_name):
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '名字只能包含中英文、数字和下划线'}})
                return player
            if new_name == player['name']:
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'你已改名为 {new_name}'}})
                return player
            with server_lock:
                if any(p['name'] == new_name and p != player for p in clients.values()):
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '该名字已被使用'}})
                    return player
                old_name = player['name']
                player['name'] = new_name
            pdata = load_player_data(player['server_uid'])
            if pdata:
                pdata['name'] = new_name
                save_player_data(player['server_uid'], pdata)
            history = load_chat_history()
            updated = False
            for msg in history:
                if msg.get('from_name') == old_name:
                    msg['from_name'] = new_name
                    updated = True
            if updated:
                with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(history, f, indent=2)
            self.server.log_emitter.log_signal.emit(f"{old_name} 改名为 {new_name} (UID: {player['server_uid']})")
            self.broadcast_system(f"{old_name} 改名为 {new_name}")
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'改名成功！你现在的名字是 {new_name}'}})
            self.send_player_list_to_all()
            return player

        elif mtype == 'get_mails' and player:
            mails = load_player_mails(player['server_uid'])
            self.send_json({'type': 'mails_data', 'data': {'mails': mails}})
            return player

        elif mtype == 'get_chat_history' and player:
            history = load_chat_history()
            recent = history[-200:] if len(history) > 200 else history
            self.send_json({'type': 'chat_history_data', 'data': {'history': recent}})
            return player

        elif mtype == 'claim_mail' and player:
            mail_id = data['mail_id']
            mails = load_player_mails(player['server_uid'])
            claimed = False
            for mail in mails:
                if mail['id'] == mail_id and not mail.get('claimed'):
                    mail['claimed'] = True
                    claimed = True
                    inv = player['inventory']
                    for item in mail['items']:
                        itype = item['type']
                        if itype == 'cake':
                            inv['balance'] = inv.get('balance', 0) + item['amount']
                        elif itype == 'credit':
                            inv['credit'] = inv.get('credit', 0) + item['amount']
                            if inv['credit'] > 99999999: inv['credit'] = 99999999
                        elif itype == 'clothes':
                            cid = item['id']
                            if cid not in inv['owned_clothes']:
                                inv['owned_clothes'].append(cid)
                        elif itype == 'star_rail_ticket':
                            inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) + item['amount']
                    player['inventory'] = inv
                    save_player_inventory(player['server_uid'], inv)
                    save_player_mails(player['server_uid'], mails)
                    break
            if claimed:
                self.send_json({'type': 'claim_mail_result', 'data': {'success': True}})
            else:
                self.send_json({'type': 'claim_mail_result', 'data': {'success': False, 'reason': '邮件不存在或已领取'}})
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
                    if player['name'] not in allowed and player['uid'] not in allowed and player['server_uid'] not in allowed:
                        self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '该兑换码不属于你'}})
                        return player
            expire_time = cinfo.get('expire_time')
            if expire_time:
                try:
                    if datetime.now() > datetime.fromisoformat(expire_time):
                        self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '兑换码已过期'}})
                        return player
                except:
                    pass
            if player['server_uid'] in cinfo.get('used_by', []):
                self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': '你已经使用过该兑换码'}})
                return player
            inv = player['inventory']
            try:
                for item in cinfo.get('items', []):
                    itype = item.get('type', '')
                    if itype == 'cake':
                        inv['balance'] = inv.get('balance', 0) + item.get('amount', 0)
                    elif itype == 'credit':
                        inv['credit'] = inv.get('credit', 0) + item.get('amount', 0)
                        if inv['credit'] > 99999999:
                            inv['credit'] = 99999999
                    elif itype == 'clothes':
                        cid = item.get('id')
                        if cid and cid not in inv.get('owned_clothes', []):
                            inv.setdefault('owned_clothes', ['normal']).append(cid)
                    elif itype == 'star_rail_ticket':
                        inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) + item.get('amount', 0)
            except Exception as e:
                self.send_json({'type': 'redeem_result', 'data': {'success': False, 'message': f'兑换失败: {str(e)}'}})
                return player
            cinfo.setdefault('used_by', []).append(player['server_uid'])
            save_server_codes(codes)
            save_player_inventory(player['server_uid'], inv)
            pdata = load_player_data(player['server_uid'])
            if pdata and code not in pdata['redeemed_codes']:
                pdata['redeemed_codes'].append(code)
                save_player_data(player['server_uid'], pdata)
            items_desc = []
            for item in cinfo.get('items', []):
                if item['type'] == 'cake':
                    items_desc.append(f"橡木蛋糕卷 x{item['amount']}")
                elif item['type'] == 'credit':
                    items_desc.append(f"信用点 x{item['amount']}")
                elif item['type'] == 'clothes':
                    items_desc.append(f"时装 [{item.get('id', '未知')}]")
                elif item['type'] == 'star_rail_ticket':
                    items_desc.append(f"星铁专票 x{item['amount']}")
            mail_content = "你成功兑换了以下物品：\n" + "\n".join(items_desc)
            mails = load_player_mails(player['server_uid'])
            new_mail = {
                "id": str(uuid.uuid4()),
                "title": "兑换码兑换成功！",
                "content": mail_content,
                "sender": "系统",
                "timestamp": datetime.now().isoformat(),
                "read": False,
                "claimed": False,
                "items": cinfo['items']
            }
            mails.append(new_mail)
            save_player_mails(player['server_uid'], mails)
            self.send_json({'type': 'redeem_result', 'data': {'success': True, 'message': '兑换成功！物品已发送至邮箱'}})
            return player

        elif mtype == 'disconnect' and player:
            server_uid = player['server_uid']
            with server_lock:
                if player['client'] in clients:
                    del clients[player['client']]
                if server_uid in player_by_server_uid:
                    del player_by_server_uid[server_uid]
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
            player['inventory']['credit'] = player['inventory'].get('credit', 0) + 7200
            if player['inventory']['credit'] > 99999999:
                player['inventory']['credit'] = 99999999
            player['last_sign_date'] = sign_date
            inv = player['inventory']
            inv['last_sign_date'] = sign_date
            save_player_inventory(player['server_uid'], inv)
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

    def handle_command(self, cmd, player):
        parts = cmd.strip().split()
        if not parts:
            return
        cmd_name = parts[0].lower()

        if cmd_name == '/help':
            help_text = (
                "【联机管理命令】\n"
                "/give <数量> <物品类型> [id=时装ID] [for <用户列表>]\n"
                "/clear <物品类型> [for <用户列表>]\n"
                "/set cake/credit <数量> [for <用户列表>]\n"
                "/set newcode <兑换码> <物品列表> [for <用户列表>] [有效期]\n"
                "/revoke code <兑换码> [for <用户列表>]\n"
                "/set user <标识> Admin\n"
                "/ban <标识>[,<标识2>...] [时长]   (时长如 30D, 1Y6M15D, 2026-12-31)\n"
                "/ban area <国家代码>\n"
                "/ban timezone <时区名>\n"
                "/unban <标识>[,<标识2>...]\n"
                "/unban area <国家代码>\n"
                "/unban timezone <时区名>\n"
                "/fix on [消息]        开启维护模式（可选自定义消息，消息中含\\n可换行）\n"
                "/fix off              关闭维护模式\n"
            )
            self.send_json({'type': 'system_message', 'data': {'text': help_text}})
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': help_text}})
            return

        # ---------- 维护模式命令 ----------
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
                # 构建踢出提示
                if msg and msg.strip():
                    reason_text = f"服务器正在维护\n{msg}"
                else:
                    reason_text = "服务器正在维护"
                # 踢出所有在线玩家
                kick_all_players_for_maintenance(reason_text)
                self.send_json({'type': 'command_result', 'data': {'success': True,
                                                                   'message': f'维护模式已开启，已踢出所有在线玩家。提示消息: {maintenance_message}'}})
                self.broadcast_system(f"服务器已进入维护模式，所有玩家已被踢出。")
            elif subcmd == 'off':
                set_maintenance(False, '')
                self.send_json({'type': 'command_result', 'data': {'success': True, 'message': '维护模式已关闭'}})
                self.broadcast_system(f"服务器维护模式已关闭，欢迎新玩家加入。")
            else:
                self.send_json({'type': 'command_result',
                                'data': {'success': False, 'message': '未知子命令，使用 /fix on 或 /fix off'}})
            return

        # ---------- 时区封禁命令 ----------
        if cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
            if player['permission'] != 'admin':
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            tzname = parts[2]
            try:
                pytz.timezone(tzname)
            except:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'无效的时区名称: {tzname}'}})
                return
            banned_tz = load_banned_timezones()
            if tzname in banned_tz:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'时区 {tzname} 已在封禁列表中'}})
                return
            banned_tz.append(tzname)
            save_banned_timezones(banned_tz)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已封禁时区 {tzname}'}})
            with server_lock:
                to_kick = [p for p in clients.values() if p.get('timezone') == tzname]
            for p in to_kick:
                try:
                    self.send_to(p['client'], {'type': 'system_message', 'data': {'text': f'你的时区 {tzname} 已被服务器封禁，你将被踢出。'}})
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
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            tzname = parts[2]
            banned_tz = load_banned_timezones()
            if tzname not in banned_tz:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'时区 {tzname} 不在封禁列表中'}})
                return
            banned_tz.remove(tzname)
            save_banned_timezones(banned_tz)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已解封时区 {tzname}'}})
            self.broadcast_system(f"时区 {tzname} 已被解封")
            return

        # ---------- 地区封禁命令 ----------
        if cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'area':
            if player['permission'] != 'admin':
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            area_code = parts[2].upper()
            banned_areas = load_banned_areas()
            if area_code in banned_areas:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'地区 {area_code} 已在封禁列表中'}})
                return
            banned_areas.append(area_code)
            save_banned_areas(banned_areas)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已封禁地区 {area_code}'}})
            with server_lock:
                to_kick = [p for p in clients.values() if p.get('country') == area_code]
            for p in to_kick:
                try:
                    self.send_to(p['client'], {'type': 'system_message', 'data': {'text': f'你的地区 {area_code} 已被服务器封禁，你将被踢出。'}})
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
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            area_code = parts[2].upper()
            banned_areas = load_banned_areas()
            if area_code not in banned_areas:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'地区 {area_code} 不在封禁列表中'}})
                return
            banned_areas.remove(area_code)
            save_banned_areas(banned_areas)
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已解封地区 {area_code}'}})
            self.broadcast_system(f"地区 {area_code} 已被解封")
            return

        # ---------- 玩家封禁 ----------
        if cmd_name == '/ban':
            if player['permission'] != 'admin':
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '格式：/ban <标识> [时长] 或 /ban area <国家代码> 或 /ban timezone <时区>'}})
                return
            targets = parts[1].split(',')
            duration_str = parts[2] if len(parts) > 2 else None
            delta, until_dt = parse_time_spec(duration_str) if duration_str else (None, None)
            if until_dt:
                banned_until = until_dt.isoformat()
                if until_dt <= datetime.now(SERVER_TIMEZONE):
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '指定的解封时间已过期'}})
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
                    target = next((p for p in clients.values() if p['name'] == identifier or p['uid'] == identifier or p['server_uid'] == identifier), None)
                if target:
                    server_uid = target['server_uid']
                    pdata = load_player_data(server_uid)
                    if pdata:
                        pdata['banned'] = True
                        pdata['banned_until'] = banned_until
                        save_player_data(server_uid, pdata)
                    try:
                        msg = f'你已被管理员封禁。'
                        if banned_until:
                            until_dt2 = datetime.fromisoformat(banned_until)
                            msg += f' 解封时间：{until_dt2.strftime("UTC%z %Y-%m-%d %H:%M:%S")}'
                        else:
                            msg += ' 永久封禁。'
                        self.send_to(target['client'], {'type': 'system_message', 'data': {'text': msg}})
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
                        self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {identifier} 的数据'}})
                        continue
                    pdata['banned'] = True
                    pdata['banned_until'] = banned_until
                    save_player_data(server_uid, pdata)
                    self.broadcast_system(f"{identifier} 已被封禁 (离线)")
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已封禁 {", ".join(targets)}'}})
            self.send_player_list_to_all()
            return

        if cmd_name == '/unban':
            if player['permission'] != 'admin':
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '只有管理员可以使用此命令'}})
                return
            if len(parts) < 2:
                self.send_json({'type': 'command_result', 'data': {'success': False, 'message': '格式：/unban <标识> 或 /unban area <国家代码> 或 /unban timezone <时区>'}})
                return
            targets = parts[1].split(',')
            for identifier in targets:
                identifier = identifier.strip()
                if not identifier:
                    continue
                server_uid, pdata = self.find_player_file_by_identifier(identifier)
                if server_uid is None:
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'未找到玩家 {identifier} 的数据'}})
                    continue
                if not pdata.get('banned'):
                    self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'玩家 {identifier} 未被封禁'}})
                    continue
                pdata['banned'] = False
                pdata['banned_until'] = None
                save_player_data(server_uid, pdata)
                with server_lock:
                    target_player = player_by_server_uid.get(server_uid)
                if target_player:
                    try:
                        self.send_to(target_player['client'], {'type': 'system_message', 'data': {'text': '你已被管理员解封，请重新登录以生效。'}})
                        target_player['client'].close()
                    except:
                        pass
                    with server_lock:
                        if target_player['client'] in clients:
                            del clients[target_player['client']]
                        if server_uid in player_by_server_uid:
                            del player_by_server_uid[server_uid]
                self.broadcast_system(f"{identifier} 已被解封")
            self.send_json({'type': 'command_result', 'data': {'success': True, 'message': f'已解封 {", ".join(targets)}'}})
            self.send_player_list_to_all()
            return

        # 其他命令（简化处理，实际使用时请根据原有完整代码补充）
        self.send_json({'type': 'command_result', 'data': {'success': False, 'message': f'未知指令 {cmd_name} 或该命令未完全实现'}})

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

    def broadcast_chat(self, from_name, text, sender_player=None):
        msg_base = {
            'type': 'chat_broadcast',
            'data': {
                'from_name': from_name,
                'text': text,
                'time': datetime.now(SERVER_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
                'is_system': False
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
            # 客户端已断开，静默处理，不打印错误堆栈
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
        # 加载维护模式状态
        load_maintenance_state()

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
        self.cmd_edit.returnPressed.connect(self.send_command)
        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self.send_command)
        cmd_layout.addWidget(self.cmd_edit)
        cmd_layout.addWidget(send_btn)
        layout.addLayout(cmd_layout)

        btn_layout = QHBoxLayout()
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
        # 写入文件
        log_file = os.path.join(SERVER_DATA, "server.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(timestamp + text + "\n")

    def on_apply_timezone(self):
        tzname = self.tz_combo.currentText()
        # 深色确认对话框
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
            self.append_log(f"服务器已启动，监听 {HOST}:{PORT}")
            self.start_btn.setEnabled(False)
            self.restart_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
        except Exception as e:
            self.append_log(f"启动失败: {e}")

    def stop_server(self):
        if self.server:
            with server_lock:
                # 先发送服务器关闭通知
                for client in list(clients.keys()):
                    try:
                        TCPHandler.send_to(client, {'type': 'server_shutdown', 'data': {'reason': '服务器已关闭\n断开连接!'}})
                    except:
                        pass
                # 然后踢出所有玩家
                for client in list(clients.keys()):
                    try:
                        TCPHandler.send_to(client,
                                           {'type': 'system_message', 'data': {'text': '服务器正在关闭，你已被踢出。'}})
                        client.close()
                    except:
                        pass
                clients.clear()
                player_by_server_uid.clear()
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.append_log("服务器已关闭!")
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
        if cmd == '/stop':
            self.stop_server()
        elif cmd == '/restart':
            self.restart_server()
        elif cmd.startswith('/'):
            parts = cmd.split()
            if not parts:
                return
            cmd_name = parts[0].lower()
            # 处理维护模式命令
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
            # 处理地区封禁/解封
            if cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'area':
                self.exec_ban_area(parts[2])
            elif cmd_name == '/unban' and len(parts) >= 3 and parts[1].lower() == 'area':
                self.exec_unban_area(parts[2])
            elif cmd_name == '/ban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
                self.exec_ban_timezone(parts[2])
            elif cmd_name == '/unban' and len(parts) >= 3 and parts[1].lower() == 'timezone':
                self.exec_unban_timezone(parts[2])
            elif cmd_name == '/op' and len(parts) == 2:
                self.exec_op(parts[1])
            elif cmd_name == '/deop' and len(parts) == 2:
                self.exec_deop(parts[1])
            elif cmd_name == '/ban' and len(parts) >= 2:
                target = parts[1]
                duration = parts[2] if len(parts) > 2 else None
                self.exec_ban(target, duration)
            elif cmd_name == '/unban' and len(parts) == 2:
                self.exec_unban(parts[1])
            else:
                self.append_log(f"未知控制台指令: {cmd}")
        else:
            if self.server:
                with server_lock:
                    if clients:
                        log_msg = f"[控制台] {cmd}"
                        self.append_log(log_msg)
                        for client in clients.keys():
                            TCPHandler.send_to(client, {'type': 'chat_broadcast', 'data': {'from_name': '系统', 'text': log_msg, 'is_system': True}})
                    else:
                        self.append_log("[控制台] 没有玩家在线")
            else:
                self.append_log("服务器未启动")

    # ---------- 管理功能实现 ----------
    def exec_op(self, identifier):
        with server_lock:
            target = next((p for p in clients.values() if p['name'] == identifier or p['uid'] == identifier or p['server_uid'] == identifier), None)
        if target:
            if target['permission'] == 'admin':
                self.append_log(f"{target['name']} 已经是管理员")
                return
            target['permission'] = 'admin'
            pdata = load_player_data(target['server_uid'])
            if pdata:
                pdata['permission'] = 'admin'
                save_player_data(target['server_uid'], pdata)
            TCPHandler.send_to(target['client'], {'type': 'permission_changed', 'data': {'permission': 'admin', 'server_uid': target['server_uid']}})
            self.append_log(f"已将 {identifier} 提升为管理员")
            self.broadcast_to_all(f"{identifier} 被提升为管理员")
            self.refresh_all_lists()
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
        pdata['permission'] = 'admin'
        save_player_data(server_uid, pdata)
        self.append_log(f"已离线提升 {identifier} 为管理员")
        self.broadcast_to_all(f"{identifier} 被提升为管理员")

    def exec_deop(self, identifier):
        with server_lock:
            target = next((p for p in clients.values() if p['name'] == identifier or p['uid'] == identifier or p['server_uid'] == identifier), None)
        if target:
            if target['permission'] == 'user':
                self.append_log(f"{target['name']} 已经是普通用户")
                return
            target['permission'] = 'user'
            pdata = load_player_data(target['server_uid'])
            if pdata:
                pdata['permission'] = 'user'
                save_player_data(target['server_uid'], pdata)
            TCPHandler.send_to(target['client'], {'type': 'permission_changed', 'data': {'permission': 'user', 'server_uid': target['server_uid']}})
            self.append_log(f"已取消 {identifier} 的管理员资格")
            self.broadcast_to_all(f"{identifier} 被取消管理员资格")
            self.refresh_all_lists()
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
        pdata['permission'] = 'user'
        save_player_data(server_uid, pdata)
        self.append_log(f"已离线取消 {identifier} 的管理员资格")
        self.broadcast_to_all(f"{identifier} 被取消管理员资格")

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
            target = next((p for p in clients.values() if p['name'] == identifier or p['uid'] == identifier or p['server_uid'] == identifier), None)
        if target:
            server_uid = target['server_uid']
            pdata = load_player_data(server_uid)
            if pdata:
                pdata['banned'] = True
                pdata['banned_until'] = banned_until
                save_player_data(server_uid, pdata)
            try:
                msg = '你已被管理员封禁。'
                if banned_until:
                    until_dt2 = datetime.fromisoformat(banned_until)
                    msg += f' 解封时间：{until_dt2.strftime("UTC%z %Y-%m-%d %H:%M:%S")}'
                else:
                    msg += ' 永久封禁。'
                TCPHandler.send_to(target['client'], {'type': 'system_message', 'data': {'text': msg}})
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
                TCPHandler.send_to(p['client'], {'type': 'system_message', 'data': {'text': f'你的地区 {area_code} 已被服务器封禁，你将被踢出。'}})
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
                TCPHandler.send_to(p['client'], {'type': 'system_message', 'data': {'text': f'你的时区 {tzname} 已被服务器封禁，你将被踢出。'}})
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