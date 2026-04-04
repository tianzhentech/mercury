"""
Mercury 虚拟卡管理 Web 服务器
所有账户 session 都保存在 accounts.json 中，每次请求后自动更新
"""

import sys
import json
import os
import re
import threading
import time
import uuid
import fcntl
from functools import wraps
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, jsonify, Response

# 添加项目路径
sys.path.insert(0, os.path.dirname(__file__))
from header import headers
from card.issue import issue_card
from card.embed_reveal import reveal_card_details
from card.cancel import cancel_card
from account.accounts import (
    get_random_account, get_account_by_user_id, get_account_count,
    add_account, add_account_by_credentials, get_all_accounts, delete_account, refresh_account, update_account_proxy,
    clear_mercury_cards, get_mercury_card_counts,
    subscribe_status_updates, unsubscribe_status_updates,
    get_all_proxies, add_proxy, delete_proxy, get_proxy_by_id, test_proxy_latency,
    subscribe_proxy_latency, unsubscribe_proxy_latency, get_proxy_latency_cache,
    start_proxy_latency_checker, has_active_accounts, get_all_active_accounts,
    get_default_proxy_id, set_default_proxy_id
)
from id.id import generate_ids, use_id, delete_id, get_all_ids, delete_all_ids, delete_ids_batch, delete_unused_ids_by_type, get_redeem_records, delete_record, delete_all_records, query_redeemed, get_hidden_ids_by_token, allocate_existing_ids_for_withdraw, get_analytics_data, record_direct_card_creation, reload_db_config, acquire_id_for_redeem, release_id_redeem_lock, import_timoes_codes, get_timoes_pool_stats, list_timoes_pool_items, update_timoes_pool_item, delete_timoes_pool_item, acquire_timoes_code_for_redeem, release_timoes_code_lock, mark_timoes_code_used, mark_timoes_code_invalid, import_manual_cards, get_manual_card_pool_stats, list_manual_card_pool_items, update_manual_card_pool_item, delete_manual_card_pool_item, acquire_manual_card_for_redeem, release_manual_card_lock, mark_manual_card_used, get_old_card_pool_stats, acquire_old_card_for_redeem, release_old_card_lock, mark_old_card_used, validate_id
from other_api import redeem_airwallex_card
from user.login import login, refresh_access_token, verify_access_token
from user.user import create_user, delete_user, update_user, get_all_users, is_admin, init_default_admin

app = Flask(__name__)

# 配置
CARD_FILE = os.path.join(os.path.dirname(__file__), "card", "card.json")
VERSION_FILE = os.path.join(os.path.dirname(__file__), "version.txt")
BACKGROUND_THREAD_LOCK_FILE = os.path.join(os.path.dirname(__file__), ".background_threads.lock")


def _local_tzinfo():
    return datetime.now().astimezone().tzinfo


def _parse_iso_to_utc(value):
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_local_tzinfo())
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def get_version():
    """读取版本号"""
    try:
        with open(VERSION_FILE, 'r') as f:
            return f.read().strip()
    except:
        return "0.0"

# 兼容旧代码的变量（不再使用，改为随机选择账户）
USER_ID = None
MERCURY_CREDIT_ACCOUNT_ID = None

def init_account_info():
    """显示账户信息"""
    from account.accounts import get_account_count, get_all_accounts

    count = get_account_count()
    if count == 0:
        print("⚠️  没有配置 Mercury 账户，请从前端「后台账户」添加")
        print("⚠️  创建卡片功能将不可用，直到添加有效账户")
    else:
        print(f"✅ 已配置 {count} 个 Mercury 账户")
        accounts = get_all_accounts()
        for acc in accounts:
            print(f"   - {acc['email']} ({acc['organization']})")

    return True

# 锁，用于线程安全
card_lock = threading.Lock()


def require_auth(f):
    """需要认证的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 优先从 Header 获取，SSE 请求从 URL 参数获取
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        else:
            token = request.args.get('token')

        if not token:
            return jsonify({"success": False, "error": "未授权"}), 401

        user_info = verify_access_token(token)

        if not user_info:
            return jsonify({"success": False, "error": "Token无效或已过期"}), 401

        # 将用户信息添加到请求上下文
        request.user = user_info
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """需要管理员权限的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 优先从 Header 获取，SSE 请求从 URL 参数获取
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        else:
            token = request.args.get('token')

        if not token:
            return jsonify({"success": False, "error": "未授权"}), 401

        user_info = verify_access_token(token)

        if not user_info:
            return jsonify({"success": False, "error": "Token无效或已过期"}), 401

        if not user_info.get('is_admin'):
            return jsonify({"success": False, "error": "需要管理员权限"}), 403

        request.user = user_info
        return f(*args, **kwargs)
    return decorated


def load_cards():
    """加载卡片数据"""
    if os.path.exists(CARD_FILE):
        try:
            with open(CARD_FILE, 'r') as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        except:
            pass
    return {"cards": []}


def save_cards(data):
    """保存卡片数据"""
    with open(CARD_FILE, 'w') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def check_expired_cards():
    """检查并删除过期卡片的后台线程"""
    while True:
        try:
            with card_lock:
                data = load_cards()
                now = datetime.now(timezone.utc)
                expired_cards = []
                active_cards = []

                for card in data.get("cards", []):
                    expire_time = _parse_iso_to_utc(card.get("expire_time"))
                    if expire_time and now >= expire_time:
                        expired_cards.append(card)
                    else:
                        active_cards.append(card)

                for card in expired_cards:
                    print(f"[自动删除] 卡片 {card['card_id']} 已过期，正在取消...")
                    try:
                        if should_remote_cancel_created_card(card):
                            account = get_account_by_user_id(card.get("account_user_id"))
                            if account:
                                card_type = get_safe_cancel_card_type(card)
                                success = cancel_card(card["card_id"], account, card_type=card_type)
                                if success:
                                    print(f"[自动删除] 卡片 {card['card_id']} 已取消")
                                else:
                                    print(f"[自动删除] 取消卡片 {card['card_id']} 失败")
                            else:
                                print(f"[自动删除] 找不到账户，无法取消卡片 {card['card_id']}")
                        else:
                            print(f"[自动删除] 卡片 {card['card_id']} 属于外部渠道，仅从本地移除")
                    except Exception as e:
                        print(f"[自动删除] 取消卡片 {card['card_id']} 失败: {e}")

                if expired_cards:
                    data["cards"] = active_cards
                    save_cards(data)

        except Exception as e:
            print(f"[后台任务] 检查过期卡片出错: {e}")

        time.sleep(30)


def auto_refresh_accounts():
    """后台线程：每10分钟自动刷新所有账户的 session，并检查卡片数量"""
    from account.accounts import refresh_all_accounts, get_account_count

    while True:
        # 等待10分钟
        time.sleep(600)

        if get_account_count() == 0:
            continue

        print("\n[定时任务] 开始刷新所有 Mercury 账户...")
        result = refresh_all_accounts()
        paused = result.get('paused', 0)
        paused_msg = f"，暂停 {paused}" if paused > 0 else ""
        print(f"[定时任务] 刷新完成: 成功 {result['success']}/{result['total']}，失败 {result['failed']}{paused_msg}")


# 模块级别启动后台线程（确保 Gunicorn 环境也能启动）
_background_threads_started = False
_background_threads_retry_after = 0.0
_background_thread_lock_handle = None


