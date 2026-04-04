"""
兑换页面独立服务器
只提供兑换页面，API 请求代理到主服务器
"""

import os
import re
import requests
from flask import Flask, render_template, request, jsonify, Response
from other_api import redeem_airwallex_card

app = Flask(__name__)

# 主服务器地址
MAIN_SERVER = "http://127.0.0.1:7999"

VERSION_FILE = os.path.join(os.path.dirname(__file__), "version.txt")
UUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

def get_version():
    """读取版本号"""
    try:
        with open(VERSION_FILE, 'r') as f:
            return f.read().strip()
    except:
        return "0.0"


# ==================== 页面路由 ====================

@app.route('/')
def redeem():
    """兑换页面"""
    return render_template('redeem.html', version=get_version())


@app.route('/withdraw/<token>')
def withdraw_page(token):
    """提卡链接页面"""
    return render_template('withdraw.html', token=token, version=get_version())


# ==================== 代理 API 到主服务器 ====================

@app.route('/api/airwallex/redeem', methods=['POST'])
def airwallex_redeem():
    """统一兑换 API - 根据卡密格式路由到不同后端"""
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip()

        if not code:
            return jsonify({"success": False, "error": "请输入兑换码"})

        if UUID_PATTERN.match(code):
            try:
                query_resp = requests.post(
                    f"{MAIN_SERVER}/api/keys/query",
                    json={"key_id": code},
                    timeout=15
                )
                query_data = query_resp.json()

                if query_data.get("success"):
                    return Response(
                        query_resp.content,
                        status=query_resp.status_code,
                        headers=dict(query_resp.headers)
                    )

                if query_data.get("error") == "卡密未使用":
                    redeem_resp = requests.post(
                        f"{MAIN_SERVER}/api/keys/redeem",
                        json={"key_id": code},
                        timeout=30
                    )
                    return Response(
                        redeem_resp.content,
                        status=redeem_resp.status_code,
                        headers=dict(redeem_resp.headers)
                    )

                if query_data.get("error") != "卡密不存在":
                    return Response(
                        query_resp.content,
                        status=query_resp.status_code,
                        headers=dict(query_resp.headers)
                    )
            except requests.exceptions.RequestException as e:
                return jsonify({"success": False, "error": f"无法连接主服务器: {str(e)}"}), 503

        result = redeem_airwallex_card(code)
        return jsonify(result)

    except Exception as e:
        return jsonify({"success": False, "error": f"处理失败: {str(e)}"})

@app.route('/api/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy_api(path):
    """代理所有 API 请求到主服务器"""
    url = f"{MAIN_SERVER}/api/{path}"
    
    # 复制请求头
    headers = {key: value for key, value in request.headers if key.lower() != 'host'}
    
    try:
        if request.method == 'GET':
            resp = requests.get(url, headers=headers, params=request.args, timeout=30)
        elif request.method == 'POST':
            resp = requests.post(url, headers=headers, json=request.json, timeout=30)
        elif request.method == 'PUT':
            resp = requests.put(url, headers=headers, json=request.json, timeout=30)
        elif request.method == 'DELETE':
            resp = requests.delete(url, headers=headers, timeout=30)
        else:
            return jsonify({"error": "Method not allowed"}), 405
        
        # 返回响应
        return Response(
            resp.content,
            status=resp.status_code,
            headers=dict(resp.headers)
        )
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": f"无法连接主服务器: {str(e)}"}), 503


# ==================== 静态文件代理 ====================

@app.route('/static/<path:path>')
def proxy_static(path):
    """代理静态文件请求到主服务器"""
    url = f"{MAIN_SERVER}/static/{path}"
    
    try:
        resp = requests.get(url, timeout=10)
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get('Content-Type', 'application/octet-stream')
        )
    except requests.exceptions.RequestException as e:
        return f"Static file error: {str(e)}", 503


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='兑换页面独立服务器')
    parser.add_argument('--port', type=int, default=8001, help='服务器端口 (默认: 8001)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='监听地址 (默认: 0.0.0.0)')
    parser.add_argument('--main-server', type=str, default='http://127.0.0.1:7999', help='主服务器地址')
    args = parser.parse_args()
    
    MAIN_SERVER = args.main_server
    display_host = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
    
    print(f"🚀 兑换页面服务器启动")
    print(f"   监听: {args.host}:{args.port}")
    print(f"   浏览器打开: http://{display_host}:{args.port}")
    print(f"   主服务器: {MAIN_SERVER}")
    print()
    
    app.run(host=args.host, port=args.port, debug=False)
