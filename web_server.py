"""
Mercury 虚拟卡管理 Web 服务器
所有账户 session 都保存在 accounts.json 中，每次请求后自动更新
"""

import sys
import json
import os
import threading
import time
import uuid
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
from id.id import generate_ids, validate_id, use_id, delete_id, get_all_ids, delete_all_ids, delete_ids_batch, delete_unused_ids_by_type, get_redeem_records, delete_record, delete_all_records, query_redeemed, get_hidden_ids_by_token, allocate_existing_ids_for_withdraw
from user.login import login, refresh_access_token, verify_access_token
from user.user import create_user, delete_user, update_user, get_all_users, is_admin, init_default_admin

app = Flask(__name__)

# 配置
CARD_FILE = os.path.join(os.path.dirname(__file__), "card", "card.json")
VERSION_FILE = os.path.join(os.path.dirname(__file__), "version.txt")


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
                        account = get_account_by_user_id(card.get("account_user_id"))
                        if account:
                            card_type = card.get("card_type", "credit")
                            success = cancel_card(card["card_id"], account, card_type=card_type)
                            if success:
                                print(f"[自动删除] 卡片 {card['card_id']} 已取消")
                            else:
                                print(f"[自动删除] 取消卡片 {card['card_id']} 失败")
                        else:
                            print(f"[自动删除] 找不到账户，无法取消卡片 {card['card_id']}")
                    except Exception as e:
                        print(f"[自动删除] 取消卡片 {card['card_id']} 失败: {e}")
                
                if expired_cards:
                    data["cards"] = active_cards
                    save_cards(data)
        
        except Exception as e:
            print(f"[后台任务] 检查过期卡片出错: {e}")
        
        time.sleep(30)


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


@app.route('/api/settings/batch', methods=['GET'])
@require_admin
def api_get_batch_settings():
    """获取批量兑换设置"""
    settings = load_settings()
    return jsonify({
        "success": True,
        "max_batch_count": settings.get("max_batch_count", 5),
        "batch_interval": settings.get("batch_interval", 5)
    })


