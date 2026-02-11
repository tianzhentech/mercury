"""
Mercury 消费记录查询模块
支持通过卡密ID或Mercury卡片ID查询消费记录
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from account.accounts import mercury_request, get_account_by_user_id, load_user_info, load_accounts
from header import default_headers


def get_organization_id_by_account_user_id(account_user_id):
    """
    根据账户 user_id 获取组织 ID
    
    Args:
        account_user_id: Mercury 账户用户 ID
        
    Returns:
        str: 组织 ID，失败返回 None
    """
    user_info_data = load_user_info()
    for acc in user_info_data.get("accounts", []):
        if acc.get("user", {}).get("id") == account_user_id:
            return acc.get("organization", {}).get("id")
    return None


def get_card_info_by_card_key(card_key_id):
    """
    根据卡密 ID 获取对应的 Mercury 卡片信息
    
    Args:
        card_key_id: 卡密 ID（如 5236xxxx-xxxx-xxxx）
        
    Returns:
        tuple: (mercury_card_id, account_user_id, card_type) 或 (None, None, None)
    """
    # 使用绝对路径导入
    import importlib.util
    id_module_path = os.path.join(os.path.dirname(__file__), '..', 'id', 'id.py')
    spec = importlib.util.spec_from_file_location("id_module", id_module_path)
    id_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(id_module)
    query_redeemed = id_module.query_redeemed
    
    success, result = query_redeemed(card_key_id)
    if not success:
        return None, None, None
    
    card = result.get("card", {})
    mercury_card_id = card.get("card_id")
    account_user_id = card.get("account_user_id")
    card_type = card.get("card_type", "credit")
    
    return mercury_card_id, account_user_id, card_type


def query_transactions(account, organization_id, limit=100):
    """
    查询组织的交易记录
    
    Args:
        account: 账户信息字典
        organization_id: 组织 ID
        limit: 返回记录数量限制
        
    Returns:
        list: 交易记录列表，失败返回空列表
    """
    url = f"https://backend.mercury.com/organizations/{organization_id}/transactions-lite"
    
    headers = default_headers.copy()
    headers["X-Frontend-Path"] = "/transactions"
    
    payload = {
        "limit": limit,
        "cursorDirection": "startAfter",
        "sortSettings": {
            "primary": {
                "tag": "date",
                "contents": "desc"
            }
        },
        "timezone": "UTC"
    }
    
    response = mercury_request(account, 'POST', url, headers=headers, json_data=payload)
    
    if response is None:
        return []
    
    if response.status_code != 200:
        print(f"❌ 查询交易记录失败: {response.status_code}")
        return []
    
    try:
        data = response.json()
        return data.get("data", {}).get("transactions", [])
    except Exception as e:
        print(f"❌ 解析交易记录失败: {e}")
        return []


def filter_transactions_by_card_id(transactions, mercury_card_id):
    """
    根据 Mercury 卡片 ID 过滤交易记录
    
    Args:
        transactions: 交易记录列表
        mercury_card_id: Mercury 卡片 ID
        
    Returns:
        list: 过滤后的交易记录
    """
    result = []
    for tx in transactions:
        details = tx.get("details", {})
        # 信用卡交易的卡片ID可能在 creditCardId 或 paymentCardId
        # 借记卡交易的卡片ID在 debitCardId
        card_id = details.get("creditCardId") or details.get("paymentCardId") or details.get("debitCardId")
        if card_id == mercury_card_id:
            result.append(tx)
    return result


def format_transaction(tx):
    """
    格式化交易记录为易读格式
    
    Args:
        tx: 原始交易记录
        
    Returns:
        dict: 格式化后的交易记录
    """
    details = tx.get("details", {})
    category_data = tx.get("categoryData", {})
    merchant_amount = tx.get("merchantAmount", {})
    merchant = tx.get("merchant", {})
    
    return {
        "id": tx.get("id"),
        "amount": tx.get("amount"),
        "status": tx.get("status"),
        "created_at": tx.get("createdAt"),
        "payment_method": details.get("humanPaymentMethod"),
        "card_id": details.get("paymentCardId") or details.get("debitCardId"),
        "category": category_data.get("mercuryCategory"),
        "bank_description": tx.get("bankDescription"),
        "counterparty_location": tx.get("counterPartyLocation"),
        "reason_for_failure": tx.get("reasonForFailure"),
        "failed_at": tx.get("failedAt"),
        # 原始货币金额（如果有）
        "merchant_amount": merchant_amount.get("amount") if merchant_amount else None,
        "merchant_currency": merchant_amount.get("currency") if merchant_amount else None,
        # 商家信息
        "merchant_name": merchant.get("name"),
        "merchant_logo": merchant.get("logoRasterUrl"),
    }


def get_transactions_by_card_key(card_key_id, limit=100):
    """
    通过卡密 ID 查询消费记录
    
    Args:
        card_key_id: 卡密 ID
        limit: 返回记录数量限制
        
    Returns:
        dict: {
            "success": bool,
            "error": str (失败时),
            "card_id": str,
            "transactions": list
        }
    """
    # 1. 获取卡密对应的 Mercury 卡片信息
    mercury_card_id, account_user_id, card_type = get_card_info_by_card_key(card_key_id)
    
    if not mercury_card_id:
        return {"success": False, "error": "卡密不存在或未兑换"}
    
    if not account_user_id:
        return {"success": False, "error": "无法获取账户信息"}
    
    # 2. 获取组织 ID
    organization_id = get_organization_id_by_account_user_id(account_user_id)
    if not organization_id:
        return {"success": False, "error": "无法获取组织信息"}
    
    # 3. 获取账户信息
    account = get_account_by_user_id(account_user_id)
    if not account:
        return {"success": False, "error": "账户不存在"}
    
    # 4. 查询交易记录
    all_transactions = query_transactions(account, organization_id, limit)
    
    # 5. 过滤出该卡片的交易
    card_transactions = filter_transactions_by_card_id(all_transactions, mercury_card_id)
    
    # 6. 格式化结果
    formatted = [format_transaction(tx) for tx in card_transactions]
    
    return {
        "success": True,
        "card_key_id": card_key_id,
        "card_id": mercury_card_id,
        "card_type": card_type,
        "account_user_id": account_user_id,
        "organization_id": organization_id,
        "transaction_count": len(formatted),
        "transactions": formatted
    }


def get_transactions_by_mercury_card_id(mercury_card_id, account_user_id, limit=100):
    """
    通过 Mercury 卡片 ID 直接查询消费记录
    
    Args:
        mercury_card_id: Mercury 返回的卡片 ID
        account_user_id: 账户用户 ID
        limit: 返回记录数量限制
        
    Returns:
        dict: {
            "success": bool,
            "error": str (失败时),
            "transactions": list
        }
    """
    # 1. 获取组织 ID
    organization_id = get_organization_id_by_account_user_id(account_user_id)
    if not organization_id:
        return {"success": False, "error": "无法获取组织信息"}
    
    # 2. 获取账户信息
    account = get_account_by_user_id(account_user_id)
    if not account:
        return {"success": False, "error": "账户不存在"}
    
    # 3. 查询交易记录
    all_transactions = query_transactions(account, organization_id, limit)
    
    # 4. 过滤出该卡片的交易
    card_transactions = filter_transactions_by_card_id(all_transactions, mercury_card_id)
    
    # 5. 格式化结果
    formatted = [format_transaction(tx) for tx in card_transactions]
    
    return {
        "success": True,
        "card_id": mercury_card_id,
        "account_user_id": account_user_id,
        "organization_id": organization_id,
        "transaction_count": len(formatted),
        "transactions": formatted
    }


# ==========================================
# 主程序示例
# ==========================================
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="查询 Mercury 卡片消费记录")
    parser.add_argument("id", help="卡密 ID 或 Mercury 卡片 ID")
    parser.add_argument("--account-user-id", help="账户用户 ID（使用 Mercury 卡片 ID 时必须提供）")
    parser.add_argument("--limit", type=int, default=100, help="返回记录数量限制")
    
    args = parser.parse_args()
    
    if args.account_user_id:
        # 使用 Mercury 卡片 ID 查询
        result = get_transactions_by_mercury_card_id(args.id, args.account_user_id, args.limit)
    else:
        # 使用卡密 ID 查询
        result = get_transactions_by_card_key(args.id, args.limit)
    
    print(json.dumps(result, indent=2, ensure_ascii=False))