def _acquire_background_thread_leader():
    """只允许一个 worker 启动后台线程。"""
    global _background_thread_lock_handle
    if _background_thread_lock_handle is not None:
        return True

    lock_handle = open(BACKGROUND_THREAD_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return False

    _background_thread_lock_handle = lock_handle
    return True

def start_background_threads():
    """启动后台线程（确保只启动一次）"""
    global _background_threads_started, _background_threads_retry_after
    if _background_threads_started:
        return

    now = time.time()
    if now < _background_threads_retry_after:
        return

    if not _acquire_background_thread_leader():
        _background_threads_retry_after = now + 60
        return

    _background_threads_started = True
    _background_threads_retry_after = 0.0

    # 初始化卡片文件
    if not os.path.exists(CARD_FILE):
        save_cards({"cards": []})

    # 启动过期检查线程
    expire_thread = threading.Thread(target=check_expired_cards, daemon=True)
    expire_thread.start()
    print("[启动] 后台过期检查线程已启动")

    # 启动账户自动刷新线程（每10分钟）
    refresh_thread = threading.Thread(target=auto_refresh_accounts, daemon=True)
    refresh_thread.start()
    print("[启动] 账户自动刷新线程已启动（每10分钟）")

    # 启动代理延迟检查线程（每60秒）
    start_proxy_latency_checker()

# 使用 Flask 的 before_request 钩子启动后台线程（只执行一次）
@app.before_request
def ensure_background_threads():
    """确保后台线程已启动"""
    start_background_threads()


# ==================== 页面路由 ====================

@app.route('/')
def index():
    """首页 - 管理面板（需要前端验证登录）"""
    return render_template('index.html', version=get_version())


@app.route('/login')
def login_page():
    """登录页面"""
    return render_template('login.html', version=get_version())


@app.route('/withdraw/<token>')
def withdraw_page(token):
    """提卡链接页面 - 用户端（无需登录）"""
    return render_template('withdraw.html', token=token, version=get_version())


@app.route('/api/service/status', methods=['GET'])
def api_service_status():
    """检查服务是否可用（手动维护开关）"""
    settings = load_settings()
    maintenance_mode = settings.get("maintenance_mode", False)
    return jsonify({"available": not maintenance_mode})



# ==================== 认证 API ====================

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """用户登录"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({"success": False, "error": "请输入用户名和密码"}), 400

        result = login(username, password)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/auth/refresh', methods=['POST'])
def api_refresh():
    """刷新Access Token"""
    try:
        data = request.json
        refresh_token = data.get('refresh_token', '')

        if not refresh_token:
            return jsonify({"success": False, "error": "缺少Refresh Token"}), 400

        result = refresh_access_token(refresh_token)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/auth/verify', methods=['GET'])
@require_auth
def api_verify():
    """验证当前Token"""
    return jsonify({
        "success": True,
        "user": request.user
    })


# ==================== 用户管理 API ====================

@app.route('/api/users', methods=['GET'])
@require_admin
def api_get_users():
    """获取所有用户"""
    users = get_all_users()
    return jsonify({"success": True, "users": users})


@app.route('/api/users', methods=['POST'])
@require_admin
def api_create_user():
    """创建用户"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')
        user_is_admin = data.get('is_admin', False)

        if not username or not password:
            return jsonify({"success": False, "error": "用户名和密码不能为空"}), 400

        result = create_user(username, password, user_is_admin)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/users/<username>', methods=['DELETE'])
@require_admin
def api_delete_user(username):
    """删除用户"""
    try:
        # 不能删除自己
        if request.user.get('username') == username:
            return jsonify({"success": False, "error": "不能删除自己"}), 400

        result = delete_user(username)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/users/<username>', methods=['PUT'])
@require_admin
def api_update_user(username):
    """更新用户"""
    try:
        data = request.json
        new_password = data.get('password')
        new_is_admin = data.get('is_admin')

        result = update_user(username, new_password=new_password, new_is_admin=new_is_admin)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== 后台账户管理 API ====================

@app.route('/api/mercury-accounts', methods=['GET'])
@require_admin
def api_get_mercury_accounts():
    """获取所有 Mercury 账户"""
    accounts = get_all_accounts()
    return jsonify({"success": True, "accounts": accounts})


@app.route('/api/mercury-accounts', methods=['POST'])
@require_admin
def api_add_mercury_account():
    """添加 Mercury 账户"""
    try:
        data = request.json
        session_cookie = data.get('session', '').strip()
        proxy = data.get('proxy', '').strip()

        if not session_cookie:
            return jsonify({"success": False, "error": "请输入 _SESSION cookie"}), 400

        result = add_account(session_cookie, proxy=proxy if proxy else None)

        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/login', methods=['POST'])
@require_admin
def api_add_mercury_account_by_credentials():
    """通过邮箱凭证添加 Mercury 账户"""
    try:
        data = request.json
        email = data.get('email', '').strip()
        mercury_password = data.get('mercury_password', '').strip()
        totp_secret = data.get('totp_secret', '').strip()
        email_password = data.get('email_password', '').strip()
        proxy = data.get('proxy', '').strip()

        print(f"[邮箱登录] 收到请求: {email}, 代理: {proxy if proxy else '无'}")

        if not email:
            return jsonify({"success": False, "error": "请输入邮箱"}), 400
        if not mercury_password:
            return jsonify({"success": False, "error": "请输入 Mercury 密码"}), 400
        if not totp_secret:
            return jsonify({"success": False, "error": "请输入 2FA 密钥"}), 400
        if not email_password:
            return jsonify({"success": False, "error": "请输入邮箱密码"}), 400

        result = add_account_by_credentials(
            email=email,
            mercury_password=mercury_password,
            totp_secret=totp_secret,
            email_password=email_password,
            proxy=proxy if proxy else None
        )

        print(f"[邮箱登录] 结果: {result.get('success')}, {result.get('error', result.get('message', ''))}")

        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        import traceback
        print(f"[邮箱登录] 异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/<user_id>', methods=['DELETE'])
@require_admin
def api_delete_mercury_account(user_id):
    """删除 Mercury 账户"""
    try:
        result = delete_account(user_id)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/<user_id>/refresh', methods=['POST'])
@require_admin
def api_refresh_mercury_account(user_id):
    """刷新 Mercury 账户信息"""
    try:
        result = refresh_account(user_id)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/<user_id>/proxy', methods=['POST'])
@require_admin
def api_update_account_proxy(user_id):
    """更新账户代理"""
    try:
        data = request.json
        proxy = data.get('proxy', '')
        result = update_account_proxy(user_id, proxy)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/proxies', methods=['GET'])
@require_admin
def api_get_proxies():
    """获取所有已保存的代理"""
    try:
        proxies = get_all_proxies()
        return jsonify({"success": True, "proxies": proxies})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/proxies', methods=['POST'])
@require_admin
def api_add_proxy():
    """添加新代理"""
    try:
        data = request.json
        name = data.get('name', '')
        proxy_url = data.get('url', '')
        result = add_proxy(name, proxy_url)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/proxies/<proxy_id>', methods=['DELETE'])
@require_admin
def api_delete_proxy(proxy_id):
    """删除代理"""
    try:
        result = delete_proxy(proxy_id)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/proxies/<proxy_id>/test', methods=['POST'])
@require_admin
def api_test_proxy(proxy_id):
    """测试代理延迟"""
    try:
        proxy = get_proxy_by_id(proxy_id)
        if not proxy:
            return jsonify({"success": False, "error": "代理不存在"}), 404

        result = test_proxy_latency(proxy["url"])
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/proxies/default', methods=['GET'])
@require_admin
def api_get_default_proxy():
    """获取默认代理ID"""
    return jsonify({"success": True, "default_proxy_id": get_default_proxy_id()})


@app.route('/api/proxies/default', methods=['POST'])
@require_admin
def api_set_default_proxy():
    """设置默认代理"""
    try:
        data = request.json
        proxy_id = data.get('proxy_id', '')
        result = set_default_proxy_id(proxy_id)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/proxies/latency-cache', methods=['GET'])
@require_admin
def api_get_proxy_latency_cache():
    """获取代理延迟缓存"""
    return jsonify(get_proxy_latency_cache())


# ==================== 系统设置 ====================
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
TIMOES_BACKEND_CHANNELS_KEY = "timoes_backend_channels"
DISABLED_TIMOES_BACKEND_CHANNELS_KEY = "disabled_timoes_backend_channels"
DISABLED_OLD_CARD_BACKEND_CHANNELS_KEY = "disabled_old_card_backend_channels"
BACKEND_CHANNEL_ADDRESS_OVERRIDES_KEY = "backend_channel_address_overrides"
BACKEND_CHANNEL_ADDRESS_CONFIGS_KEY = "backend_channel_address_configs"
BACKEND_ADDRESS_TEMPLATES_KEY = "backend_address_templates"
MANUAL_BACKEND_CHANNEL_PATTERN = re.compile(r'^manual_bin_(\d{6,8})$')
DISPLAY_CHANNEL_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{2,63}$')
TIMOES_CHANNEL_HEAD_PATTERN = re.compile(r'^\d{4,8}$')
TIMOES_RELAY_CODE_TYPE_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{1,31}$')
TIMOES_BACKEND_CHANNEL_ID_PATTERN = re.compile(r'^timoes_[a-z0-9][a-z0-9_-]{1,63}$')
ADDRESS_TEMPLATE_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{2,63}$')
LEGAL_ADDRESS_FIELDS = ("address1", "address2", "city", "region", "postal_code", "country")
BUILTIN_MERCURY_BACKEND_CHANNELS = {
    "mercury_52368601": {
        "id": "mercury_52368601",
        "provider": "mercury",
        "provider_label": "Mercury",
        "label": "52368601 卡头",
        "head": "52368601",
        "card_type": "credit"
    },
    "mercury_54810871": {
        "id": "mercury_54810871",
        "provider": "mercury",
        "provider_label": "Mercury",
        "label": "54810871 卡头",
        "head": "54810871",
        "card_type": "debit"
    }
}
DEFAULT_TIMOES_BACKEND_CHANNELS = {
    "timoes_486699": {
        "id": "timoes_486699",
        "provider": "timoes",
        "provider_label": "Timoes",
        "label": "486699 卡头",
        "head": "486699",
        "relay_code_type": "4866"
    },
    "timoes_451311": {
        "id": "timoes_451311",
        "provider": "timoes",
        "provider_label": "Timoes",
        "label": "451311 卡头",
        "head": "451311",
        "relay_code_type": "4513"
    }
}
BUILTIN_BACKEND_CHANNELS = {
    **BUILTIN_MERCURY_BACKEND_CHANNELS,
    **DEFAULT_TIMOES_BACKEND_CHANNELS
}
DEFAULT_DISPLAY_CHANNELS = [
    {
        "id": "mercury_52368601",
        "name": "52368601 卡头",
        "backend_channel_id": "mercury_52368601",
        "enabled": True
    },
    {
        "id": "mercury_54810871",
        "name": "54810871 卡头",
        "backend_channel_id": "mercury_54810871",
        "enabled": True
    },
    {
        "id": "timoes_486699",
        "name": "486699 卡头",
        "backend_channel_id": "timoes_486699",
        "enabled": True
    },
    {
        "id": "timoes_451311",
        "name": "451311 卡头",
        "backend_channel_id": "timoes_451311",
        "enabled": True
    }
]

def load_settings():
    """加载系统设置"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_settings(settings):
    """保存系统设置"""
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def normalize_legal_address(value):
    if not isinstance(value, dict):
        return {}

    normalized = {}
    for field in LEGAL_ADDRESS_FIELDS:
        raw_value = value.get(field)
        if raw_value is None:
            continue
        text = str(raw_value).strip()
        if text:
            normalized[field] = text
    return normalized


def normalize_timoes_channel_head(value):
    digits = ''.join(ch for ch in str(value or "") if ch.isdigit())
    return digits if TIMOES_CHANNEL_HEAD_PATTERN.match(digits) else None


def normalize_timoes_relay_code_type(value):
    normalized = str(value or "").strip().lower()
    return normalized if TIMOES_RELAY_CODE_TYPE_PATTERN.match(normalized) else None


def normalize_timoes_backend_channel_id(raw_value=None, head=None, relay_code_type=None):
    candidate = str(raw_value or "").strip().lower()
    if TIMOES_BACKEND_CHANNEL_ID_PATTERN.match(candidate):
        return candidate

    base = normalize_timoes_channel_head(head)
    if not base:
        base = re.sub(r'[^a-z0-9_-]+', '-', str(relay_code_type or "").strip().lower())
        base = re.sub(r'-{2,}', '-', base).strip('-_')
    if not base:
        base = uuid.uuid4().hex[:8]

    candidate = base if str(base).startswith("timoes_") else f"timoes_{base}"
    if not TIMOES_BACKEND_CHANNEL_ID_PATTERN.match(candidate):
        candidate = f"timoes_{uuid.uuid4().hex[:8]}"
    return candidate


def build_timoes_backend_channel(channel_id, head, relay_code_type, label=None, built_in=False):
    return {
        "id": channel_id,
        "provider": "timoes",
        "provider_label": "Timoes",
        "label": (str(label or "").strip() or f"{head} 卡头")[:40],
        "head": head,
        "relay_code_type": relay_code_type,
        "built_in": bool(built_in),
        "editable": not built_in,
        "deletable": True
    }


def get_disabled_timoes_backend_channel_ids(settings=None):
    settings = settings or load_settings()
    raw_ids = settings.get(DISABLED_TIMOES_BACKEND_CHANNELS_KEY)
    if not isinstance(raw_ids, list):
        return set()

    return {
        normalized_id
        for normalized_id in (
            str(item or "").strip().lower()
            for item in raw_ids
        )
        if TIMOES_BACKEND_CHANNEL_ID_PATTERN.match(normalized_id)
    }


def save_disabled_timoes_backend_channel_ids(channel_ids, settings=None):
    settings = settings or load_settings()
    settings[DISABLED_TIMOES_BACKEND_CHANNELS_KEY] = sorted({
        normalized_id
        for normalized_id in (
            str(item or "").strip().lower()
            for item in (channel_ids or [])
        )
        if TIMOES_BACKEND_CHANNEL_ID_PATTERN.match(normalized_id)
    })
    save_settings(settings)
    return settings


def get_disabled_old_card_backend_channel_ids(settings=None):
    settings = settings or load_settings()
    raw_ids = settings.get(DISABLED_OLD_CARD_BACKEND_CHANNELS_KEY)
    if not isinstance(raw_ids, list):
        return set()

    return {
        str(item or "").strip().lower()
        for item in raw_ids
        if str(item or "").strip()
    }


def is_old_card_backend_channel_enabled(backend_channel_id, settings=None):
    normalized_id = str(backend_channel_id or "").strip().lower()
    if not normalized_id:
        return False
    return normalized_id not in get_disabled_old_card_backend_channel_ids(settings=settings)


def set_old_card_backend_channel_enabled(backend_channel_id, enabled, settings=None):
    normalized_id = str(backend_channel_id or "").strip().lower()
    if not normalized_id:
        return False, "旧卡后端渠道不能为空", settings or load_settings()

    settings = settings or load_settings()
    disabled_ids = set(get_disabled_old_card_backend_channel_ids(settings=settings))
    if enabled:
        disabled_ids.discard(normalized_id)
    else:
        disabled_ids.add(normalized_id)

    if disabled_ids:
        settings[DISABLED_OLD_CARD_BACKEND_CHANNELS_KEY] = sorted(disabled_ids)
    else:
        settings.pop(DISABLED_OLD_CARD_BACKEND_CHANNELS_KEY, None)
    save_settings(settings)
    return True, None, settings


def normalize_address_template_name(value):
    return str(value or "").strip()[:40]


def normalize_address_template_id(raw_value, fallback_name=None):
    candidate = str(raw_value or "").strip().lower()
    if ADDRESS_TEMPLATE_ID_PATTERN.match(candidate):
        return candidate

    base_source = str(fallback_name or "").strip().lower()
    if not base_source:
        return None

    base = re.sub(r'[^a-z0-9_-]+', '-', base_source)
    base = re.sub(r'-{2,}', '-', base).strip('-_')
    if not base:
        base = f"address-{uuid.uuid4().hex[:8]}"
    if not ADDRESS_TEMPLATE_ID_PATTERN.match(base):
        base = f"address-{uuid.uuid4().hex[:8]}"
    return base


def get_backend_address_templates(settings=None):
    settings = settings or load_settings()
    raw_templates = settings.get(BACKEND_ADDRESS_TEMPLATES_KEY)
    if not isinstance(raw_templates, list):
        return {}

    templates = {}
    for item in raw_templates:
        if not isinstance(item, dict):
            continue
        template_name = normalize_address_template_name(item.get("name"))
        legal_address = normalize_legal_address(item.get("legal_address") or item)
        if not template_name or not legal_address:
            continue

        template_id = normalize_address_template_id(item.get("id"), fallback_name=template_name)
        if template_id in templates:
            continue

        templates[template_id] = {
            "id": template_id,
            "name": template_name,
            "legal_address": legal_address
        }
    return templates


def serialize_backend_address_templates(settings=None):
    templates = list(get_backend_address_templates(settings=settings).values())
    templates.sort(key=lambda item: (item.get("name") or item.get("id") or ""))
    return templates


def save_backend_address_templates(templates, settings=None):
    settings = settings or load_settings()
    normalized = []
    seen_ids = set()
    for item in templates or []:
        if not isinstance(item, dict):
            continue
        template_name = normalize_address_template_name(item.get("name"))
        legal_address = normalize_legal_address(item.get("legal_address") or item)
        if not template_name or not legal_address:
            continue

        template_id = normalize_address_template_id(item.get("id"), fallback_name=template_name)
        if template_id in seen_ids:
            continue
        seen_ids.add(template_id)
        normalized.append({
            "id": template_id,
            "name": template_name,
            "legal_address": legal_address
        })

    if normalized:
        settings[BACKEND_ADDRESS_TEMPLATES_KEY] = normalized
    else:
        settings.pop(BACKEND_ADDRESS_TEMPLATES_KEY, None)
    save_settings(settings)
    return settings


def upsert_backend_address_template(data, settings=None):
    settings = settings or load_settings()
    payload = dict(data or {})
    requested_id = str(payload.get("id") or "").strip().lower()
    template_name = normalize_address_template_name(payload.get("name"))
    legal_address = normalize_legal_address(payload.get("legal_address") or {})
    if not template_name:
        return False, "请输入模板名称", settings
    if not legal_address:
        return False, "请至少填写一个地址字段", settings

    templates = get_backend_address_templates(settings=settings)
    if requested_id and requested_id not in templates:
        return False, "地址模板不存在", settings

    template_id = normalize_address_template_id(requested_id or payload.get("id"), fallback_name=template_name)
    if requested_id:
        template_id = requested_id

    for existing_id, item in templates.items():
        if existing_id == template_id:
            continue
        if item.get("name") == template_name:
            return False, f"模板 {template_name} 已存在", settings

    templates[template_id] = {
        "id": template_id,
        "name": template_name,
        "legal_address": legal_address
    }
    settings = save_backend_address_templates(
        sorted(templates.values(), key=lambda item: item.get("name") or item.get("id") or ""),
        settings=settings
    )
    return True, templates[template_id], settings


def normalize_backend_channel_address_config(value, address_templates=None):
    templates = address_templates if isinstance(address_templates, dict) else None
    if not isinstance(value, dict):
        return {}

    mode = str(value.get("mode") or "").strip().lower()
    if mode == "preset":
        template_id = normalize_address_template_id(value.get("template_id"))
        if not template_id:
            return {}
        if templates is not None and template_id not in templates:
            return {}
        return {
            "mode": "preset",
            "template_id": template_id
        }

    legal_address = normalize_legal_address(value.get("legal_address") or value)
    if legal_address:
        return {
            "mode": "manual",
            "legal_address": legal_address
        }
    return {}


def get_backend_channel_address_configs(settings=None, address_templates=None):
    settings = settings or load_settings()
    templates = address_templates if isinstance(address_templates, dict) else get_backend_address_templates(settings=settings)
    raw_configs = settings.get(BACKEND_CHANNEL_ADDRESS_CONFIGS_KEY)
    configs = {}

    if isinstance(raw_configs, dict):
        for raw_channel_id, raw_config in raw_configs.items():
            channel_id = str(raw_channel_id or "").strip().lower()
            if not channel_id:
                continue
            normalized = normalize_backend_channel_address_config(raw_config, address_templates=templates)
            if normalized:
                configs[channel_id] = normalized

    legacy_overrides = settings.get(BACKEND_CHANNEL_ADDRESS_OVERRIDES_KEY)
    if isinstance(legacy_overrides, dict):
        for raw_channel_id, raw_address in legacy_overrides.items():
            channel_id = str(raw_channel_id or "").strip().lower()
            if not channel_id or channel_id in configs:
                continue
            legal_address = normalize_legal_address(raw_address)
            if legal_address:
                configs[channel_id] = {
                    "mode": "manual",
                    "legal_address": legal_address
                }

    return configs


def save_backend_channel_address_configs(configs, settings=None):
    settings = settings or load_settings()
    templates = get_backend_address_templates(settings=settings)
    normalized = {}
    if isinstance(configs, dict):
        for raw_channel_id, raw_config in configs.items():
            channel_id = str(raw_channel_id or "").strip().lower()
            if not channel_id:
                continue
            config = normalize_backend_channel_address_config(raw_config, address_templates=templates)
            if config:
                normalized[channel_id] = config

    if normalized:
        settings[BACKEND_CHANNEL_ADDRESS_CONFIGS_KEY] = normalized
    else:
        settings.pop(BACKEND_CHANNEL_ADDRESS_CONFIGS_KEY, None)
    settings.pop(BACKEND_CHANNEL_ADDRESS_OVERRIDES_KEY, None)
    save_settings(settings)
    return settings


def build_backend_channel_address_config(channel_id, settings=None, address_configs=None, address_templates=None):
    normalized_id = str(channel_id or "").strip().lower()
    if not normalized_id:
        return {
            "mode": "inherit",
            "template_id": None,
            "template_name": None,
            "legal_address": {}
        }

    templates = address_templates if isinstance(address_templates, dict) else get_backend_address_templates(settings=settings)
    configs = address_configs if isinstance(address_configs, dict) else get_backend_channel_address_configs(
        settings=settings,
        address_templates=templates
    )
    config = configs.get(normalized_id) or {}
    mode = str(config.get("mode") or "").strip().lower()

    if mode == "preset":
        template_id = str(config.get("template_id") or "").strip().lower()
        template = templates.get(template_id) or {}
        legal_address = normalize_legal_address(template.get("legal_address"))
        if legal_address:
            return {
                "mode": "preset",
                "template_id": template_id,
                "template_name": template.get("name"),
                "legal_address": legal_address
            }

    if mode == "manual":
        legal_address = normalize_legal_address(config.get("legal_address"))
        if legal_address:
            return {
                "mode": "manual",
                "template_id": None,
                "template_name": None,
                "legal_address": legal_address
            }

    return {
        "mode": "inherit",
        "template_id": None,
        "template_name": None,
        "legal_address": {}
    }


def get_backend_channel_address_overrides(settings=None, address_configs=None, address_templates=None):
    settings = settings or load_settings()
    templates = address_templates if isinstance(address_templates, dict) else get_backend_address_templates(settings=settings)
    configs = address_configs if isinstance(address_configs, dict) else get_backend_channel_address_configs(
        settings=settings,
        address_templates=templates
    )
    overrides = {}
    for channel_id in configs.keys():
        resolved = build_backend_channel_address_config(
            channel_id,
            settings=settings,
            address_configs=configs,
            address_templates=templates
        )
        legal_address = normalize_legal_address(resolved.get("legal_address"))
        if legal_address:
            overrides[channel_id] = legal_address
    return overrides


def get_backend_channel_address_override(channel_id, settings=None, address_overrides=None, address_configs=None, address_templates=None):
    normalized_id = str(channel_id or "").strip().lower()
    if not normalized_id:
        return {}

    if isinstance(address_overrides, dict):
        return normalize_legal_address(address_overrides.get(normalized_id))

    return normalize_legal_address(
        build_backend_channel_address_config(
            normalized_id,
            settings=settings,
            address_configs=address_configs,
            address_templates=address_templates
        ).get("legal_address")
    )


def set_backend_channel_address_config(channel_id, mode=None, template_id=None, legal_address=None, settings=None):
    settings = settings or load_settings()
    normalized_id = str(channel_id or "").strip().lower()
    if not normalized_id:
        return False, "后端渠道 ID 无效", settings

    templates = get_backend_address_templates(settings=settings)
    configs = get_backend_channel_address_configs(settings=settings, address_templates=templates)
    normalized_mode = str(mode or "").strip().lower()

    if normalized_mode in ("", "inherit", "default", "none"):
        configs.pop(normalized_id, None)
        settings = save_backend_channel_address_configs(configs, settings=settings)
        return True, build_backend_channel_address_config(
            normalized_id,
            settings=settings,
            address_templates=templates
        ), settings

    if normalized_mode == "preset":
        normalized_template_id = normalize_address_template_id(template_id)
        if not normalized_template_id or normalized_template_id not in templates:
            return False, "请选择有效的地址模板", settings
        configs[normalized_id] = {
            "mode": "preset",
            "template_id": normalized_template_id
        }
    elif normalized_mode == "manual":
        normalized_address = normalize_legal_address(legal_address)
        if not normalized_address:
            return False, "请至少填写一个地址字段", settings
        configs[normalized_id] = {
            "mode": "manual",
            "legal_address": normalized_address
        }
    else:
        return False, "地址模式无效", settings

    settings = save_backend_channel_address_configs(configs, settings=settings)
    templates = get_backend_address_templates(settings=settings)
    return True, build_backend_channel_address_config(
        normalized_id,
        settings=settings,
        address_templates=templates
    ), settings


def set_backend_channel_address_override(channel_id, legal_address, settings=None):
    normalized_address = normalize_legal_address(legal_address)
    return set_backend_channel_address_config(
        channel_id,
        mode="manual" if normalized_address else "inherit",
        legal_address=normalized_address,
        settings=settings
    )


def remove_backend_channel_address_override(channel_id, settings=None):
    settings = settings or load_settings()
    normalized_id = str(channel_id or "").strip().lower()
    if not normalized_id:
        return settings

    configs = get_backend_channel_address_configs(settings=settings)
    changed = normalized_id in configs
    configs.pop(normalized_id, None)

    legacy_overrides = settings.get(BACKEND_CHANNEL_ADDRESS_OVERRIDES_KEY)
    if isinstance(legacy_overrides, dict) and normalized_id in legacy_overrides:
        changed = True

    if not changed:
        return settings
    return save_backend_channel_address_configs(configs, settings=settings)


def delete_backend_address_template(template_id, settings=None):
    settings = settings or load_settings()
    normalized_id = normalize_address_template_id(template_id)
    templates = get_backend_address_templates(settings=settings)
    if normalized_id not in templates:
        return False, "地址模板不存在", settings

    configs = get_backend_channel_address_configs(settings=settings, address_templates=templates)
    referenced_channels = [
        channel_id
        for channel_id, config in configs.items()
        if str(config.get("mode") or "").strip().lower() == "preset"
        and str(config.get("template_id") or "").strip().lower() == normalized_id
    ]
    if referenced_channels:
        return False, "该地址模板仍被后端渠道使用，请先解除绑定", settings

    templates.pop(normalized_id, None)
    settings = save_backend_address_templates(
        sorted(templates.values(), key=lambda item: item.get("name") or item.get("id") or ""),
        settings=settings
    )
    return True, "地址模板已删除", settings


def attach_backend_channel_address_override(config, settings=None, address_overrides=None, address_configs=None, address_templates=None):
    if not isinstance(config, dict):
        return config

    payload = dict(config)
    resolved_config = build_backend_channel_address_config(
        payload.get("id"),
        settings=settings,
        address_configs=address_configs,
        address_templates=address_templates
    )
    override = normalize_legal_address((address_overrides or {}).get(str(payload.get("id") or "").strip().lower()))
    if not override:
        override = normalize_legal_address(resolved_config.get("legal_address"))

    payload["address_mode"] = resolved_config.get("mode") or "inherit"
    payload["address_template_id"] = resolved_config.get("template_id")
    payload["address_template_name"] = resolved_config.get("template_name")
    payload["address_config"] = {
        "mode": payload["address_mode"],
        "template_id": payload.get("address_template_id"),
        "template_name": payload.get("address_template_name"),
        "legal_address": override
    }
    payload["legal_address_override"] = override
    payload["has_legal_address_override"] = bool(override)
    return payload


def apply_backend_channel_legal_address(base_address=None, channel_config=None, backend_channel_id=None, settings=None, address_overrides=None):
    legal_address = normalize_legal_address(base_address)
    override = {}

    if isinstance(channel_config, dict):
        override = normalize_legal_address(channel_config.get("legal_address_override"))
        if not backend_channel_id:
            backend_channel_id = channel_config.get("id")

    if not override and backend_channel_id:
        override = get_backend_channel_address_override(
            backend_channel_id,
            settings=settings,
            address_overrides=address_overrides
        )

    if not override:
        return legal_address

    merged = dict(legal_address)
    merged.update(override)
    return merged


def apply_card_legal_address_override(card_info, settings=None, address_overrides=None):
    if not isinstance(card_info, dict):
        return card_info

    payload = dict(card_info)
    payload["legal_address"] = apply_backend_channel_legal_address(
        payload.get("legal_address"),
        backend_channel_id=payload.get("backend_channel_id"),
        settings=settings,
        address_overrides=address_overrides
    )
    return payload


def get_timoes_backend_channels(settings=None):
    settings = settings or load_settings()
    disabled_channel_ids = get_disabled_timoes_backend_channel_ids(settings=settings)
    channels = {
        channel_id: build_timoes_backend_channel(
            channel_id=config["id"],
            head=config["head"],
            relay_code_type=config["relay_code_type"],
            label=config.get("label"),
            built_in=True
        )
        for channel_id, config in DEFAULT_TIMOES_BACKEND_CHANNELS.items()
        if channel_id not in disabled_channel_ids
    }

    used_ids = set(channels.keys())
    used_heads = {config["head"] for config in channels.values()}
    used_code_types = {config["relay_code_type"] for config in channels.values()}

    raw_channels = settings.get(TIMOES_BACKEND_CHANNELS_KEY)
    if not isinstance(raw_channels, list):
        return channels

    for item in raw_channels:
        if not isinstance(item, dict):
            continue
        head = normalize_timoes_channel_head(item.get("head"))
        relay_code_type = normalize_timoes_relay_code_type(item.get("relay_code_type"))
        if not head or not relay_code_type:
            continue

        channel_id = normalize_timoes_backend_channel_id(
            item.get("id"),
            head=head,
            relay_code_type=relay_code_type
        )
        if channel_id in used_ids or head in used_heads or relay_code_type in used_code_types:
            continue

        channels[channel_id] = build_timoes_backend_channel(
            channel_id=channel_id,
            head=head,
            relay_code_type=relay_code_type,
            label=item.get("label"),
            built_in=False
        )
        used_ids.add(channel_id)
        used_heads.add(head)
        used_code_types.add(relay_code_type)

    return channels


def serialize_timoes_backend_channels(settings=None):
    channels = list(get_timoes_backend_channels(settings=settings).values())
    channels.sort(key=lambda item: (0 if item.get("built_in") else 1, item.get("head") or item.get("relay_code_type") or item.get("label") or ""))
    return channels


def get_timoes_code_types(settings=None, stats=None):
    ordered = []
    for config in get_timoes_backend_channels(settings=settings).values():
        relay_code_type = config.get("relay_code_type")
        if relay_code_type and relay_code_type not in ordered:
            ordered.append(relay_code_type)

    for relay_code_type in (stats.get("types") or {}).keys() if isinstance(stats, dict) else ():
        normalized_type = normalize_timoes_relay_code_type(relay_code_type)
        if normalized_type and normalized_type not in ordered:
            ordered.append(normalized_type)

    return ordered


def save_custom_timoes_backend_channels(channels, settings=None):
    settings = settings or load_settings()
    settings[TIMOES_BACKEND_CHANNELS_KEY] = [
        {
            "id": item["id"],
            "label": item["label"],
            "head": item["head"],
            "relay_code_type": item["relay_code_type"]
        }
        for item in channels
    ]
    save_settings(settings)
    return settings


def upsert_timoes_backend_channel(data, settings=None):
    settings = settings or load_settings()
    payload = dict(data or {})
    requested_id = str(payload.get("id") or "").strip().lower()
    existing_channels = get_timoes_backend_channels(settings=settings)

    if requested_id:
        existing_channel = existing_channels.get(requested_id)
        if not existing_channel:
            return False, "Timoes 后端渠道不存在", settings
        if existing_channel.get("built_in"):
            return False, "内置 Timoes 渠道不支持编辑", settings

    head = normalize_timoes_channel_head(payload.get("head"))
    if not head:
        return False, "请输入 4-8 位数字卡头", settings

    relay_code_type = normalize_timoes_relay_code_type(payload.get("relay_code_type"))
    if not relay_code_type:
        return False, "请输入有效的线路类型", settings

    channel_id = requested_id or normalize_timoes_backend_channel_id(
        head=head,
        relay_code_type=relay_code_type
    )
    label = (str(payload.get("label") or "").strip() or f"{head} 卡头")[:40]

    for existing_id, existing in existing_channels.items():
        if requested_id and existing_id == requested_id:
            continue
        if existing.get("head") == head:
            return False, f"卡头 {head} 已存在", settings
        if existing.get("relay_code_type") == relay_code_type:
            return False, f"线路类型 {relay_code_type} 已存在", settings
        if existing_id == channel_id:
            return False, f"渠道 ID {channel_id} 已存在", settings

    custom_channels = [
        dict(item)
        for item in serialize_timoes_backend_channels(settings=settings)
        if not item.get("built_in")
    ]
    new_channel = build_timoes_backend_channel(
        channel_id=channel_id,
        head=head,
        relay_code_type=relay_code_type,
        label=label,
        built_in=False
    )

    replaced = False
    for index, item in enumerate(custom_channels):
        if item["id"] == channel_id:
            custom_channels[index] = new_channel
            replaced = True
            break
    if not replaced:
        custom_channels.append(new_channel)

    custom_channels.sort(key=lambda item: (item.get("head") or "", item.get("relay_code_type") or "", item.get("label") or ""))
    settings = save_custom_timoes_backend_channels(custom_channels, settings=settings)
    return True, new_channel, settings


def delete_timoes_backend_channel(channel_id, settings=None):
    settings = settings or load_settings()
    normalized_id = str(channel_id or "").strip().lower()
    if not normalized_id:
        return False, "渠道 ID 无效", settings

    existing_channels = get_timoes_backend_channels(settings=settings)
    target = existing_channels.get(normalized_id)
    if not target:
        return False, "Timoes 后端渠道不存在", settings

    referenced_display_routes = [
        item for item in get_display_channels(settings=settings, fallback_to_default=False)
        if str(item.get("backend_channel_id") or "").strip().lower() == normalized_id
    ]
    if referenced_display_routes:
        return False, "该后端渠道仍被前台显示渠道使用，请先解除路由", settings

    if target.get("built_in"):
        disabled_channel_ids = get_disabled_timoes_backend_channel_ids(settings=settings)
        disabled_channel_ids.add(normalized_id)
        settings = save_disabled_timoes_backend_channel_ids(disabled_channel_ids, settings=settings)
        settings = remove_backend_channel_address_override(normalized_id, settings=settings)
        return True, "Timoes 后端渠道已删除", settings

    custom_channels = [
        dict(item)
        for item in serialize_timoes_backend_channels(settings=settings)
        if not item.get("built_in") and item["id"] != normalized_id
    ]
    settings = save_custom_timoes_backend_channels(custom_channels, settings=settings)
    settings = remove_backend_channel_address_override(normalized_id, settings=settings)
    return True, "Timoes 后端渠道已删除", settings


def get_manual_backend_channel_id(bin_code):
    return f"manual_bin_{''.join(ch for ch in str(bin_code or '') if ch.isdigit())}"


def parse_manual_backend_channel_id(channel_id):
    match = MANUAL_BACKEND_CHANNEL_PATTERN.match(str(channel_id or "").strip().lower())
    return match.group(1) if match else None


def is_valid_backend_channel_id(channel_id, settings=None):
    normalized = str(channel_id or "").strip().lower()
    if not normalized:
        return False
    return normalized in get_backend_channels(settings=settings) or bool(parse_manual_backend_channel_id(normalized))


def get_backend_channels(username=None, settings=None):
    settings = settings or load_settings()
    address_templates = get_backend_address_templates(settings=settings)
    address_configs = get_backend_channel_address_configs(settings=settings, address_templates=address_templates)
    address_overrides = get_backend_channel_address_overrides(
        settings=settings,
        address_configs=address_configs,
        address_templates=address_templates
    )
    channels = {
        channel_id: attach_backend_channel_address_override(
            config,
            settings=settings,
            address_overrides=address_overrides,
            address_configs=address_configs,
            address_templates=address_templates
        )
        for channel_id, config in BUILTIN_MERCURY_BACKEND_CHANNELS.items()
    }
    channels.update({
        channel_id: attach_backend_channel_address_override(
            config,
            settings=settings,
            address_overrides=address_overrides,
            address_configs=address_configs,
            address_templates=address_templates
        )
        for channel_id, config in get_timoes_backend_channels(settings=settings).items()
    })

    # 渠道池库存为共享资源，所有登录用户看到同一份可用量。
    timoes_stats = get_timoes_pool_stats(
        allowed_code_types=get_timoes_code_types(settings=settings)
    )
    for channel_id, config in channels.items():
        relay_code_type = config.get("relay_code_type")
        if relay_code_type:
            pool_stats = (timoes_stats.get("types") or {}).get(relay_code_type, {})
            config["available_count"] = pool_stats.get("available", 0)
            config["used_count"] = pool_stats.get("used", 0)
            config["invalid_count"] = pool_stats.get("invalid", 0)
            config["total_count"] = pool_stats.get("total", 0)

    manual_stats = get_manual_card_pool_stats()
    for bin_code, stats in (manual_stats.get("bins") or {}).items():
        backend_id = get_manual_backend_channel_id(bin_code)
        channels[backend_id] = attach_backend_channel_address_override({
            "id": backend_id,
            "provider": "manual",
            "provider_label": "手动卡池",
            "label": f"{bin_code} 手动卡池",
            "head": bin_code,
            "manual_bin": bin_code,
            "available_count": stats.get("available", 0),
            "used_count": stats.get("used", 0),
            "invalid_count": stats.get("invalid", 0),
            "total_count": stats.get("total", 0)
        }, settings=settings, address_overrides=address_overrides, address_configs=address_configs, address_templates=address_templates)

    return channels


def resolve_backend_channel(channel_id, username=None, settings=None):
    normalized = str(channel_id or "").strip().lower()
    channels = get_backend_channels(username=username, settings=settings)
    if normalized in channels:
        return channels[normalized]

    manual_bin = parse_manual_backend_channel_id(normalized)
    if manual_bin:
        return attach_backend_channel_address_override({
            "id": normalized,
            "provider": "manual",
            "provider_label": "手动卡池",
            "label": f"{manual_bin} 手动卡池",
            "head": manual_bin,
            "manual_bin": manual_bin,
            "available_count": 0,
            "used_count": 0,
            "invalid_count": 0,
            "total_count": 0
        }, settings=settings)

    return None


def serialize_backend_channel_options(settings=None, username=None, include_address_override=False):
    channels = []
    for channel_id, config in get_backend_channels(username=username, settings=settings).items():
        payload = {
            "id": channel_id,
            "label": config.get("label", channel_id),
            "backend_channel_id": channel_id,
            "backend_head": config.get("head"),
            "backend_provider": config.get("provider"),
            "backend_provider_label": config.get("provider_label"),
            "relay_code_type": config.get("relay_code_type"),
            "built_in": bool(config.get("built_in")),
            "editable": bool(config.get("editable", False)),
            "deletable": bool(config.get("deletable", False)),
            "backend_available_count": config.get("available_count", 0),
            "backend_total_count": config.get("total_count", 0),
            "has_legal_address_override": bool(config.get("has_legal_address_override")),
            "address_mode": config.get("address_mode") or "inherit"
        }
        if include_address_override:
            payload["legal_address_override"] = normalize_legal_address(config.get("legal_address_override"))
            payload["address_template_id"] = config.get("address_template_id")
            payload["address_template_name"] = config.get("address_template_name")
            payload["address_config"] = dict(config.get("address_config") or {})
        channels.append(payload)

    channels.sort(key=lambda item: (0 if item.get("backend_provider") == "mercury" else 1, item.get("backend_head") or item.get("label") or ""))
    return channels


def build_backend_address_admin_payload(settings=None, username=None):
    settings = settings or load_settings()
    return {
        "channels": serialize_backend_channel_options(
            settings=settings,
            username=username,
            include_address_override=True
        ),
        "address_templates": serialize_backend_address_templates(settings=settings)
    }


def build_default_display_channels(settings=None):
    settings = settings or {}
    legacy_enabled = settings.get("enabled_redeem_channels")
    if isinstance(legacy_enabled, list):
        defaults = []
        seen = set()
        for channel_id in legacy_enabled:
            normalized = str(channel_id or "").strip().lower()
            config = resolve_backend_channel(normalized, settings=settings)
            if not config or normalized in seen:
                continue
            defaults.append({
                "id": normalized,
                "name": config["label"],
                "backend_channel_id": normalized,
                "enabled": True
            })
            seen.add(normalized)
        if defaults:
            return defaults

    defaults = []
    for item in DEFAULT_DISPLAY_CHANNELS:
        backend_channel_id = str(item.get("backend_channel_id") or "").strip().lower()
        config = resolve_backend_channel(backend_channel_id, settings=settings)
        if not config:
            continue
        defaults.append({
            "id": item["id"],
            "name": config.get("label") or item.get("name") or item["id"],
            "backend_channel_id": backend_channel_id,
            "enabled": bool(item.get("enabled", True))
        })
    return defaults


def normalize_display_channel_id(raw_value, fallback_name=None):
    candidate = str(raw_value or "").strip().lower()
    if DISPLAY_CHANNEL_ID_PATTERN.match(candidate):
        return candidate

    base = re.sub(r'[^a-z0-9_-]+', '-', str(fallback_name or raw_value or "").strip().lower())
    base = re.sub(r'-{2,}', '-', base).strip('-_')
    if not base:
        base = f"channel-{uuid.uuid4().hex[:8]}"
    if not DISPLAY_CHANNEL_ID_PATTERN.match(base):
        base = f"channel-{uuid.uuid4().hex[:8]}"
    return base


def normalize_display_channels(raw_channels, fallback_to_default=True, settings=None):
    if not isinstance(raw_channels, list):
        return build_default_display_channels(settings) if fallback_to_default else []

    settings = settings or load_settings()
    normalized = []
    seen = set()
    for item in raw_channels:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("label") or "").strip()
        backend_channel_id = str(item.get("backend_channel_id") or "").strip().lower()
        if not name or not is_valid_backend_channel_id(backend_channel_id, settings=settings):
            continue
        channel_id = normalize_display_channel_id(item.get("id"), name)
        if channel_id in seen:
            channel_id = normalize_display_channel_id(f"{channel_id}-{uuid.uuid4().hex[:4]}", name)
        normalized.append({
            "id": channel_id,
            "name": name[:40],
            "backend_channel_id": backend_channel_id,
            "enabled": bool(item.get("enabled", True))
        })
        seen.add(channel_id)

    if normalized or not fallback_to_default:
        return normalized
    return build_default_display_channels(settings)


def get_display_channels(settings=None, fallback_to_default=True):
    settings = settings or load_settings()
    if "display_redeem_channels" in settings:
        return normalize_display_channels(
            settings.get("display_redeem_channels"),
            fallback_to_default=False,
            settings=settings
        )
    return build_default_display_channels(settings) if fallback_to_default else []


def serialize_display_channels(channels=None, username=None, public_only=False):
    if channels is None:
        channels = get_display_channels()

    serialized = []
    for item in channels:
        backend = resolve_backend_channel(item.get("backend_channel_id"), username=username)
        payload = {
            "id": item["id"],
            "label": item["name"],
            "name": item["name"],
            "enabled": bool(item.get("enabled", True)),
            "backend_channel_id": item["backend_channel_id"],
            "backend_label": backend.get("label") if backend else item["backend_channel_id"],
            "backend_provider": backend.get("provider") if backend else None,
            "backend_provider_label": backend.get("provider_label") if backend else None,
            "backend_head": backend.get("head") if backend else None,
            "backend_available_count": backend.get("available_count", 0) if backend else 0,
            "backend_total_count": backend.get("total_count", 0) if backend else 0
        }
        if not public_only or payload["enabled"]:
            serialized.append(payload)
    return serialized


def get_enabled_display_channels(username=None, settings=None):
    settings = settings or load_settings()
    return serialize_display_channels(
        get_display_channels(settings=settings),
        username=username,
        public_only=True
    )


def resolve_bound_display_channel(display_channel_id, username=None, settings=None):
    normalized = str(display_channel_id or "").strip().lower()
    if not normalized:
        return None

    for item in get_enabled_display_channels(username=username, settings=settings):
        if item.get("id") == normalized:
            return {
                "display_channel_id": item["id"],
                "display_channel_name": item.get("name") or item.get("label") or item["id"],
                "backend_channel_id": item.get("backend_channel_id"),
                "channel_head": item.get("backend_head")
            }
    return None


def build_key_binding_payload(data):
    bound_display_channel_id = str(
        data.get("bound_display_channel_id")
        or data.get("display_channel_id")
        or ""
    ).strip().lower()
    bound_backend_channel_id = str(
        data.get("bound_backend_channel_id")
        or data.get("backend_channel_id")
        or ""
    ).strip().lower()
    bound_display_channel_name = str(
        data.get("bound_display_channel_name")
        or data.get("display_channel_name")
        or data.get("name")
        or data.get("label")
        or ""
    ).strip()
    bound_channel_head = ''.join(
        ch for ch in str(data.get("bound_channel_head") or data.get("channel_head") or data.get("backend_head") or "")
        if ch.isdigit()
    )
    locked = bool(bound_display_channel_id and bound_backend_channel_id)
    return {
        "bound_display_channel_id": bound_display_channel_id or None,
        "bound_display_channel_name": bound_display_channel_name or None,
        "bound_backend_channel_id": bound_backend_channel_id or None,
        "bound_channel_head": bound_channel_head or None,
        "channel_binding_enabled": locked,
        "display_channel_locked": locked
    }


def derive_generated_key_card_type(bound_channel, username=None):
    backend_channel_id = (bound_channel or {}).get("backend_channel_id")
    if not backend_channel_id:
        return "credit"

    backend_config = resolve_backend_channel(backend_channel_id, username=username)
    if backend_config and backend_config.get("card_type") in ("credit", "debit"):
        return backend_config["card_type"]
    return "credit"


def normalize_key_kind(value):
    return "old_card" if str(value or "").strip().lower() == "old_card" else "normal"


def serialize_old_card_pool_channels(stats=None, settings=None):
    settings = settings or load_settings()
    stats = stats or get_old_card_pool_stats()
    serialized = []

    for item in stats.get("channels", []):
        backend_channel_id = str(item.get("backend_channel_id") or "").strip().lower()
        if not backend_channel_id:
            continue
        backend_config = resolve_backend_channel(backend_channel_id, settings=settings)
        serialized.append({
            "backend_channel_id": backend_channel_id,
            "label": (backend_config or {}).get("label") or (item.get("head") and f"{item.get('head')} 卡头") or backend_channel_id,
            "head": item.get("head"),
            "provider": item.get("provider"),
            "provider_label": item.get("provider_label") or (backend_config or {}).get("provider_label"),
            "available_count": int(item.get("available_count") or 0),
            "used_count": int(item.get("used_count") or 0),
            "invalid_count": int(item.get("invalid_count") or 0),
            "total_count": int(item.get("total_count") or 0),
            "enabled": is_old_card_backend_channel_enabled(backend_channel_id, settings=settings)
        })

    serialized.sort(key=lambda item: (item.get("provider") or "", item.get("head") or item.get("label") or ""))
    return serialized


def get_enabled_old_card_backend_channel_ids(stats=None, settings=None):
    return {
        item.get("backend_channel_id")
        for item in serialize_old_card_pool_channels(stats=stats, settings=settings)
        if item.get("backend_channel_id") and item.get("enabled", True)
    }


def resolve_create_backend_channel(backend_channel_id=None, card_type=None, username=None):
    normalized_backend_channel_id = str(backend_channel_id or "").strip().lower()
    if normalized_backend_channel_id:
        return resolve_backend_channel(normalized_backend_channel_id, username=username)

    fallback_card_type = str(card_type or "credit").strip().lower()
    if fallback_card_type == "debit":
        return resolve_backend_channel("mercury_54810871", username=username)
    return resolve_backend_channel("mercury_52368601", username=username)


def backend_channel_supports_direct_card_controls(channel_config):
    return bool(channel_config and channel_config.get("provider") == "mercury")


def append_created_card(card_info):
    with card_lock:
        data = load_cards()
        data["cards"].append(card_info)
        save_cards(data)


def remove_created_card(card_id):
    with card_lock:
        data = load_cards()
        original = len(data.get("cards", []))
        data["cards"] = [card for card in data.get("cards", []) if card.get("card_id") != card_id]
        save_cards(data)
        return len(data["cards"]) != original


def build_direct_creation_extra_info(card_info):
    return {
        "provider": card_info.get("provider"),
        "provider_label": card_info.get("provider_label"),
        "channel_head": card_info.get("channel_head"),
        "backend_channel_id": card_info.get("backend_channel_id"),
        "channel_label": card_info.get("channel_label"),
        "legal_address": card_info.get("legal_address", {}),
        "destroy_supported": bool(card_info.get("destroy_supported")),
        "created_time": card_info.get("created_time"),
    }


def derive_direct_creation_card_type(channel_config):
    if not channel_config:
        return "credit"
    provider = str(channel_config.get("provider") or "").strip().lower()
    if provider == "mercury":
        return channel_config.get("card_type", "credit")
    return provider or "external"


def should_remote_cancel_created_card(card_info):
    if not isinstance(card_info, dict):
        return True
    provider = str(card_info.get("provider") or "").strip().lower()
    if provider and provider != "mercury":
        return False
    return bool(card_info.get("destroy_supported", True))


def get_safe_cancel_card_type(card_info):
    card_type = str((card_info or {}).get("card_type") or "").strip().lower()
    return card_type if card_type in ("credit", "debit") else "credit"


@app.route('/api/settings/announcement', methods=['GET'])
@require_admin
def api_get_announcement():
    """获取公告内容"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "content": settings.get("announcement", ""),
        "announcement_size": settings.get("announcement_size", 100)
    })


