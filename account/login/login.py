"""
Mercury 账户登录模块 - 完整登录流程（包含设备验证）
"""
from curl_cffi import requests
import pyotp
import uuid
import time
import hashlib
from urllib.parse import unquote

from mail import get_device_verification_link, extract_verification_code, delete_all_emails


def generate_device_fingerprint():
    """生成随机设备指纹"""
    seed = f"{uuid.uuid4().hex}{time.time()}"
    return hashlib.md5(seed.encode()).hexdigest()


def generate_device_id():
    """生成设备ID"""
    timestamp = int(time.time() * 1000)
    random_str = uuid.uuid4().hex[:6]
    return f"{timestamp}.{random_str}"


def get_common_headers(device_id: str, device_fingerprint: str) -> dict:
    """获取通用请求头"""
    return {
        "accept": "application/json; charset=utf-8",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://app.mercury.com",
        "referer": "https://app.mercury.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "x-csrf-protect": "1",
        "x-device-fingerprint": device_fingerprint,
        "x-frontend-path": "/login",
        "x-timezone-iana": "America/New_York",
        "x-timezone-offset": "-5:00",
        "mercury-device-id": device_id,
        "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


def do_login_request(email: str, password: str, totp_secret: str, device_id: str, 
                      device_fingerprint: str, cookies: dict = None, proxy: str = None) -> requests.Response:
    """执行登录请求"""
    url = "https://backend.mercury.com/login-v2"
    
    print(f"  [DEBUG] 生成 TOTP 验证码...")
    try:
        totp = pyotp.TOTP(totp_secret)
        totp_code = int(totp.now())
        print(f"  [DEBUG] TOTP 验证码已生成")
    except Exception as e:
        print(f"  [ERROR] TOTP 生成失败: {e}")
        raise
    
    headers = get_common_headers(device_id, device_fingerprint)
    
    payload = {
        "referrer": "https://mercury.com/",
        "email": email,
        "password": password,
        "totp": totp_code,
        "rememberDeviceForMFA": False
    }
    
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
        print(f"  [DEBUG] 使用代理: {proxy[:30]}...")
    else:
        print(f"  [DEBUG] 不使用代理")
    
    print(f"  [DEBUG] 发送登录请求到 {url} (使用 curl_cffi)...")
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            cookies=cookies,
            proxies=proxies,
            timeout=30,
            impersonate="chrome"
        )
        print(f"  [DEBUG] 收到响应: HTTP {response.status_code}")
        return response
    except Exception as e:
        print(f"  [ERROR] 请求异常: {type(e).__name__}: {e}")
        raise


def verify_device(code: str, session_cookie: str, device_id: str, 
                  device_fingerprint: str, proxy: str = None) -> requests.Response:
    """验证设备"""
    url = "https://backend.mercury.com/verify-device"
    
    headers = get_common_headers(device_id, device_fingerprint)
    headers["x-frontend-path"] = "/verify-device"
    
    cookies = {"_SESSION": session_cookie}
    
    payload = {"code": code}
    
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    
    return requests.post(
        url,
        headers=headers,
        json=payload,
        cookies=cookies,
        proxies=proxies,
        timeout=30,
        impersonate="chrome"
    )


def extract_session_from_response(response) -> str:
    """从响应中提取 session cookie"""
    # curl_cffi 的 cookies 处理
    if hasattr(response, 'cookies'):
        session = response.cookies.get("_SESSION")
        if session:
            return session
    
    # 尝试从 headers 提取
    set_cookie = response.headers.get("set-cookie", "")
    if "_SESSION=" in set_cookie:
        for part in set_cookie.split(","):
            if "_SESSION=" in part:
                session_part = part.strip().split(";")[0]
                if session_part.startswith("_SESSION="):
                    return session_part.replace("_SESSION=", "")
    
    return None


