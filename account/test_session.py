import accounts
import sys
import os

# 确保能找到 accounts.py (如果放在同一目录下通常不需要这两行，但为了稳健加上)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def test_single_session():
    print("=" * 50)
    print("Mercury Session 有效性测试工具")
    print("=" * 50)
    
    # 1. 从屏幕获取输入
    try:
        session_input = input("请输入 _SESSION Cookie 值 (回车确认): ").strip()
    except KeyboardInterrupt:
        print("\n已取消")
        return

    if not session_input:
        print("❌ 错误: Session 不能为空")
        return

    print("\n🔄 正在连接 Mercury API 验证 Session...")

    try:
        # 2. 调用 accounts.py 中的核心函数进行测试
        # get_user_info_by_session 返回元组: (success, user_info_or_error, new_session)
        success, result, new_session = accounts.get_user_info_by_session(session_input)

        print("\n" + "-" * 20 + " 测试结果 " + "-" * 20)

        if success:
            print("✅ Session 有效！登录成功。")
            
            # 提取一些基本信息展示，验证数据解析是否正常
            user = result.get('user', {})
            org = result.get('organization', {})
            credit = result.get('credit_account', {})
            
            print(f"\n👤 用户信息:")
            print(f"   姓名: {user.get('first_name')} {user.get('last_name')}")
            print(f"   邮箱: {user.get('email')}")
            print(f"   状态: {user.get('status')}")
            
            print(f"\n🏢 组织信息:")
            print(f"   公司名: {org.get('name')}")
            print(f"   结构: {org.get('company_structure')}")
            
            print(f"\n💳 账户概览:")
            print(f"   可用余额: ${credit.get('available_balance')}")
            print(f"   信用额度: ${credit.get('credit_limit')}")
            
            # 检查是否有新 Session 返回
            if new_session and new_session != session_input:
                print(f"\n⚠️  注意: 服务器返回了新的 Session (已自动刷新)")
                print(f"   新 Session 前缀: {new_session[:10]}...")
            else:
                print(f"\nℹ️  Session 未变更")
                
        else:
            print("❌ Session 无效 或 请求失败")
            print(f"⚠️  错误详情: {result}")
            
            # 针对你遇到的 400/NoneType 错误的特别提示
            if "NoneType" in str(result) or "AttributeError" in str(result):
                print("\n💡 提示: 这看起来像是代码解析错误。")
                print("   如果这个账户是新注册或受限账户，某些字段可能为 null。")
                print("   请检查 accounts.py 中是否使用了 .get().get() 链式调用。")

    except Exception as e:
        print(f"\n❌ 发生未捕获的异常: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_single_session()