@app.route('/api/settings/announcement', methods=['POST'])
@require_admin
def api_set_announcement():
    """设置公告内容"""
    try:
        data = request.json
        content = data.get('content', '')
        announcement_size = data.get('announcement_size', 100)
        # 限制范围 50-150
        announcement_size = max(50, min(150, int(announcement_size)))
        settings = load_settings()
        settings['announcement'] = content
        settings['announcement_size'] = announcement_size
        settings['announcement_time'] = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        save_settings(settings)
        return jsonify({"success": True, "message": "公告已保存"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/announcement', methods=['GET'])
def api_get_public_announcement():
    """获取公告内容（公开API，用于兑换页面）"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "content": settings.get("announcement", ""),
        "time": settings.get("announcement_time", ""),
        "size": settings.get("announcement_size", 100)
    })


@app.route('/api/transaction-query-status', methods=['GET'])
def api_get_transaction_query_status():
    """获取消费记录查询开关状态（公开API，用于兑换页面）"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "enabled": settings.get("transaction_query_enabled", True)
    })


@app.route('/api/settings/maintenance', methods=['GET'])
@require_admin
def api_get_maintenance():
    """获取维护模式状态"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "maintenance_mode": settings.get("maintenance_mode", False)
    })


@app.route('/api/settings/maintenance', methods=['POST'])
@require_admin
def api_set_maintenance():
    """设置维护模式"""
    try:
        data = request.json
        maintenance_mode = data.get('maintenance_mode', False)
        settings = load_settings()
        settings['maintenance_mode'] = bool(maintenance_mode)
        save_settings(settings)
        return jsonify({
            "success": True,
            "message": "维护模式已" + ("开启" if maintenance_mode else "关闭")
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/settings/transaction-query', methods=['GET'])
@require_admin
def api_get_transaction_query():
    """获取消费记录查询开关状态"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "transaction_query_enabled": settings.get("transaction_query_enabled", True)
    })


