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
MAIN_SERVER_TIMEOUT = 30
MAIN_SERVER_QUERY_TIMEOUT = 15
MAIN_SERVER_REDEEM_TIMEOUT = 90

VERSION_FILE = os.path.join(os.path.dirname(__file__), "version.txt")
UUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

def get_version():
    """读取版本号"""
    try:
        with open(VERSION_FILE, 'r') as f:
            return f.read().strip()
    except:
        return "0.0"


def proxy_response(resp):
    """透传主服务器响应"""
    return Response(
        resp.content,
        status=resp.status_code,
        headers=dict(resp.headers)
    )


def query_key_status(key_id, timeout=MAIN_SERVER_QUERY_TIMEOUT):
    """回查卡密状态"""
    return requests.post(
        f"{MAIN_SERVER}/api/keys/query",
        json={"key_id": key_id},
        timeout=timeout
    )


def recover_timed_out_redeem(key_id):
    """兑换请求超时后，尝试回查卡密状态，避免实际成功却前端报错。"""
    key_id = str(key_id or '').strip()
    if not key_id or not UUID_PATTERN.match(key_id):
        return None

    try:
        query_resp = query_key_status(key_id)
        query_data = query_resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"[兑换代理] 超时后回查失败 {key_id}: {e}")
        return None

    if query_data.get("success"):
        print(f"[兑换代理] 超时后回查确认成功 {key_id}")
        return query_resp

    print(f"[兑换代理] 超时后回查未确认成功 {key_id}: {query_data.get('error', '未知状态')}")
    return None


def timed_out_redeem_response(key_id, error):
    """兑换超时后优先回查，回查仍失败再返回超时提示。"""
    recovered_resp = recover_timed_out_redeem(key_id)
    if recovered_resp is not None:
        return proxy_response(recovered_resp)

    return jsonify({
        "success": False,
        "error": "主服务器兑换处理超时，已自动回查但暂未确认成功，请稍后重新查询该卡密",
        "detail": str(error)
    }), 504


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
                query_resp = query_key_status(code)
                query_data = query_resp.json()

                if query_data.get("success"):
                    return proxy_response(query_resp)

                if query_data.get("error") == "卡密未使用":
                    try:
                        redeem_resp = requests.post(
                            f"{MAIN_SERVER}/api/keys/redeem",
                            json={"key_id": code},
                            timeout=MAIN_SERVER_REDEEM_TIMEOUT
                        )
                        return proxy_response(redeem_resp)
                    except requests.exceptions.Timeout as e:
                        return timed_out_redeem_response(code, e)

                if query_data.get("error") != "卡密不存在":
                    return proxy_response(query_resp)
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
    json_body = request.get_json(silent=True) if request.method in ('POST', 'PUT') else None

    try:
        if request.method == 'GET':
            resp = requests.get(url, headers=headers, params=request.args, timeout=MAIN_SERVER_TIMEOUT)
        elif request.method == 'POST':
            timeout = MAIN_SERVER_REDEEM_TIMEOUT if path == 'keys/redeem' else MAIN_SERVER_TIMEOUT
            try:
                resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
            except requests.exceptions.Timeout as e:
                if path == 'keys/redeem':
                    key_id = str((json_body or {}).get('key_id', '')).strip()
                    return timed_out_redeem_response(key_id, e)
                raise
        elif request.method == 'PUT':
            resp = requests.put(url, headers=headers, json=json_body, timeout=MAIN_SERVER_TIMEOUT)
        elif request.method == 'DELETE':
            resp = requests.delete(url, headers=headers, timeout=MAIN_SERVER_TIMEOUT)
        else:
            return jsonify({"error": "Method not allowed"}), 405

        # 返回响应
        return proxy_response(resp)
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
