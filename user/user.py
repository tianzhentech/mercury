"""
用户管理模块 - 增删改查
"""

import time
from typing import Optional
from .login import load_users, save_users, hash_password, get_user


def create_user(username: str, password: str, is_admin: bool = False) -> dict:
    """创建新用户"""
    data = load_users()
    
    # 检查用户名是否已存在
    for user in data.get("users", []):
        if user.get("username") == username:
            return {"success": False, "error": "用户名已存在"}
    
    new_user = {
        "username": username,
        "password_hash": hash_password(password),
        "is_admin": is_admin,
        "created_at": int(time.time())
    }
    
    data["users"].append(new_user)
    save_users(data)
    return {"success": True}


def delete_user(username: str) -> dict:
    """删除用户"""
    data = load_users()
    users = data.get("users", [])
    
    # 查找目标用户
    target_user = None
    for u in users:
        if u.get("username") == username:
            target_user = u
            break
    
    if not target_user:
        return {"success": False, "error": "用户不存在"}
    
    # 不能删除最后一个管理员
    admin_count = sum(1 for u in users if u.get("is_admin", False))
    if target_user.get("is_admin", False) and admin_count <= 1:
        return {"success": False, "error": "不能删除最后一个管理员"}
    
    # 删除用户
    data["users"] = [u for u in users if u.get("username") != username]
    save_users(data)
    return {"success": True}


def update_user(username: str, new_password: str = None, new_is_admin: bool = None) -> dict:
    """更新用户信息"""
    data = load_users()
    users = data.get("users", [])
    
    target_index = None
    for i, u in enumerate(users):
        if u.get("username") == username:
            target_index = i
            break
    
    if target_index is None:
        return {"success": False, "error": "用户不存在"}
    
    # 更新密码
    if new_password:
        users[target_index]["password_hash"] = hash_password(new_password)
    
    # 更新管理员状态
    if new_is_admin is not None:
        # 检查是否会导致没有管理员
        if not new_is_admin and users[target_index].get("is_admin", False):
            admin_count = sum(1 for u in users if u.get("is_admin", False))
            if admin_count <= 1:
                return {"success": False, "error": "不能取消最后一个管理员的权限"}
        users[target_index]["is_admin"] = new_is_admin
    
    save_users(data)
    return {"success": True}


def get_all_users() -> list:
    """获取所有用户列表（不包含密码哈希）"""
    data = load_users()
    users = []
    for user in data.get("users", []):
        users.append({
            "username": user.get("username"),
            "is_admin": user.get("is_admin", False),
            "created_at": user.get("created_at")
        })
    return users


def is_admin(username: str) -> bool:
    """检查用户是否是管理员"""
    user = get_user(username)
    if not user:
        return False
    return user.get("is_admin", False)


def init_default_admin():
    """初始化默认管理员账户"""
    data = load_users()
    
    # 如果没有任何用户，创建默认管理员
    if not data.get("users"):
        data["users"] = [{
            "username": "admin",
            "password_hash": hash_password("admin"),
            "is_admin": True,
            "created_at": int(time.time())
        }]
        save_users(data)
        print("[用户系统] 已创建默认管理员: admin / admin")
        return True
    return False