@app.route('/api/settings/transaction-query', methods=['POST'])
@require_admin
def api_set_transaction_query():
    """设置消费记录查询开关"""
    try:
        data = request.json
        enabled = data.get('transaction_query_enabled', True)
        settings = load_settings()
        settings['transaction_query_enabled'] = bool(enabled)
        save_settings(settings)
        return jsonify({
            "success": True,
            "message": "消费记录查询已" + ("开启" if enabled else "关闭")
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/settings/batch', methods=['GET'])
@require_admin
def api_get_batch_settings():
    """获取兑换页面设置"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "max_batch_count": settings.get("max_batch_count", 5),
        "batch_interval": settings.get("batch_interval", 5)
    })


@app.route('/api/settings/batch', methods=['POST'])
@require_admin
def api_set_batch_settings():
    """设置兑换页面参数"""
    try:
        data = request.json or {}
        max_batch_count = int(data.get('max_batch_count', 5))
        batch_interval = int(data.get('batch_interval', 5))

        # 限制范围
        max_batch_count = max(1, min(20, max_batch_count))
        batch_interval = max(1, min(30, batch_interval))

        settings = load_settings()
        settings['max_batch_count'] = max_batch_count
        settings['batch_interval'] = batch_interval
        save_settings(settings)
        return jsonify({"success": True, "message": "设置已保存"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/batch-settings', methods=['GET'])
def api_get_public_batch_settings():
    """获取批量兑换设置（公开API，用于兑换页面）"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "max_batch_count": settings.get("max_batch_count", 5),
        "batch_interval": settings.get("batch_interval", 5),
        "channels": serialize_display_channels(
            get_display_channels(settings=settings),
            public_only=True
        )
    })


@app.route('/api/settings/db-config', methods=['GET'])
@require_admin
def api_get_db_config():
    """获取数据库配置"""
    settings = load_settings()
    db_config = settings.get("db_config", {})
    return jsonify({
        "success": True,
        "db_config": {
            "host": db_config.get("host", "localhost"),
            "port": db_config.get("port", 5432),
            "database": db_config.get("database", "mercury"),
            "user": db_config.get("user", "mercury"),
            "password": db_config.get("password", ""),
            "pool_min": db_config.get("pool_min", 2),
            "pool_max": db_config.get("pool_max", 20)
        }
    })


@app.route('/api/settings/db-config', methods=['POST'])
@require_admin
def api_set_db_config():
    """设置数据库配置"""
    try:
        data = request.json

        db_config = {
            "host": data.get("host", "localhost").strip(),
            "port": int(data.get("port", 5432)),
            "database": data.get("database", "mercury").strip(),
            "user": data.get("user", "mercury").strip(),
            "password": data.get("password", ""),
            "pool_min": max(1, min(50, int(data.get("pool_min", 2)))),
            "pool_max": max(1, min(100, int(data.get("pool_max", 20))))
        }

        settings = load_settings()
        settings['db_config'] = db_config
        save_settings(settings)

        # 热重载数据库连接池
        success, message = reload_db_config()
        if success:
            return jsonify({"success": True, "message": "数据库配置已保存并生效"})
        else:
            return jsonify({"success": True, "message": f"配置已保存，但重载失败: {message}，需重启服务"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/settings/db-config/test', methods=['POST'])
@require_admin
def api_test_db_connection():
    """测试数据库连接"""
    try:
        data = request.json

        host = data.get("host", "localhost").strip()
        port = int(data.get("port", 5432))
        database = data.get("database", "mercury").strip()
        user = data.get("user", "mercury").strip()
        password = data.get("password", "")

        import psycopg2

        # 尝试连接数据库
        conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            connect_timeout=5
        )

        # 执行简单查询验证连接
        cursor = conn.cursor()
        cursor.execute("SELECT version()")
        version = cursor.fetchone()[0]
        cursor.close()
        conn.close()

        return jsonify({
            "success": True,
            "message": "连接成功",
            "version": version
        })
    except ImportError:
        return jsonify({
            "success": False,
            "error": "未安装 psycopg2 模块，请先运行: pip install psycopg2-binary"
        }), 500
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"连接失败: {str(e)}"
        }), 500


# GitHub 私有仓库配置
GITHUB_TOKEN = "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"
GITHUB_REPO = "tianzhentech/niko"


