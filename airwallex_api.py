"""
Airwallex API 模块
通过 timoes.me API 获取虚拟卡信息
"""

import requests
from typing import Optional, Dict, Any

# API 配置
AIRWALLEX_API_URL = "https://timoes.me/api/redeem/view"
AIRWALLEX_TIMEOUT = 30


def redeem_airwallex_card(code: str) -> Dict[str, Any]:
    """
    通过 code 兑换/查询 Airwallex 卡片
    
    Args:
        code: 兑换码（UUID格式）
        
    Returns:
        dict: 包含 success 状态和卡片信息的结果
        
    响应格式:
    成功:
    {
        "success": True,
        "card_type": "airwallex",
        "card": {
            "pan": "4462220001827424",
            "cvv": "326",
            "exp_month": "01",
            "exp_year": "2029"
        },
        "card_limit": 1.0,
        "legal_address": {
            "address1": "Unit 25 Enterprise Park Industrial Estate",
            "address2": "Old Lane, Beeston",
            "city": "England",
            "region": "England",
            "postal_code": "LS11 8HA",
            "country": "United Kingdom"
        },
        "expire_minutes": 60,
        "is_new": True
    }
    """
    try:
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }
        
        payload = {"code": code}
        
        response = requests.post(
            AIRWALLEX_API_URL,
            headers=headers,
            json=payload,
            timeout=AIRWALLEX_TIMEOUT
        )
        
        if response.status_code != 200:
            return {
                "success": False,
                "error": f"API请求失败: HTTP {response.status_code}"
            }
        
        data = response.json()
        
        # 检查是否有错误信息
        if "error" in data or "message" in data and "失败" in data.get("message", ""):
            return {
                "success": False,
                "error": data.get("error") or data.get("message") or "未知错误"
            }
        
        # 检查必要字段
        if "card_number" not in data:
            return {
                "success": False,
                "error": data.get("message") or "无效的响应格式"
            }
        
        # 解析有效期 (格式: "01/29" -> month=01, year=2029)
        exp_parts = data.get("exp", "01/29").split("/")
        exp_month = exp_parts[0] if len(exp_parts) >= 1 else "01"
        exp_year = "20" + exp_parts[1] if len(exp_parts) >= 2 else "2029"
        
        # 解析账单地址
        billing = data.get("billing_address", {})
        
        # 转换为统一格式
        result = {
            "success": True,
            "card_type": "airwallex",
            "card": {
                "card_id": data.get("card_id", ""),
                "pan": data.get("card_number", ""),
                "cvv": data.get("cvc", ""),
                "exp_month": exp_month,
                "exp_year": exp_year,
            },
            "card_limit": data.get("limit", 0),
            "legal_address": {
                "address1": billing.get("street", ""),
                "address2": billing.get("apt", ""),
                "city": billing.get("city", ""),
                "region": billing.get("state", ""),
                "postal_code": billing.get("zip", ""),
                "country": billing.get("country", "")
            },
            "expire_minutes": data.get("remaining_minutes", 60),
            "is_new": data.get("is_new", False),
            "used_time": None,  # Airwallex API 不提供兑换时间，前端会用当前时间
            "message": data.get("message", "")
        }
        
        return result
        
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "请求超时，请稍后重试"
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "error": f"网络请求失败: {str(e)}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"处理失败: {str(e)}"
        }


def is_mercury_code(code: str) -> bool:
    """
    判断是否是 Mercury 兑换码
    Mercury 卡密格式：
    - 信用卡：5236 开头的 UUID
    - 借记卡：5481 开头的 UUID
    - 兼容旧格式：0 或 1 开头的 UUID

    Args:
        code: 兑换码

    Returns:
        bool: 是否是 Mercury 格式的兑换码
    """
    import re
    code = code.strip()
    # UUID 格式: 8-4-4-4-12
    uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    if not re.match(uuid_pattern, code):
        return False
    # Mercury 卡密：5236（信用卡）、5481（借记卡）开头，或 0/1 开头（旧格式兼容）
    if code.startswith('5236') or code.startswith('5481'):
        return True
    if code[0] in ('0', '1'):
        return True
    return False


def is_airwallex_code(code: str) -> bool:
    """
    判断是否是 Airwallex 兑换码
    不是 Mercury 的 UUID 格式兑换码都视为 Airwallex

    Args:
        code: 兑换码

    Returns:
        bool: 是否是 Airwallex 格式的兑换码
    """
    import re
    code = code.strip()
    # UUID 格式: 8-4-4-4-12
    uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    if not re.match(uuid_pattern, code):
        return False
    # 不是 Mercury 的就是 Airwallex
    return not is_mercury_code(code)


if __name__ == "__main__":
    # 测试代码
    test_code = "c4841c14-3d91-4108-bf54-2d806afe7f16"
    print(f"Testing code: {test_code}")
    print(f"Is Airwallex code: {is_airwallex_code(test_code)}")
    
    result = redeem_airwallex_card(test_code)
    print(f"Result: {result}")
