"""
卡密管理模块 - SQLite 版本
"""

import uuid
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from contextlib import contextmanager

# 数据库文件路径
DB_FILE = os.path.join(os.path.dirname(__file__), "id.db")
# 旧的 JSON 文件路径（用于迁移）
OLD_JSON_FILE = os.path.join(os.path.dirname(__file__), "id.json")

# 连接池 - 每个线程一个连接
_local = threading.local()
_init_lock = threading.Lock()
_db_initialized = False


def _get_connection():
    """获取当前线程的数据库连接"""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        # 启用 WAL 模式，提高并发性能
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


@contextmanager
def _get_cursor():
    """获取数据库游标的上下文管理器"""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def _init_db():
    """初始化数据库表"""
    global _db_initialized
    if _db_initialized:
        return
    
    with _init_lock:
        if _db_initialized:
            return
        
        with _get_cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ids (
                    id TEXT PRIMARY KEY,
                    expire_minutes INTEGER NOT NULL,
                    card_limit REAL DEFAULT 0,
                    card_type TEXT DEFAULT 'credit',
                    created_time TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    used_time TEXT,
                    created_by TEXT,
                    redeemed_card TEXT,
                    destroyed INTEGER DEFAULT 0,
                    destroyed_time TEXT,
                    hidden INTEGER DEFAULT 0,
                    hidden_token TEXT,
                    hidden_note TEXT
                )
            ''')
            
            # 添加 destroyed 列（如果不存在）
            try:
                cursor.execute("ALTER TABLE ids ADD COLUMN destroyed INTEGER DEFAULT 0")
            except:
                pass
            try:
                cursor.execute("ALTER TABLE ids ADD COLUMN destroyed_time TEXT")
            except:
                pass
            try:
                cursor.execute("ALTER TABLE ids ADD COLUMN hidden INTEGER DEFAULT 0")
            except:
                pass
            try:
                cursor.execute("ALTER TABLE ids ADD COLUMN hidden_token TEXT")
            except:
                pass
            try:
                cursor.execute("ALTER TABLE ids ADD COLUMN hidden_note TEXT")
            except:
                pass
            
            # 创建索引以加速查询
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_used ON ids(used)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_by ON ids(created_by)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_time ON ids(created_time)')
        
        _db_initialized = True
        
        # 检查是否需要从 JSON 迁移
        _migrate_from_json()


def _migrate_from_json():
    """从旧的 JSON 文件迁移数据到 SQLite"""
    if not os.path.exists(OLD_JSON_FILE):
        return
    
    try:
        with open(OLD_JSON_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return
            data = json.loads(content)
            if not isinstance(data, dict) or "ids" not in data:
                return
        
        ids_list = data.get("ids", [])
        if not ids_list:
            return
        
        # 检查数据库是否已有数据
        with _get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM ids")
            count = cursor.fetchone()[0]
            if count > 0:
                print(f"[卡密] 数据库已有 {count} 条记录，跳过迁移")
                return
        
        # 批量插入数据
        with _get_cursor() as cursor:
            for item in ids_list:
                redeemed_card = item.get("redeemed_card")
                redeemed_card_json = json.dumps(redeemed_card, ensure_ascii=False) if redeemed_card else None
                
                cursor.execute('''
                    INSERT OR IGNORE INTO ids 
                    (id, expire_minutes, card_limit, card_type, created_time, used, used_time, created_by, redeemed_card)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item.get("id"),
                    item.get("expire_minutes", 60),
                    item.get("card_limit", 0),
                    item.get("card_type", "credit"),
                    item.get("created_time"),
                    1 if item.get("used") else 0,
                    item.get("used_time"),
                    item.get("created_by"),
                    redeemed_card_json
                ))
        
        print(f"[卡密] 已从 JSON 迁移 {len(ids_list)} 条记录到 SQLite")
        
        # 重命名旧文件
        backup_path = OLD_JSON_FILE + ".migrated"
        os.rename(OLD_JSON_FILE, backup_path)
        print(f"[卡密] 旧 JSON 文件已重命名为 {backup_path}")
        
    except Exception as e:
        print(f"[卡密] 迁移失败: {e}")


