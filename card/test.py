"""
测试脚本 - 用于测试卡片创建和取消功能
"""

import sys
import json
import os
import termios
import tty

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from account.accounts import get_random_account
from issue import issue_card
from embed_reveal import reveal_card_details
from cancel import cancel_card


def get_key():
    """获取单个按键输入"""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def create_and_reveal_card(account):
    """创建卡片并解密信息"""
    
    print("\n" + "=" * 50)
    print(f"创建虚拟卡 (账户: {account['email']})")
    print("=" * 50)
    
    # 创建卡
    card_id = issue_card(account)
    
    if not card_id:
        print("❌ 创建卡片失败")
        return None
    
    print(f"\n✅ 卡片创建成功，ID: {card_id}\n")
    
    print("=" * 50)
    print("解密卡片信息")
    print("=" * 50)
    
    # 解密卡片
    card_details = reveal_card_details(card_id, account)
    
    if not card_details:
        print("❌ 解密卡片失败")
        return None
    
    print("\n" + "=" * 50)
    print("卡片完整信息")
    print("=" * 50)
    print(f"卡片 ID: {card_id}")
    print("-" * 50)
    print(json.dumps(card_details, indent=4))
    print("=" * 50)
    
    # 取消卡片
    print("\n" + "=" * 50)
    print("取消卡片")
    print("=" * 50)
    
    if cancel_card(card_id, account):
        print(f"✅ 卡片 {card_id} 已取消")
    else:
        print(f"⚠️ 取消卡片 {card_id} 失败")
    
    return card_id


def main():
    """
    主程序：
    - 按空格: 生成新卡片
    - 按回车: 退出
    """
    
    card_count = 0
    
    print("\n" + "=" * 50)
    print("Mercury 虚拟卡生成器")
    print("=" * 50)
    print("操作说明:")
    print("  [空格] - 生成新卡片")
    print("  [回车] - 退出")
    print("=" * 50)
    
    while True:
        print(f"\n已生成 {card_count} 张卡，等待操作...")
        
        key = get_key()
        
        if key == ' ':  # 空格
            account = get_random_account()
            if not account:
                print("❌ 没有可用的账户，请先添加账户")
                continue
            result = create_and_reveal_card(account)
            if result:
                card_count += 1
                
        elif key == '\r' or key == '\n':  # 回车
            print("\n\n" + "=" * 50)
            print("正在退出...")
            print("=" * 50)
            print(f"\n本次共生成 {card_count} 张卡片")
            print("程序已退出")
            break
            
        elif key == '\x03':  # Ctrl+C
            print("\n\n强制退出")
            break


if __name__ == "__main__":
    main()
