"""
卡密管理模块 - PostgreSQL 版本（兼容 SQLite）

通过 db_config.py 中的 USE_SQLITE 变量控制使用哪个数据库。
"""

import uuid
import json
import os
import threading
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

# 导入配置 - 兼容包导入和 importlib 直接加载
try:
    from .db_config import DB_CONFIG, POOL_MIN_CONN, POOL_MAX_CONN, USE_SQLITE
except ImportError:
    # 当通过 importlib 直接加载时，使用绝对路径导入
    import importlib.util
    _db_config_path = os.path.join(os.path.dirname(__file__), 'db_config.py')
    _spec = importlib.util.spec_from_file_location("db_config", _db_config_path)
    _db_config_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_db_config_module)
    DB_CONFIG = _db_config_module.DB_CONFIG
    POOL_MIN_CONN = _db_config_module.POOL_MIN_CONN
    POOL_MAX_CONN = _db_config_module.POOL_MAX_CONN
    USE_SQLITE = _db_config_module.USE_SQLITE

# 数据库文件路径（SQLite）
DB_FILE = os.path.join(os.path.dirname(__file__), "id.db")
# 旧的 JSON 文件路径（用于迁移）
OLD_JSON_FILE = os.path.join(os.path.dirname(__file__), "id.json")

# 全局变量
_local = threading.local()
_init_lock = threading.Lock()
_pool_lock = threading.Lock()  # 新增：单独的连接池锁
_db_initialized = False
_pg_pool = None

if USE_SQLITE:
    import sqlite3
else:
    import psycopg2
    from psycopg2 import pool as pg_pool
    from psycopg2.extras import RealDictCursor


def _get_connection():
    """获取数据库连接"""
    global _pg_pool
    
    if USE_SQLITE:
        # SQLite: 每线程一个连接
        if not hasattr(_local, 'conn') or _local.conn is None:
            _local.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            _local.conn.row_factory = sqlite3.Row
            _local.conn.execute("PRAGMA journal_mode=WAL")
            _local.conn.execute("PRAGMA synchronous=NORMAL")
        return _local.conn
    else:
        # PostgreSQL: 使用连接池
        if _pg_pool is None:
            with _pool_lock:  # 使用单独的锁，避免死锁
                if _pg_pool is None:
                    _pg_pool = pg_pool.ThreadedConnectionPool(
                        minconn=POOL_MIN_CONN,
                        maxconn=POOL_MAX_CONN,
                        **DB_CONFIG
                    )
        return _pg_pool.getconn()


def _release_connection(conn):
    """释放连接回连接池（仅 PostgreSQL 需要）"""
    global _pg_pool
    if not USE_SQLITE and _pg_pool is not None:
        _pg_pool.putconn(conn)


def reload_db_config():
    """热重载数据库配置（重新创建连接池）"""
    global _pg_pool, DB_CONFIG, POOL_MIN_CONN, POOL_MAX_CONN
    
    if USE_SQLITE:
        return True, "SQLite 模式无需重载"
    
    try:
        # 重新读取配置
        from .db_config import _load_settings
        new_config = _load_settings()
        
        if not new_config:
            return False, "无法读取数据库配置"
        
        new_db_config = {
            "host": new_config.get("host", "localhost"),
            "port": int(new_config.get("port", 5432)),
            "database": new_config.get("database", "mercury"),
            "user": new_config.get("user", "mercury"),
            "password": new_config.get("password", ""),
            "connect_timeout": 10
        }
        new_pool_min = int(new_config.get("pool_min", 2))
        new_pool_max = int(new_config.get("pool_max", 20))
        
        with _pool_lock:
            # 关闭旧连接池
            if _pg_pool is not None:
                try:
                    _pg_pool.closeall()
                except:
                    pass
            
            # 创建新连接池
            _pg_pool = pg_pool.ThreadedConnectionPool(
                minconn=new_pool_min,
                maxconn=new_pool_max,
                **new_db_config
            )
            
            # 更新全局配置
            DB_CONFIG.update(new_db_config)
            POOL_MIN_CONN = new_pool_min
            POOL_MAX_CONN = new_pool_max
        
        return True, "数据库配置已重载"
    except Exception as e:
        return False, f"重载失败: {str(e)}"


@contextmanager
def _get_cursor():
    """获取数据库游标的上下文管理器"""
    conn = _get_connection()
    if USE_SQLITE:
        cursor = conn.cursor()
    else:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        if not USE_SQLITE:
            _release_connection(conn)


def _param(index=None):
    """返回参数占位符：SQLite 用 '?'，PostgreSQL 用 '%s'"""
    return "?" if USE_SQLITE else "%s"


def _params(count):
    """返回多个参数占位符"""
    placeholder = "?" if USE_SQLITE else "%s"
    return ",".join([placeholder] * count)