def mercury_full_login(email: str, mercury_password: str, totp_secret: str, 
                       email_password: str, proxy: str = None, 
                       saved_device_id: str = None, saved_device_fingerprint: str = None) -> dict:
    """
    完整的 Mercury 登录流程（包含设备验证）
    
    Args:
        email: Mercury 账户邮箱
        mercury_password: Mercury 密码
        totp_secret: 2FA 密钥
        email_password: 邮箱密码（用于获取验证链接）
        proxy: 代理 URL（可选）
        saved_device_id: 已保存的设备ID（可选，用于跳过设备验证）
        saved_device_fingerprint: 已保存的设备指纹（可选）
        
    Returns:
        dict: {"success": True, "session": "...", "device_id": "...", "device_fingerprint": "..."} 
              或 {"success": False, "error": "..."}
    """
    # 使用已保存的设备标识或生成新的
    device_id = saved_device_id or generate_device_id()
    device_fingerprint = saved_device_fingerprint or generate_device_fingerprint()
    
    print(f"[1/4] 第一次登录请求...")
    print(f"  [DEBUG] 邮箱: {email}")
    print(f"  [DEBUG] 代理: {proxy if proxy else '无'}")
    print(f"  [DEBUG] 设备ID: {device_id}")
    
    try:
        # 第一次登录 - 触发设备验证
        response1 = do_login_request(
            email, mercury_password, totp_secret, 
            device_id, device_fingerprint, proxy=proxy
        )
        
        if response1.status_code != 200:
            print(f"  [DEBUG] 响应内容: {response1.text[:500]}")
            # 检查是否是 TOTP 验证码重复使用
            if response1.status_code == 403 and "already used this code" in response1.text:
                return {"success": False, "error": "操作太频繁，请30秒后重试"}
            return {"success": False, "error": f"第一次登录失败: HTTP {response1.status_code}"}
        
        # 检查响应
        response_data = response1.json()
        
        # 如果直接登录成功（设备已验证）
        if response_data.get("kind") != "deviceVerificationRequired":
            session = extract_session_from_response(response1)
            if session:
                print("[完成] 设备已验证，直接登录成功")
                return {
                    "success": True, 
                    "session": session,
                    "device_id": device_id,
                    "device_fingerprint": device_fingerprint
                }
        
        # 需要设备验证
        print(f"[2/4] 需要设备验证，清空邮箱后获取验证邮件...")
        
        # 获取第一次登录的 session（用于后续请求）
        temp_session = extract_session_from_response(response1)
        if not temp_session:
            return {"success": False, "error": "未获取到临时 session"}
        
        # 先清空邮箱中的旧邮件
        delete_all_emails(email, email_password)
        
        # 等待邮件发送
        time.sleep(3)
        
        # 从邮箱获取验证链接
        mail_result = get_device_verification_link(email, email_password)
        if not mail_result.get("success"):
            return {"success": False, "error": f"获取验证邮件失败: {mail_result.get('error')}"}
        
        verification_link = mail_result["link"]
        verification_code = extract_verification_code(verification_link)
        
        if not verification_code:
            return {"success": False, "error": "无法提取验证码"}
        
        print(f"[3/4] 验证设备...")
        
        # 验证设备
        verify_response = verify_device(
            verification_code, temp_session, device_id, device_fingerprint, proxy
        )
        
        if verify_response.status_code != 200:
            return {"success": False, "error": f"设备验证失败: HTTP {verify_response.status_code}"}
        
        print(f"[4/4] 第二次登录请求...")
        
        # 等待一下再登录
        time.sleep(2)
        
        # 第二次登录 - 设备已验证
        response2 = do_login_request(
            email, mercury_password, totp_secret,
            device_id, device_fingerprint, proxy=proxy
        )
        
        if response2.status_code != 200:
            return {"success": False, "error": f"第二次登录失败: HTTP {response2.status_code}"}
        
        # 提取最终 session
        final_session = extract_session_from_response(response2)
        if final_session:
            print("[完成] 登录成功！")
            return {
                "success": True, 
                "session": final_session,
                "device_id": device_id,
                "device_fingerprint": device_fingerprint
            }
        else:
            return {"success": False, "error": "未获取到最终 session"}
            
    except Exception as e:
        import traceback
        print(f"  [ERROR] 登录流程异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {"success": False, "error": f"登录流程失败: {type(e).__name__}: {e}"}


def mercury_login(email: str, password: str, totp_secret: str, proxy: str = None) -> dict:
    """
    登录 Mercury 账户
    
    Args:
        email: 邮箱
        password: 密码
        totp_secret: 2FA 密钥（用于生成 TOTP 验证码）
        proxy: 代理 URL（可选）
        
    Returns:
        dict: {"success": True, "session": "..."} 或 {"success": False, "error": "..."}
    """
    url = "https://backend.mercury.com/login-v2"
    
    # 生成 TOTP 验证码
    try:
        totp = pyotp.TOTP(totp_secret)
        totp_code = int(totp.now())
    except Exception as e:
        return {"success": False, "error": f"TOTP 生成失败: {e}"}
    
    # 请求头
    headers = {
        "accept": "application/json; charset=utf-8",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://app.mercury.com",
        "referer": "https://app.mercury.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "x-csrf-protect": "1",
        "x-device-fingerprint": generate_device_fingerprint(),
        "x-frontend-path": "/login",
        "x-timezone-iana": "America/New_York",
        "x-timezone-offset": "-5:00",
        "mercury-device-id": generate_device_id(),
        "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    
    # 请求体
    payload = {
        "referrer": "https://mercury.com/",
        "email": email,
        "password": password,
        "totp": totp_code,
        "rememberDeviceForMFA": False
    }
    
    # 代理设置
    proxies = None
    if proxy:
        proxies = {
            "http": proxy,
            "https": proxy
        }
    
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=proxies,
            timeout=30
        )
        
        if response.status_code == 200:
            # 从原始 Set-Cookie 头中提取 _SESSION
            session_cookie = None
            
            # 获取所有 Set-Cookie 头
            set_cookie_headers = response.headers.get("Set-Cookie", "")
            
            # 也可能在 response.raw.headers 中有多个
            if hasattr(response.raw, '_original_response'):
                raw_headers = response.raw._original_response.headers.get_all('Set-Cookie') or []
                for header in raw_headers:
                    if header.startswith("_SESSION="):
                        # 提取 _SESSION 的值（到第一个分号）
                        session_part = header.split(";")[0]
                        session_cookie = session_part.replace("_SESSION=", "")
                        break
            
            # 备用方法：从 response.cookies 获取
            if not session_cookie:
                for cookie in response.cookies:
                    if cookie.name == "_SESSION":
                        session_cookie = cookie.value
                        break
            
            if session_cookie:
                return {"success": True, "session": session_cookie}
            else:
                return {"success": False, "error": "未找到 _SESSION cookie"}
        else:
            error_msg = f"登录失败，状态码: {response.status_code}"
            try:
                error_data = response.json()
                if "errors" in error_data:
                    error_msg = str(error_data["errors"])
            except:
                pass
            return {"success": False, "error": error_msg}
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "请求超时"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"请求失败: {e}"}


if __name__ == "__main__":
    # 测试完整登录流程
    result = mercury_full_login(
        email="lgqfbyv93@outlook.com",
        mercury_password="sphndu992.",
        totp_secret="I6IMLYR5ZWVSRCYRJLEL5RRMIJMTQ6CH",
        email_password="sphndu992"
    )
    print(result)
    
    if result.get("success"):
        print(f"Session 长度: {len(result['session'])}")
