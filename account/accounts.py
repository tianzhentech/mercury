"""
Mercury 多账户管理模块
支持存储多个 Mercury 账户的 cookies 和用户信息
所有 session 都持久化在 accounts.json 中，每次请求后自动更新
"""

import json
import os
import random
import tempfile
from curl_cffi import requests
from datetime import datetime
import threading
import queue
from contextlib import contextmanager
import fcntl

def format_proxy_url(proxy_str):
    """
    将代理格式转换为 requests 支持的标准格式
    关键修复：将 socks5 自动转换为 socks5h 以支持远程 DNS 解析
    """
    if not proxy_str:
        return ""
    
    proxy_str = proxy_str.strip()
    clean_str = proxy_str
    protocol = "http" # 默认 fallback
    
    # 分离协议头
    if "://" in proxy_str:
        protocol, clean_str = proxy_str.split("://", 1)
    
    # 核心修复：如果是 socks5，强制改为 socks5h (让代理端解析域名，防止本地DNS污染)
    if protocol == "socks5":
        protocol = "socks5h"
        
    parts = clean_str.split(":")
    
    # 处理 ip:port:user:pass 格式 (4个部分)
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"{protocol}://{user}:{password}@{ip}:{port}"
    
    # 处理 ip:port (2个部分)
    if len(parts) == 2:
        # 如果原始没带协议，且判定是socks代理，最好默认用 socks5h
        # 这里为了安全，如果没指定协议，还是保持 http 或用户原始意图，
        # 但如果是拼接出来的，确保 protocol 变量生效
        return f"{protocol}://{clean_str}"
        
    # 如果用户原来输入的是 socks5://... 这种标准格式，也要把头换掉
    if proxy_str.startswith("socks5://"):
        return "socks5h://" + proxy_str[9:]
        
    return proxy_str

def test_proxy_connection(proxy_url):
    """
    测试代理连通性 (双重验证版)
    1. 先测试访问通用网站 (Google)，验证代理本身是否可用
    2. 再测试访问 Mercury 官网，验证是否被目标网站屏蔽
    """
    # 格式化代理 (自动处理 socks5 -> socks5h)
    formatted_proxy = format_proxy_url(proxy_url)
    proxies = {
        "http": formatted_proxy,
        "https": formatted_proxy
    }
    
    print(f"🔄 正在测试代理: {formatted_proxy}")

    # ================= Step 1: 测试代理本身 (Google) =================
    try:
        print(f"   Step 1: 尝试通过代理访问 Google...")
        # 仅测试连通性，超时时间设短一点
        resp = requests.get("https://www.google.com", proxies=proxies, timeout=5, impersonate="chrome")
        
        if resp.status_code == 200:
            print("   ✅ 代理本身可用 (Google 连接成功)")
        else:
            return False, f"代理连通性测试失败 (访问 Google 返回 {resp.status_code})"
            
    except Exception as e:
        return False, f"代理无法连接外网: {str(e)}"

    # ================= Step 2: 测试目标网站 (Mercury) =================
    try:
        print(f"   Step 2: 尝试通过代理访问 Mercury 官网...")
        
        # 伪装 User-Agent
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # 使用官网主页进行测试 (避免 404)
        target_url = "https://mercury.com" 
        
        response = requests.get(target_url, headers=headers, proxies=proxies, timeout=15, impersonate="chrome")
        
        if response.status_code == 200:
            print("   ✅ Mercury 连接成功！")
            return True, formatted_proxy
        elif response.status_code == 403:
            return False, "代理被 Mercury 拒绝 (403 Forbidden) - 此时IP可能被风控"
        else:
            # 只要能连通，哪怕是其他状态码也视为网络层通过
            print(f"   ⚠️ 连接通畅但状态码非200 ({response.status_code})，视为成功")
            return True, formatted_proxy
            
    except Exception as e:
        error_str = str(e)
        if "0x03" in error_str or "Network unreachable" in error_str:
            return False, "该代理 IP 被 Mercury 网络层屏蔽 (Network unreachable)"
        return False, f"Mercury 连接异常: {error_str}"


def test_proxy_latency(proxy_url):
    """
    测试代理延迟 (使用标准 requests 库，支持 socks5h)
    
    Returns:
        dict: {"success": bool, "latency": int (ms), "error": str}
    """
    import time
    import requests as std_requests
    
    formatted_proxy = format_proxy_url(proxy_url)
    if not formatted_proxy:
        return {"success": False, "error": "代理地址无效"}
    
    proxies = {
        "http": formatted_proxy,
        "https": formatted_proxy
    }
    
    headers = {
        "User-Agent": "ClashForWindows/0.20.39"
    }
    
    try:
        start_time = time.time()
        response = std_requests.get("https://www.gstatic.com/generate_204", headers=headers, proxies=proxies, timeout=5)
        end_time = time.time()
        
        latency_ms = int((end_time - start_time) * 1000)
        
        # gstatic 返回 204 表示成功
        if response.status_code in [200, 204]:
            return {"success": True, "latency": latency_ms}
        else:
            return {"success": True, "latency": latency_ms}
            
    except std_requests.exceptions.Timeout:
        return {"success": False, "error": "超时"}
    except Exception as e:
        error_msg = str(e)
        return {"success": False, "error": error_msg[:50]}
# 文件写入锁（线程内 + 进程间）
_file_lock = threading.RLock()
_lock_state = threading.local()

# 状态更新事件队列列表（用于 SSE 推送）
_status_subscribers = []
_subscribers_lock = threading.Lock()

# 代理延迟 SSE 订阅者
_proxy_latency_subscribers = []
_proxy_latency_lock = threading.Lock()

# 代理延迟缓存
_proxy_latency_cache = {}

# 文件路径
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts.json")
USER_INFO_FILE = os.path.join(os.path.dirname(__file__), "user_info.json")
COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies.json")
PROXIES_FILE = os.path.join(os.path.dirname(__file__), "proxies.json")
STORAGE_LOCK_FILE = os.path.join(os.path.dirname(__file__), ".storage.lock")