def _init_db():
    """初始化数据库表"""
    global _db_initialized
    if _db_initialized:
        return
    
    with _init_lock:
        if _db_initialized:
            return
        
        with _get_cursor() as cursor:
            if USE_SQLITE:
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
                        hidden_note TEXT,
                        pan TEXT
                    )
                ''')
                
                # 添加列（如果不存在）
                for col, default in [
                    ("destroyed", "INTEGER DEFAULT 0"),
                    ("destroyed_time", "TEXT"),
                    ("hidden", "INTEGER DEFAULT 0"),
                    ("hidden_token", "TEXT"),
                    ("hidden_note", "TEXT"),
                    ("pan", "TEXT")
                ]:
                    try:
                        cursor.execute(f"ALTER TABLE ids ADD COLUMN {col} {default}")
                    except:
                        pass
                
                # 创建索引
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_used ON ids(used)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_by ON ids(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_time ON ids(created_time)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_pan ON ids(pan)')
            else:
                # PostgreSQL
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS ids (
                        id TEXT PRIMARY KEY,
                        expire_minutes INTEGER NOT NULL,
                        card_limit NUMERIC DEFAULT 0,
                        card_type TEXT DEFAULT 'credit',
                        created_time TEXT NOT NULL,
                        used BOOLEAN DEFAULT FALSE,
                        used_time TEXT,
                        created_by TEXT,
                        redeemed_card TEXT,
                        destroyed BOOLEAN DEFAULT FALSE,
                        destroyed_time TEXT,
                        hidden BOOLEAN DEFAULT FALSE,
                        hidden_token TEXT,
                        hidden_note TEXT,
                        pan TEXT
                    )
                ''')
                
                # 添加 pan 列（如果不存在）- 先检查再添加，避免事务中止
                cursor.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'ids' AND column_name = 'pan'
                """)
                if cursor.fetchone() is None:
                    cursor.execute("ALTER TABLE ids ADD COLUMN pan TEXT")
                
                # 创建索引
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_used ON ids(used)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_by ON ids(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_time ON ids(created_time)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_pan ON ids(pan)')
        
        _db_initialized = True
        
        # 仅 SQLite 需要检查 JSON 迁移
        if USE_SQLITE:
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
            row = cursor.fetchone()
            count = row[0] if USE_SQLITE else row['count']
            if count > 0:
                print(f"[卡密] 数据库已有 {count} 条记录，跳过迁移")
                return
        
        # 批量插入数据
        with _get_cursor() as cursor:
            for item in ids_list:
                redeemed_card = item.get("redeemed_card")
                redeemed_card_json = json.dumps(redeemed_card, ensure_ascii=False) if redeemed_card else None
                
                cursor.execute(f'''
                    INSERT INTO ids 
                    (id, expire_minutes, card_limit, card_type, created_time, used, used_time, created_by, redeemed_card)
                    VALUES ({_params(9)})
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


def _get_row_value(row, key, default=None):
    """统一获取行数据的值（兼容 SQLite Row 和 PostgreSQL RealDictCursor）"""
    if row is None:
        return default
    if USE_SQLITE:
        try:
            return row[key]
        except (KeyError, IndexError):
            return default
    else:
        return row.get(key, default)


def _row_to_dict(row):
    """将数据库行转换为字典"""
    if row is None:
        return None
    
    redeemed_card = None
    redeemed_card_raw = _get_row_value(row, "redeemed_card")
    if redeemed_card_raw:
        try:
            redeemed_card = json.loads(redeemed_card_raw)
        except:
            pass
    
    used = _get_row_value(row, "used")
    destroyed = _get_row_value(row, "destroyed")
    hidden = _get_row_value(row, "hidden")
    
    return {
        "id": _get_row_value(row, "id"),
        "expire_minutes": _get_row_value(row, "expire_minutes"),
        "card_limit": _get_row_value(row, "card_limit"),
        "card_type": _get_row_value(row, "card_type"),
        "created_time": _get_row_value(row, "created_time"),
        "used": bool(used) if used is not None else False,
        "used_time": _get_row_value(row, "used_time"),
        "created_by": _get_row_value(row, "created_by"),
        "redeemed_card": redeemed_card,
        "destroyed": bool(destroyed) if destroyed is not None else False,
        "destroyed_time": _get_row_value(row, "destroyed_time"),
        "hidden": bool(hidden) if hidden is not None else False,
        "hidden_token": _get_row_value(row, "hidden_token"),
        "hidden_note": _get_row_value(row, "hidden_note")
    }




# 注意：数据库初始化已改为延迟加载
# 在每个数据库操作函数中会自动调用 _init_db()


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
            
            used_val = 1 if item.get("used") else 0
            if not USE_SQLITE:
                used_val = bool(item.get("used"))
            
            cursor.execute(f'''
                INSERT INTO ids 
                (id, expire_minutes, card_limit, card_type, created_time, used, used_time, created_by, redeemed_card)
                VALUES ({_params(9)})
            ''', (
                item.get("id"),
                item.get("expire_minutes", 60),
                item.get("card_limit", 0),
                item.get("card_type", "credit"),
                item.get("created_time"),
                used_val,
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
            raw_uuid = str(uuid.uuid4())
            prefix = '5236' if card_type == 'credit' else '5481'
            typed_uuid = prefix + raw_uuid[4:]
            
            hidden_val = 1 if hidden else 0
            if not USE_SQLITE:
                hidden_val = bool(hidden)
            
            cursor.execute(f'''
                INSERT INTO ids 
                (id, expire_minutes, card_limit, card_type, created_time, used, created_by, hidden, hidden_token, hidden_note)
                VALUES ({_params(10)})
            ''', (typed_uuid, expire_minutes, card_limit, card_type, now_iso, False if not USE_SQLITE else 0, created_by, hidden_val, hidden_token, hidden_note))
            
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


def record_direct_card_creation(card_id, card_type, card_limit, created_by, account_email=None, account_user_id=None, card_details=None, expire_minutes=60, expire_time=None):
    """
    记录通过"创建卡片"模块直接创建的卡片到数据库，用于分析统计
    """
    _init_db()
    now_iso = _utc_now_iso()
    
    raw_uuid = str(uuid.uuid4())
    prefix = '5236' if card_type == 'credit' else '5481'
    typed_uuid = prefix + raw_uuid[4:]
    
    redeemed_card = {
        "card_id": card_id,
        "card_type": card_type,
        "account_email": account_email,
        "account_user_id": account_user_id
    }
    if card_details:
        redeemed_card.update({
            "pan": card_details.get("pan", ""),
            "cvv": card_details.get("cvv", ""),
            "exp_month": card_details.get("exp_month", ""),
            "exp_year": card_details.get("exp_year", "")
        })
    if expire_time:
        redeemed_card["expire_time"] = expire_time
    
    # 提取 pan（纯数字卡号）
    pan_value = None
    if card_details:
        raw_pan = card_details.get("pan", "")
        pan_value = ''.join(c for c in str(raw_pan) if c.isdigit()) or None
    
    try:
        with _get_cursor() as cursor:
            used_val = True if not USE_SQLITE else 1
            hidden_val = True if not USE_SQLITE else 1
            
            cursor.execute(f'''
                INSERT INTO ids 
                (id, expire_minutes, card_limit, card_type, created_time, used, used_time, created_by, redeemed_card, hidden, hidden_note, pan)
                VALUES ({_params(12)})
            ''', (
                typed_uuid,
                expire_minutes,
                card_limit,
                card_type,
                now_iso,
                used_val,
                now_iso,
                created_by,
                json.dumps(redeemed_card, ensure_ascii=False),
                hidden_val,
                "直接创建",
                pan_value
            ))
        return True, typed_uuid
    except Exception as e:
        return False, str(e)


def validate_id(card_id):
    """
    验证卡密是否有效
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if _get_row_value(row, "destroyed"):
            return False, "卡密已被销毁"
        
        if _get_row_value(row, "used"):
            return False, "卡密已被使用"
        
        return True, {
            "expire_minutes": _get_row_value(row, "expire_minutes"),
            "card_limit": _get_row_value(row, "card_limit"),
            "card_type": _get_row_value(row, "card_type")
        }


