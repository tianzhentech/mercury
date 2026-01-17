"""
获取 Mercury 虚拟卡详情（卡号、CVV、有效期）
"""

import sys
import os
import json
from curl_cffi import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from account.accounts import mercury_request
from header import default_headers


def reveal_card_details(card_id, account, card_type="credit"):
    """
    两步法获取卡片敏感信息：
    1. 请求 Mercury 获取 Lithic 的预签名 URL
    2. 请求 Lithic 获取真实卡号数据
    """
    
    # ==============================
    # 步骤 1: 请求 Mercury 获取预签名 URL (此处自动使用代理)
    # ==============================
    mercury_url = f"https://backend.mercury.com/cards/{card_type}/{card_id}/embed-reveal"
    
    headers = default_headers.copy()
    headers["X-Frontend-Path"] = f"/cards/{card_id}"
    
    print(f"[1/2] 正在请求 Mercury 获取授权链接: {card_id}...")
    
    # 这里调用了 mercury_request，所以已经走了代理
    response = mercury_request(account, 'POST', mercury_url, headers=headers, json_data={})
    
    if response is None:
        print("❌ Mercury 请求失败")
        return None
    
    if response.status_code != 200:
        print(f"❌ Mercury 请求失败: {response.status_code}")
        print(response.text)
        return None

    mercury_data = response.json()
    presigned_url = mercury_data.get("presigned_url")
    
    if not presigned_url:
        print("❌ 未能在响应中找到 'presigned_url'")
        return None
        
    print(f"✅ 成功获取预签名 URL")

    # ==============================
    # 步骤 2: 请求 Lithic API 解密数据 (必须手动添加代理！)
    # ==============================
    print(f"[2/2] 正在请求 Lithic 获取卡号详情...")
    
    lithic_headers = {
        "User-Agent": default_headers.get("User-Agent", "Mozilla/5.0"),
        "Accept": "application/json",
        "Origin": "https://app.mercury.com",
        "Referer": "https://app.mercury.com/"
    }

    # === [关键修改] 构建代理配置 ===
    proxies = None
    if account.get("proxy"):
        # 确保格式正确 (如果是 socks5，最好确保用了 socks5h 以防 DNS 泄露)
        proxy_url = account["proxy"]
        # 如果是 account.py 里保存的格式，直接用即可
        proxies = {
            "http": proxy_url,
            "https": proxy_url
        }
    # ============================

    try:
        # Lithic 请求使用 curl_cffi
        resp_lithic = requests.get(
            presigned_url, 
            headers=lithic_headers,
            timeout=15,
            proxies=proxies,
            impersonate="chrome"
        )

        if resp_lithic.status_code == 200:
            card_details = resp_lithic.json()
            print("✅ 卡片详情获取成功！")
            return card_details
        else:
            print(f"❌ Lithic 请求失败: {resp_lithic.status_code}")
            print(resp_lithic.text)
            return None

    except Exception as e:
        print(f"❌ 步骤 2 发生异常: {e}")
        return None


# ==========================================
# 主程序示例
# ==========================================
if __name__ == "__main__":
    from account.accounts import get_random_account
    
    # 填入你要查询的 Card ID
    target_card_id = "fa796222-d512-11f0-8ee3-075cf280f772"
    
    account = get_random_account()
    if account:
        result = reveal_card_details(target_card_id, account)
        if result:
            print("-" * 30)
            print("最终卡片数据:")
            print(json.dumps(result, indent=4))
            print("-" * 30)
    else:
        print("没有可用的账户")