@contextmanager
def _storage_lock():
    """账户存储的进程级锁，防止多 worker 覆盖同一份 JSON 文件。"""
    depth = getattr(_lock_state, "depth", 0)
    if depth > 0:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return

    with _file_lock:
        os.makedirs(os.path.dirname(STORAGE_LOCK_FILE), exist_ok=True)
        lock_handle = open(STORAGE_LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            _lock_state.depth = 1
            _lock_state.handle = lock_handle
            yield
        finally:
            _lock_state.depth = 0
            _lock_state.handle = None
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


def _load_json_file(file_path, default_data):
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[存储] 加载 {os.path.basename(file_path)} 失败: {e}")
    return default_data.copy() if isinstance(default_data, dict) else default_data


def _atomic_write_json(file_path, data):
    directory = os.path.dirname(file_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(file_path)}.",
        suffix=".tmp",
        dir=directory
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def load_accounts():
    """加载所有账户"""
    return _load_json_file(ACCOUNTS_FILE, {"accounts": [], "active_user_id": None})


def save_accounts(data):
    """保存账户数据"""
    _atomic_write_json(ACCOUNTS_FILE, data)


def load_user_info():
    """加载所有用户详细信息"""
    return _load_json_file(USER_INFO_FILE, {"accounts": []})


def save_user_info(data):
    """保存用户详细信息"""
    _atomic_write_json(USER_INFO_FILE, data)


def load_proxies():
    """加载已保存的代理列表"""
    return _load_json_file(PROXIES_FILE, {"proxies": []})


def save_proxies(data):
    """保存代理列表"""
    _atomic_write_json(PROXIES_FILE, data)


def get_all_proxies():
    """获取所有已保存的代理"""
    data = load_proxies()
    return data.get("proxies", [])


def add_proxy(name, proxy_url):
    """
    添加新代理
    
    Args:
        name: 代理名称/备注
        proxy_url: 代理地址
        
    Returns:
        dict: {"success": bool, "message": str, "proxy": dict}
    """
    if not proxy_url or not proxy_url.strip():
        return {"success": False, "error": "代理地址不能为空"}
    
    # 格式化并测试代理
    is_valid, result = test_proxy_connection(proxy_url)
    
    if not is_valid:
        return {"success": False, "error": f"代理验证失败: {result}"}
    
    formatted_proxy = result
    
    with _storage_lock():
        data = load_proxies()
        proxies = data.get("proxies", [])
        
        # 检查是否已存在相同代理
        for p in proxies:
            if p["url"] == formatted_proxy:
                return {"success": False, "error": "该代理已存在"}
        
        # 生成唯一ID
        import uuid
        proxy_id = str(uuid.uuid4())[:8]
        
        new_proxy = {
            "id": proxy_id,
            "name": name.strip() if name else formatted_proxy,
            "url": formatted_proxy,
            "created_at": datetime.now().isoformat()
        }
        
        proxies.append(new_proxy)
        data["proxies"] = proxies
        save_proxies(data)
        
        return {"success": True, "message": "代理添加成功", "proxy": new_proxy}


def delete_proxy(proxy_id):
    """
    删除代理
    
    Args:
        proxy_id: 代理ID
        
    Returns:
        dict: {"success": bool, "message": str}
    """
    with _storage_lock():
        data = load_proxies()
        proxies = data.get("proxies", [])
        
        # 查找要删除的代理
        target_proxy = None
        for p in proxies:
            if p["id"] == proxy_id:
                target_proxy = p
                break
        
        if not target_proxy:
            return {"success": False, "error": "代理不存在"}
        
        # 检查是否有账户正在使用该代理
        accounts_data = load_accounts()
        accounts_using = []
        for acc in accounts_data.get("accounts", []):
            if acc.get("proxy") == target_proxy["url"]:
                accounts_using.append(acc.get("email") or acc.get("name") or acc.get("user_id"))
        
        if accounts_using:
            return {
                "success": False, 
                "error": f"无法删除：有 {len(accounts_using)} 个账户正在使用此代理 ({', '.join(accounts_using[:3])}{'...' if len(accounts_using) > 3 else ''})"
            }
        
        # 删除代理
        proxies = [p for p in proxies if p["id"] != proxy_id]
        data["proxies"] = proxies
        save_proxies(data)
        
        return {"success": True, "message": "代理删除成功"}


def get_proxy_by_id(proxy_id):
    """根据ID获取代理"""
    proxies = get_all_proxies()
    for p in proxies:
        if p["id"] == proxy_id:
            return p
    return None


def get_default_proxy_id():
    """获取默认代理ID"""
    data = load_proxies()
    return data.get("default_proxy_id", "")


def set_default_proxy_id(proxy_id):
    """
    设置默认代理ID
    
    Args:
        proxy_id: 代理ID，空字符串表示取消默认
        
    Returns:
        dict: {"success": bool, "message": str}
    """
    with _storage_lock():
        data = load_proxies()
        
        if proxy_id:
            # 验证代理存在
            proxies = data.get("proxies", [])
            if not any(p["id"] == proxy_id for p in proxies):
                return {"success": False, "error": "代理不存在"}
            data["default_proxy_id"] = proxy_id
            message = "已设为默认代理"
        else:
            # 取消默认
            data["default_proxy_id"] = ""
            message = "已取消默认代理"
        
        save_proxies(data)
        return {"success": True, "message": message}


def subscribe_proxy_latency():
    """订阅代理延迟更新"""
    import queue
    q = queue.Queue()
    with _proxy_latency_lock:
        _proxy_latency_subscribers.append(q)
    return q


def unsubscribe_proxy_latency(q):
    """取消订阅代理延迟更新"""
    with _proxy_latency_lock:
        if q in _proxy_latency_subscribers:
            _proxy_latency_subscribers.remove(q)


def broadcast_proxy_latency(data):
    """广播代理延迟更新到所有订阅者"""
    with _proxy_latency_lock:
        for q in _proxy_latency_subscribers:
            try:
                q.put_nowait(data)
            except:
                pass


def get_proxy_latency_cache():
    """获取代理延迟缓存"""
    return _proxy_latency_cache.copy()


def test_local_latency():
    """测试本地延迟（不使用代理）"""
    import time
    import requests as std_requests
    
    headers = {
        "User-Agent": "ClashForWindows/0.20.39"
    }
    
    try:
        start_time = time.time()
        response = std_requests.get("https://www.gstatic.com/generate_204", headers=headers, timeout=5)
        end_time = time.time()
        
        latency_ms = int((end_time - start_time) * 1000)
        
        if response.status_code in [200, 204]:
            return {"success": True, "latency": latency_ms}
        else:
            return {"success": True, "latency": latency_ms}
            
    except std_requests.exceptions.Timeout:
        return {"success": False, "error": "超时"}
    except Exception as e:
        return {"success": False, "error": str(e)[:30]}


def _test_all_proxies_latency():
    """测试所有代理的延迟（包括本地延迟）"""
    proxies = get_all_proxies()
    results = {}
    
    # 测试本地延迟
    local_result = test_local_latency()
    results["__local__"] = local_result
    _proxy_latency_cache["__local__"] = local_result
    
    # 测试所有代理
    for proxy in proxies:
        result = test_proxy_latency(proxy["url"])
        results[proxy["id"]] = result
        _proxy_latency_cache[proxy["id"]] = result
    
    # 广播结果
    broadcast_proxy_latency(results)
    
    return results


def start_proxy_latency_checker():
    """启动代理延迟检查后台线程"""
    import time
    
    def checker_loop():
        while True:
            try:
                _test_all_proxies_latency()
            except Exception as e:
                print(f"[代理延迟检查] 错误: {e}")
            time.sleep(60)  # 每分钟检查一次
    
    thread = threading.Thread(target=checker_loop, daemon=True)
    thread.start()
    print("[启动] 代理延迟检查线程已启动（每60秒）")


def get_user_info_by_session(session_cookie, proxy=None):
    """
    通过 _SESSION cookie 获取用户信息
    (已修复 NoneType 崩溃问题)
    """
    # 导入 headers
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from header import default_headers
    
    url = "https://backend.mercury.com/get-initial-data-v2"
    request_headers = default_headers.copy()
    request_headers["X-Frontend-Path"] = "/dashboard"
    request_headers["Cookie"] = f"_SESSION={session_cookie}"
    
    # 设置代理
    proxies = None
    if proxy and proxy.strip():
        proxies = {"http": proxy.strip(), "https": proxy.strip()}
    
    # 使用 Session 来管理连接，确保连接被正确关闭
    session = None
    try:
        session = requests.Session()
        response = session.get(url, headers=request_headers, timeout=15, proxies=proxies, impersonate="chrome")
        
        # 从响应头中获取新的 _SESSION
        new_session = session_cookie  # 默认使用原始的
        set_cookie = response.headers.get('Set-Cookie', '')
        if '_SESSION=' in set_cookie:
            for part in set_cookie.split(';'):
                part = part.strip()
                if part.startswith('_SESSION='):
                    new_session = part[9:]
                    break
        
        if response.status_code != 200:
            return False, f"请求失败: {response.status_code}", None
        
        data = response.json()
        
        # 提取关键信息 (使用 get() or {} 防止 NoneType 错误)
        user_details = data.get("userDetails") or {}
        org_specifics = data.get("orgSpecifics") or {}
        org_data = org_specifics.get("organizationData") or {}
        global_data = org_specifics.get("globalData") or {}
        credit_account = global_data.get("mercuryCreditAccount") or {}
        
        user_id = user_details.get("id")
        if not user_id:
            return False, "无法获取用户 ID (可能Session已失效)", None
        
        # 检测账户是否被限制/封禁
        org_context = org_specifics.get("orgContext", "")
        
        # 检查 linkedUsers 中的 orgUserStatus
        linked_users = user_details.get("linkedUsers") or []
        org_user_status = ""
        for linked_user in linked_users:
            if linked_user.get("userId") == user_id:
                org_user_status = linked_user.get("orgUserStatus", "")
                break
        
        # 判断是否被限制
        is_restricted = False
        restriction_reason = ""
        
        # 检查组织上下文是否被锁定
        if "Locked" in org_context or org_context == "businessLocked":
            is_restricted = True
            restriction_reason = f"组织状态: {org_context}"
        
        # 检查用户组织状态是否被锁定
        if "Locked" in org_user_status or org_user_status == "orgLockedWithRemediation":
            is_restricted = True
            restriction_reason = f"用户状态: {org_user_status}"
        
        # 检查 capabilities 中的 card 权限
        user_org_data = org_data.get("userOrgData") or {}
        capabilities = user_org_data.get("capabilities") or {}
        card_capabilities = capabilities.get("card") or {}
        if card_capabilities.get("issue") == "deny" and card_capabilities.get("read") == "deny":
            # 如果已经因为其他原因被标记为限制，则保留原因
            if not is_restricted:
                is_restricted = True
                restriction_reason = "无开卡权限"
        
        # 构建用户信息
        # 提取法定地址，将 address2 合并到 address1
        legal_address = user_details.get("legalAddress") or {}
        addr1 = legal_address.get("address1", "")
        addr2 = legal_address.get("address2", "")
        combined_address1 = f"{addr1} {addr2}".strip() if addr2 else addr1
        
        # 安全提取 userOrgData
        user_org_data = org_data.get("userOrgData") or {}
        
        user_info = {
            "user": {
                "id": user_id,
                "email": user_details.get("email"),
                "first_name": user_details.get("firstName"),
                "last_name": user_details.get("lastName"),
                "phone_number": user_details.get("phoneNumber"),
                "status": user_details.get("status"),
                "created_at": user_details.get("createdAt"),
                "has_verified_email": user_details.get("hasVerifiedEmail"),
                "legal_address": {
                    "address1": combined_address1,
                    "address2": "",
                    "city": legal_address.get("city", ""),
                    "region": legal_address.get("region", ""),
                    "postal_code": legal_address.get("postalCode", ""),
                    "country": legal_address.get("country", ""),
                },
            },
            "organization": {
                "id": org_data.get("organizationId"),
                "name": org_data.get("name"),
                "legal_business_name": org_data.get("legalBusinessName"),
                "callsign": org_data.get("callsign"),
                "is_closed": org_data.get("isOrgClosed"),
                "company_structure": org_data.get("companyStructure"),
            },
            "user_role": {
                "role": user_org_data.get("role"),
                "role_name": user_org_data.get("orgUserRoleName"),
                "is_admin": user_org_data.get("role") == "administrator",
                "org_user_id": user_org_data.get("orgUserId"),
            },
            "credit_account": {
                "account_id": credit_account.get("accountId"),
                "available_balance": credit_account.get("availableBalance"),
                "credit_limit": credit_account.get("creditLimit"),
                "account_status": credit_account.get("accountStatus"),
                "created_at": credit_account.get("createdAt"),
            },
            "depository_accounts": [],
        }
        
        # 提取存款账户
        # 即使 mercuryDepositoryAccounts 是 None，or [] 也会将其变为空列表，防止循环报错
        depository_accounts_list = global_data.get("mercuryDepositoryAccounts") or []
        for account in depository_accounts_list:
            # 安全提取 routingInfo
            routing_info = account.get("routingInfo") or {}
            
            user_info["depository_accounts"].append({
                "id": account.get("id"),
                "name": account.get("name"),
                "nickname": account.get("nickname"),
                "account_number": routing_info.get("accountNumber"),
                "routing_number": routing_info.get("routingNumber"),
                "available_balance": account.get("availableBalance"),
                "current_balance": account.get("currentBalance"),
                "account_status": account.get("accountStatus"),
            })
        
        # 添加限制状态到 user_info
        user_info["restriction"] = {
            "is_restricted": is_restricted,
            "reason": restriction_reason,
            "org_context": org_context,
            "org_user_status": org_user_status,
        }
        
        return True, user_info, new_session
        
    except Exception as e:
        # 打印错误堆栈以便调试
        # import traceback
        # traceback.print_exc() 
        return False, f"请求异常: {str(e)}", None
    finally:
        # 确保 Session 被关闭，释放底层连接
        if session:
            try:
                session.close()
            except:
                pass



def add_account(session_cookie, proxy=None):
    """
    通过 _SESSION cookie 添加新账户
    
    Args:
        session_cookie: _SESSION cookie 值
        proxy: 代理地址（可选）
        
    Returns:
        dict: {"success": bool, "message": str, "account": dict}
    """
    # 如果传入了代理，先格式化
    if proxy:
        proxy = format_proxy_url(proxy)
    # 获取用户信息
    success, result, new_session = get_user_info_by_session(session_cookie, proxy=proxy)
    
    if not success:
        return {"success": False, "error": result}
    
    user_info = result
    user_id = user_info["user"]["id"]
    
    # 检查账户是否被限制/封禁
    restriction = user_info.get("restriction", {})
    is_restricted = restriction.get("is_restricted", False)
    restriction_reason = restriction.get("reason", "")
    
    # 使用响应中返回的新 session（如果有）
    final_session = new_session if new_session else session_cookie
    
    with _storage_lock():
        # 加载现有账户
        accounts_data = load_accounts()
        user_info_data = load_user_info()
        
        # 检查是否已存在，并保留现有代理和凭证
        existing_index = None
        existing_account = None
        for i, acc in enumerate(accounts_data["accounts"]):
            if acc["user_id"] == user_id:
                existing_index = i
                existing_account = acc
                break
        
        # 如果账户被限制且是新添加的账户，拒绝添加
        if is_restricted and existing_index is None:
            email = user_info["user"]["email"]
            print(f"❌ 账户 {email} 被 Mercury 限制，拒绝添加: {restriction_reason}")
            return {"success": False, "error": "账户被 Mercury 限制，拒绝添加"}
        
        # 构建账户摘要信息
        depository_account_id = None
        if user_info.get("depository_accounts") and len(user_info["depository_accounts"]) > 0:
            depository_account_id = user_info["depository_accounts"][0].get("id")
        
        # 根据限制状态设置 account_status
        if is_restricted:
            account_status = "restricted"
            organization_name = user_info["organization"]["name"]
            print(f"⚠️ 账户 {user_info['user']['email']} 被 Mercury 限制，标记为 restricted: {restriction_reason}")
            # 将同组织下的所有账户都标记为 restricted
            mark_organization_restricted(organization_name, restriction_reason)
            # 重新加载 accounts_data，避免覆盖 mark_organization_restricted 的修改
            accounts_data = load_accounts()
            # 重新查找 existing_index
            for i, acc in enumerate(accounts_data.get("accounts", [])):
                if acc["user_id"] == user_id:
                    existing_index = i
                    existing_account = acc
                    break
        else:
            account_status = "active"
        
        account_summary = {
            "user_id": user_id,
            "email": user_info["user"]["email"],
            "name": f"{user_info['user']['first_name']} {user_info['user']['last_name']}",
            "organization": user_info["organization"]["name"],
            "credit_account_id": user_info["credit_account"]["account_id"],
            "depository_account_id": depository_account_id,
            "credit_limit": user_info["credit_account"]["credit_limit"],
            "available_balance": user_info["credit_account"]["available_balance"],
            "status": user_info["user"]["status"],
            "account_status": account_status,
            "restriction_reason": restriction_reason if is_restricted else "",
            "legal_address": user_info["user"].get("legal_address", {}),
            "proxy": proxy if proxy is not None else (existing_account.get("proxy", "") if existing_account else ""),
            "_SESSION": final_session,
            "ajs_user_id": user_id,
            "updated_at": datetime.now().isoformat(),
        }
        
        if existing_index is not None:
            # 更新现有账户，保留凭证信息
            if existing_account:
                # 保留密码和设备信息
                for key in ["mercury_password", "totp_secret", "email_password", "device_id", "device_fingerprint", "created_at"]:
                    if key in existing_account:
                        account_summary[key] = existing_account[key]
            accounts_data["accounts"][existing_index] = account_summary
            
            # 更新用户详细信息
            for i, info in enumerate(user_info_data.get("accounts", [])):
                if info.get("user", {}).get("id") == user_id:
                    user_info_data["accounts"][i] = user_info
                    break
            else:
                user_info_data.setdefault("accounts", []).append(user_info)
            
            message = "账户已更新"
        else:
            # 添加新账户
            account_summary["created_at"] = datetime.now().isoformat()
            accounts_data["accounts"].append(account_summary)
            user_info_data.setdefault("accounts", []).append(user_info)
            message = "账户添加成功"
        
        # 保存
        save_accounts(accounts_data)
        save_user_info(user_info_data)
    
    return {
        "success": True,
        "message": message,
        "account": account_summary
    }


def add_account_by_credentials(email: str, mercury_password: str, totp_secret: str, 
                                email_password: str, proxy: str = None) -> dict:
    """
    通过邮箱凭证添加新账户（自动登录获取session）
    
    Args:
        email: Mercury 账户邮箱
        mercury_password: Mercury 密码
        totp_secret: 2FA 密钥
        email_password: 邮箱密码
        proxy: 代理地址（可选）
        
    Returns:
        dict: {"success": bool, "message": str, "account": dict}
    """
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'login'))
    from login import mercury_full_login
    
    # 如果传入了代理，先格式化
    if proxy:
        proxy = format_proxy_url(proxy)
    
    # 加载现有账户，检查是否有保存的设备信息
    accounts_data = load_accounts()
    saved_device_id = None
    saved_device_fingerprint = None
    
    for acc in accounts_data["accounts"]:
        if acc.get("email") == email:
            saved_device_id = acc.get("device_id")
            saved_device_fingerprint = acc.get("device_fingerprint")
            break
    
    # 执行登录
    login_result = mercury_full_login(
        email=email,
        mercury_password=mercury_password,
        totp_secret=totp_secret,
        email_password=email_password,
        proxy=proxy,
        saved_device_id=saved_device_id,
        saved_device_fingerprint=saved_device_fingerprint
    )
    
    if not login_result.get("success"):
        return {"success": False, "error": login_result.get("error", "登录失败")}
    
    session_cookie = login_result["session"]
    device_id = login_result.get("device_id")
    device_fingerprint = login_result.get("device_fingerprint")
    
    # 获取用户信息
    success, result, new_session = get_user_info_by_session(session_cookie, proxy=proxy)
    
    if not success:
        return {"success": False, "error": result}
    
    user_info = result
    user_id = user_info["user"]["id"]
    
    # 检查账户是否被限制/封禁
    restriction = user_info.get("restriction", {})
    is_restricted = restriction.get("is_restricted", False)
    restriction_reason = restriction.get("reason", "")
    
    # 使用响应中返回的新 session（如果有）
    final_session = new_session if new_session else session_cookie
    
    with _storage_lock():
        # 加载现有账户
        accounts_data = load_accounts()
        user_info_data = load_user_info()
        
        # 检查是否已存在
        existing_index = None
        existing_proxy = None
        for i, acc in enumerate(accounts_data["accounts"]):
            if acc["user_id"] == user_id:
                existing_index = i
                existing_proxy = acc.get("proxy", "")
                break
        
        # 如果账户被限制且是新添加的账户，拒绝添加
        if is_restricted and existing_index is None:
            print(f"❌ 账户 {email} 被 Mercury 限制，拒绝添加: {restriction_reason}")
            return {"success": False, "error": "账户被 Mercury 限制，拒绝添加"}
        
        # 构建账户摘要信息
        depository_account_id = None
        if user_info.get("depository_accounts") and len(user_info["depository_accounts"]) > 0:
            depository_account_id = user_info["depository_accounts"][0].get("id")
        
        # 根据限制状态设置 account_status
        if is_restricted:
            account_status = "restricted"
            organization_name = user_info["organization"]["name"]
            print(f"⚠️ 账户 {email} 被 Mercury 限制，标记为 restricted: {restriction_reason}")
            # 将同组织下的所有账户都标记为 restricted
            mark_organization_restricted(organization_name, restriction_reason)
            # 重新加载 accounts_data，避免覆盖 mark_organization_restricted 的修改
            accounts_data = load_accounts()
            # 重新查找 existing_index
            for i, acc in enumerate(accounts_data.get("accounts", [])):
                if acc["user_id"] == user_id:
                    existing_index = i
                    break
        else:
            account_status = "active"
        
        account_summary = {
            "user_id": user_id,
            "email": user_info["user"]["email"],
            "name": f"{user_info['user']['first_name']} {user_info['user']['last_name']}",
            "organization": user_info["organization"]["name"],
            "credit_account_id": user_info["credit_account"]["account_id"],
            "depository_account_id": depository_account_id,
            "credit_limit": user_info["credit_account"]["credit_limit"],
            "available_balance": user_info["credit_account"]["available_balance"],
            "status": user_info["user"]["status"],
            "account_status": account_status,
            "restriction_reason": restriction_reason if is_restricted else "",
            "legal_address": user_info["user"].get("legal_address", {}),
            "proxy": proxy if proxy is not None else (existing_proxy or ""),
            "_SESSION": final_session,
            "ajs_user_id": user_id,
            "updated_at": datetime.now().isoformat(),
            # 保存登录凭证和设备信息
            "mercury_password": mercury_password,
            "totp_secret": totp_secret,
            "email_password": email_password,
            "device_id": device_id,
            "device_fingerprint": device_fingerprint,
        }
        
        if existing_index is not None:
            # 更新现有账户
            existing_account = accounts_data["accounts"][existing_index]
            if "created_at" in existing_account:
                account_summary["created_at"] = existing_account["created_at"]
            accounts_data["accounts"][existing_index] = account_summary
            
            # 更新用户详细信息
            for i, info in enumerate(user_info_data.get("accounts", [])):
                if info.get("user", {}).get("id") == user_id:
                    user_info_data["accounts"][i] = user_info
                    break
            else:
                user_info_data.setdefault("accounts", []).append(user_info)
            
            message = "账户已更新"
        else:
            # 添加新账户
            account_summary["created_at"] = datetime.now().isoformat()
            accounts_data["accounts"].append(account_summary)
            user_info_data.setdefault("accounts", []).append(user_info)
            message = "账户添加成功"
        
        # 保存
        save_accounts(accounts_data)
        save_user_info(user_info_data)
    
    return {
        "success": True,
        "message": message,
        "account": account_summary
    }