def use_id(card_id, card_info=None):
    """
    使用卡密（标记为已使用）
    """
    _init_db()
    now_iso = _utc_now_iso()
    redeemed_card_json = json.dumps(card_info, ensure_ascii=False) if card_info else None
    
    # 提取 pan（纯数字卡号）
    pan_value = None
    if card_info and isinstance(card_info, dict):
        raw_pan = card_info.get("pan", "")
        pan_value = ''.join(c for c in str(raw_pan) if c.isdigit()) or None
    
    with _get_cursor() as cursor:
        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if _get_row_value(row, "used"):
            return False, "卡密已被使用"
        
        used_val = True if not USE_SQLITE else 1
        if USE_SQLITE:
            cursor.execute(f'''
                UPDATE ids SET used = {_param()}, used_time = {_param()}, redeemed_card = {_param()}, pan = {_param()}
                WHERE id = {_param()} AND used = 0
            ''', (used_val, now_iso, redeemed_card_json, pan_value, card_id))
        else:
            cursor.execute(f'''
                UPDATE ids SET used = {_param()}, used_time = {_param()}, redeemed_card = {_param()}, pan = {_param()}
                WHERE id = {_param()} AND used = FALSE
            ''', (used_val, now_iso, redeemed_card_json, pan_value, card_id))
        
        if cursor.rowcount == 0:
            return False, "卡密已被使用"
        
        return True, _get_row_value(row, "expire_minutes")


