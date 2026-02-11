"""
邮件模块 - 从邮箱获取设备验证链接
"""

import re
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote


BASE_URL = "https://ms.lqqq.cc"


def delete_all_emails(email: str, password: str, timeout: int = 30) -> dict:
    """
    删除邮箱中的所有邮件
    
    Args:
        email: 邮箱地址
        password: 邮箱密码
        timeout: 请求超时时间（秒）
        
    Returns:
        dict: {"success": True} 或 {"success": False, "error": "..."}
    """
    try:
        # 使用完整邮箱地址
        delete_url = f"{BASE_URL}/delete/{email}----{password}?all=true"
        response = requests.get(delete_url, timeout=timeout, impersonate="chrome")
        
        if response.status_code == 200:
            print(f"  [邮箱] 已清空邮箱 {email}")
            return {"success": True}
        else:
            print(f"  [邮箱] 清空邮箱失败: HTTP {response.status_code}")
            return {"success": False, "error": f"删除邮件失败: HTTP {response.status_code}"}
            
    except Exception as e:
        print(f"  [邮箱] 清空邮箱异常: {e}")
        return {"success": False, "error": f"删除邮件异常: {e}"}


def get_device_verification_link(email: str, password: str, timeout: int = 120) -> dict:
    """
    从邮箱获取最新的设备验证链接
    
    Args:
        email: 邮箱地址
        password: 邮箱密码
        timeout: 请求超时时间（秒）
        
    Returns:
        dict: {"success": True, "link": "https://..."} 或 {"success": False, "error": "..."}
    """
    try:
        # 1. 获取邮件列表
        list_url = f"{BASE_URL}/web/{email}----{password}"
        response = requests.get(list_url, timeout=timeout, impersonate="chrome")
        
        if response.status_code != 200:
            return {"success": False, "error": f"获取邮件列表失败: HTTP {response.status_code}"}
        
        # 2. 解析邮件列表，找到最新的 "New device detected" 邮件
        soup = BeautifulSoup(response.text, "html.parser")
        email_cards = soup.find_all("div", class_="email-card")
        
        device_email_link = None
        for card in email_cards:
            subject_div = card.find("div", class_="email-subject")
            if subject_div and "New device detected" in subject_div.text:
                # 找到查看链接
                view_link = card.find("a", class_="btn-view")
                if view_link and view_link.get("href"):
                    device_email_link = view_link["href"]
                    break  # 取最新的一封
        
        if not device_email_link:
            return {"success": False, "error": "未找到设备验证邮件"}
        
        # 3. 获取邮件内容
        email_url = urljoin(BASE_URL, device_email_link)
        email_response = requests.get(email_url, timeout=timeout, impersonate="chrome")
        
        if email_response.status_code != 200:
            return {"success": False, "error": f"获取邮件内容失败: HTTP {email_response.status_code}"}
        
        # 4. 从邮件内容中提取验证链接
        # 链接格式: https://app.mercury.com/verify-device?code=xxx
        verify_pattern = r'https://app\.mercury\.com/verify-device\?code=[A-Za-z0-9%+/=]+'
        matches = re.findall(verify_pattern, email_response.text)
        
        if not matches:
            # 尝试另一种方式：从 href 属性中查找
            email_soup = BeautifulSoup(email_response.text, "html.parser")
            for link in email_soup.find_all("a", href=True):
                href = link["href"]
                if "verify-device" in href and "code=" in href:
                    return {"success": True, "link": href}
            
            return {"success": False, "error": "未找到验证链接"}
        
        # 返回找到的链接
        return {"success": True, "link": matches[0]}
        
    except requests.exceptions.Timeout:
        return {"success": False, "error": "请求超时"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"请求失败: {e}"}
    except Exception as e:
        return {"success": False, "error": f"解析失败: {e}"}


def extract_verification_code(link: str) -> str:
    """
    从验证链接中提取 code 参数
    
    Args:
        link: 验证链接
        
    Returns:
        str: code 值（URL解码后）
    """
    match = re.search(r'code=([A-Za-z0-9%+/=]+)', link)
    if match:
        return unquote(match.group(1))
    return None


if __name__ == "__main__":
    # 测试
    result = get_device_verification_link(
        email="lgqfbyv93@outlook.com",
        password="sphndu992"
    )
    print(result)
    
    if result.get("success"):
        code = extract_verification_code(result["link"])
        print(f"Verification code: {code}")