def update_account_proxy(user_id, proxy):
    """
    更新账户代理设置 (不验证，代理已在代理模块验证过)
    
    Args:
        user_id: 用户 ID
        proxy: 代理地址
        
    Returns:
        dict: {"success": bool, "message": str}
    """
    # 直接保存代理，不再验证（代理已在代理模块验证过）
    return _save_proxy_to_file(user_id, proxy.strip() if proxy else "")

def _save_proxy_to_file(user_id, proxy_str):
    """内部函数：保存代理到文件"""
    with _storage_lock():
        accounts_data = load_accounts()
        found = False
        
        for acc in accounts_data["accounts"]:
            if acc["user_id"] == user_id:
                acc["proxy"] = proxy_str
                acc["updated_at"] = datetime.now().isoformat()
                found = True
                break
        
        if found:
            save_accounts(accounts_data)
            return {"success": True, "message": "代理已验证并保存"}
        else:
            return {"success": False, "error": "账户不存在"}

def delete_account(user_id):
    """
    删除账户
    
    Args:
        user_id: 用户 ID
        
    Returns:
        dict: {"success": bool, "message": str}
    """
    with _storage_lock():
        accounts_data = load_accounts()
        user_info_data = load_user_info()
        
        # 删除账户摘要
        original_count = len(accounts_data["accounts"])
        accounts_data["accounts"] = [acc for acc in accounts_data["accounts"] if acc["user_id"] != user_id]
        
        if len(accounts_data["accounts"]) == original_count:
            return {"success": False, "error": "账户不存在"}
        
        # 如果删除的是当前活跃账户，清除 active_user_id
        if accounts_data.get("active_user_id") == user_id:
            accounts_data["active_user_id"] = None
        
        # 删除用户详细信息
        user_info_data["accounts"] = [
            info for info in user_info_data.get("accounts", [])
            if info.get("user", {}).get("id") != user_id
        ]
        
        # 保存
        save_accounts(accounts_data)
        save_user_info(user_info_data)
    
    return {"success": True, "message": "账户已删除"}