@app.route('/api/check-update', methods=['GET'])
@require_admin
def api_check_update():
    """检查远程仓库是否有新版本"""
    import requests as req
    try:
        current_version = get_version()

        # 从 GitHub API 获取最新的 tags
        url = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        resp = req.get(url, headers=headers, timeout=10)

        if resp.status_code != 200:
            return jsonify({"success": False, "error": f"GitHub API 请求失败: {resp.status_code}"})

        tags = resp.json()
        if not tags:
            return jsonify({"success": True, "has_update": False, "current_version": current_version})

        # 获取最新 tag
        latest_tag = tags[0]["name"]
        latest_version = latest_tag

        # 比较版本号（都去掉 v 前缀再比较）
        current_normalized = current_version.lstrip("v")
        latest_normalized = latest_version.lstrip("v")
        has_update = latest_normalized != current_normalized

        return jsonify({
            "success": True,
            "has_update": has_update,
            "current_version": current_version,
            "latest_version": latest_version
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/version', methods=['GET'])
def api_get_version():
    """获取当前版本号"""
    return jsonify({"success": True, "version": get_version()})


@app.route('/api/perform-update', methods=['GET'])
def api_perform_update():
    """执行更新 - SSE 流式返回更新进度"""
    import subprocess

    token = request.args.get('token')
    if not token:
        return jsonify({"error": "未授权"}), 401

    payload = verify_access_token(token)
    if not payload or not payload.get('is_admin'):
        return jsonify({"error": "需要管理员权限"}), 403

    def generate():
        try:
            # 发送开始拉取代码的消息
            yield f"data: {json.dumps({'status': 'pulling', 'message': '正在从 GitHub 拉取最新代码...'})}\n\n"

            # 执行 git pull
            script_dir = os.path.dirname(__file__)
            result = subprocess.run(
                ['git', 'pull', 'origin', 'main'],
                cwd=script_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                yield f"data: {json.dumps({'status': 'error', 'message': f'Git pull 失败: {result.stderr}'})}\n\n"
                return

            yield f"data: {json.dumps({'status': 'pulling', 'message': result.stdout.strip() or '代码已更新'})}\n\n"

            # 发送重启服务的消息
            yield f"data: {json.dumps({'status': 'restarting', 'message': '正在重启服务...'})}\n\n"

            # 异步执行重启命令（使用 nohup 确保命令在连接断开后继续执行）
            subprocess.Popen(
                ['sudo', 'systemctl', 'restart', 'niko'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )

            yield f"data: {json.dumps({'status': 'done', 'message': '更新完成'})}\n\n"

        except subprocess.TimeoutExpired:
            yield f"data: {json.dumps({'status': 'error', 'message': 'Git pull 超时'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
    })


@app.route('/api/proxies/latency-stream', methods=['GET'])
def api_proxy_latency_stream():
    """代理延迟 SSE 推送"""
    token = request.args.get('token')
    if not token:
        return jsonify({"error": "未授权"}), 401

    payload = verify_access_token(token)
    if not payload or not payload.get('is_admin'):
        return jsonify({"error": "需要管理员权限"}), 403

    def generate():
        q = subscribe_proxy_latency()
        try:
            # 先发送当前缓存
            cache = get_proxy_latency_cache()
            if cache:
                yield f"data: {json.dumps(cache)}\n\n"

            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except:
                    yield f": keepalive\n\n"
        finally:
            unsubscribe_proxy_latency(q)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/mercury-accounts/<user_id>/card-counts', methods=['GET'])
@require_admin
def api_get_mercury_card_counts(user_id):
    """获取 Mercury 账户的卡片数量"""
    try:
        minutes_ago = request.args.get('minutes_ago', 0, type=int)
        result = get_mercury_card_counts(user_id, minutes_ago=minutes_ago)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/<user_id>/cards', methods=['GET'])
@require_admin
def api_get_mercury_cards(user_id):
    """获取 Mercury 账户的所有卡片（用于前端缓存和本地过滤）"""
    from account.accounts import get_account_by_user_id, list_mercury_cards, refresh_account

    try:
        account = get_account_by_user_id(user_id)
        if not account:
            return jsonify({"success": False, "error": "账户不存在"}), 404

        # 如果账户被限制，直接返回空卡片列表，不尝试刷新
        if account.get("account_status") == "restricted":
            return jsonify({"success": True, "cards": [], "restricted": True})

        # 使用账户姓名作为持卡人过滤条件
        cardholder_name = account.get("name", "")
        success, cards = list_mercury_cards(account, cardholder_name_filter=cardholder_name, status_filter="active")

        if success:
            return jsonify({"success": True, "cards": cards})

        # 请求失败，可能是 session 过期，尝试刷新账户
        original_error = cards
        print(f"[API] 获取卡片失败 (错误: {original_error})，尝试刷新账户 {user_id} 的 session...")
        refresh_result = refresh_account(user_id)

        if not refresh_result.get("success"):
            # 刷新失败，返回原始错误
            print(f"[API] 账户 {user_id} session 刷新失败: {refresh_result.get('error', '未知错误')}")
            return jsonify({"success": False, "error": f"Session 已过期且刷新失败: {refresh_result.get('error', original_error)}"}), 400

        print(f"[API] 账户 {user_id} session 刷新成功，正在重新获取卡片...")

        # 刷新成功，重新获取账户信息并重试
        account = get_account_by_user_id(user_id)
        if not account:
            return jsonify({"success": False, "error": "刷新后账户不存在"}), 404

        success, cards = list_mercury_cards(account, cardholder_name_filter=cardholder_name, status_filter="active")

        if success:
            print(f"[API] 账户 {user_id} 重新获取卡片成功，共 {len(cards)} 张")
            return jsonify({"success": True, "cards": cards})
        else:
            print(f"[API] 账户 {user_id} 刷新后仍然获取卡片失败: {cards}")
            return jsonify({"success": False, "error": cards}), 400
    except Exception as e:
        print(f"[API] 获取卡片异常: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/cards-batch', methods=['POST'])
@require_admin
def api_get_mercury_cards_batch():
    """
    批量获取组织下所有账户的卡片
    同一组织只请求一次 Mercury API，然后根据持卡人姓名分配给各个账户

    请求体:
    {
        "organization": "组织名称"  // 可选，不传则获取全部
    }

    返回:
    {
        "success": true,
        "cards_by_user": {
            "user_id_1": [cards...],
            "user_id_2": [cards...]
        }
    }
    """
    from account.accounts import load_accounts, list_mercury_cards

    try:
        data = request.json or {}
        org_filter = data.get("organization", "")

        # 加载所有账户
        accounts_data = load_accounts()
        accounts = accounts_data.get("accounts", [])

        # 按组织筛选
        if org_filter:
            accounts = [acc for acc in accounts if acc.get("organization") == org_filter]

        if not accounts:
            return jsonify({"success": True, "cards_by_user": {}})

        # 按组织分组
        org_groups = {}
        for acc in accounts:
            org_name = acc.get("organization", "unknown")
            if org_name not in org_groups:
                org_groups[org_name] = []
            org_groups[org_name].append(acc)

        # 结果字典
        cards_by_user = {}

        # 每个组织只请求一次
        for org_name, org_accounts in org_groups.items():
            # 找到第一个非 restricted 且有 session 的 active 账户
            active_account = None
            for acc in org_accounts:
                if acc.get("account_status") == "restricted":
                    # restricted 账户直接返回空卡片列表
                    cards_by_user[acc["user_id"]] = []
                    continue
                if acc.get("account_status") == "active" and acc.get("_SESSION"):
                    active_account = acc
                    break

            if not active_account:
                for acc in org_accounts:
                    if acc.get("_SESSION"):
                        active_account = acc
                        break

            if not active_account:
                # 该组织没有可用账户，所有账户返回空列表
                for acc in org_accounts:
                    cards_by_user[acc["user_id"]] = []
                continue

            # 请求该组织的所有卡片（不过滤持卡人）
            success, all_cards = list_mercury_cards(active_account, cardholder_name_filter=None, status_filter="active")

            if not success:
                # 请求失败，所有账户返回空列表
                for acc in org_accounts:
                    cards_by_user[acc["user_id"]] = []
                continue

            # 建立持卡人姓名到 user_id 的映射
            name_to_user_id = {}
            for acc in org_accounts:
                name = acc.get("name", "")
                if name:
                    name_to_user_id[name] = acc["user_id"]
                # 初始化空列表
                cards_by_user[acc["user_id"]] = []

            # 根据持卡人姓名分配卡片
            for card in all_cards:
                cardholder_name = card.get("cardholder_name", "")
                user_id = name_to_user_id.get(cardholder_name)
                if user_id and user_id in cards_by_user:
                    cards_by_user[user_id].append(card)

        return jsonify({"success": True, "cards_by_user": cards_by_user})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/status-stream', methods=['GET'])
@require_admin
def api_mercury_accounts_status_stream():
    """SSE 流式推送账户状态和卡片数量更新"""
    import queue as queue_module

    def generate():
        q = subscribe_status_updates()
        try:
            # 发送初始连接确认
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"

            while True:
                try:
                    # 等待事件，超时 30 秒发送心跳
                    event = q.get(timeout=30)
                    # 事件已包含 type 字段，直接发送
                    yield f"data: {json.dumps(event)}\n\n"
                except queue_module.Empty:
                    # 超时，发送心跳保持连接
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                except GeneratorExit:
                    # 客户端断开连接，退出循环
                    return
        finally:
            unsubscribe_status_updates(q)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/mercury-accounts/<user_id>/clear-cards', methods=['POST'])
@require_admin
def api_clear_mercury_cards(user_id):
    """清空 Mercury 账户的卡片"""
    try:
        data = request.json or {}
        card_type = data.get('card_type')  # "credit" or "debit" or None for all
        result = clear_mercury_cards(user_id, card_type_filter=card_type)
        if result["success"]:
            return jsonify(result)
        else:
            return jsonify(result), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/mercury-accounts/<user_id>/clear-cards-stream', methods=['GET'])
@require_admin
def api_clear_mercury_cards_stream(user_id):
    """流式清空 Mercury 账户的卡片"""
    from account.accounts import get_account_by_user_id, list_mercury_cards

    card_type = request.args.get('card_type')  # "credit" or "debit"
    minutes_ago = request.args.get('minutes_ago', 0, type=int)

    def generate():
        account = get_account_by_user_id(user_id)
        if not account:
            yield f"data: {json.dumps({'type': 'error', 'message': '账户不存在'})}\n\n"
            return

        cardholder_name = account.get("name", "")
        success, cards = list_mercury_cards(
            account,
            card_type_filter=card_type,
            cardholder_name_filter=cardholder_name,
            minutes_ago=minutes_ago,
            status_filter="active"
        )

        if not success:
            yield f"data: {json.dumps({'type': 'error', 'message': cards})}\n\n"
            return

        total = len(cards)
        if total == 0:
            yield f"data: {json.dumps({'type': 'complete', 'deleted': 0, 'failed': 0, 'total': 0})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

        deleted = 0
        failed = 0

        for i, card in enumerate(cards):
            card_id = card["id"]
            card_type_val = card["card_type"]
            current = i + 1

            yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': total, 'card_id': card_id})}\n\n"

            try:
                if cancel_card(card_id, account, card_type=card_type_val):
                    deleted += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1

        yield f"data: {json.dumps({'type': 'complete', 'deleted': deleted, 'failed': failed, 'total': total})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


# ==================== 卡片 API ====================

@app.route('/api/cards', methods=['GET'])
@require_auth
def get_cards():
    """获取当前用户的卡片"""
    with card_lock:
        data = load_cards()
        settings = load_settings()
        address_overrides = get_backend_channel_address_overrides(settings=settings)
        now = datetime.now(timezone.utc)

        # 每个用户只能看到自己创建的卡片
        current_user = request.user.get('username')
        data["cards"] = [c for c in data.get("cards", []) if c.get("created_by") == current_user]

        for card in data.get("cards", []):
            card["legal_address"] = apply_backend_channel_legal_address(
                card.get("legal_address"),
                backend_channel_id=card.get("backend_channel_id"),
                settings=settings,
                address_overrides=address_overrides
            )
            expire_time = _parse_iso_to_utc(card.get("expire_time"))
            if expire_time is None:
                card["remaining_seconds"] = 0
                continue
            remaining = (expire_time - now).total_seconds()
            card["remaining_seconds"] = max(0, int(remaining))

        return jsonify(data)


@app.route('/api/create_stream', methods=['GET'])
@require_auth
def create_cards_stream():
    """流式创建卡片 - 按实际后端渠道创建或提取"""
    count = max(1, int(request.args.get('count', 1)))
    card_limit = int(request.args.get('card_limit', 1))
    expire_minutes = int(request.args.get('expire_minutes', 60))
    interval = max(5, int(request.args.get('interval', 5)))
    legacy_card_type = request.args.get('card_type', 'credit')
    current_user = request.user.get('username')
    backend_channel_id = request.args.get('backend_channel_id')
    channel_config = resolve_create_backend_channel(
        backend_channel_id=backend_channel_id,
        card_type=legacy_card_type,
        username=current_user
    )

    def error_response(message):
        def error_generate():
            yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"
        return Response(error_generate(), mimetype='text/event-stream')

    if not channel_config:
        return error_response('请选择有效的实际后端渠道')

    provider = channel_config.get("provider")
    if provider == "mercury" and get_account_count() == 0:
        return error_response('没有配置 Mercury 账户，请先在「后台账户」中添加账户')

    def generate():
        created_count = 0
        failed_count = 0
        channel_label = channel_config.get("label") or channel_config.get("id") or "渠道"
        direct_card_type = derive_direct_creation_card_type(channel_config)

        for i in range(count):
            if i > 0:
                for sec in range(interval, 0, -1):
                    yield f"data: {json.dumps({'type': 'waiting', 'seconds': sec, 'message': f'等待 {sec} 秒后创建下一张...'})}\n\n"
                    time.sleep(1)
            current = i + 1

            if provider == "mercury":
                account = get_random_account()
                if not account:
                    yield f"data: {json.dumps({'type': 'error', 'message': '没有可用的 Mercury 账户'})}\n\n"
                    break

                account_email = account["email"]
                target_card_type = channel_config["card_type"]
                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'creating', 'message': f'正在通过 {channel_label} 使用 {account_email} 创建第 {current}/{count} 张卡片...'})}\n\n"

                print(f"\n[创建卡片] 通过 {channel_label} 使用账户 {account_email} 创建第 {current}/{count} 张卡片...")

                card_id, create_error = issue_card(account, transaction_limit=card_limit, card_type=target_card_type)

                if not card_id:
                    failed_count += 1
                    error_msg = create_error or "创建失败"
                    yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败 ({account_email}): {error_msg}'})}\n\n"
                    continue

                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'revealing', 'message': f'正在获取第 {current}/{count} 张卡片详情...'})}\n\n"

                card_details = reveal_card_details(card_id, account, card_type=target_card_type)

                if not card_details:
                    failed_count += 1
                    cancel_card(card_id, account, card_type=target_card_type)
                    yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片获取详情失败'})}\n\n"
                    continue

                now = datetime.now(timezone.utc)
                expire_time = now + timedelta(minutes=expire_minutes)
                legal_address = apply_backend_channel_legal_address(
                    account.get("legal_address", {}),
                    channel_config=channel_config
                )

                card_info = {
                    "card_id": card_id,
                    "pan": card_details.get("pan", ""),
                    "cvv": card_details.get("cvv", ""),
                    "exp_month": card_details.get("exp_month", ""),
                    "exp_year": card_details.get("exp_year", ""),
                    "created_time": now.isoformat(),
                    "expire_time": expire_time.isoformat(),
                    "expire_minutes": expire_minutes,
                    "card_limit": card_limit,
                    "card_type": target_card_type,
                    "account_email": account_email,
                    "account_user_id": account["user_id"],
                    "provider": "mercury",
                    "provider_label": "Mercury",
                    "backend_channel_id": channel_config["id"],
                    "channel_head": channel_config["head"],
                    "channel_label": channel_label,
                    "legal_address": legal_address,
                    "destroy_supported": True,
                    "created_by": current_user
                }

                append_created_card(card_info)

                record_direct_card_creation(
                    card_id=card_id,
                    card_type=target_card_type,
                    card_limit=card_limit,
                    created_by=current_user,
                    account_email=account_email,
                    account_user_id=account["user_id"],
                    card_details=card_details,
                    expire_minutes=expire_minutes,
                    expire_time=expire_time.isoformat(),
                    extra_card_info=build_direct_creation_extra_info(card_info)
                )

                created_count += 1
                print(f"[创建卡片] 卡片 {card_id} 创建成功 (账户: {account_email})")

                yield f"data: {json.dumps({'type': 'card_created', 'current': current, 'total': count, 'card': card_info})}\n\n"
                continue

            if provider == "timoes":
                relay_code_type = channel_config["relay_code_type"]
                provider_label = channel_config["provider_label"]
                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'creating', 'message': f'正在通过 {channel_label} 提取第 {current}/{count} 张卡片...'})}\n\n"

                last_error = ""
                invalidated_count = 0

                while True:
                    acquired, relay_result = acquire_timoes_code_for_redeem(
                        relay_code_type,
                        allowed_code_types=get_timoes_code_types()
                    )
                    if not acquired:
                        failed_count += 1
                        error_message = relay_result
                        if invalidated_count > 0 and last_error:
                            error_message = f"{relay_result}，最近失败: {last_error}"
                        yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败: {error_message}'})}\n\n"
                        break

                    relay_code = relay_result.get("code", "")
                    upstream = redeem_airwallex_card(relay_code)

                    if upstream.get("success"):
                        actual_expire_minutes = upstream.get("expire_minutes", 0)
                        actual_card_limit = upstream.get("card_limit", 0)
                        try:
                            actual_expire_minutes = int(actual_expire_minutes or 0)
                        except (TypeError, ValueError):
                            actual_expire_minutes = 0
                        try:
                            actual_card_limit = float(actual_card_limit or 0)
                        except (TypeError, ValueError):
                            actual_card_limit = 0

                        now = datetime.now(timezone.utc)
                        expire_time = now + timedelta(minutes=actual_expire_minutes) if actual_expire_minutes > 0 else None
                        upstream_card = upstream.get("card") or {}
                        card_id = upstream_card.get("card_id") or f"timoes:{relay_code}"
                        legal_address = apply_backend_channel_legal_address(
                            upstream.get("legal_address") or {},
                            channel_config=channel_config
                        )

                        card_info = {
                            "card_id": card_id,
                            "pan": upstream_card.get("pan", ""),
                            "cvv": upstream_card.get("cvv", ""),
                            "exp_month": upstream_card.get("exp_month", ""),
                            "exp_year": upstream_card.get("exp_year", ""),
                            "created_time": now.isoformat(),
                            "expire_time": expire_time.isoformat() if expire_time else None,
                            "expire_minutes": actual_expire_minutes,
                            "card_limit": actual_card_limit,
                            "card_type": direct_card_type,
                            "provider": "timoes",
                            "provider_label": provider_label,
                            "backend_channel_id": channel_config["id"],
                            "channel_head": channel_config["head"],
                            "channel_label": channel_label,
                            "relay_code_type": relay_code_type,
                            "legal_address": legal_address,
                            "destroy_supported": False,
                            "created_by": current_user
                        }

                        append_created_card(card_info)

                        try:
                            mark_timoes_code_used(relay_code, used_by_key=f"direct_create:{current_user}", redeemed_card=card_info)
                        except Exception as pool_error:
                            print(f"[创建卡片] Timoes 卡密标记已使用失败 {relay_code}: {pool_error}")

                        record_direct_card_creation(
                            card_id=card_id,
                            card_type=direct_card_type,
                            card_limit=actual_card_limit,
                            created_by=current_user,
                            card_details=card_info,
                            expire_minutes=actual_expire_minutes,
                            expire_time=expire_time.isoformat() if expire_time else None,
                            extra_card_info=build_direct_creation_extra_info(card_info)
                        )

                        created_count += 1
                        print(f"[创建卡片] 通过 {channel_label} 获取卡片成功: {card_id}")

                        yield f"data: {json.dumps({'type': 'card_created', 'current': current, 'total': count, 'card': card_info})}\n\n"
                        break

                    if upstream.get("retryable"):
                        release_timoes_code_lock(relay_code)
                        failed_count += 1
                        error_message = upstream.get("error") or f"{provider_label} 上游暂时不可用"
                        yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败: {error_message}'})}\n\n"
                        break

                    invalidated_count += 1
                    last_error = upstream.get("error") or f"{provider_label} 卡密无效"
                    try:
                        mark_timoes_code_invalid(relay_code, last_error)
                    except Exception as pool_error:
                        print(f"[创建卡片] Timoes 卡密标记失效失败 {relay_code}: {pool_error}")
                    print(f"[创建卡片] Timoes 卡密失效 {relay_code}: {last_error}")

                continue

            if provider == "manual":
                manual_bin = channel_config["manual_bin"]
                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'creating', 'message': f'正在通过 {channel_label} 提取第 {current}/{count} 张卡片...'})}\n\n"

                acquired, manual_card = acquire_manual_card_for_redeem(manual_bin)
                if not acquired:
                    failed_count += 1
                    yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败: {manual_card}'})}\n\n"
                    continue

                locked_pan = manual_card["pan"]
                try:
                    actual_expire_minutes = manual_card.get("expire_minutes")
                    actual_card_limit = manual_card.get("card_limit")
                    try:
                        actual_expire_minutes = int(actual_expire_minutes or 0)
                    except (TypeError, ValueError):
                        actual_expire_minutes = 0
                    try:
                        actual_card_limit = float(actual_card_limit or 0)
                    except (TypeError, ValueError):
                        actual_card_limit = 0

                    now = datetime.now(timezone.utc)
                    expire_time = now + timedelta(minutes=actual_expire_minutes) if actual_expire_minutes > 0 else None
                    legal_address = apply_backend_channel_legal_address(
                        manual_card.get("legal_address") or {},
                        channel_config=channel_config
                    )
                    card_id = f"manual:{manual_card['pan']}"

                    card_info = {
                        "card_id": card_id,
                        "pan": manual_card["pan"],
                        "cvv": manual_card["cvv"],
                        "exp_month": manual_card["exp_month"],
                        "exp_year": manual_card["exp_year"],
                        "created_time": now.isoformat(),
                        "expire_time": expire_time.isoformat() if expire_time else None,
                        "expire_minutes": actual_expire_minutes,
                        "card_limit": actual_card_limit,
                        "card_type": direct_card_type,
                        "provider": "manual",
                        "provider_label": "手动卡池",
                        "backend_channel_id": channel_config["id"],
                        "channel_head": manual_bin,
                        "channel_label": channel_label,
                        "legal_address": legal_address,
                        "destroy_supported": False,
                        "created_by": current_user
                    }

                    append_created_card(card_info)

                    try:
                        mark_manual_card_used(manual_card["pan"], used_by_key=f"direct_create:{current_user}", redeemed_card=card_info)
                        locked_pan = ""
                    except Exception as pool_error:
                        print(f"[创建卡片] 手动卡标记已使用失败 {manual_card['pan']}: {pool_error}")
                        locked_pan = ""

                    record_direct_card_creation(
                        card_id=card_id,
                        card_type=direct_card_type,
                        card_limit=actual_card_limit,
                        created_by=current_user,
                        card_details=card_info,
                        expire_minutes=actual_expire_minutes,
                        expire_time=expire_time.isoformat() if expire_time else None,
                        extra_card_info=build_direct_creation_extra_info(card_info)
                    )

                    created_count += 1
                    print(f"[创建卡片] 通过 {channel_label} 获取手动卡成功: {card_id}")

                    yield f"data: {json.dumps({'type': 'card_created', 'current': current, 'total': count, 'card': card_info})}\n\n"
                except Exception as e:
                    failed_count += 1
                    try:
                        if locked_pan:
                            release_manual_card_lock(locked_pan)
                    except Exception as release_error:
                        print(f"[创建卡片] 释放手动卡锁失败 {locked_pan}: {release_error}")
                    yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败: {str(e)}'})}\n\n"
                continue

            failed_count += 1
            yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败: 不支持的渠道 {provider}'})}\n\n"

        yield f"data: {json.dumps({'type': 'complete', 'created': created_count, 'failed': failed_count, 'total': count})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/cancel/<card_id>', methods=['POST'])
@require_auth
def cancel_single_card(card_id):
    """取消单张卡片 - 使用卡片归属账户"""
    try:
        current_user = request.user.get('username')

        # 查找卡片及其归属账户
        with card_lock:
            data = load_cards()
            target_card = None
            for card in data["cards"]:
                if card["card_id"] == card_id:
                    target_card = card
                    break

        if not target_card:
            return jsonify({"success": False, "error": "卡片不存在"}), 404

        # 权限检查：只能删除自己创建的卡片
        if target_card.get("created_by") != current_user:
            return jsonify({"success": False, "error": "无权删除此卡片"}), 403

        if not should_remote_cancel_created_card(target_card):
            remove_created_card(card_id)
            return jsonify({"success": True})

        # 获取卡片归属账户
        account_user_id = target_card.get("account_user_id")
        account = None
        if account_user_id:
            account = get_account_by_user_id(account_user_id)

        if not account:
            # 账户已删除或旧卡片没有账户信息，使用随机账户
            account = get_random_account()

        if not account:
            return jsonify({"success": False, "error": "没有可用的账户"}), 500

        card_type = get_safe_cancel_card_type(target_card)
        success = cancel_card(card_id, account, card_type=card_type)

        if success:
            remove_created_card(card_id)
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "取消失败"}), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/cancel_all_stream', methods=['GET'])
@require_auth
def cancel_all_cards_stream():
    """流式取消当前用户的所有卡片"""
    from account.accounts import load_accounts

    # 获取当前用户（在闭包外获取）
    current_user = request.user.get('username')

    def generate():
        with card_lock:
            data = load_cards()
            all_cards = data.get("cards", [])

            # 每个用户只能删除自己的卡片
            cards = [c for c in all_cards if c.get("created_by") == current_user]

        total = len(cards)

        if total == 0:
            yield f"data: {json.dumps({'type': 'complete', 'cancelled': 0, 'failed': 0, 'total': 0})}\n\n"
            return

        # 预加载所有账户
        accounts_data = load_accounts()
        accounts_map = {acc["user_id"]: acc for acc in accounts_data.get("accounts", [])}

        cancelled_count = 0
        failed_count = 0
        cancelled_card_ids = set()

        for i, card in enumerate(cards):
            current = i + 1
            card_id = card["card_id"]
            account_email = card.get("account_email") or card.get("provider_label") or "未知"

            yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': total, 'status': 'cancelling', 'message': f'正在取消第 {current}/{total} 张卡片 ({account_email})...'})}\n\n"

            try:
                if not should_remote_cancel_created_card(card):
                    success = True
                else:
                    account_user_id = card.get("account_user_id")
                    account = None
                    if account_user_id and account_user_id in accounts_map:
                        account = accounts_map[account_user_id]
                    else:
                        account = get_random_account()

                    if account:
                        card_type = get_safe_cancel_card_type(card)
                        success = cancel_card(card_id, account, card_type=card_type)
                    else:
                        success = False

                if success:
                    cancelled_count += 1
                    cancelled_card_ids.add(card_id)
                else:
                    failed_count += 1
            except Exception:
                failed_count += 1

        if cancelled_card_ids:
            with card_lock:
                data = load_cards()
                data["cards"] = [
                    c for c in data.get("cards", [])
                    if not (c.get("created_by") == current_user and c.get("card_id") in cancelled_card_ids)
                ]
                save_cards(data)

        yield f"data: {json.dumps({'type': 'complete', 'cancelled': cancelled_count, 'failed': failed_count, 'total': total})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


