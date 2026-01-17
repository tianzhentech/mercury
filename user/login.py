"""
用户登录和Token管理模块

Token设计:
- Refresh Token (RT): 有效期3个月, 存储在浏览器中
- Access Token (AT): 有效期15分钟, 用于API请求认证
"""

import json
import os
import time
import hashlib
import secrets
import hmac
import base64
from typing import Optional

# 配置
_current_dir = os.path.dirname(os.path.abspath(__file__))
USER_FILE = os.path.join(_current_dir, "user.json")

# Token有效期（秒）
RT_EXPIRY = 90 * 24 * 60 * 60  # 3个月
AT_EXPIRY = 15 * 60  # 15分钟

# 密钥（生产环境应该从环境变量读取）
SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "mercury-secret-key-change-in-production")


def load_users():
    """加载用户数据"""
    if not os.path.exists(USER_FILE):
        return {"users": []}
    
    try:
        with open(USER_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if content:
                return json.loads(content)
            return {"users": []}
    except (json.JSONDecodeError, IOError):
        return {"users": []}


def save_users(data):
    """保存用户数据"""
    with open(USER_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def hash_password(password: str) -> str:
    """对密码进行SHA256哈希"""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码"""
    return hash_password(password) == password_hash


def get_user(username: str) -> Optional[dict]:
    """根据用户名获取用户"""
    data = load_users()
    for user in data.get("users", []):
        if user.get("username") == username:
            return user
    return None


def generate_token(payload: dict, expiry: int) -> str:
    """
    生成JWT风格的token
    格式: base64(header).base64(payload).signature
    """
    header = {"alg": "HS256", "typ": "JWT"}
    
    # 添加过期时间
    payload = payload.copy()
    payload["exp"] = int(time.time()) + expiry
    payload["iat"] = int(time.time())
    
    # 编码
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip('=')
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    
    # 签名
    message = f"{header_b64}.{payload_b64}"
    signature = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
    
    return f"{header_b64}.{payload_b64}.{signature}"


def verify_token(token: str) -> Optional[dict]:
    """
    验证token并返回payload
    返回None表示token无效或已过期
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        
        header_b64, payload_b64, signature = parts
        
        # 验证签名
        message = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(signature, expected_sig):
            return None
        
        # 解码payload
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding
        
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        
        # 检查过期时间
        if payload.get("exp", 0) < time.time():
            return None
        
        return payload
    
    except Exception as e:
        print(f"Token验证失败: {e}")
        return None


def create_refresh_token(username: str) -> str:
    """创建Refresh Token"""
    payload = {
        "username": username,
        "type": "refresh",
        "jti": secrets.token_hex(16)
    }
    return generate_token(payload, RT_EXPIRY)


def create_access_token(username: str) -> str:
    """创建Access Token"""
    user = get_user(username)
    payload = {
        "username": username,
        "type": "access",
        "is_admin": user.get("is_admin", False) if user else False
    }
    return generate_token(payload, AT_EXPIRY)


def login(username: str, password: str) -> dict:
    """
    用户登录
    返回: {"success": True/False, "refresh_token": ..., "access_token": ..., "error": ...}
    """
    user = get_user(username)
    
    if not user:
        return {"success": False, "error": "用户名或密码错误"}
    
    if not verify_password(password, user.get("password_hash", "")):
        return {"success": False, "error": "用户名或密码错误"}
    
    # 生成tokens
    refresh_token = create_refresh_token(username)
    access_token = create_access_token(username)
    
    return {
        "success": True,
        "refresh_token": refresh_token,
        "access_token": access_token,
        "expires_in": AT_EXPIRY,
        "rt_expires_in": RT_EXPIRY,
        "is_admin": user.get("is_admin", False),
        "username": username
    }


def refresh_access_token(refresh_token: str) -> dict:
    """
    使用Refresh Token刷新Access Token
    返回: {"success": True/False, "access_token": ..., "error": ...}
    """
    payload = verify_token(refresh_token)
    
    if not payload:
        return {"success": False, "error": "Refresh Token无效或已过期"}
    
    if payload.get("type") != "refresh":
        return {"success": False, "error": "无效的Token类型"}
    
    username = payload.get("username")
    if not username:
        return {"success": False, "error": "Token数据无效"}
    
    # 检查用户是否仍然存在
    user = get_user(username)
    if not user:
        return {"success": False, "error": "用户不存在"}
    
    # 生成新的Access Token
    access_token = create_access_token(username)
    
    return {
        "success": True,
        "access_token": access_token,
        "expires_in": AT_EXPIRY,
        "is_admin": user.get("is_admin", False)
    }


def verify_access_token(token: str) -> Optional[dict]:
    """
    验证Access Token
    返回用户信息或None
    """
    payload = verify_token(token)
    
    if not payload:
        return None
    
    if payload.get("type") != "access":
        return None
    
    username = payload.get("username")
    if not username:
        return None
    
    return {
        "username": username,
        "is_admin": payload.get("is_admin", False)
    }


def get_token_remaining_time(token: str) -> int:
    """获取token剩余有效时间（秒）"""
    payload = verify_token(token)
    if not payload:
        return 0
    
    exp = payload.get("exp", 0)
    remaining = int(exp - time.time())
    return max(0, remaining)