def switch_account(user_id):
    """
    切换活跃账户
    
    Args:
        user_id: 要切换到的用户 ID
        
    Returns:
        dict: {"success": bool, "message": str}
    """
    with _storage_lock():
        accounts_data = load_accounts()
        
        # 查找账户
        target_account = None
        for acc in accounts_data["accounts"]:
            if acc["user_id"] == user_id:
                target_account = acc
                break
        
        if not target_account:
            return {"success": False, "error": "账户不存在"}
        
        # 更新活跃账户
        accounts_data["active_user_id"] = user_id
        save_accounts(accounts_data)
    
    # 更新 cookies.json
    cookies = {
        "_SESSION": target_account["_SESSION"],
        "ajs_user_id": target_account["ajs_user_id"],
        "userLoggedIn": "true",
        "canSeePerks": "true",
    }
    
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cookies, f, indent=4, ensure_ascii=False)
    
    return {
        "success": True,
        "message": f"已切换到账户: {target_account['email']}",
        "account": target_account
    }


def get_all_accounts():
    """
    获取所有账户列表
    
    Returns:
        list: 账户列表（不包含敏感信息）
    """
    accounts_data = load_accounts()
    active_user_id = accounts_data.get("active_user_id")
    
    result = []
    for acc in accounts_data.get("accounts", []):
        # 不返回 _SESSION，只返回显示信息
        result.append({
            "user_id": acc.get("user_id"),
            "email": acc.get("email"),
            "name": acc.get("name"),
            "organization": acc.get("organization"),
            "credit_account_id": acc.get("credit_account_id"),
            "credit_limit": acc.get("credit_limit"),
            "available_balance": acc.get("available_balance"),
            "status": acc.get("status"),
            "account_status": acc.get("account_status", "active"),
            "legal_address": acc.get("legal_address", {}),
            "proxy": acc.get("proxy", ""),
            "is_active": acc.get("user_id") == active_user_id,
            "created_at": acc.get("created_at"),
            "updated_at": acc.get("updated_at"),
        })
    
    return result


