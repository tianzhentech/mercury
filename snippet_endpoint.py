@app.route('/api/keys/user-destroy', methods=['POST'])
def destroy_keys_user():
    """无需登录的销毁接口，需验证卡密和卡号匹配"""
    from card.cancel import cancel_card
    from id.id import mark_destroyed
    
    try:
        data = request.get_json() or {}
        items = data.get('items', []) # List of {key: '...', card_id: '...'}
        
        if not items:
            return jsonify({"success": False, "error": "参数错误"}), 400
        
        results = []
        destroyed_keys = 0
        deleted_cards = 0
        failed = 0
        
        for item in items:
            key_id = item.get('key').strip()
            # 允许只传卡号后4位或完整card_id进行校验，这里暂用card_id
            request_card_id = item.get('card_id', '').strip()
            
            result = {"key": key_id}
            
            # 1. 验证卡密信息
            success, redeemed_info = query_redeemed(key_id)
            if not success:
                result["status"] = "failed"
                result["error"] = "卡密无效"
                failed += 1
                results.append(result)
                continue

            card = redeemed_info.get("card", {})
            server_card_id = card.get("card_id")
            
            # 2. 核心校验：传入的 card_id 必须匹配
            if not server_card_id or server_card_id != request_card_id:
                result["status"] = "failed"
                result["error"] = "卡片信息验证失败"
                failed += 1
                results.append(result)
                continue
            
            # 3. 验证通过，执行销毁
            # ... (Existing logic for cancel_card and mark_destroyed) ...
            card_type = card.get("card_type", "credit")
            account_user_id = card.get("account_user_id")
            
            # 取消卡片
            account = get_account_by_user_id(account_user_id)
            card_deleted = False
            if account:
                if cancel_card(server_card_id, account, card_type=card_type):
                    card_deleted = True
                    deleted_cards += 1
            
            # 标记销毁
            mark_success, mark_error = mark_destroyed(key_id, username='user_action')
            if mark_success:
                destroyed_keys += 1
                result["status"] = "success"
            else:
                result["status"] = "failed"
                result["error"] = mark_error
                failed += 1
                
            results.append(result)

        return jsonify({
            "success": True, 
            "destroyed_keys": destroyed_keys,
            "deleted_cards": deleted_cards,
            "failed": failed,
            "results": results
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