@app.route('/api/settings/batch', methods=['POST'])
@require_admin
def api_set_batch_settings():
    """设置批量兑换参数"""
    try:
        data = request.json
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
        "batch_interval": settings.get("batch_interval", 5)
    })


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
    from account.accounts import get_account_by_user_id, list_mercury_cards
    
    try:
        account = get_account_by_user_id(user_id)
        if not account:
            return jsonify({"success": False, "error": "账户不存在"}), 404
        
        # 使用账户姓名作为持卡人过滤条件
        cardholder_name = account.get("name", "")
        success, cards = list_mercury_cards(account, cardholder_name_filter=cardholder_name)
        
        if success:
            return jsonify({"success": True, "cards": cards})
        else:
            return jsonify({"success": False, "error": cards}), 400
    except Exception as e:
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
            # 找到第一个有 session 的账户
            active_account = None
            for acc in org_accounts:
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
            success, all_cards = list_mercury_cards(active_account, cardholder_name_filter=None)
            
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
        success, cards = list_mercury_cards(account, card_type_filter=card_type, cardholder_name_filter=cardholder_name, minutes_ago=minutes_ago)
        
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
        now = datetime.now(timezone.utc)
        
        # 每个用户只能看到自己创建的卡片
        current_user = request.user.get('username')
        data["cards"] = [c for c in data.get("cards", []) if c.get("created_by") == current_user]
        
        for card in data.get("cards", []):
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
    """流式创建卡片 - 随机从多个账户中选择"""
    # 检查是否有账户
    if get_account_count() == 0:
        def error_generate():
            yield f"data: {json.dumps({'type': 'error', 'message': '没有配置 Mercury 账户，请先在「后台账户」中添加账户'})}\n\n"
        return Response(error_generate(), mimetype='text/event-stream')
    
    count = int(request.args.get('count', 1))
    card_limit = int(request.args.get('card_limit', 1))
    expire_minutes = int(request.args.get('expire_minutes', 60))
    interval = max(5, int(request.args.get('interval', 5)))
    card_type = request.args.get('card_type', 'credit')
    
    # 获取当前用户（在闭包外获取）
    current_user = request.user.get('username')
    
    def generate():
        created_count = 0
        failed_count = 0
        
        for i in range(count):
            if i > 0:
                for sec in range(interval, 0, -1):
                    yield f"data: {json.dumps({'type': 'waiting', 'seconds': sec, 'message': f'等待 {sec} 秒后创建下一张...'})}\n\n"
                    time.sleep(1)
            current = i + 1
            
            # 随机获取一个账户
            account = get_random_account()
            if not account:
                yield f"data: {json.dumps({'type': 'error', 'message': '没有可用的账户'})}\n\n"
                break
            
            account_email = account["email"]
            yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'creating', 'message': f'正在用 {account_email} 创建第 {current}/{count} 张 ${card_limit} 卡片...'})}\n\n"
            
            print(f"\n[创建卡片] 使用账户 {account_email} 创建第 {current}/{count} 张 ${card_limit} 卡片...")
            
            card_id, create_error = issue_card(account, transaction_limit=card_limit, card_type=card_type)
            
            if not card_id:
                failed_count += 1
                error_msg = create_error or "创建失败"
                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片创建失败 ({account_email}): {error_msg}'})}\n\n"
                continue
            
            yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'revealing', 'message': f'正在获取第 {current}/{count} 张卡片详情...'})}\n\n"
            
            card_details = reveal_card_details(card_id, account, card_type=card_type)
            
            if not card_details:
                failed_count += 1
                cancel_card(card_id, account, card_type=card_type)
                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': count, 'status': 'failed', 'message': f'第 {current} 张卡片获取详情失败'})}\n\n"
                continue
            
            now = datetime.now(timezone.utc)
            expire_time = now + timedelta(minutes=expire_minutes)
            
            card_info = {
                "card_id": card_id,
                "pan": card_details.get("pan", ""),
                "cvv": card_details.get("cvv", ""),
                "exp_month": card_details.get("exp_month", ""),
                "exp_year": card_details.get("exp_year", ""),
                "created_time": now.isoformat(),
                "expire_time": expire_time.isoformat(),
                "card_limit": card_limit,
                "card_type": card_type,
                "account_email": account_email,
                "account_user_id": account["user_id"],
                "created_by": current_user
            }
            
            with card_lock:
                data = load_cards()
                data["cards"].append(card_info)
                save_cards(data)
            
            created_count += 1
            print(f"[创建卡片] 卡片 {card_id} 创建成功 (账户: {account_email})")
            
            yield f"data: {json.dumps({'type': 'card_created', 'current': current, 'total': count, 'card': card_info})}\n\n"
        
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
        
        card_type = target_card.get("card_type", "credit")
        success = cancel_card(card_id, account, card_type=card_type)
        
        if success:
            with card_lock:
                data = load_cards()
                data["cards"] = [c for c in data["cards"] if c["card_id"] != card_id]
                save_cards(data)
            
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
            
            for i, card in enumerate(cards):
                current = i + 1
                card_id = card["card_id"]
                account_email = card.get("account_email", "未知")
                
                yield f"data: {json.dumps({'type': 'progress', 'current': current, 'total': total, 'status': 'cancelling', 'message': f'正在取消第 {current}/{total} 张卡片 ({account_email})...'})}\n\n"
                
                try:
                    # 使用卡片归属账户
                    account_user_id = card.get("account_user_id")
                    account = None
                    if account_user_id and account_user_id in accounts_map:
                        account = accounts_map[account_user_id]
                    else:
                        # 使用随机账户
                        account = get_random_account()
                    
                    if account:
                        card_type = card.get("card_type", "credit")
                        success = cancel_card(card_id, account, card_type=card_type)
                    else:
                        success = False
                    
                    if success:
                        cancelled_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    failed_count += 1
            
            data["cards"] = []
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
    data = get_all_ids(username=current_user, page=page, page_size=page_size)
    return jsonify(data)


@app.route('/api/keys/count', methods=['GET'])
@require_auth
def get_keys_count():
    """获取卡密数量（按类型）"""
    from id.id import get_unused_count_by_type
    current_user = request.user.get('username')
    card_type = request.args.get('card_type')
    count = get_unused_count_by_type(username=current_user, card_type=card_type)
    return jsonify({"count": count})