def get_active_account():
    """
    获取当前活跃账户
    
    Returns:
        dict or None: 活跃账户信息
    """
    accounts_data = load_accounts()
    active_user_id = accounts_data.get("active_user_id")
    
    if not active_user_id:
        # 如果没有设置活跃账户，使用第一个
        if accounts_data.get("accounts"):
            return accounts_data["accounts"][0]
        return None
    
    for acc in accounts_data.get("accounts", []):
        if acc["user_id"] == active_user_id:
            return acc
    
    return None


def refresh_account(user_id):
    """
    刷新账户信息（重新从 API 获取）
    优先使用 session 刷新，失败后尝试用保存的凭证重新登录
    
    Args:
        user_id: 用户 ID
        
    Returns:
        dict: {"success": bool, "message": str, "account": dict}
    """
    accounts_data = load_accounts()
    
    # 查找账户
    target_account = None
    target_index = None
    for i, acc in enumerate(accounts_data["accounts"]):
        if acc["user_id"] == user_id:
            target_account = acc
            target_index = i
            break
    
    if not target_account:
        return {"success": False, "error": "账户不存在"}
    
    # 1. 首先尝试使用 session 刷新
    result = add_account(target_account["_SESSION"], proxy=target_account.get("proxy", ""))
    
    if result.get("success"):
        return result
    
    # 2. session 刷新失败，检查是否有保存的登录凭证
    email = target_account.get("email")
    mercury_password = target_account.get("mercury_password")
    totp_secret = target_account.get("totp_secret")
    email_password = target_account.get("email_password")
    
    if not all([email, mercury_password, totp_secret, email_password]):
        # 没有保存凭证，标记为 inactive
        with _storage_lock():
            accounts_data = load_accounts()
            for i, acc in enumerate(accounts_data.get("accounts", [])):
                if acc["user_id"] == user_id:
                    accounts_data["accounts"][i]["account_status"] = "inactive"
                    accounts_data["accounts"][i]["updated_at"] = datetime.now().isoformat()
                    break
            save_accounts(accounts_data)
        print(f"❌ 账户 {email or 'unknown'} session 失效，无保存凭证，状态更新为 inactive")
        return result
    
    # 3. 尝试使用凭证重新登录
    print(f"🔄 账户 {email} session 失效，尝试重新登录...")
    
    login_result = add_account_by_credentials(
        email=email,
        mercury_password=mercury_password,
        totp_secret=totp_secret,
        email_password=email_password,
        proxy=target_account.get("proxy", "")
    )
    
    if login_result.get("success"):
        # 检查是否被标记为 restricted
        new_account = get_account_by_user_id(user_id)
        if new_account and new_account.get("account_status") == "restricted":
            print(f"⛔ 账户 {email} 被 Mercury 限制，跳过后续操作")
            return {"success": False, "error": "账户被 Mercury 限制", "restricted": True}
        print(f"✅ 账户 {email} 重新登录成功")
        return login_result
    else:
        # 重新登录也失败，标记为 inactive
        with _storage_lock():
            accounts_data = load_accounts()  # 重新加载
            for i, acc in enumerate(accounts_data["accounts"]):
                if acc["user_id"] == user_id:
                    accounts_data["accounts"][i]["account_status"] = "inactive"
                    accounts_data["accounts"][i]["updated_at"] = datetime.now().isoformat()
                    break
            save_accounts(accounts_data)
        print(f"❌ 账户 {email} 重新登录失败: {login_result.get('error')}")
        return login_result


