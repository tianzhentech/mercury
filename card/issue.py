"""
创建 Mercury 虚拟卡
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from account.accounts import mercury_request
from header import default_headers


def issue_card(account, transaction_limit=1000, card_type="credit"):
    """
    创建虚拟卡并返回卡ID
    
    Args:
        account: 账户信息字典（包含 _SESSION, user_id, credit_account_id, email）
        transaction_limit: 每日交易限额，默认 1000
        card_type: 卡片类型，"credit" 或 "debit"
        
    Returns:
        str: 成功时返回卡 ID，失败时返回 None
    """
    if card_type == "debit":
        return issue_debit_card(account, transaction_limit)
    
    url = "https://backend.mercury.com/cards/credit/issue"
    
    # 复制 headers 并添加特定路径
    headers = default_headers.copy()
    headers["X-Frontend-Path"] = "/issue-card"
    
    payload = {
        "mercuryCreditAccountId": account["credit_account_id"],
        "userId": account["user_id"],
        "creditCardCreationDetails": {
            "realm": "virtual"
        },
        "creditCardLimit": {
            "tag": "daily",
            "contents": {
                "transactionLimit": transaction_limit
            }
        },
        "categoryLocks": []
    }
    
    print("正在创建信用卡...")
    
    response = mercury_request(account, 'POST', url, headers=headers, json_data=payload)
    
    if response is None:
        return None, "网络请求失败"

    if response.status_code == 200:
        response_data = response.json()
        card_id = response_data.get("id")
        
        if card_id:
            print("=" * 30)
            print(f"成功获取 Card ID: {card_id}")
            print("=" * 30)
            return card_id, None
        else:
            print("未在响应中找到 ID 字段，完整响应如下：")
            print(json.dumps(response_data, indent=4))
            return None, "未找到卡片ID"
    else:
        print(f"请求失败，状态码: {response.status_code}")
        print("错误详情:", response.text)
        
        error_msg = f"请求失败 ({response.status_code})"
        if response.status_code == 429:
            error_msg = "当前服务器繁忙，请五分钟后再试"
        elif response.status_code == 401:
            error_msg = "账户授权过期，请重新登录"
            
        try:
            err_json = response.json()
            if "errors" in err_json and "rate-limited" in err_json["errors"]:
                error_msg = "当前服务器繁忙，请五分钟后再试"
        except:
            pass
            
        return None, error_msg


def issue_debit_card(account, transaction_limit=1000):
    """
    创建借记卡并返回卡ID
    
    Args:
        account: 账户信息字典（包含 _SESSION, user_id, depository_account_id, email）
        transaction_limit: 每日交易限额，默认 1000
        
    Returns:
        str: 成功时返回卡 ID，失败时返回 None
    """
    url = "https://backend.mercury.com/cards/debit/issue"
    
    # 复制 headers 并添加特定路径
    headers = default_headers.copy()
    headers["X-Frontend-Path"] = "/issue-card"
    
    # 借记卡需要 depository_account_id
    depository_account_id = account.get("depository_account_id")
    depository_account_id = account.get("depository_account_id")
    if not depository_account_id:
        print("❌ 账户没有存款账户ID，无法创建借记卡")
        return None, "账户没有存款账户ID"
    
    payload = {
        "cardRealm": "virtual",
        "cardLimits": {
            "tag": "daily",
            "contents": {
                "transactionLimit": transaction_limit
            }
        },
        "accountId": depository_account_id,
        "userId": account["user_id"],
        "issuingAddress": {
            "tag": "useOrgAddress"
        },
        "categoryLocks": []
    }
    
    print("正在创建借记卡...")
    
    response = mercury_request(account, 'POST', url, headers=headers, json_data=payload)
    
    if response is None:
        return None, "网络请求失败"

    if response.status_code == 200:
        response_data = response.json()
        card_id = response_data.get("debitCardId")
        
        if card_id:
            print("=" * 30)
            print(f"成功获取借记卡 ID: {card_id}")
            print("=" * 30)
            return card_id, None
        else:
            print("未在响应中找到 debitCardId 字段，完整响应如下：")
            print(json.dumps(response_data, indent=4))
            return None, "未找到借记卡ID"
    else:
        print(f"请求失败，状态码: {response.status_code}")
        print("错误详情:", response.text)
        
        error_msg = f"请求失败 ({response.status_code})"
        if response.status_code == 429:
            error_msg = "当前服务器繁忙，请五分钟后再试"
            
        return None, error_msg


# ==========================================
# 主程序示例
# ==========================================
if __name__ == "__main__":
    from account.accounts import get_random_account
    
    account = get_random_account()
    if account:
        card_id, error = issue_card(account)
        if card_id:
            print(f"\n创建的卡 ID: {card_id}")
        else:
            print(f"\n创建失败: {error}")
    else:
        print("没有可用的账户")