@app.route('/api/keys/generate', methods=['POST'])
@require_auth
def generate_keys():
    """生成卡密"""
    try:
        current_user = request.user.get('username')
        req_data = request.json
        count = int(req_data.get('count', 1))
        card_limit = int(req_data.get('card_limit', 1))
        expire_minutes = int(req_data.get('expire_minutes', 60))
        card_type = req_data.get('card_type', 'credit')
        
        ids = generate_ids(count, expire_minutes, card_limit=card_limit, card_type=card_type, created_by=current_user)
        
        return jsonify({
            "success": True,
            "generated": len(ids),
            "ids": ids
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
        card_type = req_data.get('card_type', 'credit')
        note = (req_data.get('note') or '').strip()
        expire_minutes = int(req_data.get('expire_minutes', 60))
        card_limit = int(req_data.get('card_limit', 0))
        
        if count < 1:
            return jsonify({"success": False, "error": "数量必须大于 0"}), 400
        
        token = uuid.uuid4().hex
        allocated = allocate_existing_ids_for_withdraw(
            token=token,
            note=note,
            username=current_user,
            card_type=card_type,
            count=count
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
                hidden_note=note
            )
        
        prepared_total = len(allocated) + len(generated)
        
        link = f"{request.host_url.rstrip('/')}/withdraw/{token}"
        
        return jsonify({
            "success": True,
            "generated": prepared_total,
            "from_existing": len(allocated),
            "generated_new": len(generated),
            "link": link,
            "token": token
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
        
        count = delete_unused_ids_by_type(card_type=card_type, username=current_user)
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/records', methods=['GET'])
@require_auth
def get_key_records():
    """获取兑换记录"""
    try:
        current_user = request.user.get('username')
        records = get_redeem_records(username=current_user)
        return jsonify({"success": True, "records": records})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/records/<key_id>', methods=['DELETE'])
@require_auth
def delete_key_record(key_id):
    """删除单条兑换记录"""
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
    """删除所有兑换记录"""
    try:
        current_user = request.user.get('username')
        count = delete_all_records(username=current_user)
        return jsonify({"success": True, "deleted": count})
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
                
                print(f"[销毁卡密] 卡密已兑换，卡片ID: {card_id}, 类型: {card_type}, 账户: {account_user_id}")
                
                if card_id:
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
            
            # 标记卡密为已销毁（而不是删除）
            mark_success, mark_error = mark_destroyed(key_id, username=current_user)
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
            if account:
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
    """兑换卡密 - 无需登录，自动重试所有活跃账户"""
    # 检查是否有账户
    if get_account_count() == 0:
        return jsonify({"success": False, "error": "系统维护中", "maintenance": True}), 503
    
    try:
        req_data = request.json
        key_id = req_data.get('key_id', '').strip()
        
        if not key_id:
            return jsonify({"success": False, "error": "请输入卡密"}), 400
        
        valid, result = validate_id(key_id)
        if not valid:
            return jsonify({"success": False, "error": result}), 400
        
        expire_minutes = result["expire_minutes"]
        card_limit = result["card_limit"]
        card_type = result.get("card_type", "credit")
        
        # 获取所有活跃账户（已随机打乱）
        active_accounts = get_all_active_accounts()
        if not active_accounts:
            return jsonify({"success": False, "error": "系统维护中", "maintenance": True}), 503
        
        card_type_name = "借记卡" if card_type == "debit" else "信用卡"
        last_error = "创建卡片失败"
        
        # 尝试所有活跃账户
        for i, account in enumerate(active_accounts):
            account_email = account["email"]
            print(f"[兑换卡密] 尝试账户 {i+1}/{len(active_accounts)}: {account_email} 创建 ${card_limit} {card_type_name}...")
            
            try:
                card_id, create_error = issue_card(account, transaction_limit=card_limit, card_type=card_type)
                
                if not card_id:
                    print(f"[兑换卡密] 账户 {account_email} 创建卡片失败: {create_error}，尝试下一个...")
                    last_error = create_error or "创建卡片失败"
                    continue
                
                card_details = reveal_card_details(card_id, account, card_type=card_type)
                
                if not card_details:
                    cancel_card(card_id, account, card_type=card_type)
                    print(f"[兑换卡密] 账户 {account_email} 获取卡片详情失败，尝试下一个...")
                    last_error = "获取卡片详情失败"
                    continue
                
                # 成功了
                now = datetime.now(timezone.utc)
                expire_time = now + timedelta(minutes=expire_minutes)
                
                card_info = {
                    "card_id": card_id,
                    "pan": card_details.get("pan", ""),
                    "cvv": card_details.get("cvv", ""),
                    "exp_month": card_details.get("exp_month", ""),
                    "exp_year": card_details.get("exp_year", ""),
                    "created_time": now.isoformat(),
                    "expire_time": expire_time.isoformat(),
                    "card_limit": card_limit,
                    "card_type": card_type,
                    "account_email": account_email,
                    "account_user_id": account["user_id"]
                }
                
                # 标记卡密已使用，并保存兑换的卡片信息
                use_id(key_id, card_info={
                    "card_id": card_id,
                    "pan": card_info["pan"],
                    "cvv": card_info["cvv"],
                    "exp_month": card_info["exp_month"],
                    "exp_year": card_info["exp_year"],
                    "card_limit": card_limit,
                    "card_type": card_type,
                    "expire_time": expire_time.isoformat(),
                    "account_user_id": account["user_id"]
                })
                
                with card_lock:
                    data = load_cards()
                    data["cards"].append(card_info)
                    save_cards(data)
                
                print(f"[兑换卡密] 卡片 {card_id} 创建成功 (使用账户 {account_email})")
                
                return jsonify({
                    "success": True,
                    "card": card_info,
                    "expire_minutes": expire_minutes,
                    "legal_address": account.get("legal_address", {})
                })
                
            except Exception as e:
                print(f"[兑换卡密] 账户 {account_email} 异常: {e}，尝试下一个...")
                last_error = str(e)
                continue
        
        # 所有账户都失败了
        print(f"[兑换卡密] 所有 {len(active_accounts)} 个账户都失败了，最后错误: {last_error}")
        
        # 如果是 429 错误，标记为 maintenance 以便前端显示特定状态（可选）
        if "429" in str(last_error) or "操作过于频繁" in str(last_error) or "rate-limited" in str(last_error):
             return jsonify({"success": False, "error": last_error or "系统繁忙，请稍后再试", "maintenance": False}), 429

        return jsonify({"success": False, "error": last_error}), 500
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keys/query', methods=['POST'])
def query_key():
    """查询已兑换卡密的卡片信息 - 无需登录"""
    try:
        req_data = request.json
        key_id = req_data.get('key_id', '').strip()
        
        if not key_id:
            return jsonify({"success": False, "error": "请输入卡密"}), 400
        
        success, result = query_redeemed(key_id)
        if not success:
            return jsonify({"success": False, "error": result}), 400
        
        # 获取账户地址
        legal_address = {}
        card_data = result.get("card", {})
        account_user_id = card_data.get("account_user_id")
        if account_user_id:
            account = get_account_by_user_id(account_user_id)
            if account:
                legal_address = account.get("legal_address", {})
        
        return jsonify({
            "success": True,
            "card": result["card"],
            "expire_minutes": result["expire_minutes"],
            "card_limit": result["card_limit"],
            "used_time": result["used_time"],
            "destroyed": result.get("destroyed", False),
            "destroyed_time": result.get("destroyed_time"),
            "legal_address": legal_address
        })
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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


if __name__ == '__main__':
    from account.accounts import get_account_count
    
    # 初始化卡片文件
    if not os.path.exists(CARD_FILE):
        save_cards({"cards": []})
    
    # 初始化默认管理员
    init_default_admin()
    
    # 显示账户信息
    init_account_info()
    
    # 启动后台过期检查线程
    expire_thread = threading.Thread(target=check_expired_cards, daemon=True)
    expire_thread.start()
    print("[启动] 后台过期检查线程已启动")
    
    # 启动账户自动刷新线程（每10分钟）
    refresh_thread = threading.Thread(target=auto_refresh_accounts, daemon=True)
    refresh_thread.start()
    print("[启动] 账户自动刷新线程已启动（每10分钟）")
    
    # 启动代理延迟检查线程（每60秒）
    start_proxy_latency_checker()
    
    # 启动服务器
    print("=" * 60)
    print("[启动] Web 服务器启动在 http://127.0.0.1:7999")
    print("[提示] 默认管理员: admin / admin")
    print("[提示] 创建卡片时将随机使用已配置的账户")
    if get_account_count() == 0:
        print("[提示] 请登录后台，在「后台账户」中添加 Mercury 账户")
    print("=" * 60)
    app.run(host='0.0.0.0', port=7999, debug=False, threaded=True)