def has_active_accounts():
    """
    检查是否有活跃的后台账户
    
    Returns:
        bool: 是否有活跃账户
    """
    accounts_data = load_accounts()
    accounts = accounts_data.get("accounts", [])
    active_accounts = [acc for acc in accounts if acc.get("account_status", "active") == "active"]
    return len(active_accounts) > 0


def get_all_active_accounts():
    """
    获取所有活跃的后台账户（随机打乱顺序）
    
    Returns:
        list: 活跃账户列表（已随机打乱）
    """
    accounts_data = load_accounts()
    accounts = accounts_data.get("accounts", [])
    active_accounts = [acc for acc in accounts if acc.get("account_status", "active") == "active"]
    random.shuffle(active_accounts)
    return active_accounts


def get_random_account():
    """
    随机获取一个活跃账户信息
    跳过 inactive 和 paused 状态的账户
    
    Returns:
        dict or None: 账户信息，或 None 如果没有活跃账户
    """
    active_accounts = get_all_active_accounts()
    
    if not active_accounts:
        return None
    
    return active_accounts[0]


def get_account_by_user_id(user_id):
    """
    根据 user_id 获取账户信息
    
    Args:
        user_id: 用户 ID
        
    Returns:
        dict or None: 账户信息
    """
    accounts_data = load_accounts()
    for acc in accounts_data.get("accounts", []):
        if acc["user_id"] == user_id:
            return acc
    return None


def update_account_session(user_id, new_session):
    """
    更新账户的 _SESSION
    
    Args:
        user_id: 用户 ID
        new_session: 新的 _SESSION 值
    """
    with _storage_lock():
        accounts_data = load_accounts()
        for acc in accounts_data.get("accounts", []):
            if acc["user_id"] == user_id:
                acc["_SESSION"] = new_session
                acc["updated_at"] = datetime.now().isoformat()
                break
        save_accounts(accounts_data)


def subscribe_status_updates():
    """
    订阅账户状态更新
    返回一个 queue 用于接收状态更新事件
    """
    q = queue.Queue()
    with _subscribers_lock:
        _status_subscribers.append(q)
    return q


def unsubscribe_status_updates(q):
    """
    取消订阅账户状态更新
    """
    with _subscribers_lock:
        if q in _status_subscribers:
            _status_subscribers.remove(q)


def broadcast_status_update(user_id, status, email=None):
    """
    广播账户状态更新到所有订阅者
    """
    event = {
        "type": "status_update",
        "user_id": user_id,
        "status": status,
        "email": email,
        "updated_at": datetime.now().isoformat()
    }
    _broadcast_event(event)


def broadcast_card_counts(user_id, credit, debit, email=None):
    """
    广播账户卡片数量更新到所有订阅者
    """
    event = {
        "type": "card_counts",
        "user_id": user_id,
        "credit": credit,
        "debit": debit,
        "total": credit + debit,
        "email": email,
        "updated_at": datetime.now().isoformat()
    }
    _broadcast_event(event)


def _broadcast_event(event):
    """
    内部函数：广播事件到所有订阅者
    """
    with _subscribers_lock:
        dead_queues = []
        for q in _status_subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead_queues.append(q)
        # 移除无法写入的队列
        for q in dead_queues:
            _status_subscribers.remove(q)


def update_account_status(user_id, status):
    """
    更新账户状态
    
    Args:
        user_id: 用户 ID
        status: 新状态 ("active", "inactive", "paused")
    """
    email = None
    with _storage_lock():
        accounts_data = load_accounts()
        for acc in accounts_data.get("accounts", []):
            if acc["user_id"] == user_id:
                acc["account_status"] = status
                acc["updated_at"] = datetime.now().isoformat()
                email = acc.get("email")
                break
        save_accounts(accounts_data)
    
    # 广播状态更新
    broadcast_status_update(user_id, status, email)


def mark_organization_restricted(organization_name, restriction_reason="组织被 Mercury 限制"):
    """
    将指定组织下的所有账户标记为 restricted
    
    Mercury 限制账户时通常是整个组织被限制，所以当检测到一个账户被限制时，
    应该将同一组织下的所有账户都标记为 restricted
    
    Args:
        organization_name: 组织名称
        restriction_reason: 限制原因
    
    Returns:
        int: 被标记的账户数量
    """
    marked_count = 0
    with _storage_lock():
        accounts_data = load_accounts()
        for acc in accounts_data.get("accounts", []):
            if acc.get("organization") == organization_name and acc.get("account_status") != "restricted":
                acc["account_status"] = "restricted"
                acc["restriction_reason"] = restriction_reason
                acc["updated_at"] = datetime.now().isoformat()
                marked_count += 1
                # 广播状态更新
                broadcast_status_update(acc["user_id"], "restricted", acc.get("email"))
                print(f"⚠️ 组织 {organization_name} 被限制，账户 {acc.get('email')} 已标记为 restricted")
        if marked_count > 0:
            save_accounts(accounts_data)
    
    return marked_count