# ==================== 卡密 API ====================

@app.route('/api/keys', methods=['GET'])
@require_auth
def get_keys():
    """获取当前用户的卡密（支持分页）"""
    current_user = request.user.get('username')
    page = request.args.get('page', type=int)
    page_size = request.args.get('page_size', 100, type=int)
    key_kind = request.args.get('key_kind')
    data = get_all_ids(username=current_user, page=page, page_size=page_size, key_kind=key_kind)
    return jsonify(data)


@app.route('/api/keys/count', methods=['GET'])
@require_auth
def get_keys_count():
    """获取卡密数量（按类型）"""
    from id.id import get_unused_count_by_type
    current_user = request.user.get('username')
    card_type = request.args.get('card_type')
    key_kind = request.args.get('key_kind')
    count = get_unused_count_by_type(username=current_user, card_type=card_type, key_kind=key_kind)
    return jsonify({"count": count})


@app.route('/api/timoes-pool/stats', methods=['GET'])
@require_auth
def api_get_timoes_pool_stats():
    """获取共享 Timoes 码池统计"""
    settings = load_settings()
    stats = get_timoes_pool_stats(
        allowed_code_types=get_timoes_code_types(settings=settings)
    )
    return jsonify({
        "success": True,
        **stats,
        "code_types": get_timoes_code_types(settings=settings, stats=stats)
    })