def _local_tzinfo():
    return datetime.now().astimezone().tzinfo


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _normalize_iso_to_utc(value):
    if not value:
        return value

    try:
        s = str(value)
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_local_tzinfo())
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return value


def _row_to_dict(row):
    """将数据库行转换为字典"""
    if row is None:
        return None
    
    redeemed_card = None
    if row["redeemed_card"]:
        try:
            redeemed_card = json.loads(row["redeemed_card"])
        except:
            pass
    
    return {
        "id": row["id"],
        "expire_minutes": row["expire_minutes"],
        "card_limit": row["card_limit"],
        "card_type": row["card_type"],
        "created_time": row["created_time"],
        "used": bool(row["used"]),
        "used_time": row["used_time"],
        "created_by": row["created_by"],
        "redeemed_card": redeemed_card,
        "destroyed": bool(row["destroyed"]) if row["destroyed"] is not None else False,
        "destroyed_time": row["destroyed_time"] if "destroyed_time" in row.keys() else None,
        "hidden": bool(row["hidden"]) if "hidden" in row.keys() and row["hidden"] is not None else False,
        "hidden_token": row["hidden_token"] if "hidden_token" in row.keys() else None,
        "hidden_note": row["hidden_note"] if "hidden_note" in row.keys() else None
    }


# 确保数据库初始化
_init_db()