def allocate_existing_ids_for_withdraw(token, note, username, card_type, count):
    """
    优先提取现有未使用的卡密并标记为隐藏，用于提卡链接
    """
    if count <= 0:
        return []

    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            conditions = [
                "used = 0",
                "(destroyed = 0 OR destroyed IS NULL)",
                "(hidden = 0 OR hidden IS NULL)",
                f"card_type = {_param()}"
            ]
        else:
            conditions = [
                "used = FALSE",
                "(destroyed = FALSE OR destroyed IS NULL)",
                "(hidden = FALSE OR hidden IS NULL)",
                f"card_type = {_param()}"
            ]
        params = [card_type]

        if username:
            conditions.append(f"created_by = {_param()}")
            params.append(username)

        where_clause = " AND ".join(conditions)
        params.append(count)

        cursor.execute(
            f"""
            SELECT * FROM ids
            WHERE {where_clause}
            ORDER BY created_time ASC
            LIMIT {_param()}
            """,
            params
        )
        rows = cursor.fetchall()

        if not rows:
            return []

        ids = [_get_row_value(row, "id") for row in rows]
        placeholders = _params(len(ids))
        
        hidden_val = True if not USE_SQLITE else 1
        update_params = [hidden_val, token, note, *ids]

        cursor.execute(
            f"UPDATE ids SET hidden = {_param()}, hidden_token = {_param()}, hidden_note = {_param()} WHERE id IN ({placeholders})",
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
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if not _get_row_value(row, "used"):
            return False, "卡密未使用"
        
        redeemed_card_raw = _get_row_value(row, "redeemed_card")
        if not redeemed_card_raw:
            return False, "卡片信息已被删除"
        
        try:
            card = json.loads(redeemed_card_raw)
        except:
            return False, "卡片信息解析失败"
        
        used_time = _normalize_iso_to_utc(_get_row_value(row, "used_time"))
        expire_time = _normalize_iso_to_utc(card.get("expire_time")) if isinstance(card, dict) else None
        if isinstance(card, dict) and expire_time:
            card = dict(card)
            card["expire_time"] = expire_time
        
        return True, {
            "card": card,
            "expire_minutes": _get_row_value(row, "expire_minutes"),
            "card_limit": _get_row_value(row, "card_limit"),
            "used_time": used_time,
            "destroyed": bool(_get_row_value(row, "destroyed")),
            "destroyed_time": _get_row_value(row, "destroyed_time")
        }


def query_by_pan(pan):
    """
    通过卡号（PAN）查询已兑换卡密的卡片信息
    使用 pan 索引进行快速查询
    """
    clean_pan = ''.join(c for c in pan if c.isdigit())
    
    if len(clean_pan) < 12:
        return False, "卡号格式无效"
    
    _init_db()
    with _get_cursor() as cursor:
        # 使用 pan 索引进行快速查询
        if USE_SQLITE:
            cursor.execute(f"""
                SELECT * FROM ids 
                WHERE pan = {_param()} AND used = 1 AND redeemed_card IS NOT NULL
                ORDER BY used_time DESC
                LIMIT 1
            """, (clean_pan,))
        else:
            cursor.execute(f"""
                SELECT * FROM ids 
                WHERE pan = {_param()} AND used = TRUE AND redeemed_card IS NOT NULL
                ORDER BY used_time DESC
                LIMIT 1
            """, (clean_pan,))
        
        row = cursor.fetchone()
        
        if row is None:
            return False, "未找到对应的卡片"
        
        try:
            card = json.loads(_get_row_value(row, "redeemed_card"))
            used_time = _normalize_iso_to_utc(_get_row_value(row, "used_time"))
            expire_time = _normalize_iso_to_utc(card.get("expire_time")) if isinstance(card, dict) else None
            if isinstance(card, dict) and expire_time:
                card = dict(card)
                card["expire_time"] = expire_time
            
            return True, {
                "key_id": _get_row_value(row, "id"),
                "card": card,
                "expire_minutes": _get_row_value(row, "expire_minutes"),
                "card_limit": _get_row_value(row, "card_limit"),
                "used_time": used_time,
                "destroyed": bool(_get_row_value(row, "destroyed")),
                "destroyed_time": _get_row_value(row, "destroyed_time")
            }
        except:
            return False, "卡片信息解析失败"


def delete_id(card_id, username=None):
    """
    删除卡密
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if _get_row_value(row, "created_by") != username:
            return False, "无权删除此卡密"
        
        cursor.execute(f"DELETE FROM ids WHERE id = {_param()}", (card_id,))
        return True, None


def mark_destroyed(card_id, username=None, is_admin=False):
    """
    标记卡密为已销毁（不删除记录）
    """
    _init_db()
    now_iso = _utc_now_iso()
    
    with _get_cursor() as cursor:
        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "卡密不存在"
        
        if username and not is_admin and _get_row_value(row, "created_by") != username:
            return False, "无权操作此卡密"
        
        destroyed_val = True if not USE_SQLITE else 1
        cursor.execute(
            f"UPDATE ids SET destroyed = {_param()}, destroyed_time = {_param()} WHERE id = {_param()}",
            (destroyed_val, now_iso, card_id)
        )
        return True, None


def get_all_ids(username=None, page=None, page_size=100):
    """
    获取卡密列表（只返回未使用且未销毁的卡密）
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            base_condition = "used = 0 AND (destroyed = 0 OR destroyed IS NULL) AND (hidden = 0 OR hidden IS NULL)"
        else:
            base_condition = "used = FALSE AND (destroyed = FALSE OR destroyed IS NULL) AND (hidden = FALSE OR hidden IS NULL)"
        
        # 获取总数
        if username:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {base_condition} AND created_by = {_param()}", (username,))
        else:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {base_condition}")
        
        count_row = cursor.fetchone()
        total = count_row[0] if USE_SQLITE else count_row['cnt']
        
        # 分页查询
        if page is not None and page > 0:
            offset = (page - 1) * page_size
            if username:
                cursor.execute(
                    f"SELECT * FROM ids WHERE {base_condition} AND created_by = {_param()} ORDER BY created_time DESC LIMIT {_param()} OFFSET {_param()}",
                    (username, page_size, offset)
                )
            else:
                cursor.execute(f"SELECT * FROM ids WHERE {base_condition} ORDER BY created_time DESC LIMIT {_param()} OFFSET {_param()}", (page_size, offset))
        else:
            if username:
                cursor.execute(
                    f"SELECT * FROM ids WHERE {base_condition} AND created_by = {_param()} ORDER BY created_time DESC",
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
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            base_condition = "used = 0 AND (destroyed = 0 OR destroyed IS NULL) AND (hidden = 0 OR hidden IS NULL)"
        else:
            base_condition = "used = FALSE AND (destroyed = FALSE OR destroyed IS NULL) AND (hidden = FALSE OR hidden IS NULL)"
        params = []
        
        if username:
            base_condition += f" AND created_by = {_param()}"
            params.append(username)
        
        if card_type:
            base_condition += f" AND card_type = {_param()}"
            params.append(card_type)
        
        cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {base_condition}", params)
        row = cursor.fetchone()
        return row[0] if USE_SQLITE else row['cnt']


def delete_all_ids(username=None):
    """
    删除当前用户的所有卡密
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            hidden_cond = "(hidden = 0 OR hidden IS NULL)"
        else:
            hidden_cond = "(hidden = FALSE OR hidden IS NULL)"
        
        if username:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE created_by = {_param()} AND {hidden_cond}", (username,))
            row = cursor.fetchone()
            count = row[0] if USE_SQLITE else row['cnt']
            cursor.execute(f"DELETE FROM ids WHERE created_by = {_param()} AND {hidden_cond}", (username,))
        else:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {hidden_cond}")
            row = cursor.fetchone()
            count = row[0] if USE_SQLITE else row['cnt']
            cursor.execute(f"DELETE FROM ids WHERE {hidden_cond}")
        
        return count


def delete_ids_batch(id_list, username=None):
    """
    批量删除卡密（高性能）
    """
    if not id_list:
        return 0
    
    _init_db()
    with _get_cursor() as cursor:
        placeholders = _params(len(id_list))
        if username:
            cursor.execute(
                f"DELETE FROM ids WHERE id IN ({placeholders}) AND created_by = {_param()}",
                (*id_list, username)
            )
        else:
            cursor.execute(
                f"DELETE FROM ids WHERE id IN ({placeholders})",
                id_list
            )
        
        return cursor.rowcount


def delete_unused_ids_by_type(card_type=None, username=None):
    """
    删除未使用的卡密（按类型过滤）
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            conditions = ["used = 0", "(hidden = 0 OR hidden IS NULL)"]
        else:
            conditions = ["used = FALSE", "(hidden = FALSE OR hidden IS NULL)"]
        params = []
        
        if username:
            conditions.append(f"created_by = {_param()}")
            params.append(username)
        
        if card_type:
            conditions.append(f"card_type = {_param()}")
            params.append(card_type)
        
        where_clause = " AND ".join(conditions)
        
        cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {where_clause}", params)
        row = cursor.fetchone()
        count = row[0] if USE_SQLITE else row['cnt']
        
        cursor.execute(f"DELETE FROM ids WHERE {where_clause}", params)
        
        return count


def get_hidden_ids_by_token(token):
    """
    获取指定提卡链接下的卡密列表
    """
    if not token:
        return None
    
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            cursor.execute(
                f"SELECT * FROM ids WHERE hidden = 1 AND hidden_token = {_param()} ORDER BY created_time DESC",
                (token,)
            )
        else:
            cursor.execute(
                f"SELECT * FROM ids WHERE hidden = TRUE AND hidden_token = {_param()} ORDER BY created_time DESC",
                (token,)
            )
        rows = cursor.fetchall()
        
        if not rows:
            return None
        
        ids = []
        for row in rows:
            ids.append({
                "id": _get_row_value(row, "id"),
                "card_limit": _get_row_value(row, "card_limit"),
                "card_type": _get_row_value(row, "card_type"),
                "expire_minutes": _get_row_value(row, "expire_minutes"),
                "used": bool(_get_row_value(row, "used")),
                "used_time": _normalize_iso_to_utc(_get_row_value(row, "used_time")),
                "destroyed": bool(_get_row_value(row, "destroyed")),
                "destroyed_time": _get_row_value(row, "destroyed_time")
            })
        
        first = rows[0]
        return {
            "token": token,
            "note": _get_row_value(first, "hidden_note"),
            "created_time": _normalize_iso_to_utc(_get_row_value(first, "created_time")),
            "created_by": _get_row_value(first, "created_by"),
            "card_type": _get_row_value(first, "card_type"),
            "count": len(ids),
            "ids": ids
        }


def get_redeem_records(username=None, page=None, page_size=100):
    """
    获取开卡记录（已使用的卡密）
    支持分页查询
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            used_cond = "used = 1"
        else:
            used_cond = "used = TRUE"
        
        base_condition = f"{used_cond} AND redeemed_card IS NOT NULL"
        
        # 获取总数
        if username:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {base_condition} AND created_by = {_param()}", (username,))
        else:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {base_condition}")
        
        count_row = cursor.fetchone()
        total = count_row[0] if USE_SQLITE else count_row['cnt']
        
        # 分页查询
        if page is not None and page > 0:
            offset = (page - 1) * page_size
            if username:
                cursor.execute(f'''
                    SELECT * FROM ids 
                    WHERE {base_condition} AND created_by = {_param()}
                    ORDER BY used_time DESC
                    LIMIT {_param()} OFFSET {_param()}
                ''', (username, page_size, offset))
            else:
                cursor.execute(f'''
                    SELECT * FROM ids 
                    WHERE {base_condition}
                    ORDER BY used_time DESC
                    LIMIT {_param()} OFFSET {_param()}
                ''', (page_size, offset))
        else:
            if username:
                cursor.execute(f'''
                    SELECT * FROM ids 
                    WHERE {base_condition} AND created_by = {_param()}
                    ORDER BY used_time DESC
                ''', (username,))
            else:
                cursor.execute(f'''
                    SELECT * FROM ids 
                    WHERE {base_condition}
                    ORDER BY used_time DESC
                ''')
        
        rows = cursor.fetchall()
        records = []
        
        for row in rows:
            try:
                redeemed_card_raw = _get_row_value(row, "redeemed_card")
                card = json.loads(redeemed_card_raw) if redeemed_card_raw else {}
            except:
                card = {}
            
            used_time = _normalize_iso_to_utc(_get_row_value(row, "used_time"))
            expire_time = _normalize_iso_to_utc(card.get("expire_time")) if isinstance(card, dict) else None
            
            hidden_note = _get_row_value(row, "hidden_note")
            expire_minutes = _get_row_value(row, "expire_minutes")
            raw_used_time = _get_row_value(row, "used_time")
            
            if not expire_time and raw_used_time and expire_minutes:
                try:
                    if raw_used_time.endswith('Z'):
                        raw_used_time = raw_used_time[:-1] + '+00:00'
                    used_dt = datetime.fromisoformat(raw_used_time)
                    if used_dt.tzinfo is None:
                        used_dt = used_dt.replace(tzinfo=timezone.utc)
                    expire_dt = used_dt + timedelta(minutes=expire_minutes)
                    expire_time = expire_dt.isoformat()
                except Exception:
                    pass
            
            if isinstance(card, dict) and expire_time:
                card = dict(card)
                card["expire_time"] = expire_time
            
            records.append({
                "key_id": _get_row_value(row, "id"),
                "card_limit": _get_row_value(row, "card_limit"),
                "card_type": _get_row_value(row, "card_type"),
                "expire_minutes": expire_minutes,
                "created_by": _get_row_value(row, "created_by"),
                "used_time": used_time,
                "expire_time": expire_time,
                "card": card,
                "destroyed": bool(_get_row_value(row, "destroyed")),
                "destroyed_time": _get_row_value(row, "destroyed_time"),
                "is_direct_creation": hidden_note == "直接创建"
            })
        
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1
        
        return {
            "records": records,
            "total": total,
            "page": page or 1,
            "page_size": page_size,
            "total_pages": total_pages
        }


def delete_record(key_id, username=None):
    """
    删除单条开卡记录（清除卡片信息）
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (key_id,))
        row = cursor.fetchone()
        
        if row is None:
            return False, "记录不存在"
        
        if username and _get_row_value(row, "created_by") != username:
            return False, "无权删除此记录"
        
        if not _get_row_value(row, "redeemed_card"):
            return False, "记录不存在"
        
        cursor.execute(f"UPDATE ids SET redeemed_card = NULL WHERE id = {_param()}", (key_id,))
        return True, None


def delete_all_records(username=None):
    """
    删除用户的所有开卡记录
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            used_cond = "used = 1"
        else:
            used_cond = "used = TRUE"
        
        if username:
            cursor.execute(f'''
                SELECT COUNT(*) as cnt FROM ids 
                WHERE {used_cond} AND redeemed_card IS NOT NULL AND created_by = {_param()}
            ''', (username,))
            row = cursor.fetchone()
            count = row[0] if USE_SQLITE else row['cnt']
            cursor.execute(f'''
                UPDATE ids SET redeemed_card = NULL 
                WHERE {used_cond} AND redeemed_card IS NOT NULL AND created_by = {_param()}
            ''', (username,))
        else:
            cursor.execute(f'''
                SELECT COUNT(*) as cnt FROM ids 
                WHERE {used_cond} AND redeemed_card IS NOT NULL
            ''')
            row = cursor.fetchone()
            count = row[0] if USE_SQLITE else row['cnt']
            cursor.execute(f'''
                UPDATE ids SET redeemed_card = NULL 
                WHERE {used_cond} AND redeemed_card IS NOT NULL
            ''')
        
        return count


def get_stats():
    """
    获取统计信息
    """
    _init_db()
    with _get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) as cnt FROM ids")
        row = cursor.fetchone()
        total = row[0] if USE_SQLITE else row['cnt']
        
        if USE_SQLITE:
            cursor.execute("SELECT COUNT(*) as cnt FROM ids WHERE used = 0")
        else:
            cursor.execute("SELECT COUNT(*) as cnt FROM ids WHERE used = FALSE")
        row = cursor.fetchone()
        unused = row[0] if USE_SQLITE else row['cnt']
        
        if USE_SQLITE:
            cursor.execute("SELECT COUNT(*) as cnt FROM ids WHERE used = 1")
        else:
            cursor.execute("SELECT COUNT(*) as cnt FROM ids WHERE used = TRUE")
        row = cursor.fetchone()
        used = row[0] if USE_SQLITE else row['cnt']
        
        if USE_SQLITE:
            cursor.execute("SELECT COUNT(*) as cnt FROM ids WHERE used = 1 AND redeemed_card IS NOT NULL")
        else:
            cursor.execute("SELECT COUNT(*) as cnt FROM ids WHERE used = TRUE AND redeemed_card IS NOT NULL")
        row = cursor.fetchone()
        redeemed = row[0] if USE_SQLITE else row['cnt']
        
        return {
            "total": total,
            "unused": unused,
            "used": used,
            "redeemed": redeemed
        }


def get_analytics_data(start_date=None, end_date=None, username=None, tz_offset=None):
    """
    获取开卡分析数据（支持日期范围和用户时区）
    
    注意：PostgreSQL 使用不同的日期/时间函数
    """
    _init_db()
    
    # 计算用户本地时间的"今天"
    if tz_offset is not None:
        user_offset_seconds = -tz_offset * 60
        user_now = datetime.now(timezone.utc) + timedelta(seconds=user_offset_seconds)
        today = user_now.strftime("%Y-%m-%d")
        offset_hours = user_offset_seconds // 3600
        offset_minutes = abs(user_offset_seconds % 3600) // 60
        if user_offset_seconds >= 0:
            tz_modifier = f"+{offset_hours:02d}:{offset_minutes:02d}"
        else:
            tz_modifier = f"-{abs(offset_hours):02d}:{offset_minutes:02d}"
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        tz_modifier = "localtime" if USE_SQLITE else "+00:00"
    
    is_all_time = (start_date == '' and end_date == '') or (start_date is None and end_date is None)
    
    if is_all_time:
        start_date = ''
        end_date = ''
    elif not start_date and not end_date:
        start_date = today
        end_date = today
    elif start_date and not end_date:
        end_date = start_date
    elif end_date and not start_date:
        start_date = end_date
    
    with _get_cursor() as cursor:
        # 构建日期条件
        if USE_SQLITE:
            used_cond = "used = 1"
            if is_all_time:
                date_condition = "1=1"
                date_params = []
            elif start_date == end_date:
                date_condition = f"DATE(used_time, '{tz_modifier}') = {_param()}"
                date_params = [start_date]
            else:
                date_condition = f"DATE(used_time, '{tz_modifier}') BETWEEN {_param()} AND {_param()}"
                date_params = [start_date, end_date]
        else:
            used_cond = "used = TRUE"
            # PostgreSQL: 使用 AT TIME ZONE 或直接转换
            if is_all_time:
                date_condition = "1=1"
                date_params = []
            elif start_date == end_date:
                date_condition = f"DATE(used_time::timestamp + interval '{tz_modifier}') = {_param()}"
                date_params = [start_date]
            else:
                date_condition = f"DATE(used_time::timestamp + interval '{tz_modifier}') BETWEEN {_param()} AND {_param()}"
                date_params = [start_date, end_date]
        
        # 期间开卡总数
        cursor.execute(f"""
            SELECT COUNT(*) as cnt FROM ids 
            WHERE {used_cond}
            AND redeemed_card IS NOT NULL
            AND {date_condition}
        """, date_params)
        row = cursor.fetchone()
        today_total = row[0] if USE_SQLITE else row['cnt']
        
        # 期间信用卡数量
        cursor.execute(f"""
            SELECT COUNT(*) as cnt FROM ids 
            WHERE {used_cond}
            AND redeemed_card IS NOT NULL
            AND card_type = 'credit'
            AND {date_condition}
        """, date_params)
        row = cursor.fetchone()
        today_credit = row[0] if USE_SQLITE else row['cnt']
        
        # 期间借记卡数量
        cursor.execute(f"""
            SELECT COUNT(*) as cnt FROM ids 
            WHERE {used_cond}
            AND redeemed_card IS NOT NULL
            AND card_type = 'debit'
            AND {date_condition}
        """, date_params)
        row = cursor.fetchone()
        today_debit = row[0] if USE_SQLITE else row['cnt']
        
        # 每个用户期间的开卡数量
        cursor.execute(f"""
            SELECT 
                created_by,
                COUNT(*) as total,
                SUM(CASE WHEN card_type = 'credit' THEN 1 ELSE 0 END) as credit,
                SUM(CASE WHEN card_type = 'debit' THEN 1 ELSE 0 END) as debit
            FROM ids 
            WHERE {used_cond}
            AND redeemed_card IS NOT NULL
            AND {date_condition}
            GROUP BY created_by
            ORDER BY total DESC
        """, date_params)
        
        if USE_SQLITE:
            user_stats = [{
                "username": row[0] or "未知", 
                "total": row[1],
                "credit": row[2] or 0,
                "debit": row[3] or 0
            } for row in cursor.fetchall()]
        else:
            user_stats = [{
                "username": row['created_by'] or "未知", 
                "total": row['total'],
                "credit": row['credit'] or 0,
                "debit": row['debit'] or 0
            } for row in cursor.fetchall()]
        
        # 判断是否为单日查询
        is_single_day = start_date == end_date and start_date != ''
        
        if is_single_day:
            chart_type = "hourly"
            
            # 按小时统计 - SQLite 和 PostgreSQL 使用不同的函数
            hourly_credit = {h: 0 for h in range(24)}
            hourly_debit = {h: 0 for h in range(24)}
            
            if USE_SQLITE:
                hour_extract = f"CAST(strftime('%H', used_time, '{tz_modifier}') AS INTEGER)"
            else:
                hour_extract = f"EXTRACT(HOUR FROM used_time::timestamp + interval '{tz_modifier}')::INTEGER"
            
            # 信用卡小时统计
            query_params = date_params + ([username] if username else [])
            user_filter = f" AND created_by = {_param()}" if username else ""
            
            cursor.execute(f"""
                SELECT {hour_extract} as hour, COUNT(*) as count 
                FROM ids 
                WHERE {used_cond}
                AND redeemed_card IS NOT NULL
                AND card_type = 'credit'
                AND {date_condition}
                {user_filter}
                GROUP BY hour
                ORDER BY hour
            """, query_params)
            
            for row in cursor.fetchall():
                if USE_SQLITE:
                    hour = row[0]
                    cnt = row[1]
                else:
                    hour = row['hour']
                    cnt = row['count']
                if hour is not None:
                    hourly_credit[int(hour)] = cnt
            
            # 借记卡小时统计
            cursor.execute(f"""
                SELECT {hour_extract} as hour, COUNT(*) as count 
                FROM ids 
                WHERE {used_cond}
                AND redeemed_card IS NOT NULL
                AND card_type = 'debit'
                AND {date_condition}
                {user_filter}
                GROUP BY hour
                ORDER BY hour
            """, query_params)
            
            for row in cursor.fetchall():
                if USE_SQLITE:
                    hour = row[0]
                    cnt = row[1]
                else:
                    hour = row['hour']
                    cnt = row['count']
                if hour is not None:
                    hourly_debit[int(hour)] = cnt
            
            chart_credit = [{"label": f"{h:02d}:00", "count": hourly_credit[h]} for h in range(24)]
            chart_debit = [{"label": f"{h:02d}:00", "count": hourly_debit[h]} for h in range(24)]
        else:
            chart_type = "daily"
            
            # 按天统计
            if USE_SQLITE:
                date_extract = f"DATE(used_time, '{tz_modifier}')"
            else:
                date_extract = f"DATE(used_time::timestamp + interval '{tz_modifier}')"
            
            query_params = date_params + ([username] if username else [])
            user_filter = f" AND created_by = {_param()}" if username else ""
            
            # 信用卡天统计
            cursor.execute(f"""
                SELECT {date_extract} as day, COUNT(*) as count 
                FROM ids 
                WHERE {used_cond}
                AND redeemed_card IS NOT NULL
                AND card_type = 'credit'
                AND {date_condition}
                {user_filter}
                GROUP BY day
                ORDER BY day
            """, query_params)
            
            if USE_SQLITE:
                daily_credit = {row[0]: row[1] for row in cursor.fetchall()}
            else:
                daily_credit = {str(row['day']): row['count'] for row in cursor.fetchall()}
            
            # 借记卡天统计
            cursor.execute(f"""
                SELECT {date_extract} as day, COUNT(*) as count 
                FROM ids 
                WHERE {used_cond}
                AND redeemed_card IS NOT NULL
                AND card_type = 'debit'
                AND {date_condition}
                {user_filter}
                GROUP BY day
                ORDER BY day
            """, query_params)
            
            if USE_SQLITE:
                daily_debit = {row[0]: row[1] for row in cursor.fetchall()}
            else:
                daily_debit = {str(row['day']): row['count'] for row in cursor.fetchall()}
            
            # 合并所有日期
            all_days = sorted(set(list(daily_credit.keys()) + list(daily_debit.keys())))
            
            if all_days:
                if start_date and end_date:
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                else:
                    start_dt = datetime.strptime(all_days[0], "%Y-%m-%d")
                    end_dt = datetime.strptime(today, "%Y-%m-%d")
                
                current_dt = start_dt
                filled_days = []
                while current_dt <= end_dt:
                    filled_days.append(current_dt.strftime("%Y-%m-%d"))
                    current_dt += timedelta(days=1)
                all_days = filled_days
            
            chart_credit = [{"label": day, "count": daily_credit.get(day, 0)} for day in all_days]
            chart_debit = [{"label": day, "count": daily_debit.get(day, 0)} for day in all_days]
        
        return {
            "start_date": start_date,
            "end_date": end_date,
            "today_total": today_total,
            "today_credit": today_credit,
            "today_debit": today_debit,
            "user_stats": user_stats,
            "chart_type": chart_type,
            "chart_credit": chart_credit,
            "chart_debit": chart_debit,
            "selected_user": username
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