@app.route('/api/timoes-pool/import', methods=['POST'])
@require_auth
def api_import_timoes_pool():
    """导入共享 Timoes 接力卡密"""
    try:
        current_user = request.user.get('username')
        settings = load_settings()
        req_data = request.json or {}
        code_type = req_data.get('code_type')
        codes = req_data.get('codes', '')

        success, result = import_timoes_codes(
            codes,
            code_type,
            created_by=current_user,
            allowed_code_types=get_timoes_code_types(settings=settings)
        )
        if not success:
            return jsonify({"success": False, "error": result}), 400

        stats = get_timoes_pool_stats(
            allowed_code_types=get_timoes_code_types(settings=settings)
        )
        return jsonify({
            "success": True,
            **result,
            **stats,
            "code_types": get_timoes_code_types(settings=settings, stats=stats)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/timoes-pool/items', methods=['GET'])
@require_auth
def api_list_timoes_pool_items():
    """获取共享 Timoes 码池明细"""
    settings = load_settings()
    code_type = request.args.get('code_type', '').strip()
    status = request.args.get('status', '').strip().lower()
    limit = request.args.get('limit', 100, type=int)
    result = list_timoes_pool_items(
        code_type=code_type or None,
        status=status or None,
        limit=limit,
        allowed_code_types=get_timoes_code_types(settings=settings)
    )
    stats = get_timoes_pool_stats(
        allowed_code_types=get_timoes_code_types(settings=settings)
    )
    return jsonify({
        "success": True,
        **result,
        "code_types": get_timoes_code_types(settings=settings, stats=stats)
    })


@app.route('/api/timoes-pool/items/<code>', methods=['PATCH'])
@require_auth
def api_update_timoes_pool_item(code):
    """编辑共享 Timoes 码池记录"""
    try:
        settings = load_settings()
        req_data = request.json or {}
        success, result = update_timoes_pool_item(
            code,
            updates=req_data,
            allowed_code_types=get_timoes_code_types(settings=settings)
        )
        if not success:
            return jsonify({"success": False, "error": result}), 400

        stats = get_timoes_pool_stats(
            allowed_code_types=get_timoes_code_types(settings=settings)
        )
        return jsonify({
            "success": True,
            "item": result,
            **stats,
            "code_types": get_timoes_code_types(settings=settings, stats=stats)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/timoes-pool/items/<code>', methods=['DELETE'])
@require_auth
def api_delete_timoes_pool_item(code):
    """删除共享 Timoes 码池记录"""
    try:
        settings = load_settings()
        success, result = delete_timoes_pool_item(code)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        stats = get_timoes_pool_stats(
            allowed_code_types=get_timoes_code_types(settings=settings)
        )
        return jsonify({
            "success": True,
            "message": result,
            **stats,
            "code_types": get_timoes_code_types(settings=settings, stats=stats)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/manual-card-pool/stats', methods=['GET'])
@require_auth
def api_get_manual_card_pool_stats():
    """获取共享手动卡池统计"""
    stats = get_manual_card_pool_stats()
    return jsonify({"success": True, **stats})


@app.route('/api/manual-card-pool/import', methods=['POST'])
@require_auth
def api_import_manual_card_pool():
    """导入共享手动卡池"""
    try:
        current_user = request.user.get('username')
        req_data = request.json or {}
        cards = req_data.get('cards', '')

        success, result = import_manual_cards(cards, created_by=current_user)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        stats = get_manual_card_pool_stats()
        return jsonify({"success": True, **result, **stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/manual-card-pool/items', methods=['GET'])
@require_auth
def api_list_manual_card_pool_items():
    """获取共享手动卡池明细"""
    bin_code = request.args.get('bin_code', '').strip()
    status = request.args.get('status', '').strip().lower()
    limit = request.args.get('limit', 100, type=int)
    result = list_manual_card_pool_items(
        bin_code=bin_code or None,
        status=status or None,
        limit=limit
    )
    return jsonify({"success": True, **result})


@app.route('/api/manual-card-pool/items/<pan>', methods=['PATCH'])
@require_auth
def api_update_manual_card_pool_item(pan):
    """编辑共享手动卡池记录"""
    try:
        req_data = request.json or {}
        success, result = update_manual_card_pool_item(pan, updates=req_data)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        stats = get_manual_card_pool_stats()
        return jsonify({"success": True, "item": result, **stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/manual-card-pool/items/<pan>', methods=['DELETE'])
@require_auth
def api_delete_manual_card_pool_item(pan):
    """删除共享手动卡池记录"""
    try:
        success, result = delete_manual_card_pool_item(pan)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        stats = get_manual_card_pool_stats()
        return jsonify({"success": True, "message": result, **stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/old-card-pool/stats', methods=['GET'])
@require_auth
def api_get_old_card_pool_stats():
    """获取旧卡池统计"""
    current_user = request.user.get('username')
    settings = load_settings()
    stats = get_old_card_pool_stats()
    channels = serialize_old_card_pool_channels(stats=stats, settings=settings)
    enabled_backend_ids = {
        item.get("backend_channel_id")
        for item in channels
        if item.get("backend_channel_id") and item.get("enabled", True)
    }
    display_channels = []
    for item in get_enabled_display_channels(username=current_user, settings=settings):
        backend_channel_id = str(item.get("backend_channel_id") or "").strip().lower()
        if backend_channel_id not in enabled_backend_ids:
            continue
        pool_channel = next((channel for channel in channels if channel.get("backend_channel_id") == backend_channel_id), None)
        display_channels.append({
            **item,
            "old_card_available_count": (pool_channel or {}).get("available_count", 0),
            "old_card_enabled": True
        })

    return jsonify({
        "success": True,
        "channels": channels,
        "display_channels": display_channels,
        "total_available": stats.get("total_available", 0),
        "total_channels": stats.get("total_channels", 0),
        "enabled_available": sum(item.get("available_count", 0) for item in channels if item.get("enabled", True))
    })


@app.route('/api/old-card-pool/backend-channels/<backend_channel_id>', methods=['POST'])
@require_admin
def api_set_old_card_backend_channel_status(backend_channel_id):
    """设置旧卡池卡头启用状态"""
    try:
        normalized_backend_channel_id = str(backend_channel_id or "").strip().lower()
        if not normalized_backend_channel_id:
            return jsonify({"success": False, "error": "后端渠道不能为空"}), 400

        req_data = request.json or {}
        enabled = bool(req_data.get("enabled", True))
        settings = load_settings()
        stats = get_old_card_pool_stats()
        known_backend_ids = {
            str(item.get("backend_channel_id") or "").strip().lower()
            for item in stats.get("channels", [])
            if str(item.get("backend_channel_id") or "").strip()
        }
        if normalized_backend_channel_id not in known_backend_ids and not resolve_backend_channel(normalized_backend_channel_id, settings=settings):
            return jsonify({"success": False, "error": "旧卡卡头不存在"}), 400

        success, error, settings = set_old_card_backend_channel_enabled(
            normalized_backend_channel_id,
            enabled=enabled,
            settings=settings
        )
        if not success:
            return jsonify({"success": False, "error": error or "保存失败"}), 400

        refreshed_stats = get_old_card_pool_stats()
        channels = serialize_old_card_pool_channels(stats=refreshed_stats, settings=settings)
        current_user = request.user.get('username')
        enabled_backend_ids = {
            item.get("backend_channel_id")
            for item in channels
            if item.get("backend_channel_id") and item.get("enabled", True)
        }
        display_channels = []
        for item in get_enabled_display_channels(username=current_user, settings=settings):
            backend_id = str(item.get("backend_channel_id") or "").strip().lower()
            if backend_id not in enabled_backend_ids:
                continue
            pool_channel = next((channel for channel in channels if channel.get("backend_channel_id") == backend_id), None)
            display_channels.append({
                **item,
                "old_card_available_count": (pool_channel or {}).get("available_count", 0),
                "old_card_enabled": True
            })

        return jsonify({
            "success": True,
            "message": "旧卡卡头已开启" if enabled else "旧卡卡头已关闭",
            "channels": channels,
            "display_channels": display_channels,
            "total_available": refreshed_stats.get("total_available", 0),
            "total_channels": refreshed_stats.get("total_channels", 0),
            "enabled_available": sum(item.get("available_count", 0) for item in channels if item.get("enabled", True))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/timoes-backend-channels', methods=['GET'])
@require_admin
def api_get_timoes_backend_channels():
    """获取 Timoes 后端渠道配置"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "channels": serialize_timoes_backend_channels(settings=settings),
        "code_types": get_timoes_code_types(settings=settings)
    })


@app.route('/api/timoes-backend-channels', methods=['POST'])
@require_admin
def api_save_timoes_backend_channel():
    """新增或编辑 Timoes 后端渠道配置"""
    try:
        settings = load_settings()
        req_data = request.json or {}
        success, result, settings = upsert_timoes_backend_channel(req_data, settings=settings)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        return jsonify({
            "success": True,
            "channel": result,
            "channels": serialize_timoes_backend_channels(settings=settings),
            "code_types": get_timoes_code_types(settings=settings)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/timoes-backend-channels/<channel_id>', methods=['DELETE'])
@require_admin
def api_delete_timoes_backend_channel(channel_id):
    """删除 Timoes 后端渠道配置"""
    try:
        settings = load_settings()
        success, result, settings = delete_timoes_backend_channel(channel_id, settings=settings)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        return jsonify({
            "success": True,
            "message": result,
            "channels": serialize_timoes_backend_channels(settings=settings),
            "code_types": get_timoes_code_types(settings=settings)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/other-channels/backend-options', methods=['GET'])
@require_auth
def api_get_backend_channel_options():
    """获取当前可用的实际后端渠道"""
    current_user = request.user.get('username')
    settings = load_settings()
    channels = serialize_backend_channel_options(
        settings=settings,
        username=current_user,
        include_address_override=is_admin(current_user)
    )
    payload = {"success": True, "channels": channels}
    if is_admin(current_user):
        payload["address_templates"] = serialize_backend_address_templates(settings=settings)
    return jsonify(payload)


@app.route('/api/backend-address-templates', methods=['POST'])
@require_admin
def api_save_backend_address_template():
    """保存地址模板"""
    try:
        req_data = request.json or {}
        settings = load_settings()
        success, result, settings = upsert_backend_address_template(
            req_data,
            settings=settings
        )
        if not success:
            return jsonify({"success": False, "error": result}), 400

        return jsonify({
            "success": True,
            "message": "地址模板已保存",
            "template": result,
            **build_backend_address_admin_payload(settings=settings, username=request.user.get('username'))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/backend-address-templates/<template_id>', methods=['DELETE'])
@require_admin
def api_delete_backend_address_template(template_id):
    """删除地址模板"""
    try:
        settings = load_settings()
        success, result, settings = delete_backend_address_template(template_id, settings=settings)
        if not success:
            return jsonify({"success": False, "error": result}), 400

        return jsonify({
            "success": True,
            "message": result,
            **build_backend_address_admin_payload(settings=settings, username=request.user.get('username'))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/other-channels/backend-address-config', methods=['POST'])
@app.route('/api/other-channels/backend-address-override', methods=['POST'])
@require_admin
def api_set_backend_channel_address_config():
    """保存后端渠道地址配置"""
    try:
        req_data = request.json or {}
        backend_channel_id = str(req_data.get("backend_channel_id") or "").strip().lower()
        if not backend_channel_id:
            return jsonify({"success": False, "error": "请选择后端渠道"}), 400

        settings = load_settings()
        channel_config = resolve_backend_channel(backend_channel_id, settings=settings)
        if not channel_config:
            return jsonify({"success": False, "error": "后端渠道不存在"}), 400

        mode = req_data.get("mode")
        if mode is None:
            legal_address = normalize_legal_address(req_data.get("legal_address") or {})
            mode = "manual" if legal_address else "inherit"
        else:
            legal_address = normalize_legal_address(req_data.get("legal_address") or {})

        success, result, settings = set_backend_channel_address_config(
            backend_channel_id,
            mode=mode,
            template_id=req_data.get("template_id"),
            legal_address=legal_address,
            settings=settings
        )
        if not success:
            return jsonify({"success": False, "error": result}), 400

        address_mode = result.get("mode") or "inherit"
        return jsonify({
            "success": True,
            "message": "后端渠道地址已更新" if address_mode != "inherit" else "后端渠道地址配置已清空",
            "address_config": result,
            **build_backend_address_admin_payload(settings=settings, username=request.user.get('username'))
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/other-channels/display-routes', methods=['GET'])
@require_admin
def api_get_display_routes():
    """获取前台显示渠道映射"""
    current_user = request.user.get('username')
    settings = load_settings()
    channels = serialize_display_channels(
        get_display_channels(settings=settings),
        username=current_user,
        public_only=False
    )
    return jsonify({"success": True, "channels": channels})


@app.route('/api/other-channels/display-routes', methods=['POST'])
@require_admin
def api_set_display_routes():
    """保存前台显示渠道映射"""
    try:
        req_data = request.json or {}
        channels = normalize_display_channels(
            req_data.get('channels'),
            fallback_to_default=False,
            settings=load_settings()
        )
        settings = load_settings()
        settings['display_redeem_channels'] = channels
        save_settings(settings)

        current_user = request.user.get('username')
        return jsonify({
            "success": True,
            "channels": serialize_display_channels(channels, username=current_user, public_only=False)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/display-channel-options', methods=['GET'])
@require_auth
def api_get_key_display_channel_options():
    """获取可绑定的前台显示渠道"""
    current_user = request.user.get('username')
    settings = load_settings()
    return jsonify({
        "success": True,
        "channels": get_enabled_display_channels(username=current_user, settings=settings)
    })


@app.route('/api/keys/generate', methods=['POST'])
@require_auth
def generate_keys():
    """生成卡密"""
    try:
        current_user = request.user.get('username')
        req_data = request.json or {}
        key_kind = normalize_key_kind(req_data.get('key_kind'))
        count = int(req_data.get('count', 1))
        card_limit = int(req_data.get('card_limit', 1))
        expire_minutes = int(req_data.get('expire_minutes', 60))
        settings = load_settings()
        bound_channel = resolve_bound_display_channel(
            req_data.get('display_channel_id'),
            username=current_user,
            settings=settings
        ) if req_data.get('display_channel_id') else None

        if req_data.get('display_channel_id') and not bound_channel:
            return jsonify({"success": False, "error": "绑定的前台渠道不存在或已停用"}), 400

        if key_kind == "old_card" and bound_channel and not is_old_card_backend_channel_enabled(
            bound_channel.get("backend_channel_id"),
            settings=settings
        ):
            return jsonify({"success": False, "error": "绑定的旧卡卡头当前已关闭"}), 400

        card_type = derive_generated_key_card_type(bound_channel, username=current_user)
        if key_kind == "old_card":
            card_limit = 0
            expire_minutes = 0

        ids = generate_ids(
            count,
            expire_minutes,
            card_limit=card_limit,
            card_type=card_type,
            created_by=current_user,
            bound_channel=bound_channel,
            key_kind=key_kind
        )

        return jsonify({
            "success": True,
            "generated": len(ids),
            "ids": ids,
            "key_kind": key_kind,
            **build_key_binding_payload(bound_channel or {})
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/withdraw', methods=['POST'])
@require_auth
def create_withdraw_keys():
    """生成提卡链接（隐藏卡密）"""
    try:
        current_user = request.user.get('username')
        req_data = request.json or {}
        count = int(req_data.get('count', 1))
        note = (req_data.get('note') or '').strip()
        expire_minutes = int(req_data.get('expire_minutes', 60))
        card_limit = int(req_data.get('card_limit', 0))
        settings = load_settings()
        bound_channel = resolve_bound_display_channel(
            req_data.get('display_channel_id'),
            username=current_user,
            settings=settings
        ) if req_data.get('display_channel_id') else None
        if req_data.get('display_channel_id') and not bound_channel:
            return jsonify({"success": False, "error": "绑定的前台渠道不存在或已停用"}), 400
        card_type = derive_generated_key_card_type(bound_channel, username=current_user)

        if count < 1:
            return jsonify({"success": False, "error": "数量必须大于 0"}), 400

        token = uuid.uuid4().hex
        allocated = allocate_existing_ids_for_withdraw(
            token=token,
            note=note,
            username=current_user,
            card_type=card_type,
            count=count,
            bound_channel=bound_channel,
            key_kind="normal"
        )
        remaining = max(0, count - len(allocated))

        generated = []
        if remaining > 0:
            generated = generate_ids(
                remaining,
                expire_minutes,
                card_limit=card_limit,
                card_type=card_type,
                created_by=current_user,
                hidden=True,
                hidden_token=token,
                hidden_note=note,
                bound_channel=bound_channel
            )

        prepared_total = len(allocated) + len(generated)

        link = f"{request.host_url.rstrip('/')}/withdraw/{token}"

        return jsonify({
            "success": True,
            "generated": prepared_total,
            "from_existing": len(allocated),
            "generated_new": len(generated),
            "link": link,
            "token": token,
            **build_key_binding_payload(bound_channel or {})
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/withdraw/<token>', methods=['GET'])
def get_withdraw_keys(token):
    """获取提卡链接下的卡密（无需登录）"""
    try:
        batch = get_hidden_ids_by_token(token)
        if not batch:
            return jsonify({"success": False, "error": "链接无效或已过期"}), 404
        return jsonify({"success": True, "batch": batch})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/delete/<key_id>', methods=['POST'])
@require_auth
def delete_key(key_id):
    """删除单个卡密"""
    try:
        current_user = request.user.get('username')
        success, error = delete_id(key_id, username=current_user)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": error}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/delete_all', methods=['POST'])
@require_auth
def delete_all_keys():
    """删除当前用户的所有卡密"""
    try:
        current_user = request.user.get('username')
        count = delete_all_ids(username=current_user)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/delete_batch', methods=['POST'])
@require_auth
def delete_keys_batch():
    """批量删除卡密（高性能）"""
    try:
        current_user = request.user.get('username')
        data = request.get_json() or {}
        id_list = data.get('ids', [])

        if not id_list:
            return jsonify({"success": False, "error": "没有指定要删除的卡密"}), 400

        count = delete_ids_batch(id_list, username=current_user)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/delete_unused', methods=['POST'])
@require_auth
def delete_unused_keys():
    """删除未使用的卡密（按类型过滤）"""
    try:
        current_user = request.user.get('username')
        data = request.get_json() or {}
        card_type = data.get('card_type')  # "credit", "debit", or None for all
        key_kind = data.get('key_kind')

        count = delete_unused_ids_by_type(card_type=card_type, username=current_user, key_kind=key_kind)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/records', methods=['GET'])
@require_auth
def get_key_records():
    """获取开卡记录（支持分页）"""
    try:
        current_user = request.user.get('username')
        page = request.args.get('page', type=int)
        page_size = request.args.get('page_size', 100, type=int)
        data = get_redeem_records(username=current_user, page=page, page_size=page_size)
        return jsonify({"success": True, **data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/records/<key_id>', methods=['DELETE'])
@require_auth
def delete_key_record(key_id):
    """删除单条开卡记录"""
    try:
        current_user = request.user.get('username')
        success, error = delete_record(key_id, username=current_user)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": error}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/records', methods=['DELETE'])
@require_auth
def delete_all_key_records():
    """删除所有开卡记录"""
    try:
        current_user = request.user.get('username')
        count = delete_all_records(username=current_user)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/analytics', methods=['GET'])
@require_auth
def api_get_analytics():
    """获取开卡分析数据（支持日期范围和时区）"""
    try:
        start_date = request.args.get('start_date')  # 可选，格式 YYYY-MM-DD
        end_date = request.args.get('end_date')  # 可选，格式 YYYY-MM-DD
        username = request.args.get('username')  # 可选，筛选特定用户的小时统计
        tz_offset = request.args.get('tz_offset', type=int)  # 用户时区偏移（分钟），UTC+8 返回 -480
        data = get_analytics_data(start_date=start_date, end_date=end_date, username=username, tz_offset=tz_offset)
        return jsonify({"success": True, **data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/destroy', methods=['POST'])
@require_auth
def destroy_keys():
    """销毁卡密，如果已兑换则先删除卡片，并标记为已销毁"""
    from card.cancel import cancel_card
    from id.id import mark_destroyed

    try:
        current_user = request.user.get('username')
        is_admin = request.user.get('is_admin', False)  # 获取管理员状态
        data = request.get_json() or {}
        keys_text = data.get('keys', '').strip()

        if not keys_text:
            return jsonify({"success": False, "error": "请输入卡密"}), 400

        keys = [k.strip() for k in keys_text.split('\n') if k.strip()]
        if not keys:
            return jsonify({"success": False, "error": "请输入卡密"}), 400

        results = []
        destroyed_keys = 0
        deleted_cards = 0
        failed = 0

        for key_id in keys:
            result = {"key": key_id}
            print(f"[销毁卡密] 处理: {key_id}")

            # 检查卡密是否已兑换
            success, redeemed_info = query_redeemed(key_id)

            if success:
                # 已兑换，尝试删除卡片
                card = redeemed_info.get("card", {})
                card_id = card.get("card_id")
                card_type = card.get("card_type", "credit")
                account_user_id = card.get("account_user_id")
                provider = card.get("provider", "mercury")

                print(f"[销毁卡密] 卡密已兑换，卡片ID: {card_id}, 类型: {card_type}, 账户: {account_user_id}, 提供方: {provider}")

                if provider != "mercury":
                    result["card_deleted"] = False
                    result["card_error"] = "该线路卡片不支持远程销毁，仅销毁外层卡密"
                elif card_id:
                    # 获取对应账户
                    account = get_account_by_user_id(account_user_id)
                    if account:
                        print(f"[销毁卡密] 使用账户 {account.get('email')} 取消卡片 {card_id}")
                        try:
                            if cancel_card(card_id, account, card_type=card_type):
                                deleted_cards += 1
                                result["card_deleted"] = True
                                print(f"[销毁卡密] ✅ 卡片 {card_id} 取消成功")
                            else:
                                result["card_deleted"] = False
                                result["card_error"] = "取消卡片失败"
                                print(f"[销毁卡密] ❌ 卡片 {card_id} 取消失败")
                        except Exception as e:
                            result["card_deleted"] = False
                            result["card_error"] = str(e)
                            print(f"[销毁卡密] ❌ 卡片 {card_id} 取消异常: {e}")
                    else:
                        result["card_deleted"] = False
                        result["card_error"] = f"账户 {account_user_id} 不存在"
                        print(f"[销毁卡密] ❌ 账户 {account_user_id} 不存在")
                else:
                    print(f"[销毁卡密] 卡密已兑换但无卡片ID")
            else:
                print(f"[销毁卡密] 卡密未兑换或查询失败: {redeemed_info}")

            # 标记卡密为已销毁（管理员可销毁任何用户的卡密）
            mark_success, mark_error = mark_destroyed(key_id, username=current_user, is_admin=is_admin)
            if mark_success:
                destroyed_keys += 1
                result["destroyed"] = True
                print(f"[销毁卡密] ✅ 卡密 {key_id} 已标记为销毁")
            else:
                result["destroyed"] = False
                result["destroy_error"] = mark_error
                failed += 1
                print(f"[销毁卡密] ❌ 卡密 {key_id} 标记销毁失败: {mark_error}")

            results.append(result)

        return jsonify({
            "success": True,
            "message": f"处理完成: 销毁 {destroyed_keys} 个卡密, 取消 {deleted_cards} 张卡片, 失败 {failed}",
            "destroyed_keys": destroyed_keys,
            "deleted_cards": deleted_cards,
            "failed": failed,
            "results": results
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/user-destroy', methods=['POST'])
def destroy_keys_user():
    """无需登录的销毁接口，需验证卡密和卡号匹配"""
    from card.cancel import cancel_card
    from id.id import mark_destroyed

    try:
        data = request.get_json() or {}
        items = data.get('items', []) # List of {key: '...', card_id: '...'}

        if not items:
            return jsonify({"success": False, "error": "参数错误"}), 400

        results = []
        destroyed_keys = 0
        deleted_cards = 0
        failed = 0

        for item in items:
            key_id = item.get('key', '').strip()
            request_card_id = item.get('card_id', '').strip()

            result = {"key": key_id}
            print(f"[用户销毁] 请求销毁: key={key_id}, card_id={request_card_id}")

            # 1. 验证卡密信息
            success, redeemed_info = query_redeemed(key_id)
            if not success:
                result["status"] = "failed"
                result["error"] = "卡密无效"
                failed += 1
                results.append(result)
                print(f"[用户销毁] ❌ 卡密无效: {key_id}")
                continue

            card = redeemed_info.get("card", {})
            server_card_id = card.get("card_id")
            provider = card.get("provider", "mercury")

            # 2. 核心校验：传入的 card_id 必须匹配
            # 也可以校验 PAN 等，但 card_id 最准确
            if not server_card_id or server_card_id != request_card_id:
                result["status"] = "failed"
                result["error"] = "卡片信息验证失败"
                failed += 1
                results.append(result)
                print(f"[用户销毁] ❌ 验证失败: 提交={request_card_id}, 实际={server_card_id}")
                continue

            # 3. 验证通过，执行销毁
            card_type = card.get("card_type", "credit")
            account_user_id = card.get("account_user_id")

            # 取消卡片
            account = get_account_by_user_id(account_user_id)
            card_deleted = False
            if provider == "mercury" and account:
                print(f"[用户销毁] 取消卡片 {server_card_id} (账户: {account.get('email')})")
                try:
                    if cancel_card(server_card_id, account, card_type=card_type):
                        card_deleted = True
                        deleted_cards += 1
                    else:
                        print(f"[用户销毁] ⚠️ 卡片取消失败")
                except Exception as e:
                     print(f"[用户销毁] ⚠️ 卡片取消异常: {e}")

            # 标记销毁
            # 已经通过 card_id 强校验，此处无需再校验 username，传入 None 跳过 id.py 中的检查
            mark_success, mark_error = mark_destroyed(key_id, username=None)
            if mark_success:
                destroyed_keys += 1
                result["status"] = "success"
                print(f"[用户销毁] ✅ 卡密已销毁")
            else:
                result["status"] = "failed"
                result["error"] = mark_error
                failed += 1
                print(f"[用户销毁] ❌ 标记销毁失败: {mark_error}")

            results.append(result)

        return jsonify({
            "success": True,
            "destroyed_keys": destroyed_keys,
            "deleted_cards": deleted_cards,
            "failed": failed,
            "results": results
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/redeem', methods=['POST'])
def redeem_key():
    """兑换卡密 - 无需登录，支持 Mercury 开卡和 Timoes 接力"""
    key_id = ""
    lock_acquired = False
    redeemed_success = False
    manual_locked_pan = ""
    old_card_source_key_id = ""

    try:
        req_data = request.json or {}
        key_id = req_data.get('key_id', '').strip()
        requested_redeem_mode = str(req_data.get('redeem_mode', '')).strip().lower()
        if requested_redeem_mode in ("auto", "__auto__"):
            requested_redeem_mode = ""

        if not key_id:
            return jsonify({"success": False, "error": "请输入卡密"}), 400

        valid, result = acquire_id_for_redeem(key_id)
        if not valid:
            status_code = 409 if result == "卡密兑换中，请稍后重试" else 400
            return jsonify({"success": False, "error": result}), status_code

        lock_acquired = True
        expire_minutes = int(result["expire_minutes"] or 0)
        try:
            card_limit = float(result["card_limit"] or 0)
        except (TypeError, ValueError):
            card_limit = 0
        binding_payload = build_key_binding_payload(result)
        key_kind = normalize_key_kind(result.get("key_kind"))
        settings = load_settings()

        selected_display_channel = None
        if binding_payload["channel_binding_enabled"]:
            selected_display_channel = {
                "id": binding_payload["bound_display_channel_id"],
                "name": binding_payload["bound_display_channel_name"] or binding_payload["bound_display_channel_id"],
                "backend_channel_id": binding_payload["bound_backend_channel_id"]
            }
        elif key_kind != "old_card":
            display_channels = [
                item for item in get_display_channels(settings=settings)
                if item.get("enabled", True)
            ]
            if not display_channels:
                return jsonify({"success": False, "error": "当前未开放任何开卡渠道"}), 503

            display_channel_map = {item["id"]: item for item in display_channels}
            if requested_redeem_mode:
                if requested_redeem_mode not in display_channel_map:
                    return jsonify({"success": False, "error": "不支持的开卡渠道"}), 400
                selected_display_channel = display_channel_map[requested_redeem_mode]
            else:
                selected_display_channel = display_channels[0]

        if key_kind == "old_card" and not selected_display_channel and requested_redeem_mode:
            display_channels = [
                item for item in get_display_channels(settings=settings)
                if item.get("enabled", True)
            ]
            display_channel_map = {item["id"]: item for item in display_channels}
            if requested_redeem_mode not in display_channel_map:
                return jsonify({"success": False, "error": "不支持的开卡渠道"}), 400
            selected_display_channel = display_channel_map[requested_redeem_mode]

        redeem_mode = selected_display_channel["id"] if selected_display_channel else None
        backend_channel_id = selected_display_channel["backend_channel_id"] if selected_display_channel else None

        if key_kind == "old_card":
            old_card_stats = get_old_card_pool_stats()
            enabled_old_card_backend_ids = get_enabled_old_card_backend_channel_ids(
                stats=old_card_stats,
                settings=settings
            )

            if backend_channel_id:
                if backend_channel_id not in enabled_old_card_backend_ids:
                    return jsonify({"success": False, "error": "当前指定旧卡卡头已关闭"}), 409
                allowed_backend_ids = {backend_channel_id}
            else:
                allowed_backend_ids = enabled_old_card_backend_ids
                if not allowed_backend_ids:
                    return jsonify({"success": False, "error": "当前没有启用的旧卡卡头"}), 409

            acquired, old_card = acquire_old_card_for_redeem(
                backend_channel_id=backend_channel_id,
                allowed_backend_channel_ids=allowed_backend_ids
            )
            if not acquired:
                return jsonify({"success": False, "error": old_card}), 409
            old_card_source_key_id = old_card.get("source_key_id") or ""

            source_card = dict(old_card.get("card") or {})
            expire_dt = _parse_iso_to_utc(old_card.get("expire_time"))
            now = datetime.now(timezone.utc)
            if not expire_dt or expire_dt <= now:
                return jsonify({"success": False, "error": "旧卡已过期，请重试"}), 409

            remaining_seconds = max(1, int((expire_dt - now).total_seconds()))
            actual_expire_minutes = max(1, (remaining_seconds + 59) // 60)
            try:
                actual_card_limit = float(source_card.get("card_limit", 0) or 0)
            except (TypeError, ValueError):
                actual_card_limit = 0

            legal_address = {}
            account_user_id = source_card.get("account_user_id")
            if account_user_id:
                account = get_account_by_user_id(account_user_id)
                if account:
                    legal_address = account.get("legal_address", {})
            if not legal_address:
                legal_address = source_card.get("legal_address", {}) or {}
            legal_address = apply_backend_channel_legal_address(
                legal_address,
                backend_channel_id=old_card.get("backend_channel_id"),
                settings=settings
            )

            card_info = dict(source_card)
            card_info.update({
                "expire_time": expire_dt.isoformat(),
                "expire_minutes": actual_expire_minutes,
                "card_limit": actual_card_limit,
                "backend_channel_id": old_card.get("backend_channel_id"),
                "channel_head": old_card.get("channel_head") or source_card.get("channel_head"),
                "provider": source_card.get("provider") or old_card.get("provider"),
                "provider_label": source_card.get("provider_label") or old_card.get("provider_label"),
                "legal_address": legal_address,
                "redeem_source": "old_card_pool",
                "key_kind": "old_card",
                "old_card_source_key_id": old_card_source_key_id,
                "old_card_source_used_time": old_card.get("source_used_time"),
                "destroy_supported": bool(source_card.get("destroy_supported", source_card.get("provider") == "mercury"))
            })
            if selected_display_channel:
                card_info.update({
                    "channel_id": selected_display_channel["id"],
                    "display_channel_id": selected_display_channel["id"],
                    "display_channel_name": selected_display_channel["name"]
                })
            else:
                card_info.update({
                    "channel_id": None,
                    "display_channel_id": None,
                    "display_channel_name": None
                })

            used_ok, used_result = use_id(key_id, card_info=card_info)
            if not used_ok:
                status_code = 409 if used_result == "卡密已被使用" else 500
                return jsonify({"success": False, "error": used_result or "卡密标记失败"}), status_code

            try:
                mark_old_card_used(old_card_source_key_id, used_by_key=key_id)
                old_card_source_key_id = ""
            except Exception as pool_error:
                print(f"[兑换卡密] 旧卡池标记已使用失败 {key_id}: {pool_error}")

            redeemed_success = True
            return jsonify({
                "success": True,
                "card": card_info,
                "expire_minutes": actual_expire_minutes,
                "card_limit": actual_card_limit,
                "used_time": now.isoformat(),
                "legal_address": legal_address,
                "provider": card_info.get("provider"),
                "provider_label": card_info.get("provider_label"),
                "channel_id": card_info.get("channel_id"),
                "display_channel_id": card_info.get("display_channel_id"),
                "display_channel_name": card_info.get("display_channel_name"),
                "backend_channel_id": old_card.get("backend_channel_id"),
                "channel_head": old_card.get("channel_head"),
                "redeem_source": "old_card_pool",
                "key_kind": key_kind,
                **binding_payload
            }), 200

        channel_config = resolve_backend_channel(backend_channel_id)
        if not channel_config:
            return jsonify({"success": False, "error": "当前渠道后端未配置完成"}), 503

        active_accounts = []
        if channel_config["provider"] == 'mercury':
            if get_account_count() == 0:
                return jsonify({"success": False, "error": "系统维护中", "maintenance": True}), 503

            active_accounts = get_all_active_accounts()
            if not active_accounts:
                return jsonify({"success": False, "error": "系统维护中", "maintenance": True}), 503

        if channel_config["provider"] == 'timoes':
            relay_code_type = channel_config["relay_code_type"]
            channel_head = channel_config["head"]
            provider_label = channel_config["provider_label"]
            last_error = ""
            invalidated_count = 0

            while True:
                acquired, relay_result = acquire_timoes_code_for_redeem(
                    relay_code_type,
                    allowed_code_types=get_timoes_code_types()
                )
                if not acquired:
                    error_message = relay_result
                    if invalidated_count > 0 and last_error:
                        error_message = f"{relay_result}，最近失败: {last_error}"
                    return jsonify({"success": False, "error": error_message}), 409

                relay_code = relay_result.get("code", "")
                upstream = redeem_airwallex_card(relay_code)

                if upstream.get("success"):
                    actual_expire_minutes = upstream.get("expire_minutes", expire_minutes)
                    actual_card_limit = upstream.get("card_limit", card_limit)

                    try:
                        actual_expire_minutes = int(actual_expire_minutes or 0)
                    except (TypeError, ValueError):
                        actual_expire_minutes = expire_minutes

                    try:
                        actual_card_limit = float(actual_card_limit or 0)
                    except (TypeError, ValueError):
                        actual_card_limit = card_limit

                    now = datetime.now(timezone.utc)
                    expire_time = now + timedelta(minutes=actual_expire_minutes) if actual_expire_minutes > 0 else None
                    upstream_card = upstream.get("card") or {}
                    legal_address = apply_backend_channel_legal_address(
                        upstream.get("legal_address") or {},
                        channel_config=channel_config
                    )

                    card_info = {
                        "card_id": upstream_card.get("card_id", ""),
                        "pan": upstream_card.get("pan", ""),
                        "cvv": upstream_card.get("cvv", ""),
                        "exp_month": upstream_card.get("exp_month", ""),
                        "exp_year": upstream_card.get("exp_year", ""),
                        "created_time": now.isoformat(),
                        "expire_time": expire_time.isoformat() if expire_time else None,
                        "expire_minutes": actual_expire_minutes,
                        "card_limit": actual_card_limit,
                        "card_type": result.get("card_type", "credit"),
                        "provider": "timoes",
                        "provider_label": provider_label,
                        "channel_id": redeem_mode,
                        "display_channel_id": redeem_mode,
                        "display_channel_name": selected_display_channel["name"],
                        "backend_channel_id": backend_channel_id,
                        "channel_head": channel_head,
                        "relay_code_type": relay_code_type,
                        "legal_address": legal_address,
                        "destroy_supported": False
                    }

                    used_ok, used_result = use_id(key_id, card_info=card_info)
                    if not used_ok:
                        try:
                            mark_timoes_code_used(relay_code, used_by_key=key_id, redeemed_card=card_info)
                        except Exception as pool_error:
                            print(f"[兑换卡密] Timoes 卡密消费后记录失败 {relay_code}: {pool_error}")

                        status_code = 409 if used_result == "卡密已被使用" else 500
                        return jsonify({"success": False, "error": used_result or "卡密标记失败"}), status_code

                    try:
                        mark_timoes_code_used(relay_code, used_by_key=key_id, redeemed_card=card_info)
                    except Exception as pool_error:
                        print(f"[兑换卡密] Timoes 卡密标记已使用失败 {relay_code}: {pool_error}")

                    redeemed_success = True
                    return jsonify({
                        "success": True,
                        "card": card_info,
                        "expire_minutes": actual_expire_minutes,
                        "card_limit": actual_card_limit,
                        "used_time": now.isoformat(),
                        "legal_address": legal_address,
                        "provider": "timoes",
                        "provider_label": provider_label,
                        "channel_id": redeem_mode,
                        "display_channel_id": redeem_mode,
                        "display_channel_name": selected_display_channel["name"],
                        "backend_channel_id": backend_channel_id,
                        "channel_head": channel_head,
                        "relay_code_type": relay_code_type,
                        "key_kind": key_kind,
                        **binding_payload
                    }), 200

                if upstream.get("retryable"):
                    release_timoes_code_lock(relay_code)
                    return jsonify({
                        "success": False,
                        "error": upstream.get("error") or f"{provider_label} 上游暂时不可用"
                    }), 503

                invalidated_count += 1
                last_error = upstream.get("error") or f"{provider_label} 卡密无效"
                try:
                    mark_timoes_code_invalid(relay_code, last_error)
                except Exception as pool_error:
                    print(f"[兑换卡密] Timoes 卡密标记失效失败 {relay_code}: {pool_error}")
                print(f"[兑换卡密] Timoes 卡密失效 {relay_code}: {last_error}")

        if channel_config["provider"] == 'manual':
            manual_bin = channel_config["manual_bin"]
            acquired, manual_card = acquire_manual_card_for_redeem(manual_bin)
            if not acquired:
                return jsonify({"success": False, "error": manual_card}), 409
            manual_locked_pan = manual_card["pan"]

            actual_expire_minutes = manual_card.get("expire_minutes")
            if actual_expire_minutes is None:
                actual_expire_minutes = expire_minutes
            try:
                actual_expire_minutes = int(actual_expire_minutes or 0)
            except (TypeError, ValueError):
                actual_expire_minutes = expire_minutes

            actual_card_limit = manual_card.get("card_limit")
            if actual_card_limit is None:
                actual_card_limit = card_limit
            try:
                actual_card_limit = float(actual_card_limit or 0)
            except (TypeError, ValueError):
                actual_card_limit = card_limit

            now = datetime.now(timezone.utc)
            expire_time = now + timedelta(minutes=actual_expire_minutes) if actual_expire_minutes > 0 else None
            legal_address = apply_backend_channel_legal_address(
                manual_card.get("legal_address") or {},
                channel_config=channel_config
            )
            card_info = {
                "card_id": f"manual:{manual_card['pan']}",
                "pan": manual_card["pan"],
                "cvv": manual_card["cvv"],
                "exp_month": manual_card["exp_month"],
                "exp_year": manual_card["exp_year"],
                "created_time": now.isoformat(),
                "expire_time": expire_time.isoformat() if expire_time else None,
                "expire_minutes": actual_expire_minutes,
                "card_limit": actual_card_limit,
                "card_type": result.get("card_type", "credit"),
                "provider": "manual",
                "provider_label": "手动卡池",
                "channel_id": redeem_mode,
                "display_channel_id": redeem_mode,
                "display_channel_name": selected_display_channel["name"],
                "backend_channel_id": backend_channel_id,
                "channel_head": manual_bin,
                "legal_address": legal_address,
                "destroy_supported": False
            }

            used_ok, used_result = use_id(key_id, card_info=card_info)
            if not used_ok:
                try:
                    release_manual_card_lock(manual_card["pan"])
                except Exception as release_error:
                    print(f"[兑换卡密] 手动卡释放锁失败 {manual_card['pan']}: {release_error}")
                status_code = 409 if used_result == "卡密已被使用" else 500
                return jsonify({"success": False, "error": used_result or "卡密标记失败"}), status_code

            try:
                mark_manual_card_used(manual_card["pan"], used_by_key=key_id, redeemed_card=card_info)
                manual_locked_pan = ""
            except Exception as pool_error:
                print(f"[兑换卡密] 手动卡标记已使用失败 {manual_card['pan']}: {pool_error}")

            redeemed_success = True
            return jsonify({
                "success": True,
                "card": card_info,
                "expire_minutes": actual_expire_minutes,
                "card_limit": actual_card_limit,
                "used_time": now.isoformat(),
                "legal_address": legal_address,
                "provider": "manual",
                "provider_label": "手动卡池",
                "channel_id": redeem_mode,
                "display_channel_id": redeem_mode,
                "display_channel_name": selected_display_channel["name"],
                "backend_channel_id": backend_channel_id,
                "channel_head": manual_bin,
                "key_kind": key_kind,
                **binding_payload
            }), 200

        target_card_type = channel_config["card_type"]
        target_channel_head = channel_config["head"]
        target_card_type_name = "借记卡" if target_card_type == "debit" else "信用卡"
        last_error = "创建卡片失败"

        print(
            f"[兑换卡密] 显式渠道兑换: {channel_config['label']}，"
            f"共尝试 {len(active_accounts)} 个账户"
        )

        for i, account in enumerate(active_accounts):
            account_email = account["email"]
            print(
                f"[兑换卡密] 渠道 {channel_config['label']} 尝试账户 {i+1}/{len(active_accounts)}: "
                f"{account_email} 创建 ${card_limit} {target_card_type_name}..."
            )

            try:
                card_id, create_error = issue_card(account, transaction_limit=card_limit, card_type=target_card_type)

                if not card_id:
                    print(f"[兑换卡密] 账户 {account_email} 创建卡片失败: {create_error}，尝试下一个...")
                    last_error = create_error or "创建卡片失败"
                    continue

                card_details = reveal_card_details(card_id, account, card_type=target_card_type)

                if not card_details:
                    cancel_card(card_id, account, card_type=target_card_type)
                    print(f"[兑换卡密] 账户 {account_email} 获取卡片详情失败，尝试下一个...")
                    last_error = "获取卡片详情失败"
                    continue

                now = datetime.now(timezone.utc)
                expire_time = now + timedelta(minutes=expire_minutes)
                legal_address = apply_backend_channel_legal_address(
                    account.get("legal_address", {}),
                    channel_config=channel_config
                )

                card_info = {
                    "card_id": card_id,
                    "pan": card_details.get("pan", ""),
                    "cvv": card_details.get("cvv", ""),
                    "exp_month": card_details.get("exp_month", ""),
                    "exp_year": card_details.get("exp_year", ""),
                    "created_time": now.isoformat(),
                    "expire_time": expire_time.isoformat(),
                    "card_limit": card_limit,
                    "card_type": target_card_type,
                    "account_email": account_email,
                    "account_user_id": account["user_id"],
                    "provider": "mercury",
                    "provider_label": "Mercury",
                    "channel_id": redeem_mode,
                    "display_channel_id": redeem_mode,
                    "display_channel_name": selected_display_channel["name"],
                    "backend_channel_id": backend_channel_id,
                    "channel_head": target_channel_head,
                    "legal_address": legal_address,
                    "destroy_supported": True
                }

                used_ok, used_result = use_id(key_id, card_info={
                    "card_id": card_id,
                    "pan": card_info["pan"],
                    "cvv": card_info["cvv"],
                    "exp_month": card_info["exp_month"],
                    "exp_year": card_info["exp_year"],
                    "card_limit": card_limit,
                    "card_type": target_card_type,
                    "expire_time": expire_time.isoformat(),
                    "expire_minutes": expire_minutes,
                    "account_user_id": account["user_id"],
                    "provider": "mercury",
                    "provider_label": "Mercury",
                    "channel_id": redeem_mode,
                    "display_channel_id": redeem_mode,
                    "display_channel_name": selected_display_channel["name"],
                    "backend_channel_id": backend_channel_id,
                    "channel_head": target_channel_head,
                    "legal_address": legal_address,
                    "destroy_supported": True
                })

                if not used_ok:
                    try:
                        cancel_card(card_id, account, card_type=target_card_type)
                    except Exception as cancel_error:
                        print(f"[兑换卡密] 卡密标记失败后，回滚取消卡片异常: {cancel_error}")

                    if used_result == "卡密已被使用":
                        return jsonify({"success": False, "error": "卡密已被使用"}), 409

                    last_error = used_result or "卡密标记失败"
                    print(f"[兑换卡密] 卡片 {card_id} 已回滚，原因: {last_error}")
                    break

                with card_lock:
                    data = load_cards()
                    data["cards"].append(card_info)
                    save_cards(data)

                redeemed_success = True
                print(f"[兑换卡密] 卡片 {card_id} 创建成功 (使用账户 {account_email})")

                return jsonify({
                    "success": True,
                    "card": card_info,
                    "expire_minutes": expire_minutes,
                    "legal_address": legal_address,
                    "issued_card_type": target_card_type,
                    "provider": "mercury",
                    "provider_label": "Mercury",
                    "channel_id": redeem_mode,
                    "display_channel_id": redeem_mode,
                    "display_channel_name": selected_display_channel["name"],
                    "backend_channel_id": backend_channel_id,
                    "channel_head": target_channel_head,
                    "key_kind": key_kind,
                    **binding_payload
                }), 200

            except Exception as e:
                print(f"[兑换卡密] 账户 {account_email} 异常: {e}，尝试下一个...")
                last_error = str(e)
                continue

        print(f"[兑换卡密] 渠道 {channel_config['label']} 创建失败，最后错误: {last_error}")

        if "429" in str(last_error) or "操作过于频繁" in str(last_error) or "rate-limited" in str(last_error):
            return jsonify({"success": False, "error": last_error or "系统繁忙，请稍后再试", "maintenance": False}), 429

        return jsonify({"success": False, "error": last_error}), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if old_card_source_key_id and not redeemed_success:
            try:
                release_old_card_lock(old_card_source_key_id)
            except Exception as release_error:
                print(f"[兑换卡密] 释放旧卡锁失败 {old_card_source_key_id}: {release_error}")
        if manual_locked_pan and not redeemed_success:
            try:
                release_manual_card_lock(manual_locked_pan)
            except Exception as release_error:
                print(f"[兑换卡密] 释放手动卡锁失败 {manual_locked_pan}: {release_error}")
        if lock_acquired and not redeemed_success:
            try:
                release_id_redeem_lock(key_id)
            except Exception as release_error:
                print(f"[兑换卡密] 释放兑换锁失败 {key_id}: {release_error}")


@app.route('/api/keys/query', methods=['POST'])
def query_key():
    """查询已兑换卡密的卡片信息 - 无需登录，支持卡密ID或卡号查询"""
    try:
        req_data = request.json or {}
        key_id = req_data.get('key_id', '').strip()

        if not key_id:
            return jsonify({"success": False, "error": "请输入卡密或卡号"}), 400

        # 判断输入类型：如果全是数字（可能包含空格），则视为卡号
        # 修复：检查原始输入是否只包含数字和空格，避免将卡密误判为卡号
        clean_input = ''.join(c for c in key_id if c.isdigit())
        input_without_spaces = key_id.replace(' ', '')
        is_only_digits = input_without_spaces.isdigit()  # 原始输入去掉空格后必须全是数字
        is_pan = is_only_digits and len(clean_input) >= 12 and len(clean_input) <= 19

        if is_pan:
            # 通过卡号查询
            from id.id import query_by_pan
            success, result = query_by_pan(key_id)
            if not success:
                return jsonify({"success": False, "error": result}), 400

            # 获取账户地址
            card_data = dict(result.get("card", {}) or {})
            legal_address = {}
            account_user_id = card_data.get("account_user_id")
            if account_user_id:
                account = get_account_by_user_id(account_user_id)
                if account:
                    legal_address = account.get("legal_address", {})
            if not legal_address:
                legal_address = card_data.get("legal_address", {}) or {}
            legal_address = apply_backend_channel_legal_address(
                legal_address,
                backend_channel_id=card_data.get("backend_channel_id")
            )
            card_data["legal_address"] = legal_address

            return jsonify({
                "success": True,
                "key_id": result.get("key_id"),  # 返回卡密 ID
                "card": card_data,
                "expire_minutes": result["expire_minutes"],
                "card_limit": result["card_limit"],
                "key_kind": result.get("key_kind"),
                "used_time": result["used_time"],
                "destroyed": result.get("destroyed", False),
                "destroyed_time": result.get("destroyed_time"),
                "legal_address": legal_address,
                "provider": card_data.get("provider", "mercury"),
                "provider_label": card_data.get("provider_label", "Mercury"),
                **build_key_binding_payload(result)
            })
        else:
            # 通过卡密查询
            success, result = query_redeemed(key_id)
            if not success:
                if result == "卡密未使用":
                    valid, key_meta = validate_id(key_id)
                    if valid:
                        return jsonify({
                            "success": False,
                            "error": result,
                            "key_id": key_id,
                            "expire_minutes": key_meta.get("expire_minutes"),
                            "card_limit": key_meta.get("card_limit"),
                            "key_kind": key_meta.get("key_kind"),
                            **build_key_binding_payload(key_meta)
                        }), 400
                return jsonify({"success": False, "error": result}), 400

            # 获取账户地址
            card_data = dict(result.get("card", {}) or {})
            legal_address = {}
            account_user_id = card_data.get("account_user_id")
            if account_user_id:
                account = get_account_by_user_id(account_user_id)
                if account:
                    legal_address = account.get("legal_address", {})
            if not legal_address:
                legal_address = card_data.get("legal_address", {}) or {}
            legal_address = apply_backend_channel_legal_address(
                legal_address,
                backend_channel_id=card_data.get("backend_channel_id")
            )
            card_data["legal_address"] = legal_address

            return jsonify({
                "success": True,
                "key_id": key_id,  # 返回原始卡密 ID
                "card": card_data,
                "expire_minutes": result["expire_minutes"],
                "card_limit": result["card_limit"],
                "key_kind": result.get("key_kind"),
                "used_time": result["used_time"],
                "destroyed": result.get("destroyed", False),
                "destroyed_time": result.get("destroyed_time"),
                "legal_address": legal_address,
                "provider": card_data.get("provider", "mercury"),
                "provider_label": card_data.get("provider_label", "Mercury"),
                **build_key_binding_payload(result)
            })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/transactions', methods=['POST'])
def query_key_transactions():
    """查询卡片消费记录 - 无需登录"""
    try:
        # 检查消费记录查询是否开启
        settings = load_settings()
        if not settings.get("transaction_query_enabled", True):
            return jsonify({"success": False, "error": "消费记录查询功能已关闭", "disabled": True})

        req_data = request.json
        key_id = req_data.get('key_id', '').strip()

        if not key_id:
            return jsonify({"success": False, "error": "请输入卡密"}), 400

        query_success, query_result = query_redeemed(key_id)
        if query_success:
            provider = (query_result.get("card") or {}).get("provider", "mercury")
            if provider != "mercury":
                return jsonify({"success": False, "error": "该线路卡片暂不支持消费记录查询", "disabled": True})

        # 导入消费记录查询模块
        from card.transaction import get_transactions_by_card_key

        result = get_transactions_by_card_key(key_id, limit=50)
        return jsonify(result)

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    from account.accounts import get_account_count

    # 初始化默认管理员
    init_default_admin()

    # 显示账户信息
    init_account_info()

    # 启动服务器
    print("=" * 60)
    print("[启动] Web 服务器启动在 http://127.0.0.1:7999")
    print("[提示] 默认管理员: admin / admin")
    print("[提示] 创建卡片时将随机使用已配置的账户")
    if get_account_count() == 0:
        print("[提示] 请登录后台，在「后台账户」中添加 Mercury 账户")
    print("=" * 60)
    app.run(host='0.0.0.0', port=7999, debug=False, threaded=True)