def mercury_request(account, method, url, headers=None, json_data=None, timeout=15):
    """
    发送 Mercury API 请求
    
    Args:
        account: 账户信息字典（需要包含 _SESSION，可选 proxy）
        method: 请求方法 ('GET' 或 'POST')
        url: 请求 URL
        headers: 请求头字典
        json_data: JSON 请求体
        timeout: 超时时间
        
    Returns:
        response 或 None 失败时
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from header import default_headers
    
    if headers is None:
        headers = default_headers.copy()
    
    # 添加 Cookie 头
    headers["Cookie"] = f"_SESSION={account['_SESSION']}"
    
    # 设置代理
    proxies = None
    proxy = account.get('proxy', '').strip()
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    
    # 使用 Session 来管理连接，确保连接被正确复用和关闭
    session = None
    try:
        session = requests.Session()
        if method.upper() == 'GET':
            response = session.get(url, headers=headers, timeout=timeout, proxies=proxies, impersonate="chrome")
        else:
            response = session.post(url, headers=headers, json=json_data or {}, timeout=timeout, proxies=proxies, impersonate="chrome")
        
        return response
        
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return None
    finally:
        # 确保 Session 被关闭，释放底层连接
        if session:
            try:
                session.close()
            except:
                pass


def get_account_count():
    """获取账户数量"""
    accounts_data = load_accounts()
    return len(accounts_data.get("accounts", []))


def refresh_all_accounts():
    """
    刷新所有账户的信息和 session
    用于定时任务
    同时获取卡片数量并通过 SSE 推送，如果信用卡+借记卡>=100，则标记账户为暂停状态
    
    优化：按组织分组获取卡片数量，每个组织只请求一次 API
    
    Returns:
        dict: {"success": int, "failed": int, "paused": int, "total": int}
    """
    accounts_data = load_accounts()
    accounts = accounts_data.get("accounts", [])
    
    success_count = 0
    failed_count = 0
    paused_count = 0
    
    # 第一步：刷新所有账户的 session（跳过 restricted 账户）
    refreshed_accounts = []
    skipped_count = 0
    for acc in accounts:
        # 跳过被 Mercury 限制的账户
        if acc.get("account_status") == "restricted":
            skipped_count += 1
            continue
        
        try:
            result = refresh_account(acc["user_id"])
            if result.get("success"):
                success_count += 1
                # 重新加载账户信息（包含更新后的 session）
                refreshed_acc = get_account_by_user_id(acc["user_id"])
                if refreshed_acc:
                    refreshed_accounts.append(refreshed_acc)
            else:
                failed_count += 1
        except Exception as e:
            print(f"❌ 刷新账户 {acc.get('email', 'unknown')} 失败: {e}")
            failed_count += 1
    
    # 第二步：按组织分组账户
    org_groups = {}
    for acc in refreshed_accounts:
        org_name = acc.get("organization", "unknown")
        if org_name not in org_groups:
            org_groups[org_name] = []
        org_groups[org_name].append(acc)
    
    print(f"📊 开始批量获取卡片数量，共 {len(org_groups)} 个组织")
    
    # 第三步：按组织批量获取卡片数量
    for org_name, org_accounts in org_groups.items():
        try:
            # 批量获取该组织的所有卡片数量
            card_results = get_org_card_counts_batch(org_name, org_accounts, minutes_ago=0)
            
            # 处理每个账户的结果
            for acc in org_accounts:
                user_id = acc["user_id"]
                card_info = card_results.get(user_id, {})
                
                if card_info.get("success"):
                    credit_count = card_info.get("credit", 0)
                    debit_count = card_info.get("debit", 0)
                    total_cards = card_info.get("total", 0)
                    
                    # 广播卡片数量更新
                    broadcast_card_counts(user_id, credit_count, debit_count, acc.get("email"))
                    
                    # 检查卡片数量，>=100 则暂停账户，<100 则恢复活跃
                    if total_cards >= 100:
                        update_account_status(user_id, "paused")
                        paused_count += 1
                        print(f"⚠️ 账户 {acc.get('email', 'unknown')} 卡片数量已达 {total_cards}，已暂停")
                    else:
                        # 只有当前状态是 paused 时才恢复为 active
                        if acc.get("account_status") == "paused":
                            update_account_status(user_id, "active")
                            print(f"✅ 账户 {acc.get('email', 'unknown')} 卡片数量 {total_cards}，已恢复活跃")
        except Exception as e:
            print(f"❌ 组织 {org_name} 获取卡片数量失败: {e}")
    
    return {
        "success": success_count,
        "failed": failed_count,
        "paused": paused_count,
        "total": len(accounts)
    }


def list_mercury_cards(account, card_type_filter=None, cardholder_name_filter=None, minutes_ago=0, status_filter=None):
    """
    获取账户的所有 Mercury 卡片
    
    Args:
        account: 账户信息字典（包含 _SESSION）
        card_type_filter: 可选，过滤卡片类型 "credit" 或 "debit"
        cardholder_name_filter: 可选，过滤持卡人姓名
        minutes_ago: 可选，只返回创建时间超过指定分钟数的卡片
        status_filter: 可选，过滤卡片状态（如 "active"）
        
    Returns:
        tuple: (success, cards_list or error_message)
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from header import default_headers
    
    url = "https://backend.mercury.com/cards/list"
    request_headers = default_headers.copy()
    request_headers["X-Frontend-Path"] = "/cards"
    
    response = mercury_request(account, 'POST', url, headers=request_headers, json_data={})
    
    if response is None:
        return False, "请求失败"
    
    if response.status_code != 200:
        return False, f"请求失败: {response.status_code}"
    
    try:
        data = response.json()
        cards = data.get("cards", [])
        
        # 计算时间过滤阈值（使用 UTC 时间）
        time_threshold = None
        if minutes_ago > 0:
            from datetime import timedelta, timezone
            time_threshold = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        
        # 提取卡片信息
        result = []
        for card in cards:
            contents = card.get("contents", {})
            card_id = contents.get("id")
            if card_id:
                card_type = contents.get("cardType", "credit")  # debit or credit
                status = (contents.get("fullStatus", {}).get("tag", "unknown") or "unknown").lower()
                
                # 跳过已取消的卡片
                if status == "cancelled":
                    continue

                # 按状态过滤
                if status_filter and status != status_filter.lower():
                    continue
                
                # 按类型过滤
                if card_type_filter and card_type != card_type_filter:
                    continue
                
                # 按持卡人姓名过滤
                cardholder_name = contents.get("cardholderName", "")
                if cardholder_name_filter and cardholder_name != cardholder_name_filter:
                    continue
                
                # 按时间过滤（使用 createdAtDateStr，UTC 时间比较）
                created_at_str = contents.get("createdAtDateStr", "")
                if time_threshold and created_at_str:
                    try:
                        # 解析 ISO 格式时间（UTC）
                        created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                        # 使用 UTC 时间比较
                        if created_at > time_threshold:
                            # 卡片创建时间在阈值之后，跳过
                            continue
                    except:
                        pass
                
                result.append({
                    "id": card_id,
                    "card_type": card_type,
                    "last4": contents.get("last4Digits", ""),
                    "cardholder_name": contents.get("cardholderName", ""),
                    "status": status,
                    "createdAtDateStr": created_at_str,
                })
        
        return True, result
    except Exception as e:
        return False, f"解析响应失败: {str(e)}"


