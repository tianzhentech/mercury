"""
取消/删除 Mercury 虚拟卡
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from account.accounts import mercury_request
from header import default_headers


def cancel_card(card_id, account, card_type="credit"):
    """
    取消/删除虚拟卡
    
    Args:
        card_id: 卡片 ID
        account: 账户信息字典（包含 _SESSION, user_id, email）
        card_type: 卡片类型，"credit" 或 "debit"
        
    Returns:
        bool: 成功返回 True，失败返回 False
    """
    url = f"https://backend.mercury.com/cards/{card_type}/{card_id}/cancel"
    
    # 复制 headers 并添加特定路径
    headers = default_headers.copy()
    headers["X-Frontend-Path"] = "/cards/:paymentCardId"
    
    print(f"正在取消卡片: {card_id}...")
    
    response = mercury_request(account, 'POST', url, headers=headers, json_data={})
    
    if response is None:
        return False

    if response.status_code == 200:
        response_data = response.json()
        full_status = response_data.get("fullStatus", {})
        tag = full_status.get("tag")
        
        if tag == "cancelled":
            print("=" * 30)
            print(f"✅ 卡片已取消: {card_id}")
            print("=" * 30)
            return True
        else:
            print(f"⚠️ 响应状态异常: {tag}")
            print(json.dumps(response_data, indent=4))
            return False
    else:
        print(f"❌ 请求失败，状态码: {response.status_code}")
        print("错误详情:", response.text)
        return False


# ==========================================
# 主程序示例
# ==========================================
if __name__ == "__main__":
    from account.accounts import get_random_account
    
    # 填入要取消的卡片 ID
    card_id = "f0116a16-d533-11f0-9a36-47d97a821065"
    
    account = get_random_account()
    if account:
        success = cancel_card(card_id, account)
        if success:
            print(f"\n卡片 {card_id} 已成功取消")
        else:
            print(f"\n取消卡片 {card_id} 失败")
    else:
        print("没有可用的账户")