def load_ids():
    """
    加载卡密数据（兼容旧 API）
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("SELECT * FROM ids ORDER BY created_time DESC")
        rows = cursor.fetchall()
        return {"ids": [_row_to_dict(row) for row in rows]}


def save_ids(data):
    """
    保存卡密数据（兼容旧 API - 不推荐使用）
    注意：这个函数会清空并重写所有数据，仅用于兼容性
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("DELETE FROM ids")
        for item in data.get("ids", []):
            redeemed_card = item.get("redeemed_card")
            redeemed_card_json = json.dumps(redeemed_card, ensure_ascii=False) if redeemed_card else None
            
            cursor.execute('''
                INSERT INTO ids 
                (id, expire_minutes, card_limit, card_type, created_time, used, used_time, created_by, redeemed_card)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item.get("id"),
                item.get("expire_minutes", 60),
                item.get("card_limit", 0),
                item.get("card_type", "credit"),
                item.get("created_time"),
                1 if item.get("used") else 0,
                item.get("used_time"),
                item.get("created_by"),
                redeemed_card_json
            ))


def generate_ids(count, expire_minutes, card_limit=1, card_type="credit", created_by=None, hidden=False, hidden_token=None, hidden_note=None):
    """
    生成卡密
    
    Args:
        count: 生成数量
        expire_minutes: 兑换后卡片有效时间（分钟）
        card_limit: 卡片余额（美元）
        card_type: 卡片类型 ("credit" 或 "debit")
        created_by: 创建者用户名
        
    Returns:
        list: 生成的卡密列表
    """
    _init_db()
    generated = []
    now_iso = _utc_now_iso()
    
    with _get_cursor() as cursor:
        for _ in range(count):
            # 根据卡类型生成UUID：信用卡以0开头，借记卡以1开头
            raw_uuid = str(uuid.uuid4())
            prefix = '0' if card_type == 'credit' else '1'
            typed_uuid = prefix + raw_uuid[1:]
            
            cursor.execute('''
                INSERT INTO ids 
                (id, expire_minutes, card_limit, card_type, created_time, used, created_by, hidden, hidden_token, hidden_note)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ''', (typed_uuid, expire_minutes, card_limit, card_type, now_iso, created_by, 1 if hidden else 0, hidden_token, hidden_note))
            
            generated.append({
                "id": typed_uuid,
                "expire_minutes": expire_minutes,
                "card_limit": card_limit,
                "card_type": card_type,
                "created_time": now_iso,
                "used": False,
                "used_time": None,
                "created_by": created_by,
                "hidden": bool(hidden),
                "hidden_token": hidden_token,
                "hidden_note": hidden_note
            })
    
    return generated


def validate_id(card_id):
    """
    验证卡密是否有效
    
    Args:
        card_id: 卡密 ID
        
    Returns:
        tuple: (是否有效, 卡密信息字典或错误信息)
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("SELECT * FROM ids WHERE id = ?", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if row["destroyed"]:
            return False, "卡密已被销毁"
        
        if row["used"]:
            return False, "卡密已被使用"
        
        return True, {
            "expire_minutes": row["expire_minutes"],
            "card_limit": row["card_limit"],
            "card_type": row["card_type"]
        }


def use_id(card_id, card_info=None):
    """
    使用卡密（标记为已使用）
    
    Args:
        card_id: 卡密 ID
        card_info: 兑换的卡片信息（可选）
        
    Returns:
        tuple: (是否成功, 有效分钟数或错误信息)
    """
    _init_db()
    now_iso = _utc_now_iso()
    redeemed_card_json = json.dumps(card_info, ensure_ascii=False) if card_info else None
    
    with _get_cursor() as cursor:
        # 先查询确认存在且未使用
        cursor.execute("SELECT * FROM ids WHERE id = ?", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if row["used"]:
            return False, "卡密已被使用"
        
        # 更新为已使用
        cursor.execute('''
            UPDATE ids SET used = 1, used_time = ?, redeemed_card = ?
            WHERE id = ? AND used = 0
        ''', (now_iso, redeemed_card_json, card_id))
        
        if cursor.rowcount == 0:
            return False, "卡密已被使用"
        
        return True, row["expire_minutes"]


def allocate_existing_ids_for_withdraw(token, note, username, card_type, count):
    """
    优先提取现有未使用的卡密并标记为隐藏，用于提卡链接
    """
    if count <= 0:
        return []

    _init_db()
    with _get_cursor() as cursor:
        conditions = [
            "used = 0",
            "(destroyed = 0 OR destroyed IS NULL)",
            "(hidden = 0 OR hidden IS NULL)",
            "card_type = ?"
        ]
        params = [card_type]

        if username:
            conditions.append("created_by = ?")
            params.append(username)

        where_clause = " AND ".join(conditions)
        params.append(count)

        cursor.execute(
            f"""
            SELECT * FROM ids
            WHERE {where_clause}
            ORDER BY created_time ASC
            LIMIT ?
            """,
            params
        )
        rows = cursor.fetchall()

        if not rows:
            return []

        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" * len(ids))
        update_params = [token, note, *ids]

        cursor.execute(
            f"UPDATE ids SET hidden = 1, hidden_token = ?, hidden_note = ? WHERE id IN ({placeholders})",
            update_params
        )

        result = []
        for row in rows:
            item = _row_to_dict(row)
            item["hidden"] = True
            item["hidden_token"] = token
            item["hidden_note"] = note
            result.append(item)

        return result


def query_redeemed(card_id):
    """
    查询已兑换卡密的卡片信息
    
    Args:
        card_id: 卡密 ID
        
    Returns:
        tuple: (是否成功, 卡片信息或错误信息)
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("SELECT * FROM ids WHERE id = ?", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if not row["used"]:
            return False, "卡密未使用"
        
        if not row["redeemed_card"]:
            return False, "卡片信息已被删除"
        
        try:
            card = json.loads(row["redeemed_card"])
        except:
            return False, "卡片信息解析失败"
        
        used_time = _normalize_iso_to_utc(row["used_time"])
        expire_time = _normalize_iso_to_utc(card.get("expire_time")) if isinstance(card, dict) else None
        if isinstance(card, dict) and expire_time:
            card = dict(card)
            card["expire_time"] = expire_time
        
        return True, {
            "card": card,
            "expire_minutes": row["expire_minutes"],
            "card_limit": row["card_limit"],
            "used_time": used_time
        }


def delete_id(card_id, username=None):
    """
    删除卡密
    
    Args:
        card_id: 卡密 ID
        username: 当前用户名（用于权限检查）
        
    Returns:
        tuple: (是否删除成功, 错误信息或None)
    """
    _init_db()
    with _get_cursor() as cursor:
        # 先查询确认存在和权限
        cursor.execute("SELECT * FROM ids WHERE id = ?", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if row["created_by"] != username:
            return False, "无权删除此卡密"
        
        cursor.execute("DELETE FROM ids WHERE id = ?", (card_id,))
        return True, None


def mark_destroyed(card_id, username=None):
    """
    标记卡密为已销毁（不删除记录）
    
    Args:
        card_id: 卡密 ID
        username: 当前用户名（用于权限检查）
        
    Returns:
        tuple: (是否成功, 错误信息或None)
    """
    _init_db()
    now_iso = _utc_now_iso()
    
    with _get_cursor() as cursor:
        cursor.execute("SELECT * FROM ids WHERE id = ?", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if username and row["created_by"] != username:
            return False, "无权操作此卡密"
        
        cursor.execute(
            "UPDATE ids SET destroyed = 1, destroyed_time = ? WHERE id = ?",
            (now_iso, card_id)
        )
        return True, None


def get_all_ids(username=None, page=None, page_size=100):
    """
    获取卡密列表（只返回未使用且未销毁的卡密）
    
    Args:
        username: 当前用户名（用于过滤）
        page: 页码（从1开始），None表示获取全部
        page_size: 每页数量，默认100
        
    Returns:
        dict: 卡密数据，包含 ids, total, page, page_size, total_pages
    """
    _init_db()
    with _get_cursor() as cursor:
        # 基础条件：未使用且未销毁
        base_condition = "used = 0 AND (destroyed = 0 OR destroyed IS NULL) AND (hidden = 0 OR hidden IS NULL)"
        
        # 获取总数
        if username:
            cursor.execute(f"SELECT COUNT(*) FROM ids WHERE {base_condition} AND created_by = ?", (username,))
        else:
            cursor.execute(f"SELECT COUNT(*) FROM ids WHERE {base_condition}")
        total = cursor.fetchone()[0]
        
        # 分页查询
        if page is not None and page > 0:
            offset = (page - 1) * page_size
            if username:
                cursor.execute(
                    f"SELECT * FROM ids WHERE {base_condition} AND created_by = ? ORDER BY created_time DESC LIMIT ? OFFSET ?",
                    (username, page_size, offset)
                )
            else:
                cursor.execute(f"SELECT * FROM ids WHERE {base_condition} ORDER BY created_time DESC LIMIT ? OFFSET ?", (page_size, offset))
        else:
            # 不分页，获取全部
            if username:
                cursor.execute(
                    f"SELECT * FROM ids WHERE {base_condition} AND created_by = ? ORDER BY created_time DESC",
                    (username,)
                )
            else:
                cursor.execute(f"SELECT * FROM ids WHERE {base_condition} ORDER BY created_time DESC")
        
        rows = cursor.fetchall()
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1
        
        return {
            "ids": [_row_to_dict(row) for row in rows],
            "total": total,
            "page": page or 1,
            "page_size": page_size,
            "total_pages": total_pages
        }


def get_unused_count_by_type(username=None, card_type=None):
    """
    获取未使用且未销毁的卡密数量（按类型）
    
    Args:
        username: 用户名
        card_type: 卡密类型 (credit/debit)，None表示所有类型
        
    Returns:
        int: 卡密数量
    """
    _init_db()
    with _get_cursor() as cursor:
        base_condition = "used = 0 AND (destroyed = 0 OR destroyed IS NULL) AND (hidden = 0 OR hidden IS NULL)"
        params = []
        
        if username:
            base_condition += " AND created_by = ?"
            params.append(username)
        
        if card_type:
            base_condition += " AND card_type = ?"
            params.append(card_type)
        
        cursor.execute(f"SELECT COUNT(*) FROM ids WHERE {base_condition}", params)
        return cursor.fetchone()[0]


def delete_all_ids(username=None):
    """
    删除当前用户的所有卡密
    
    Args:
        username: 当前用户名（用于过滤）
        
    Returns:
        int: 删除数量
    """
    _init_db()
    with _get_cursor() as cursor:
        if username:
            cursor.execute("SELECT COUNT(*) FROM ids WHERE created_by = ? AND (hidden = 0 OR hidden IS NULL)", (username,))
            count = cursor.fetchone()[0]
            cursor.execute("DELETE FROM ids WHERE created_by = ? AND (hidden = 0 OR hidden IS NULL)", (username,))
        else:
            cursor.execute("SELECT COUNT(*) FROM ids WHERE (hidden = 0 OR hidden IS NULL)")
            count = cursor.fetchone()[0]
            cursor.execute("DELETE FROM ids WHERE (hidden = 0 OR hidden IS NULL)")
        
        return count


def delete_ids_batch(id_list, username=None):
    """
    批量删除卡密（高性能）
    
    Args:
        id_list: 要删除的卡密 ID 列表
        username: 当前用户名（用于权限检查）
        
    Returns:
        int: 成功删除的数量
    """
    if not id_list:
        return 0
    
    _init_db()
    with _get_cursor() as cursor:
        # 使用单个事务批量删除
        if username:
            # 只删除属于该用户的卡密
            placeholders = ','.join('?' * len(id_list))
            cursor.execute(
                f"DELETE FROM ids WHERE id IN ({placeholders}) AND created_by = ?",
                (*id_list, username)
            )
        else:
            placeholders = ','.join('?' * len(id_list))
            cursor.execute(
                f"DELETE FROM ids WHERE id IN ({placeholders})",
                id_list
            )
        
        return cursor.rowcount


def delete_unused_ids_by_type(card_type=None, username=None):
    """
    删除未使用的卡密（按类型过滤）
    
    Args:
        card_type: 卡片类型 ("credit" 或 "debit"，None 表示全部)
        username: 当前用户名
        
    Returns:
        int: 删除数量
    """
    _init_db()
    with _get_cursor() as cursor:
        conditions = ["used = 0", "(hidden = 0 OR hidden IS NULL)"]
        params = []
        
        if username:
            conditions.append("created_by = ?")
            params.append(username)
        
        if card_type:
            conditions.append("card_type = ?")
            params.append(card_type)
        
        where_clause = " AND ".join(conditions)
        
        # 先获取数量
        cursor.execute(f"SELECT COUNT(*) FROM ids WHERE {where_clause}", params)
        count = cursor.fetchone()[0]
        
        # 执行删除
        cursor.execute(f"DELETE FROM ids WHERE {where_clause}", params)
        
        return count


def get_hidden_ids_by_token(token):
    """
    获取指定提卡链接下的卡密列表
    
    Args:
        token: 提卡链接 Token
        
    Returns:
        dict: 批次信息或 None
    """
    if not token:
        return None
    
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM ids WHERE hidden = 1 AND hidden_token = ? ORDER BY created_time DESC",
            (token,)
        )
        rows = cursor.fetchall()
        
        if not rows:
            return None
        
        ids = []
        for row in rows:
            ids.append({
                "id": row["id"],
                "card_limit": row["card_limit"],
                "card_type": row["card_type"],
                "expire_minutes": row["expire_minutes"],
                "used": bool(row["used"]),
                "used_time": _normalize_iso_to_utc(row["used_time"]),
                "destroyed": bool(row["destroyed"]) if row["destroyed"] is not None else False,
                "destroyed_time": row["destroyed_time"] if "destroyed_time" in row.keys() else None
            })
        
        first = rows[0]
        return {
            "token": token,
            "note": first["hidden_note"] if "hidden_note" in first.keys() else None,
            "created_time": _normalize_iso_to_utc(first["created_time"]),
            "created_by": first["created_by"],
            "card_type": first["card_type"],
            "count": len(ids),
            "ids": ids
        }


def get_redeem_records(username=None):
    """
    获取兑换记录（已使用的卡密）
    
    Args:
        username: 当前用户名（用于过滤）
        
    Returns:
        list: 兑换记录列表
    """
    _init_db()
    with _get_cursor() as cursor:
        if username:
            cursor.execute('''
                SELECT * FROM ids 
                WHERE used = 1 AND redeemed_card IS NOT NULL AND created_by = ?
                ORDER BY used_time DESC
            ''', (username,))
        else:
            cursor.execute('''
                SELECT * FROM ids 
                WHERE used = 1 AND redeemed_card IS NOT NULL
                ORDER BY used_time DESC
            ''')
        
        rows = cursor.fetchall()
        records = []
        
        for row in rows:
            try:
                card = json.loads(row["redeemed_card"]) if row["redeemed_card"] else {}
            except:
                card = {}
            
            used_time = _normalize_iso_to_utc(row["used_time"])
            expire_time = _normalize_iso_to_utc(card.get("expire_time")) if isinstance(card, dict) else None
            if isinstance(card, dict) and expire_time:
                card = dict(card)
                card["expire_time"] = expire_time
            
            records.append({
                "key_id": row["id"],
                "card_limit": row["card_limit"],
                "card_type": row["card_type"],
                "expire_minutes": row["expire_minutes"],
                "created_by": row["created_by"],
                "used_time": used_time,
                "expire_time": expire_time,
                "card": card,
                "destroyed": bool(row["destroyed"]) if row["destroyed"] is not None else False,
                "destroyed_time": row["destroyed_time"] if "destroyed_time" in row.keys() else None
            })
        
        return records


def delete_record(key_id, username=None):
    """
    删除单条兑换记录（清除卡片信息）
    
    Args:
        key_id: 卡密 ID
        username: 当前用户名（用于权限检查）
        
    Returns:
        tuple: (是否成功, 错误信息)
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("SELECT * FROM ids WHERE id = ?", (key_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "记录不存在"
        
        if username and row["created_by"] != username:
            return False, "无权删除此记录"
        
        if not row["redeemed_card"]:
            return False, "记录不存在"
        
        cursor.execute("UPDATE ids SET redeemed_card = NULL WHERE id = ?", (key_id,))
        return True, None


def delete_all_records(username=None):
    """
    删除用户的所有兑换记录
    
    Args:
        username: 当前用户名
        
    Returns:
        int: 删除数量
    """
    _init_db()
    with _get_cursor() as cursor:
        if username:
            cursor.execute('''
                SELECT COUNT(*) FROM ids 
                WHERE used = 1 AND redeemed_card IS NOT NULL AND created_by = ?
            ''', (username,))
            count = cursor.fetchone()[0]
            cursor.execute('''
                UPDATE ids SET redeemed_card = NULL 
                WHERE used = 1 AND redeemed_card IS NOT NULL AND created_by = ?
            ''', (username,))
        else:
            cursor.execute('''
                SELECT COUNT(*) FROM ids 
                WHERE used = 1 AND redeemed_card IS NOT NULL
            ''')
            count = cursor.fetchone()[0]
            cursor.execute('''
                UPDATE ids SET redeemed_card = NULL 
                WHERE used = 1 AND redeemed_card IS NOT NULL
            ''')
        
        return count


def get_stats():
    """
    获取统计信息
    
    Returns:
        dict: 统计数据
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM ids")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM ids WHERE used = 0")
        unused = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM ids WHERE used = 1")
        used = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM ids WHERE used = 1 AND redeemed_card IS NOT NULL")
        redeemed = cursor.fetchone()[0]
        
        return {
            "total": total,
            "unused": unused,
            "used": used,
            "redeemed": redeemed
        }


# 测试
if __name__ == "__main__":
    print("数据库统计:", get_stats())
    
    # 生成 3 个有效期 60 分钟的卡密
    ids = generate_ids(3, 60)
    print("\n生成的卡密:")
    for item in ids:
        print(f"  {item['id']} - {item['expire_minutes']}分钟")
    
    # 验证第一个卡密
    if ids:
        valid, result = validate_id(ids[0]["id"])
        print(f"\n验证卡密: {valid}, {result}")
        
        # 使用卡密
        success, result = use_id(ids[0]["id"])
        print(f"使用卡密: {success}, {result}")
        
        # 再次验证
        valid, result = validate_id(ids[0]["id"])
        print(f"再次验证: {valid}, {result}")
    
    print("\n更新后统计:", get_stats())