def get_mercury_card_counts(user_id, minutes_ago=0):
    """
    获取账户的卡片数量统计（按持卡人姓名过滤）
    当 minutes_ago=0 时，同时检查总数>=100时自动暂停账户
    当 minutes_ago>0 时，只统计创建时间超过指定分钟数的卡片（不触发暂停逻辑）
    
    Args:
        user_id: 用户 ID
        minutes_ago: 只统计创建时间超过指定分钟数的卡片
        
    Returns:
        dict: {"success": bool, "credit": int, "debit": int}
    """
    account = get_account_by_user_id(user_id)
    if not account:
        return {"success": False, "error": "账户不存在"}
    
    # 使用账户姓名作为持卡人过滤条件
    cardholder_name = account.get("name", "")
    success, result = list_mercury_cards(
        account,
        cardholder_name_filter=cardholder_name,
        minutes_ago=minutes_ago,
        status_filter="active"
    )
    if not success:
        return {"success": False, "error": result}
    
    credit_count = sum(1 for c in result if c["card_type"] == "credit")
    debit_count = sum(1 for c in result if c["card_type"] == "debit")
    total_cards = credit_count + debit_count
    
    # 只有在不带时间过滤时才进行暂停/恢复逻辑
    if minutes_ago == 0:
        # 广播卡片数量更新
        broadcast_card_counts(user_id, credit_count, debit_count, account.get("email"))
        
        # 检查卡片数量，>=100 则暂停账户，<100 则恢复活跃
        if total_cards >= 100:
            update_account_status(user_id, "paused")
            print(f"⚠️ 账户 {account.get('email', 'unknown')} 卡片数量已达 {total_cards}，已暂停")
        else:
            # 只有当前状态是 paused 时才恢复为 active
            if account.get("account_status") == "paused":
                update_account_status(user_id, "active")
                print(f"✅ 账户 {account.get('email', 'unknown')} 卡片数量 {total_cards}，已恢复活跃")
    
    return {
        "success": True,
        "credit": credit_count,
        "debit": debit_count,
        "total": total_cards
    }


def get_org_card_counts_batch(organization_name, accounts_list, minutes_ago=0):
    """
    批量获取同一组织下所有账户的卡片数量
    只请求一次 API，然后根据持卡人姓名分配给各个账户
    
    Args:
        organization_name: 组织名称
        accounts_list: 该组织下的所有账户列表
        minutes_ago: 只统计创建时间超过指定分钟数的卡片
        
    Returns:
        dict: {user_id: {"success": bool, "credit": int, "debit": int, "total": int}}
    """
    results = {}
    
    # 初始化所有账户的结果为 0
    for acc in accounts_list:
        results[acc["user_id"]] = {
            "success": False,
            "credit": 0,
            "debit": 0,
            "total": 0
        }
    
    # 找到第一个状态为 active 的账户进行请求
    active_account = None
    for acc in accounts_list:
        if acc.get("account_status") == "active" and acc.get("_SESSION"):
            active_account = acc
            break
    
    # 如果没有 active 账户，尝试使用任何有 session 的账户
    if not active_account:
        for acc in accounts_list:
            if acc.get("_SESSION"):
                active_account = acc
                break
    
    if not active_account:
        print(f"⚠️ 组织 {organization_name} 没有可用的账户进行请求")
        return results
    
    # 用该账户请求所有卡片（不过滤持卡人姓名）
    success, cards = list_mercury_cards(
        active_account,
        cardholder_name_filter=None,
        minutes_ago=minutes_ago,
        status_filter="active"
    )
    
    if not success:
        print(f"❌ 组织 {organization_name} 获取卡片失败: {cards}")
        return results
    
    print(f"✅ 组织 {organization_name} 获取到 {len(cards)} 张卡片（使用账户: {active_account.get('email', 'unknown')}）")
    
    # 建立持卡人姓名到账户的映射
    name_to_account = {}
    for acc in accounts_list:
        name = acc.get("name", "")
        if name:
            name_to_account[name] = acc["user_id"]
    
    # 根据持卡人姓名分配卡片
    for card in cards:
        cardholder_name = card.get("cardholder_name", "")
        card_type = card.get("card_type", "credit")
        
        # 查找匹配的账户
        user_id = name_to_account.get(cardholder_name)
        if user_id and user_id in results:
            if card_type == "credit":
                results[user_id]["credit"] += 1
            elif card_type == "debit":
                results[user_id]["debit"] += 1
    
    # 计算总数并标记成功
    for user_id in results:
        results[user_id]["total"] = results[user_id]["credit"] + results[user_id]["debit"]
        results[user_id]["success"] = True
    
    return results


def clear_mercury_cards(user_id, card_type_filter=None):
    """
    清空账户的 Mercury 卡片
    
    Args:
        user_id: 用户 ID
        card_type_filter: 可选，过滤卡片类型 "credit" 或 "debit"
        
    Returns:
        dict: {"success": bool, "message": str, "deleted": int, "failed": int}
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from card.cancel import cancel_card
    
    # 获取账户
    account = get_account_by_user_id(user_id)
    if not account:
        return {"success": False, "error": "账户不存在"}
    
    # 使用账户姓名作为持卡人过滤条件
    cardholder_name = account.get("name", "")
    
    # 获取卡片（按类型和持卡人过滤）
    success, result = list_mercury_cards(
        account,
        card_type_filter=card_type_filter,
        cardholder_name_filter=cardholder_name,
        status_filter="active"
    )
    if not success:
        return {"success": False, "error": result}
    
    cards = result
    type_name = "信用卡" if card_type_filter == "credit" else ("借记卡" if card_type_filter == "debit" else "卡片")
    if not cards:
        return {"success": True, "message": f"没有{type_name}需要删除", "deleted": 0, "failed": 0}
    
    deleted = 0
    failed = 0
    
    for card in cards:
        card_id = card["id"]
        card_type = card["card_type"]
        
        # 跳过已取消的卡片
        if card.get("status") == "cancelled":
            continue
        
        try:
            if cancel_card(card_id, account, card_type=card_type):
                deleted += 1
            else:
                failed += 1
        except Exception as e:
            print(f"❌ 删除卡片 {card_id} 失败: {e}")
            failed += 1
    
    return {
        "success": True,
        "message": f"清空完成: 删除 {deleted} 张，失败 {failed} 张",
        "deleted": deleted,
        "failed": failed,
        "total": len(cards)
    }


# ==========================================
# 主程序示例
# ==========================================
if __name__ == "__main__":
    # 测试获取所有账户
    accounts = get_all_accounts()
    print(f"当前有 {len(accounts)} 个账户")
    for acc in accounts:
        print(f"  - {acc['email']} ({acc['organization']}) {'[活跃]' if acc['is_active'] else ''}")
