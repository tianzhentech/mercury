"""
卡密管理模块 - PostgreSQL 版本（兼容 SQLite）

通过 db_config.py 中的 USE_SQLITE 变量控制使用哪个数据库。
"""

import uuid
import json
import os
import re
import threading
import time
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
_old_card_pool_sync_lock = threading.Lock()
_db_initialized = False
_pg_pool = None
_old_card_pool_last_sync_monotonic = 0.0
REDEEM_LOCK_TIMEOUT_SECONDS = 900
TIMOES_LOCK_TIMEOUT_SECONDS = 300
MANUAL_CARD_LOCK_TIMEOUT_SECONDS = 300
OLD_CARD_LOCK_TIMEOUT_SECONDS = 300
OLD_CARD_POOL_SYNC_MIN_INTERVAL_SECONDS = 10
TIMOES_CODE_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
TIMOES_CODE_TYPES = ("4866", "4513")
TIMOES_CODE_TYPE_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{1,31}$')
POOL_STATUS_TYPES = ("available", "used", "invalid")
MANUAL_CARD_PAN_PATTERN = re.compile(r'^\d{12,19}$')
MANUAL_CARD_CVV_PATTERN = re.compile(r'^\d{3,4}$')
UUID_PREFIX_PATTERN = re.compile(r'^\d{4,8}$')
KEY_KIND_TYPES = ("normal", "old_card")

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
                        key_kind TEXT DEFAULT 'normal',
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
                        pan TEXT,
                        bound_display_channel_id TEXT,
                        bound_display_channel_name TEXT,
                        bound_backend_channel_id TEXT,
                        bound_channel_head TEXT,
                        redeeming INTEGER DEFAULT 0,
                        redeeming_time TEXT
                    )
                ''')

                # 添加列（如果不存在）
                for col, default in [
                    ("destroyed", "INTEGER DEFAULT 0"),
                    ("destroyed_time", "TEXT"),
                    ("hidden", "INTEGER DEFAULT 0"),
                    ("hidden_token", "TEXT"),
                    ("hidden_note", "TEXT"),
                    ("pan", "TEXT"),
                    ("key_kind", "TEXT DEFAULT 'normal'"),
                    ("bound_display_channel_id", "TEXT"),
                    ("bound_display_channel_name", "TEXT"),
                    ("bound_backend_channel_id", "TEXT"),
                    ("bound_channel_head", "TEXT"),
                    ("redeeming", "INTEGER DEFAULT 0"),
                    ("redeeming_time", "TEXT")
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
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_redeeming ON ids(redeeming)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_key_kind ON ids(key_kind)')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS timoes_codes (
                        code TEXT PRIMARY KEY,
                        code_type TEXT NOT NULL,
                        created_time TEXT NOT NULL,
                        created_by TEXT,
                        status TEXT DEFAULT 'available',
                        used_time TEXT,
                        used_by_key TEXT,
                        redeemed_card TEXT,
                        last_error TEXT,
                        locked INTEGER DEFAULT 0,
                        locked_time TEXT,
                        lock_token TEXT
                    )
                ''')

                for col, default in [
                    ("status", "TEXT DEFAULT 'available'"),
                    ("used_time", "TEXT"),
                    ("used_by_key", "TEXT"),
                    ("redeemed_card", "TEXT"),
                    ("last_error", "TEXT"),
                    ("locked", "INTEGER DEFAULT 0"),
                    ("locked_time", "TEXT"),
                    ("lock_token", "TEXT")
                ]:
                    try:
                        cursor.execute(f"ALTER TABLE timoes_codes ADD COLUMN {col} {default}")
                    except:
                        pass

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_created_by ON timoes_codes(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_status ON timoes_codes(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_code_type ON timoes_codes(code_type)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_locked ON timoes_codes(locked)')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS manual_cards (
                        pan TEXT PRIMARY KEY,
                        bin_code TEXT NOT NULL,
                        exp_month TEXT NOT NULL,
                        exp_year TEXT NOT NULL,
                        cvv TEXT NOT NULL,
                        created_time TEXT NOT NULL,
                        created_by TEXT,
                        status TEXT DEFAULT 'available',
                        used_time TEXT,
                        used_by_key TEXT,
                        redeemed_card TEXT,
                        last_error TEXT,
                        locked INTEGER DEFAULT 0,
                        locked_time TEXT,
                        lock_token TEXT,
                        card_limit REAL,
                        expire_minutes INTEGER,
                        legal_address TEXT
                    )
                ''')

                for col, default in [
                    ("status", "TEXT DEFAULT 'available'"),
                    ("used_time", "TEXT"),
                    ("used_by_key", "TEXT"),
                    ("redeemed_card", "TEXT"),
                    ("last_error", "TEXT"),
                    ("locked", "INTEGER DEFAULT 0"),
                    ("locked_time", "TEXT"),
                    ("lock_token", "TEXT"),
                    ("card_limit", "REAL"),
                    ("expire_minutes", "INTEGER"),
                    ("legal_address", "TEXT")
                ]:
                    try:
                        cursor.execute(f"ALTER TABLE manual_cards ADD COLUMN {col} {default}")
                    except:
                        pass

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_created_by ON manual_cards(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_status ON manual_cards(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_bin_code ON manual_cards(bin_code)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_locked ON manual_cards(locked)')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS old_card_pool (
                        source_key_id TEXT PRIMARY KEY,
                        backend_channel_id TEXT NOT NULL,
                        channel_head TEXT,
                        provider TEXT,
                        provider_label TEXT,
                        pan TEXT,
                        expire_time TEXT,
                        source_used_time TEXT,
                        card_data TEXT NOT NULL,
                        status TEXT DEFAULT 'available',
                        used_time TEXT,
                        used_by_key TEXT,
                        last_error TEXT,
                        locked INTEGER DEFAULT 0,
                        locked_time TEXT,
                        lock_token TEXT
                    )
                ''')

                for col, default in [
                    ("backend_channel_id", "TEXT"),
                    ("channel_head", "TEXT"),
                    ("provider", "TEXT"),
                    ("provider_label", "TEXT"),
                    ("pan", "TEXT"),
                    ("expire_time", "TEXT"),
                    ("source_used_time", "TEXT"),
                    ("card_data", "TEXT"),
                    ("status", "TEXT DEFAULT 'available'"),
                    ("used_time", "TEXT"),
                    ("used_by_key", "TEXT"),
                    ("last_error", "TEXT"),
                    ("locked", "INTEGER DEFAULT 0"),
                    ("locked_time", "TEXT"),
                    ("lock_token", "TEXT"),
                ]:
                    try:
                        cursor.execute(f"ALTER TABLE old_card_pool ADD COLUMN {col} {default}")
                    except:
                        pass

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_backend ON old_card_pool(backend_channel_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_status ON old_card_pool(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_locked ON old_card_pool(locked)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_pan ON old_card_pool(pan)')
            else:
                # PostgreSQL
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS ids (
                        id TEXT PRIMARY KEY,
                        expire_minutes INTEGER NOT NULL,
                        card_limit NUMERIC DEFAULT 0,
                        card_type TEXT DEFAULT 'credit',
                        key_kind TEXT DEFAULT 'normal',
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
                        pan TEXT,
                        bound_display_channel_id TEXT,
                        bound_display_channel_name TEXT,
                        bound_backend_channel_id TEXT,
                        bound_channel_head TEXT,
                        redeeming BOOLEAN DEFAULT FALSE,
                        redeeming_time TEXT
                    )
                ''')

                # 添加列（如果不存在）- 先检查再添加，避免事务中止
                for col, ddl in [
                    ("pan", "TEXT"),
                    ("key_kind", "TEXT DEFAULT 'normal'"),
                    ("bound_display_channel_id", "TEXT"),
                    ("bound_display_channel_name", "TEXT"),
                    ("bound_backend_channel_id", "TEXT"),
                    ("bound_channel_head", "TEXT"),
                    ("redeeming", "BOOLEAN DEFAULT FALSE"),
                    ("redeeming_time", "TEXT"),
                ]:
                    cursor.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = 'ids' AND column_name = %s
                    """, (col,))
                    if cursor.fetchone() is None:
                        cursor.execute(f"ALTER TABLE ids ADD COLUMN {col} {ddl}")

                # 创建索引
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_used ON ids(used)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_by ON ids(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_time ON ids(created_time)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_pan ON ids(pan)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_redeeming ON ids(redeeming)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_key_kind ON ids(key_kind)')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS timoes_codes (
                        code TEXT PRIMARY KEY,
                        code_type TEXT NOT NULL,
                        created_time TEXT NOT NULL,
                        created_by TEXT,
                        status TEXT DEFAULT 'available',
                        used_time TEXT,
                        used_by_key TEXT,
                        redeemed_card TEXT,
                        last_error TEXT,
                        locked BOOLEAN DEFAULT FALSE,
                        locked_time TEXT,
                        lock_token TEXT
                    )
                ''')

                for col, ddl in [
                    ("status", "TEXT DEFAULT 'available'"),
                    ("used_time", "TEXT"),
                    ("used_by_key", "TEXT"),
                    ("redeemed_card", "TEXT"),
                    ("last_error", "TEXT"),
                    ("locked", "BOOLEAN DEFAULT FALSE"),
                    ("locked_time", "TEXT"),
                    ("lock_token", "TEXT"),
                ]:
                    cursor.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = 'timoes_codes' AND column_name = %s
                    """, (col,))
                    if cursor.fetchone() is None:
                        cursor.execute(f"ALTER TABLE timoes_codes ADD COLUMN {col} {ddl}")

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_created_by ON timoes_codes(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_status ON timoes_codes(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_code_type ON timoes_codes(code_type)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_timoes_locked ON timoes_codes(locked)')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS manual_cards (
                        pan TEXT PRIMARY KEY,
                        bin_code TEXT NOT NULL,
                        exp_month TEXT NOT NULL,
                        exp_year TEXT NOT NULL,
                        cvv TEXT NOT NULL,
                        created_time TEXT NOT NULL,
                        created_by TEXT,
                        status TEXT DEFAULT 'available',
                        used_time TEXT,
                        used_by_key TEXT,
                        redeemed_card TEXT,
                        last_error TEXT,
                        locked BOOLEAN DEFAULT FALSE,
                        locked_time TEXT,
                        lock_token TEXT,
                        card_limit NUMERIC,
                        expire_minutes INTEGER,
                        legal_address TEXT
                    )
                ''')

                for col, ddl in [
                    ("status", "TEXT DEFAULT 'available'"),
                    ("used_time", "TEXT"),
                    ("used_by_key", "TEXT"),
                    ("redeemed_card", "TEXT"),
                    ("last_error", "TEXT"),
                    ("locked", "BOOLEAN DEFAULT FALSE"),
                    ("locked_time", "TEXT"),
                    ("lock_token", "TEXT"),
                    ("card_limit", "NUMERIC"),
                    ("expire_minutes", "INTEGER"),
                    ("legal_address", "TEXT"),
                ]:
                    cursor.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = 'manual_cards' AND column_name = %s
                    """, (col,))
                    if cursor.fetchone() is None:
                        cursor.execute(f"ALTER TABLE manual_cards ADD COLUMN {col} {ddl}")

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_created_by ON manual_cards(created_by)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_status ON manual_cards(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_bin_code ON manual_cards(bin_code)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_cards_locked ON manual_cards(locked)')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS old_card_pool (
                        source_key_id TEXT PRIMARY KEY,
                        backend_channel_id TEXT NOT NULL,
                        channel_head TEXT,
                        provider TEXT,
                        provider_label TEXT,
                        pan TEXT,
                        expire_time TEXT,
                        source_used_time TEXT,
                        card_data TEXT NOT NULL,
                        status TEXT DEFAULT 'available',
                        used_time TEXT,
                        used_by_key TEXT,
                        last_error TEXT,
                        locked BOOLEAN DEFAULT FALSE,
                        locked_time TEXT,
                        lock_token TEXT
                    )
                ''')

                for col, ddl in [
                    ("backend_channel_id", "TEXT"),
                    ("channel_head", "TEXT"),
                    ("provider", "TEXT"),
                    ("provider_label", "TEXT"),
                    ("pan", "TEXT"),
                    ("expire_time", "TEXT"),
                    ("source_used_time", "TEXT"),
                    ("card_data", "TEXT"),
                    ("status", "TEXT DEFAULT 'available'"),
                    ("used_time", "TEXT"),
                    ("used_by_key", "TEXT"),
                    ("last_error", "TEXT"),
                    ("locked", "BOOLEAN DEFAULT FALSE"),
                    ("locked_time", "TEXT"),
                    ("lock_token", "TEXT"),
                ]:
                    cursor.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = 'old_card_pool' AND column_name = %s
                    """, (col,))
                    if cursor.fetchone() is None:
                        cursor.execute(f"ALTER TABLE old_card_pool ADD COLUMN {col} {ddl}")

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_backend ON old_card_pool(backend_channel_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_status ON old_card_pool(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_locked ON old_card_pool(locked)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_old_card_pool_pan ON old_card_pool(pan)')

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


def _parse_iso_datetime(value):
    normalized = _normalize_iso_to_utc(value)
    if not normalized:
        return None

    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


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


def _normalize_timoes_code_type(code_type, allowed_code_types=None, allow_unknown=False):
    value = str(code_type or "").strip().lower()
    if not TIMOES_CODE_TYPE_PATTERN.match(value):
        return None

    if allowed_code_types is None:
        return value if value in TIMOES_CODE_TYPES else (value if allow_unknown else None)

    normalized_allowed = {
        str(item or "").strip().lower()
        for item in (allowed_code_types or [])
        if str(item or "").strip()
    }
    if value in normalized_allowed:
        return value
    return value if allow_unknown else None


def _normalize_pool_status(value, allow_used=True):
    status = str(value or "").strip().lower()
    allowed = POOL_STATUS_TYPES if allow_used else ("available", "invalid")
    return status if status in allowed else None


def _normalize_key_kind(value, default="normal"):
    kind = str(value or "").strip().lower()
    return kind if kind in KEY_KIND_TYPES else default


def _normalize_manual_pan(value):
    pan = ''.join(ch for ch in str(value or "") if ch.isdigit())
    return pan if MANUAL_CARD_PAN_PATTERN.match(pan) else None


def _normalize_manual_bin_code(value):
    digits = ''.join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    if len(digits) >= 6:
        return digits[:6]
    return None


def _normalize_exp_month(value):
    digits = ''.join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return None
    month = int(digits)
    if month < 1 or month > 12:
        return None
    return f"{month:02d}"


def _normalize_exp_year(value):
    digits = ''.join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 2:
        digits = f"20{digits}"
    if len(digits) != 4:
        return None
    year = int(digits)
    if year < 2000 or year > 2099:
        return None
    return str(year)


def _parse_exp_token(token):
    token = str(token or "").strip()
    if not token:
        return None, None

    if "/" in token:
        parts = [part.strip() for part in token.split("/") if part.strip()]
        if len(parts) != 2:
            return None, None
        month_token, year_token = parts
    else:
        digits = ''.join(ch for ch in token if ch.isdigit())
        if len(digits) == 4:
            month_token, year_token = digits[:2], digits[2:]
        elif len(digits) == 6:
            month_token, year_token = digits[:2], digits[2:]
        else:
            return None, None

    month_digits = ''.join(ch for ch in month_token if ch.isdigit())
    year_digits = ''.join(ch for ch in year_token if ch.isdigit())
    if not month_digits or not year_digits:
        return None, None

    month = int(month_digits)
    if month < 1 or month > 12:
        return None, None

    if len(year_digits) == 2:
        year_digits = f"20{year_digits}"
    elif len(year_digits) != 4:
        return None, None

    return f"{month:02d}", year_digits


def _parse_manual_card_line(raw_line):
    line = str(raw_line or "").strip()
    if not line:
        return None, "空行"

    parts = [part.strip() for part in line.replace("\t", " ").split() if part.strip()]

    if len(parts) != 6:
        return None, "格式错误，应为：卡号 月份 年份 CVV 余额 有效期分钟"

    pan = _normalize_manual_pan(parts[0])
    if not pan:
        return None, "卡号格式错误"

    exp_month = _normalize_exp_month(parts[1])
    year_digits = ''.join(ch for ch in str(parts[2] or "") if ch.isdigit())
    if len(year_digits) != 4:
        return None, "年份格式错误"
    exp_year = _normalize_exp_year(year_digits)
    if not exp_month or not exp_year:
        return None, "有效期格式错误"

    cvv = ''.join(ch for ch in str(parts[3] or "") if ch.isdigit())
    if not MANUAL_CARD_CVV_PATTERN.match(cvv):
        return None, "CVV 格式错误"

    try:
        card_limit = float(parts[4])
    except (TypeError, ValueError):
        return None, "余额格式错误"
    if card_limit < 0:
        return None, "余额不能小于 0"

    try:
        expire_minutes = int(parts[5])
    except (TypeError, ValueError):
        return None, "有效期分钟格式错误"
    if expire_minutes <= 0:
        return None, "有效期分钟必须大于 0"

    return {
        "pan": pan,
        "bin_code": _normalize_manual_bin_code(pan),
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvv": cvv,
        "card_limit": card_limit,
        "expire_minutes": expire_minutes,
        "legal_address": {}
    }, None


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
    bound_display_channel_id = str(_get_row_value(row, "bound_display_channel_id") or "").strip().lower()
    bound_display_channel_name = str(_get_row_value(row, "bound_display_channel_name") or "").strip()
    bound_backend_channel_id = str(_get_row_value(row, "bound_backend_channel_id") or "").strip().lower()
    bound_channel_head = ''.join(ch for ch in str(_get_row_value(row, "bound_channel_head") or "") if ch.isdigit())
    channel_binding_enabled = bool(bound_display_channel_id and bound_backend_channel_id)
    key_kind = _normalize_key_kind(_get_row_value(row, "key_kind"), default="normal")

    return {
        "id": _get_row_value(row, "id"),
        "expire_minutes": _get_row_value(row, "expire_minutes"),
        "card_limit": _get_row_value(row, "card_limit"),
        "card_type": _get_row_value(row, "card_type"),
        "key_kind": key_kind,
        "created_time": _get_row_value(row, "created_time"),
        "used": bool(used) if used is not None else False,
        "used_time": _get_row_value(row, "used_time"),
        "created_by": _get_row_value(row, "created_by"),
        "redeemed_card": redeemed_card,
        "destroyed": bool(destroyed) if destroyed is not None else False,
        "destroyed_time": _get_row_value(row, "destroyed_time"),
        "hidden": bool(hidden) if hidden is not None else False,
        "hidden_token": _get_row_value(row, "hidden_token"),
        "hidden_note": _get_row_value(row, "hidden_note"),
        "bound_display_channel_id": bound_display_channel_id or None,
        "bound_display_channel_name": bound_display_channel_name or None,
        "bound_backend_channel_id": bound_backend_channel_id or None,
        "bound_channel_head": bound_channel_head or None,
        "channel_binding_enabled": channel_binding_enabled
    }


def _normalize_bound_channel(channel):
    if not isinstance(channel, dict):
        return None

    display_channel_id = str(
        channel.get("display_channel_id")
        or channel.get("id")
        or ""
    ).strip().lower()
    display_channel_name = str(
        channel.get("display_channel_name")
        or channel.get("name")
        or channel.get("label")
        or ""
    ).strip()
    backend_channel_id = str(channel.get("backend_channel_id") or "").strip().lower()
    channel_head = ''.join(
        ch for ch in str(channel.get("channel_head") or channel.get("backend_head") or "")
        if ch.isdigit()
    )

    if not display_channel_id or not backend_channel_id:
        return None

    return {
        "display_channel_id": display_channel_id,
        "display_channel_name": display_channel_name or display_channel_id,
        "backend_channel_id": backend_channel_id,
        "channel_head": channel_head or None
    }


def _apply_uuid_prefix(raw_uuid, prefix):
    normalized_prefix = ''.join(ch for ch in str(prefix or "") if ch.isdigit())
    if not UUID_PREFIX_PATTERN.match(normalized_prefix):
        return raw_uuid
    return normalized_prefix + raw_uuid[len(normalized_prefix):]


def _infer_backend_channel_id(provider, backend_channel_id=None, channel_head=None):
    normalized_backend = str(backend_channel_id or "").strip().lower()
    if normalized_backend:
        return normalized_backend

    head = ''.join(ch for ch in str(channel_head or "") if ch.isdigit())
    if not head:
        return None

    provider_name = str(provider or "").strip().lower()
    if provider_name == "mercury":
        return f"mercury_{head}"
    if provider_name == "timoes":
        return f"timoes_{head}"
    if provider_name == "manual":
        return f"manual_bin_{head}"
    return None


def _resolve_card_expire_time(card, row=None):
    card = card if isinstance(card, dict) else {}
    expire_time = _normalize_iso_to_utc(card.get("expire_time"))
    expire_dt = _parse_iso_datetime(expire_time)
    if expire_dt:
        return expire_time, expire_dt

    raw_used_time = _get_row_value(row, "used_time") if row is not None else None
    expire_minutes = card.get("expire_minutes")
    if expire_minutes in ("", None) and row is not None:
        expire_minutes = _get_row_value(row, "expire_minutes")
    try:
        expire_minutes = int(expire_minutes or 0)
    except (TypeError, ValueError):
        expire_minutes = 0

    if not raw_used_time or expire_minutes <= 0:
        return None, None

    used_dt = _parse_iso_datetime(raw_used_time)
    if not used_dt:
        return None, None

    expire_dt = used_dt + timedelta(minutes=expire_minutes)
    return expire_dt.isoformat(), expire_dt


def _extract_old_card_source(row, now_dt=None):
    if row is None:
        return None

    row_data = _row_to_dict(row)
    if not row_data:
        return None

    if row_data.get("key_kind") == "old_card":
        return None

    if row_data.get("hidden_note") == "直接创建":
        return None

    if row_data.get("destroyed"):
        return None

    card = row_data.get("redeemed_card")
    if not isinstance(card, dict) or not card:
        return None

    channel_head = ''.join(
        ch for ch in str(card.get("channel_head") or row_data.get("bound_channel_head") or "")
        if ch.isdigit()
    )
    pan = ''.join(ch for ch in str(card.get("pan") or "") if ch.isdigit()) or None
    provider = str(card.get("provider") or "").strip().lower()
    backend_channel_id = _infer_backend_channel_id(
        provider,
        backend_channel_id=card.get("backend_channel_id"),
        channel_head=channel_head
    )
    if not backend_channel_id:
        return None

    expire_time, expire_dt = _resolve_card_expire_time(card, row=row)
    if not expire_dt:
        return None

    now_dt = now_dt or datetime.now(timezone.utc)
    if expire_dt <= now_dt:
        return None

    normalized_card = dict(card)
    normalized_card["backend_channel_id"] = backend_channel_id
    if channel_head:
        normalized_card["channel_head"] = channel_head
    if pan:
        normalized_card["pan"] = pan
    if expire_time:
        normalized_card["expire_time"] = expire_time

    return {
        "source_key_id": row_data.get("id"),
        "backend_channel_id": backend_channel_id,
        "channel_head": channel_head or None,
        "provider": provider or None,
        "provider_label": str(card.get("provider_label") or "").strip() or None,
        "pan": pan,
        "expire_time": expire_time,
        "source_used_time": _normalize_iso_to_utc(row_data.get("used_time")),
        "card_data": normalized_card
    }


def _expire_old_card_pool_entries(cursor, now_dt=None):
    now_dt = now_dt or datetime.now(timezone.utc)
    locked_reset = 0 if USE_SQLITE else False
    invalid_reason = "旧卡已过期或记录已失效"
    cursor.execute(f"""
        UPDATE old_card_pool
        SET status = {_param()},
            last_error = {_param()},
            locked = {_param()},
            locked_time = NULL,
            lock_token = NULL
        WHERE status != {_param()}
          AND expire_time IS NOT NULL
          AND expire_time != ''
          AND expire_time <= {_param()}
    """, ("invalid", invalid_reason, locked_reset, "used", now_dt.isoformat()))
    return cursor.rowcount


def _upsert_old_card_pool_source(cursor, row, now_dt=None):
    source = _extract_old_card_source(row, now_dt=now_dt)
    if not source or not source.get("source_key_id"):
        return False

    source_key_id = source["source_key_id"]
    card_data_json = json.dumps(source.get("card_data") or {}, ensure_ascii=False)
    locked_reset = 0 if USE_SQLITE else False

    cursor.execute(f"""
        SELECT status FROM old_card_pool
        WHERE source_key_id = {_param()}
    """, (source_key_id,))
    existing = cursor.fetchone()
    current_status = _get_row_value(existing, "status") if existing is not None else None

    if current_status == "used":
        cursor.execute(f"""
            UPDATE old_card_pool
            SET backend_channel_id = {_param()},
                channel_head = {_param()},
                provider = {_param()},
                provider_label = {_param()},
                pan = {_param()},
                expire_time = {_param()},
                source_used_time = {_param()},
                card_data = {_param()}
            WHERE source_key_id = {_param()}
        """, (
            source.get("backend_channel_id"),
            source.get("channel_head"),
            source.get("provider"),
            source.get("provider_label"),
            source.get("pan"),
            source.get("expire_time"),
            source.get("source_used_time"),
            card_data_json,
            source_key_id
        ))
        return True

    if current_status:
        cursor.execute(f"""
            UPDATE old_card_pool
            SET backend_channel_id = {_param()},
                channel_head = {_param()},
                provider = {_param()},
                provider_label = {_param()},
                pan = {_param()},
                expire_time = {_param()},
                source_used_time = {_param()},
                card_data = {_param()},
                status = {_param()},
                used_time = NULL,
                used_by_key = NULL,
                last_error = NULL,
                locked = {_param()},
                locked_time = NULL,
                lock_token = NULL
            WHERE source_key_id = {_param()}
        """, (
            source.get("backend_channel_id"),
            source.get("channel_head"),
            source.get("provider"),
            source.get("provider_label"),
            source.get("pan"),
            source.get("expire_time"),
            source.get("source_used_time"),
            card_data_json,
            "available",
            locked_reset,
            source_key_id
        ))
        return True

    cursor.execute(f"""
        INSERT INTO old_card_pool
        (
            source_key_id,
            backend_channel_id,
            channel_head,
            provider,
            provider_label,
            pan,
            expire_time,
            source_used_time,
            card_data,
            status,
            locked
        )
        VALUES ({_params(11)})
    """, (
        source_key_id,
        source.get("backend_channel_id"),
        source.get("channel_head"),
        source.get("provider"),
        source.get("provider_label"),
        source.get("pan"),
        source.get("expire_time"),
        source.get("source_used_time"),
        card_data_json,
        "available",
        locked_reset
    ))
    return True


def _invalidate_old_card_pool_sources(cursor, source_key_ids, reason="来源卡片已删除或失效"):
    normalized_ids = sorted({
        str(item or "").strip()
        for item in (source_key_ids or [])
        if str(item or "").strip()
    })
    if not normalized_ids:
        return 0

    locked_reset = 0 if USE_SQLITE else False
    placeholders = _params(len(normalized_ids))
    cursor.execute(f"""
        UPDATE old_card_pool
        SET status = {_param()},
            last_error = {_param()},
            locked = {_param()},
            locked_time = NULL,
            lock_token = NULL
        WHERE source_key_id IN ({placeholders})
          AND status != {_param()}
    """, ("invalid", reason, locked_reset, *normalized_ids, "used"))
    return cursor.rowcount




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
                (id, expire_minutes, card_limit, card_type, key_kind, created_time, used, used_time, created_by, redeemed_card)
                VALUES ({_params(10)})
            ''', (
                item.get("id"),
                item.get("expire_minutes", 60),
                item.get("card_limit", 0),
                item.get("card_type", "credit"),
                _normalize_key_kind(item.get("key_kind"), default="normal"),
                item.get("created_time"),
                used_val,
                item.get("used_time"),
                item.get("created_by"),
                redeemed_card_json
            ))


def generate_ids(count, expire_minutes, card_limit=1, card_type="credit", created_by=None, hidden=False, hidden_token=None, hidden_note=None, bound_channel=None, key_kind="normal"):
    """
    生成卡密

    Args:
        count: 生成数量
        expire_minutes: 兑换后卡片有效时间（分钟）
        card_limit: 卡片余额（美元）
        card_type: 内部卡片类型标记
        created_by: 创建者用户名

    Returns:
        list: 生成的卡密列表
    """
    _init_db()
    generated = []
    now_iso = _utc_now_iso()
    normalized_bound_channel = _normalize_bound_channel(bound_channel)
    normalized_key_kind = _normalize_key_kind(key_kind, default="normal")

    with _get_cursor() as cursor:
        for _ in range(count):
            raw_uuid = str(uuid.uuid4())
            typed_uuid = _apply_uuid_prefix(
                raw_uuid,
                normalized_bound_channel.get("channel_head") if normalized_bound_channel else None
            )

            hidden_val = 1 if hidden else 0
            if not USE_SQLITE:
                hidden_val = bool(hidden)

            cursor.execute(f'''
                INSERT INTO ids
                (
                    id, expire_minutes, card_limit, card_type, key_kind, created_time, used, created_by,
                    hidden, hidden_token, hidden_note,
                    bound_display_channel_id, bound_display_channel_name, bound_backend_channel_id, bound_channel_head
                )
                VALUES ({_params(15)})
            ''', (
                typed_uuid,
                expire_minutes,
                card_limit,
                card_type,
                normalized_key_kind,
                now_iso,
                False if not USE_SQLITE else 0,
                created_by,
                hidden_val,
                hidden_token,
                hidden_note,
                normalized_bound_channel.get("display_channel_id") if normalized_bound_channel else None,
                normalized_bound_channel.get("display_channel_name") if normalized_bound_channel else None,
                normalized_bound_channel.get("backend_channel_id") if normalized_bound_channel else None,
                normalized_bound_channel.get("channel_head") if normalized_bound_channel else None
            ))

            generated.append({
                "id": typed_uuid,
                "expire_minutes": expire_minutes,
                "card_limit": card_limit,
                "card_type": card_type,
                "key_kind": normalized_key_kind,
                "created_time": now_iso,
                "used": False,
                "used_time": None,
                "created_by": created_by,
                "hidden": bool(hidden),
                "hidden_token": hidden_token,
                "hidden_note": hidden_note,
                "bound_display_channel_id": normalized_bound_channel.get("display_channel_id") if normalized_bound_channel else None,
                "bound_display_channel_name": normalized_bound_channel.get("display_channel_name") if normalized_bound_channel else None,
                "bound_backend_channel_id": normalized_bound_channel.get("backend_channel_id") if normalized_bound_channel else None,
                "bound_channel_head": normalized_bound_channel.get("channel_head") if normalized_bound_channel else None,
                "channel_binding_enabled": bool(normalized_bound_channel)
            })

    return generated


def record_direct_card_creation(card_id, card_type, card_limit, created_by, account_email=None, account_user_id=None, card_details=None, expire_minutes=60, expire_time=None, extra_card_info=None):
    """
    记录通过"创建卡片"模块直接创建的卡片到数据库，用于分析统计
    """
    _init_db()
    now_iso = _utc_now_iso()

    typed_uuid = str(uuid.uuid4())

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
    if isinstance(extra_card_info, dict):
        redeemed_card.update(extra_card_info)

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
                (
                    id, expire_minutes, card_limit, card_type, key_kind, created_time, used, used_time,
                    created_by, redeemed_card, hidden, hidden_note, pan,
                    bound_display_channel_id, bound_display_channel_name, bound_backend_channel_id, bound_channel_head
                )
                VALUES ({_params(17)})
            ''', (
                typed_uuid,
                expire_minutes,
                card_limit,
                card_type,
                "normal",
                now_iso,
                used_val,
                now_iso,
                created_by,
                json.dumps(redeemed_card, ensure_ascii=False),
                hidden_val,
                "直接创建",
                pan_value,
                None,
                None,
                None,
                None
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

        row_data = _row_to_dict(row)
        return True, {
            "expire_minutes": row_data.get("expire_minutes"),
            "card_limit": row_data.get("card_limit"),
            "card_type": row_data.get("card_type"),
            "key_kind": row_data.get("key_kind"),
            "created_by": row_data.get("created_by"),
            "bound_display_channel_id": row_data.get("bound_display_channel_id"),
            "bound_display_channel_name": row_data.get("bound_display_channel_name"),
            "bound_backend_channel_id": row_data.get("bound_backend_channel_id"),
            "bound_channel_head": row_data.get("bound_channel_head"),
            "channel_binding_enabled": row_data.get("channel_binding_enabled", False)
        }


def acquire_id_for_redeem(card_id, lock_timeout_seconds=REDEEM_LOCK_TIMEOUT_SECONDS):
    """
    原子抢占卡密兑换锁，防止并发重复兑换。
    返回结构与 validate_id 一致。
    """
    _init_db()
    now_iso = _utc_now_iso()
    stale_before_iso = (datetime.now(timezone.utc) - timedelta(seconds=max(int(lock_timeout_seconds or 0), 1))).isoformat()

    with _get_cursor() as cursor:
        if USE_SQLITE:
            redeeming_val = 1
            cursor.execute(f"""
                UPDATE ids
                SET redeeming = {_param()}, redeeming_time = {_param()}
                WHERE id = {_param()}
                AND used = 0
                AND (destroyed = 0 OR destroyed IS NULL)
                AND (
                    redeeming = 0
                    OR redeeming IS NULL
                    OR redeeming_time IS NULL
                    OR redeeming_time < {_param()}
                )
            """, (redeeming_val, now_iso, card_id, stale_before_iso))
        else:
            redeeming_val = True
            cursor.execute(f"""
                UPDATE ids
                SET redeeming = {_param()}, redeeming_time = {_param()}
                WHERE id = {_param()}
                AND used = FALSE
                AND (destroyed = FALSE OR destroyed IS NULL)
                AND (
                    redeeming = FALSE
                    OR redeeming IS NULL
                    OR redeeming_time IS NULL
                    OR redeeming_time < {_param()}
                )
            """, (redeeming_val, now_iso, card_id, stale_before_iso))

        if cursor.rowcount == 0:
            cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
            row = cursor.fetchone()

            if row is None:
                return False, "卡密不存在"

            if _get_row_value(row, "destroyed"):
                return False, "卡密已被销毁"

            if _get_row_value(row, "used"):
                return False, "卡密已被使用"

            if _get_row_value(row, "redeeming"):
                return False, "卡密兑换中，请稍后重试"

            return False, "卡密暂时不可用，请稍后重试"

        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        row = cursor.fetchone()
        if row is None:
            return False, "卡密不存在"

        row_data = _row_to_dict(row)
        return True, {
            "expire_minutes": row_data.get("expire_minutes"),
            "card_limit": row_data.get("card_limit"),
            "card_type": row_data.get("card_type"),
            "key_kind": row_data.get("key_kind"),
            "created_by": row_data.get("created_by"),
            "bound_display_channel_id": row_data.get("bound_display_channel_id"),
            "bound_display_channel_name": row_data.get("bound_display_channel_name"),
            "bound_backend_channel_id": row_data.get("bound_backend_channel_id"),
            "bound_channel_head": row_data.get("bound_channel_head"),
            "channel_binding_enabled": row_data.get("channel_binding_enabled", False)
        }


def release_id_redeem_lock(card_id):
    """
    释放卡密兑换锁（仅在未使用时释放）。
    """
    _init_db()
    with _get_cursor() as cursor:
        redeeming_reset = 0 if USE_SQLITE else False
        if USE_SQLITE:
            cursor.execute(f"""
                UPDATE ids
                SET redeeming = {_param()}, redeeming_time = NULL
                WHERE id = {_param()} AND used = 0
            """, (redeeming_reset, card_id))
        else:
            cursor.execute(f"""
                UPDATE ids
                SET redeeming = {_param()}, redeeming_time = NULL
                WHERE id = {_param()} AND used = FALSE
            """, (redeeming_reset, card_id))
    return True


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

        # 默认沿用卡密原始类型；若兑换时显式传入了实际卡片类型，则以实际类型为准
        final_card_type = _get_row_value(row, "card_type")
        if card_info and isinstance(card_info, dict):
            card_type_from_card = str(card_info.get("card_type", "")).strip().lower()
            if card_type_from_card in ("credit", "debit"):
                final_card_type = card_type_from_card

        final_card_limit = _get_row_value(row, "card_limit")
        if card_info and isinstance(card_info, dict):
            card_limit_from_card = card_info.get("card_limit")
            try:
                if card_limit_from_card is not None:
                    final_card_limit = float(card_limit_from_card)
            except (TypeError, ValueError):
                pass

        final_expire_minutes = _get_row_value(row, "expire_minutes")
        if card_info and isinstance(card_info, dict):
            expire_minutes_from_card = card_info.get("expire_minutes")
            try:
                if expire_minutes_from_card is not None:
                    final_expire_minutes = int(expire_minutes_from_card)
            except (TypeError, ValueError):
                pass

        used_val = True if not USE_SQLITE else 1
        redeeming_reset = False if not USE_SQLITE else 0
        if USE_SQLITE:
            cursor.execute(f'''
                UPDATE ids
                SET used = {_param()}, used_time = {_param()}, redeemed_card = {_param()}, pan = {_param()},
                    card_type = {_param()}, card_limit = {_param()}, expire_minutes = {_param()},
                    redeeming = {_param()}, redeeming_time = NULL
                WHERE id = {_param()} AND used = 0
            ''', (used_val, now_iso, redeemed_card_json, pan_value, final_card_type, final_card_limit, final_expire_minutes, redeeming_reset, card_id))
        else:
            cursor.execute(f'''
                UPDATE ids
                SET used = {_param()}, used_time = {_param()}, redeemed_card = {_param()}, pan = {_param()},
                    card_type = {_param()}, card_limit = {_param()}, expire_minutes = {_param()},
                    redeeming = {_param()}, redeeming_time = NULL
                WHERE id = {_param()} AND used = FALSE
            ''', (used_val, now_iso, redeemed_card_json, pan_value, final_card_type, final_card_limit, final_expire_minutes, redeeming_reset, card_id))

        if cursor.rowcount == 0:
            return False, "卡密已被使用"

        cursor.execute(f"SELECT * FROM ids WHERE id = {_param()}", (card_id,))
        updated_row = cursor.fetchone()
        if updated_row is not None:
            _upsert_old_card_pool_source(cursor, updated_row, now_dt=datetime.now(timezone.utc))

        return True, final_expire_minutes


def import_timoes_codes(codes, code_type, created_by=None, allowed_code_types=None):
    """
    导入 Timoes 接力卡密。
    """
    normalized_type = _normalize_timoes_code_type(code_type, allowed_code_types=allowed_code_types)
    if not normalized_type:
        return False, "Timoes 类型无效"

    _init_db()

    if isinstance(codes, str):
        raw_codes = codes.splitlines()
    else:
        raw_codes = list(codes or [])

    seen = set()
    normalized_codes = []
    invalid_codes = []
    duplicate_inputs = 0

    for raw in raw_codes:
        code = str(raw or "").strip()
        if not code:
            continue
        if code in seen:
            duplicate_inputs += 1
            continue
        seen.add(code)
        if not TIMOES_CODE_PATTERN.match(code):
            invalid_codes.append(code)
            continue
        normalized_codes.append(code)

    imported = 0
    duplicates = 0
    now_iso = _utc_now_iso()

    with _get_cursor() as cursor:
        for code in normalized_codes:
            if USE_SQLITE:
                cursor.execute(f'''
                    INSERT OR IGNORE INTO timoes_codes
                    (code, code_type, created_time, created_by, status, locked)
                    VALUES ({_params(6)})
                ''', (code, normalized_type, now_iso, created_by, 'available', 0))
            else:
                cursor.execute(f'''
                    INSERT INTO timoes_codes
                    (code, code_type, created_time, created_by, status, locked)
                    VALUES ({_params(6)})
                    ON CONFLICT (code) DO NOTHING
                ''', (code, normalized_type, now_iso, created_by, 'available', False))

            if cursor.rowcount > 0:
                imported += 1
            else:
                duplicates += 1

    return True, {
        "code_type": normalized_type,
        "imported": imported,
        "duplicates": duplicates + duplicate_inputs,
        "invalid": len(invalid_codes),
        "invalid_codes": invalid_codes[:20],
        "total_input": len([c for c in raw_codes if str(c or "").strip()])
    }


def get_timoes_pool_stats(username=None, allowed_code_types=None):
    """
    获取 Timoes 码池统计。
    """
    _init_db()
    default_types = []
    for raw_type in (allowed_code_types if allowed_code_types is not None else TIMOES_CODE_TYPES):
        normalized_type = _normalize_timoes_code_type(raw_type, allow_unknown=True)
        if normalized_type and normalized_type not in default_types:
            default_types.append(normalized_type)

    stats = {
        code_type: {
            "available": 0,
            "used": 0,
            "invalid": 0,
            "total": 0
        } for code_type in default_types
    }

    with _get_cursor() as cursor:
        params = []
        where_clause = ""
        if username is not None:
            where_clause = f"WHERE created_by = {_param()}"
            params.append(username)

        cursor.execute(f"""
            SELECT
                code_type,
                status,
                COUNT(*) as cnt
            FROM timoes_codes
            {where_clause}
            GROUP BY code_type, status
        """, params)

        rows = cursor.fetchall()
        for row in rows:
            code_type = _normalize_timoes_code_type(
                _get_row_value(row, "code_type"),
                allow_unknown=True
            )
            status = _get_row_value(row, "status")
            cnt = row[2] if USE_SQLITE else row['cnt']
            if not code_type:
                continue
            if code_type not in stats:
                stats[code_type] = {
                    "available": 0,
                    "used": 0,
                    "invalid": 0,
                    "total": 0
                }
            if status not in ("available", "used", "invalid"):
                continue
            stats[code_type][status] = cnt
            stats[code_type]["total"] += cnt

    total_available = sum(item["available"] for item in stats.values())
    return {
        "types": stats,
        "total_available": total_available
    }


def list_timoes_pool_items(username=None, code_type=None, status=None, limit=100, allowed_code_types=None):
    """
    获取 Timoes 码池明细，按创建时间倒序返回。
    """
    _init_db()
    normalized_type = _normalize_timoes_code_type(
        code_type,
        allowed_code_types=allowed_code_types,
        allow_unknown=True
    ) if code_type else None
    normalized_status = _normalize_pool_status(status) if status else None
    limit = max(1, min(int(limit or 100), 200))

    conditions = []
    params = []
    if username is not None:
        conditions.append(f"created_by = {_param()}")
        params.append(username)
    if normalized_type:
        conditions.append(f"code_type = {_param()}")
        params.append(normalized_type)
    if normalized_status:
        conditions.append(f"status = {_param()}")
        params.append(normalized_status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _get_cursor() as cursor:
        count_params = list(params)
        cursor.execute(f"SELECT COUNT(*) as total FROM timoes_codes {where_clause}", count_params)
        total_row = cursor.fetchone()
        total = total_row[0] if USE_SQLITE else total_row["total"]

        cursor.execute(f"""
            SELECT
                code,
                code_type,
                created_time,
                created_by,
                status,
                used_time,
                used_by_key,
                redeemed_card,
                last_error,
                locked,
                locked_time
            FROM timoes_codes
            {where_clause}
            ORDER BY
                CASE status
                    WHEN 'available' THEN 0
                    WHEN 'invalid' THEN 1
                    ELSE 2
                END,
                created_time DESC
            LIMIT {_param()}
        """, [*params, limit])

        rows = cursor.fetchall()

    items = []
    for row in rows:
        items.append({
            "code": _get_row_value(row, "code"),
            "code_type": _get_row_value(row, "code_type"),
            "created_time": _get_row_value(row, "created_time"),
            "created_by": _get_row_value(row, "created_by"),
            "status": _get_row_value(row, "status"),
            "used_time": _get_row_value(row, "used_time"),
            "used_by_key": _get_row_value(row, "used_by_key"),
            "redeemed_card": _get_row_value(row, "redeemed_card"),
            "last_error": _get_row_value(row, "last_error"),
            "locked": bool(_get_row_value(row, "locked", False)),
            "locked_time": _get_row_value(row, "locked_time"),
        })

    return {
        "items": items,
        "total": total,
        "limit": limit
    }


def update_timoes_pool_item(code, created_by=None, updates=None, allowed_code_types=None):
    """
    编辑单条 Timoes 码池记录。
    允许修改类型，以及在 available / invalid 之间切换状态。
    """
    normalized_code = str(code or "").strip()
    if not TIMOES_CODE_PATTERN.match(normalized_code):
        return False, "Timoes 卡密格式无效"

    payload = dict(updates or {})
    code_type = payload.get("code_type") if "code_type" in payload else None
    status = payload.get("status") if "status" in payload else None

    normalized_type = _normalize_timoes_code_type(
        code_type,
        allowed_code_types=allowed_code_types
    ) if code_type is not None else None
    if code_type is not None and not normalized_type:
        return False, "Timoes 类型无效"

    normalized_status = _normalize_pool_status(status, allow_used=False) if status is not None else None
    if status is not None and not normalized_status:
        return False, "仅支持切换为可用或失效状态"

    if normalized_type is None and normalized_status is None:
        return False, "没有可更新的内容"

    _init_db()
    owner_clause = ""
    owner_params = []
    if created_by is not None:
        owner_clause = f" AND created_by = {_param()}"
        owner_params.append(created_by)

    with _get_cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM timoes_codes WHERE code = {_param()}{owner_clause}",
            (normalized_code, *owner_params)
        )
        row = cursor.fetchone()
        if row is None:
            return False, "卡密不存在"

        current_status = _get_row_value(row, "status", "available")
        if current_status == "used":
            return False, "已使用的卡密不支持编辑，请直接删除记录"

        set_clauses = []
        update_params = []
        locked_reset = 0 if USE_SQLITE else False

        if normalized_type is not None:
            set_clauses.append(f"code_type = {_param()}")
            update_params.append(normalized_type)

        if normalized_status is not None:
            if normalized_status == "available":
                set_clauses.extend([
                    f"status = {_param()}",
                    "used_time = NULL",
                    "used_by_key = NULL",
                    "redeemed_card = NULL",
                    "last_error = NULL",
                    f"locked = {_param()}",
                    "locked_time = NULL",
                    "lock_token = NULL",
                ])
                update_params.extend(["available", locked_reset])
            else:
                set_clauses.extend([
                    f"status = {_param()}",
                    f"used_time = {_param()}",
                    "used_by_key = NULL",
                    "redeemed_card = NULL",
                    f"last_error = {_param()}",
                    f"locked = {_param()}",
                    "locked_time = NULL",
                    "lock_token = NULL",
                ])
                update_params.extend([
                    "invalid",
                    _utc_now_iso(),
                    _get_row_value(row, "last_error") or "手动标记为失效",
                    locked_reset
                ])

        cursor.execute(
            f"UPDATE timoes_codes SET {', '.join(set_clauses)} WHERE code = {_param()}{owner_clause}",
            (*update_params, normalized_code, *owner_params)
        )

        cursor.execute(
            f"SELECT * FROM timoes_codes WHERE code = {_param()}{owner_clause}",
            (normalized_code, *owner_params)
        )
        updated = cursor.fetchone()

    return True, {
        "code": _get_row_value(updated, "code"),
        "code_type": _get_row_value(updated, "code_type"),
        "created_time": _get_row_value(updated, "created_time"),
        "created_by": _get_row_value(updated, "created_by"),
        "status": _get_row_value(updated, "status"),
        "used_time": _get_row_value(updated, "used_time"),
        "used_by_key": _get_row_value(updated, "used_by_key"),
        "last_error": _get_row_value(updated, "last_error"),
        "locked": bool(_get_row_value(updated, "locked", False)),
        "locked_time": _get_row_value(updated, "locked_time"),
    }


def delete_timoes_pool_item(code, created_by=None):
    """
    删除单条 Timoes 码池记录。
    """
    normalized_code = str(code or "").strip()
    if not TIMOES_CODE_PATTERN.match(normalized_code):
        return False, "Timoes 卡密格式无效"

    _init_db()
    owner_clause = ""
    owner_params = []
    if created_by is not None:
        owner_clause = f" AND created_by = {_param()}"
        owner_params.append(created_by)

    with _get_cursor() as cursor:
        cursor.execute(
            f"DELETE FROM timoes_codes WHERE code = {_param()}{owner_clause}",
            (normalized_code, *owner_params)
        )
        if cursor.rowcount == 0:
            return False, "卡密不存在"

    return True, "卡密已删除"


def acquire_timoes_code_for_redeem(code_type, created_by=None, lock_timeout_seconds=TIMOES_LOCK_TIMEOUT_SECONDS, allowed_code_types=None):
    """
    原子抢占一个可用的 Timoes 接力卡密。
    """
    normalized_type = _normalize_timoes_code_type(code_type, allowed_code_types=allowed_code_types)
    if not normalized_type:
        return False, "Timoes 类型无效"

    _init_db()
    now_iso = _utc_now_iso()
    stale_before_iso = (datetime.now(timezone.utc) - timedelta(seconds=max(int(lock_timeout_seconds or 0), 1))).isoformat()
    lock_token = uuid.uuid4().hex

    owner_condition = ""
    owner_params = []
    if created_by is not None:
        owner_condition = f"AND created_by = {_param()}"
        owner_params.append(created_by)

    with _get_cursor() as cursor:
        if USE_SQLITE:
            cursor.execute(f"""
                UPDATE timoes_codes
                SET locked = {_param()}, locked_time = {_param()}, lock_token = {_param()}
                WHERE code = (
                    SELECT code FROM timoes_codes
                    WHERE code_type = {_param()}
                    AND status = 'available'
                    {owner_condition}
                    AND (
                        locked = 0
                        OR locked IS NULL
                        OR locked_time IS NULL
                        OR locked_time < {_param()}
                    )
                    ORDER BY created_time ASC
                    LIMIT 1
                )
                AND status = 'available'
                AND (
                    locked = 0
                    OR locked IS NULL
                    OR locked_time IS NULL
                    OR locked_time < {_param()}
                )
            """, (1, now_iso, lock_token, normalized_type, *owner_params, stale_before_iso, stale_before_iso))
        else:
            cursor.execute(f"""
                UPDATE timoes_codes
                SET locked = {_param()}, locked_time = {_param()}, lock_token = {_param()}
                WHERE code = (
                    SELECT code FROM timoes_codes
                    WHERE code_type = {_param()}
                    AND status = 'available'
                    {owner_condition}
                    AND (
                        locked = FALSE
                        OR locked IS NULL
                        OR locked_time IS NULL
                        OR locked_time < {_param()}
                    )
                    ORDER BY created_time ASC
                    LIMIT 1
                )
                AND status = 'available'
                AND (
                    locked = FALSE
                    OR locked IS NULL
                    OR locked_time IS NULL
                    OR locked_time < {_param()}
                )
            """, (True, now_iso, lock_token, normalized_type, *owner_params, stale_before_iso, stale_before_iso))

        if cursor.rowcount == 0:
            return False, f"当前没有可用的 Timoes {normalized_type} 卡密"

        cursor.execute(f"SELECT * FROM timoes_codes WHERE lock_token = {_param()}", (lock_token,))
        row = cursor.fetchone()
        if row is None:
            return False, "获取 Timoes 卡密失败"

        return True, {
            "code": _get_row_value(row, "code"),
            "code_type": _get_row_value(row, "code_type"),
            "created_by": _get_row_value(row, "created_by")
        }


def release_timoes_code_lock(code):
    """
    释放 Timoes 卡密锁，仅对可用状态生效。
    """
    if not code:
        return False

    _init_db()
    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE timoes_codes
            SET locked = {_param()}, locked_time = NULL, lock_token = NULL
            WHERE code = {_param()} AND status = 'available'
        """, (locked_reset, code))
    return True


def mark_timoes_code_used(code, used_by_key=None, redeemed_card=None):
    """
    标记 Timoes 卡密为已使用。
    """
    if not code:
        return False

    _init_db()
    now_iso = _utc_now_iso()
    redeemed_card_json = json.dumps(redeemed_card, ensure_ascii=False) if redeemed_card else None

    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE timoes_codes
            SET status = {_param()},
                used_time = {_param()},
                used_by_key = {_param()},
                redeemed_card = {_param()},
                last_error = NULL,
                locked = {_param()},
                locked_time = NULL,
                lock_token = NULL
            WHERE code = {_param()}
        """, ('used', now_iso, used_by_key, redeemed_card_json, locked_reset, code))
    return True


def mark_timoes_code_invalid(code, error=None):
    """
    标记 Timoes 卡密为失效。
    """
    if not code:
        return False

    _init_db()
    now_iso = _utc_now_iso()

    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE timoes_codes
            SET status = {_param()},
                used_time = {_param()},
                last_error = {_param()},
                locked = {_param()},
                locked_time = NULL,
                lock_token = NULL
            WHERE code = {_param()}
        """, ('invalid', now_iso, str(error or ""), locked_reset, code))
    return True


def import_manual_cards(cards, created_by=None):
    """
    导入手动录入的卡信息。
    仅支持格式：
    - 卡号 月份 年份 CVV 余额 有效期分钟
    """
    _init_db()

    if isinstance(cards, str):
        raw_cards = cards.splitlines()
    else:
        raw_cards = list(cards or [])

    seen = set()
    parsed_cards = []
    invalid_lines = []
    duplicate_inputs = 0

    for idx, raw in enumerate(raw_cards, start=1):
        line = str(raw or "").strip()
        if not line:
            continue
        normalized_key = line.lower()
        if normalized_key in seen:
            duplicate_inputs += 1
            continue
        seen.add(normalized_key)

        parsed, error = _parse_manual_card_line(line)
        if not parsed or not parsed.get("bin_code"):
            invalid_lines.append({"line": idx, "content": line[:80], "error": error or "BIN 识别失败"})
            continue
        parsed_cards.append(parsed)

    imported = 0
    duplicates = 0
    now_iso = _utc_now_iso()

    with _get_cursor() as cursor:
        for card in parsed_cards:
            legal_address_json = json.dumps(card.get("legal_address") or {}, ensure_ascii=False)
            if USE_SQLITE:
                cursor.execute(f'''
                    INSERT OR IGNORE INTO manual_cards
                    (pan, bin_code, exp_month, exp_year, cvv, created_time, created_by, status, locked, card_limit, expire_minutes, legal_address)
                    VALUES ({_params(12)})
                ''', (
                    card["pan"],
                    card["bin_code"],
                    card["exp_month"],
                    card["exp_year"],
                    card["cvv"],
                    now_iso,
                    created_by,
                    'available',
                    0,
                    card.get("card_limit"),
                    card.get("expire_minutes"),
                    legal_address_json
                ))
            else:
                cursor.execute(f'''
                    INSERT INTO manual_cards
                    (pan, bin_code, exp_month, exp_year, cvv, created_time, created_by, status, locked, card_limit, expire_minutes, legal_address)
                    VALUES ({_params(12)})
                    ON CONFLICT (pan) DO NOTHING
                ''', (
                    card["pan"],
                    card["bin_code"],
                    card["exp_month"],
                    card["exp_year"],
                    card["cvv"],
                    now_iso,
                    created_by,
                    'available',
                    False,
                    card.get("card_limit"),
                    card.get("expire_minutes"),
                    legal_address_json
                ))

            if cursor.rowcount > 0:
                imported += 1
            else:
                duplicates += 1

    return True, {
        "imported": imported,
        "duplicates": duplicates + duplicate_inputs,
        "invalid": len(invalid_lines),
        "invalid_lines": invalid_lines[:20],
        "total_input": len([c for c in raw_cards if str(c or "").strip()])
    }


def get_manual_card_pool_stats(username=None):
    """
    获取手动卡池统计，按 BIN 聚合。
    """
    _init_db()
    bins = {}

    with _get_cursor() as cursor:
        params = []
        where_clause = ""
        if username is not None:
            where_clause = f"WHERE created_by = {_param()}"
            params.append(username)

        cursor.execute(f"""
            SELECT
                bin_code,
                status,
                COUNT(*) as cnt
            FROM manual_cards
            {where_clause}
            GROUP BY bin_code, status
        """, params)

        rows = cursor.fetchall()
        for row in rows:
            bin_code = _get_row_value(row, "bin_code")
            status = _get_row_value(row, "status")
            cnt = row[2] if USE_SQLITE else row['cnt']
            if not bin_code:
                continue
            bucket = bins.setdefault(bin_code, {
                "available": 0,
                "used": 0,
                "invalid": 0,
                "total": 0
            })
            if status in ("available", "used", "invalid"):
                bucket[status] = cnt
                bucket["total"] += cnt

    total_available = sum(item["available"] for item in bins.values())
    return {
        "bins": dict(sorted(bins.items(), key=lambda item: item[0])),
        "total_available": total_available,
        "total_bins": len(bins)
    }


def list_manual_card_pool_items(username=None, bin_code=None, status=None, limit=100):
    """
    获取手动卡池明细。
    """
    _init_db()
    limit = max(1, min(int(limit or 100), 200))
    raw_bin = str(bin_code or "").strip()
    normalized_bin = _normalize_manual_bin_code(raw_bin) if raw_bin else None
    if raw_bin and not normalized_bin:
        return {
            "items": [],
            "total": 0,
            "limit": limit
        }

    normalized_status = _normalize_pool_status(status) if status else None

    conditions = []
    params = []
    if username is not None:
        conditions.append(f"created_by = {_param()}")
        params.append(username)
    if normalized_bin:
        conditions.append(f"bin_code = {_param()}")
        params.append(normalized_bin)
    if normalized_status:
        conditions.append(f"status = {_param()}")
        params.append(normalized_status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _get_cursor() as cursor:
        count_params = list(params)
        cursor.execute(f"SELECT COUNT(*) as total FROM manual_cards {where_clause}", count_params)
        total_row = cursor.fetchone()
        total = total_row[0] if USE_SQLITE else total_row["total"]

        cursor.execute(f"""
            SELECT
                pan,
                bin_code,
                exp_month,
                exp_year,
                cvv,
                created_time,
                created_by,
                status,
                used_time,
                used_by_key,
                redeemed_card,
                last_error,
                locked,
                locked_time,
                card_limit,
                expire_minutes,
                legal_address
            FROM manual_cards
            {where_clause}
            ORDER BY
                CASE status
                    WHEN 'available' THEN 0
                    WHEN 'invalid' THEN 1
                    ELSE 2
                END,
                created_time DESC
            LIMIT {_param()}
        """, [*params, limit])
        rows = cursor.fetchall()

    items = []
    for row in rows:
        legal_address_raw = _get_row_value(row, "legal_address")
        try:
            legal_address = json.loads(legal_address_raw) if legal_address_raw else {}
        except Exception:
            legal_address = {}
        items.append({
            "pan": _get_row_value(row, "pan"),
            "bin_code": _get_row_value(row, "bin_code"),
            "exp_month": _get_row_value(row, "exp_month"),
            "exp_year": _get_row_value(row, "exp_year"),
            "cvv": _get_row_value(row, "cvv"),
            "created_time": _get_row_value(row, "created_time"),
            "created_by": _get_row_value(row, "created_by"),
            "status": _get_row_value(row, "status"),
            "used_time": _get_row_value(row, "used_time"),
            "used_by_key": _get_row_value(row, "used_by_key"),
            "redeemed_card": _get_row_value(row, "redeemed_card"),
            "last_error": _get_row_value(row, "last_error"),
            "locked": bool(_get_row_value(row, "locked", False)),
            "locked_time": _get_row_value(row, "locked_time"),
            "card_limit": _get_row_value(row, "card_limit"),
            "expire_minutes": _get_row_value(row, "expire_minutes"),
            "legal_address": legal_address,
        })

    return {
        "items": items,
        "total": total,
        "limit": limit
    }


def update_manual_card_pool_item(pan, created_by=None, updates=None):
    """
    编辑手动卡池单条记录。
    支持修改到期时间、CVV、余额、有效期分钟和可用/失效状态。
    """
    normalized_pan = _normalize_manual_pan(pan)
    if not normalized_pan:
        return False, "手动卡号格式无效"

    payload = dict(updates or {})
    has_updates = False

    normalized_month = None
    if "exp_month" in payload:
        has_updates = True
        normalized_month = _normalize_exp_month(payload.get("exp_month"))
        if not normalized_month:
            return False, "月份格式无效"

    normalized_year = None
    if "exp_year" in payload:
        has_updates = True
        normalized_year = _normalize_exp_year(payload.get("exp_year"))
        if not normalized_year:
            return False, "年份格式无效"

    normalized_cvv = None
    if "cvv" in payload:
        has_updates = True
        normalized_cvv = ''.join(ch for ch in str(payload.get("cvv") or "") if ch.isdigit())
        if not MANUAL_CARD_CVV_PATTERN.match(normalized_cvv):
            return False, "CVV 格式无效"

    normalized_status = None
    if "status" in payload:
        has_updates = True
        normalized_status = _normalize_pool_status(payload.get("status"), allow_used=False)
        if not normalized_status:
            return False, "仅支持切换为可用或失效状态"

    card_limit = None
    if "card_limit" in payload:
        has_updates = True
        raw_limit = payload.get("card_limit")
        if raw_limit in ("", None):
            card_limit = None
        else:
            try:
                card_limit = float(raw_limit)
            except (TypeError, ValueError):
                return False, "余额格式无效"
            if card_limit < 0:
                return False, "余额不能小于 0"

    expire_minutes = None
    if "expire_minutes" in payload:
        has_updates = True
        raw_expire = payload.get("expire_minutes")
        if raw_expire in ("", None):
            expire_minutes = None
        else:
            try:
                expire_minutes = int(raw_expire)
            except (TypeError, ValueError):
                return False, "有效期分钟格式无效"
            if expire_minutes <= 0:
                return False, "有效期分钟必须大于 0"

    if not has_updates:
        return False, "没有可更新的内容"

    _init_db()
    owner_clause = ""
    owner_params = []
    if created_by is not None:
        owner_clause = f" AND created_by = {_param()}"
        owner_params.append(created_by)

    with _get_cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM manual_cards WHERE pan = {_param()}{owner_clause}",
            (normalized_pan, *owner_params)
        )
        row = cursor.fetchone()
        if row is None:
            return False, "卡片不存在"

        current_status = _get_row_value(row, "status", "available")
        if current_status == "used":
            return False, "已使用的卡片不支持编辑，请直接删除记录"

        set_clauses = []
        update_params = []
        locked_reset = 0 if USE_SQLITE else False

        if normalized_month is not None:
            set_clauses.append(f"exp_month = {_param()}")
            update_params.append(normalized_month)
        if normalized_year is not None:
            set_clauses.append(f"exp_year = {_param()}")
            update_params.append(normalized_year)
        if normalized_cvv is not None:
            set_clauses.append(f"cvv = {_param()}")
            update_params.append(normalized_cvv)
        if "card_limit" in payload:
            set_clauses.append(f"card_limit = {_param()}")
            update_params.append(card_limit)
        if "expire_minutes" in payload:
            set_clauses.append(f"expire_minutes = {_param()}")
            update_params.append(expire_minutes)

        if normalized_status is not None:
            if normalized_status == "available":
                set_clauses.extend([
                    f"status = {_param()}",
                    "used_time = NULL",
                    "used_by_key = NULL",
                    "redeemed_card = NULL",
                    "last_error = NULL",
                    f"locked = {_param()}",
                    "locked_time = NULL",
                    "lock_token = NULL",
                ])
                update_params.extend(["available", locked_reset])
            else:
                set_clauses.extend([
                    f"status = {_param()}",
                    f"used_time = {_param()}",
                    "used_by_key = NULL",
                    "redeemed_card = NULL",
                    f"last_error = {_param()}",
                    f"locked = {_param()}",
                    "locked_time = NULL",
                    "lock_token = NULL",
                ])
                update_params.extend([
                    "invalid",
                    _utc_now_iso(),
                    _get_row_value(row, "last_error") or "手动标记为失效",
                    locked_reset
                ])

        cursor.execute(
            f"UPDATE manual_cards SET {', '.join(set_clauses)} WHERE pan = {_param()}{owner_clause}",
            (*update_params, normalized_pan, *owner_params)
        )

        cursor.execute(
            f"SELECT * FROM manual_cards WHERE pan = {_param()}{owner_clause}",
            (normalized_pan, *owner_params)
        )
        updated = cursor.fetchone()

    legal_address_raw = _get_row_value(updated, "legal_address")
    try:
        legal_address = json.loads(legal_address_raw) if legal_address_raw else {}
    except Exception:
        legal_address = {}

    return True, {
        "pan": _get_row_value(updated, "pan"),
        "bin_code": _get_row_value(updated, "bin_code"),
        "exp_month": _get_row_value(updated, "exp_month"),
        "exp_year": _get_row_value(updated, "exp_year"),
        "cvv": _get_row_value(updated, "cvv"),
        "created_time": _get_row_value(updated, "created_time"),
        "created_by": _get_row_value(updated, "created_by"),
        "status": _get_row_value(updated, "status"),
        "used_time": _get_row_value(updated, "used_time"),
        "used_by_key": _get_row_value(updated, "used_by_key"),
        "last_error": _get_row_value(updated, "last_error"),
        "locked": bool(_get_row_value(updated, "locked", False)),
        "locked_time": _get_row_value(updated, "locked_time"),
        "card_limit": _get_row_value(updated, "card_limit"),
        "expire_minutes": _get_row_value(updated, "expire_minutes"),
        "legal_address": legal_address,
    }


def delete_manual_card_pool_item(pan, created_by=None):
    """
    删除手动卡池单条记录。
    """
    normalized_pan = _normalize_manual_pan(pan)
    if not normalized_pan:
        return False, "手动卡号格式无效"

    _init_db()
    owner_clause = ""
    owner_params = []
    if created_by is not None:
        owner_clause = f" AND created_by = {_param()}"
        owner_params.append(created_by)

    with _get_cursor() as cursor:
        cursor.execute(
            f"DELETE FROM manual_cards WHERE pan = {_param()}{owner_clause}",
            (normalized_pan, *owner_params)
        )
        if cursor.rowcount == 0:
            return False, "卡片不存在"

    return True, "卡片已删除"


def acquire_manual_card_for_redeem(bin_code, created_by=None, lock_timeout_seconds=MANUAL_CARD_LOCK_TIMEOUT_SECONDS):
    """
    原子抢占一个可用的手动卡池卡信息。
    """
    normalized_bin = _normalize_manual_bin_code(bin_code)
    if not normalized_bin:
        return False, "手动卡池 BIN 无效"

    _init_db()
    now_iso = _utc_now_iso()
    stale_before_iso = (datetime.now(timezone.utc) - timedelta(seconds=max(int(lock_timeout_seconds or 0), 1))).isoformat()
    lock_token = uuid.uuid4().hex

    owner_condition = ""
    owner_params = []
    if created_by is not None:
        owner_condition = f"AND created_by = {_param()}"
        owner_params.append(created_by)

    with _get_cursor() as cursor:
        if USE_SQLITE:
            cursor.execute(f"""
                UPDATE manual_cards
                SET locked = {_param()}, locked_time = {_param()}, lock_token = {_param()}
                WHERE pan = (
                    SELECT pan FROM manual_cards
                    WHERE bin_code = {_param()}
                    AND status = 'available'
                    {owner_condition}
                    AND (
                        locked = 0
                        OR locked IS NULL
                        OR locked_time IS NULL
                        OR locked_time < {_param()}
                    )
                    ORDER BY created_time ASC
                    LIMIT 1
                )
                AND status = 'available'
                AND (
                    locked = 0
                    OR locked IS NULL
                    OR locked_time IS NULL
                    OR locked_time < {_param()}
                )
            """, (1, now_iso, lock_token, normalized_bin, *owner_params, stale_before_iso, stale_before_iso))
        else:
            cursor.execute(f"""
                UPDATE manual_cards
                SET locked = {_param()}, locked_time = {_param()}, lock_token = {_param()}
                WHERE pan = (
                    SELECT pan FROM manual_cards
                    WHERE bin_code = {_param()}
                    AND status = 'available'
                    {owner_condition}
                    AND (
                        locked = FALSE
                        OR locked IS NULL
                        OR locked_time IS NULL
                        OR locked_time < {_param()}
                    )
                    ORDER BY created_time ASC
                    LIMIT 1
                )
                AND status = 'available'
                AND (
                    locked = FALSE
                    OR locked IS NULL
                    OR locked_time IS NULL
                    OR locked_time < {_param()}
                )
            """, (True, now_iso, lock_token, normalized_bin, *owner_params, stale_before_iso, stale_before_iso))

        if cursor.rowcount == 0:
            return False, f"当前没有可用的 {normalized_bin} 手动卡"

        cursor.execute(f"SELECT * FROM manual_cards WHERE lock_token = {_param()}", (lock_token,))
        row = cursor.fetchone()
        if row is None:
            return False, "获取手动卡失败"

        legal_address_raw = _get_row_value(row, "legal_address")
        try:
            legal_address = json.loads(legal_address_raw) if legal_address_raw else {}
        except Exception:
            legal_address = {}

        return True, {
            "pan": _get_row_value(row, "pan"),
            "bin_code": _get_row_value(row, "bin_code"),
            "exp_month": _get_row_value(row, "exp_month"),
            "exp_year": _get_row_value(row, "exp_year"),
            "cvv": _get_row_value(row, "cvv"),
            "card_limit": _get_row_value(row, "card_limit"),
            "expire_minutes": _get_row_value(row, "expire_minutes"),
            "created_by": _get_row_value(row, "created_by"),
            "legal_address": legal_address
        }


def release_manual_card_lock(pan):
    """
    释放手动卡池卡信息锁，仅对可用状态生效。
    """
    pan = _normalize_manual_pan(pan)
    if not pan:
        return False

    _init_db()
    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE manual_cards
            SET locked = {_param()}, locked_time = NULL, lock_token = NULL
            WHERE pan = {_param()} AND status = 'available'
        """, (locked_reset, pan))
    return True


def mark_manual_card_used(pan, used_by_key=None, redeemed_card=None):
    """
    标记手动卡池卡信息为已使用。
    """
    pan = _normalize_manual_pan(pan)
    if not pan:
        return False

    _init_db()
    now_iso = _utc_now_iso()
    redeemed_card_json = json.dumps(redeemed_card, ensure_ascii=False) if redeemed_card else None

    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE manual_cards
            SET status = {_param()},
                used_time = {_param()},
                used_by_key = {_param()},
                redeemed_card = {_param()},
                last_error = NULL,
                locked = {_param()},
                locked_time = NULL,
                lock_token = NULL
            WHERE pan = {_param()}
        """, ('used', now_iso, used_by_key, redeemed_card_json, locked_reset, pan))
    return True


def mark_manual_card_invalid(pan, error=None):
    """
    标记手动卡池卡信息为失效。
    """
    pan = _normalize_manual_pan(pan)
    if not pan:
        return False

    _init_db()
    now_iso = _utc_now_iso()

    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE manual_cards
            SET status = {_param()},
                used_time = {_param()},
                last_error = {_param()},
                locked = {_param()},
                locked_time = NULL,
                lock_token = NULL
            WHERE pan = {_param()}
        """, ('invalid', now_iso, str(error or ""), locked_reset, pan))
    return True


def sync_old_card_pool(force=False, min_interval_seconds=OLD_CARD_POOL_SYNC_MIN_INTERVAL_SECONDS):
    """
    刷新旧卡池状态。
    仅处理过期状态，不再扫描 ids 全表回填旧卡池。
    """
    global _old_card_pool_last_sync_monotonic

    if min_interval_seconds is None:
        min_interval_seconds = 0

    now_monotonic = time.monotonic()
    if (
        not force
        and _old_card_pool_last_sync_monotonic
        and now_monotonic - _old_card_pool_last_sync_monotonic < min_interval_seconds
    ):
        return False

    acquired = _old_card_pool_sync_lock.acquire(blocking=force)
    if not acquired:
        return False

    try:
        now_monotonic = time.monotonic()
        if (
            not force
            and _old_card_pool_last_sync_monotonic
            and now_monotonic - _old_card_pool_last_sync_monotonic < min_interval_seconds
        ):
            return False

        _init_db()
        now_dt = datetime.now(timezone.utc)

        with _get_cursor() as cursor:
            _expire_old_card_pool_entries(cursor, now_dt=now_dt)

        _old_card_pool_last_sync_monotonic = time.monotonic()
        return True
    finally:
        _old_card_pool_sync_lock.release()


def get_old_card_pool_stats(sync=True):
    """
    获取旧卡池统计，按实际后端渠道聚合。
    """
    if sync:
        sync_old_card_pool()
    _init_db()

    channels = {}
    with _get_cursor() as cursor:
        cursor.execute("""
            SELECT
                backend_channel_id,
                channel_head,
                provider,
                provider_label,
                status,
                COUNT(*) as cnt
            FROM old_card_pool
            GROUP BY backend_channel_id, channel_head, provider, provider_label, status
        """)
        rows = cursor.fetchall()

    for row in rows:
        backend_channel_id = str(_get_row_value(row, "backend_channel_id") or "").strip().lower()
        if not backend_channel_id:
            continue
        status = _get_row_value(row, "status")
        cnt = row[5] if USE_SQLITE else row["cnt"]
        bucket = channels.setdefault(backend_channel_id, {
            "backend_channel_id": backend_channel_id,
            "head": ''.join(ch for ch in str(_get_row_value(row, "channel_head") or "") if ch.isdigit()) or None,
            "provider": str(_get_row_value(row, "provider") or "").strip().lower() or None,
            "provider_label": _get_row_value(row, "provider_label"),
            "available_count": 0,
            "used_count": 0,
            "invalid_count": 0,
            "total_count": 0
        })
        if status == "available":
            bucket["available_count"] += cnt
        elif status == "used":
            bucket["used_count"] += cnt
        else:
            bucket["invalid_count"] += cnt
        bucket["total_count"] += cnt

    items = sorted(
        channels.values(),
        key=lambda item: (
            item.get("provider") or "",
            item.get("head") or item.get("backend_channel_id") or ""
        )
    )
    return {
        "channels": items,
        "total_available": sum(item.get("available_count", 0) for item in items),
        "total_channels": len(items)
    }


def acquire_old_card_for_redeem(backend_channel_id=None, allowed_backend_channel_ids=None, lock_timeout_seconds=OLD_CARD_LOCK_TIMEOUT_SECONDS):
    """
    原子抢占一个可用旧卡。
    未指定 backend_channel_id 时会在允许的后端渠道内随机分配。
    """
    sync_old_card_pool()
    _init_db()

    normalized_backend_channel_id = str(backend_channel_id or "").strip().lower()
    allowed_backend_ids = sorted({
        str(item or "").strip().lower()
        for item in (allowed_backend_channel_ids or [])
        if str(item or "").strip()
    })
    if normalized_backend_channel_id and allowed_backend_ids and normalized_backend_channel_id not in allowed_backend_ids:
        return False, "指定的旧卡渠道当前未开启"

    now_iso = _utc_now_iso()
    stale_before_iso = (datetime.now(timezone.utc) - timedelta(seconds=max(int(lock_timeout_seconds or 0), 1))).isoformat()
    lock_token = uuid.uuid4().hex

    filters = ["status = 'available'"]
    params = []
    if normalized_backend_channel_id:
        filters.append(f"backend_channel_id = {_param()}")
        params.append(normalized_backend_channel_id)
    elif allowed_backend_ids:
        placeholders = _params(len(allowed_backend_ids))
        filters.append(f"backend_channel_id IN ({placeholders})")
        params.extend(allowed_backend_ids)

    if USE_SQLITE:
        filters.append(f"(locked = 0 OR locked IS NULL OR locked_time IS NULL OR locked_time < {_param()})")
        params.append(stale_before_iso)
        where_clause = " AND ".join(filters)
        cursor_params = [1, now_iso, lock_token, *params, stale_before_iso]
        select_sql = f"""
            SELECT source_key_id FROM old_card_pool
            WHERE {where_clause}
            ORDER BY RANDOM()
            LIMIT 1
        """
        update_sql = f"""
            UPDATE old_card_pool
            SET locked = {_param()}, locked_time = {_param()}, lock_token = {_param()}
            WHERE source_key_id = ({select_sql})
            AND status = 'available'
            AND (
                locked = 0
                OR locked IS NULL
                OR locked_time IS NULL
                OR locked_time < {_param()}
            )
        """
    else:
        filters.append(f"(locked = FALSE OR locked IS NULL OR locked_time IS NULL OR locked_time < {_param()})")
        params.append(stale_before_iso)
        where_clause = " AND ".join(filters)
        cursor_params = [True, now_iso, lock_token, *params, stale_before_iso]
        select_sql = f"""
            SELECT source_key_id FROM old_card_pool
            WHERE {where_clause}
            ORDER BY RANDOM()
            LIMIT 1
        """
        update_sql = f"""
            UPDATE old_card_pool
            SET locked = {_param()}, locked_time = {_param()}, lock_token = {_param()}
            WHERE source_key_id = ({select_sql})
            AND status = 'available'
            AND (
                locked = FALSE
                OR locked IS NULL
                OR locked_time IS NULL
                OR locked_time < {_param()}
            )
        """

    with _get_cursor() as cursor:
        cursor.execute(update_sql, cursor_params)
        if cursor.rowcount == 0:
            if normalized_backend_channel_id:
                return False, "当前指定卡头没有可用旧卡"
            return False, "当前没有可用的旧卡"

        cursor.execute("""
            SELECT
                source_key_id,
                backend_channel_id,
                channel_head,
                provider,
                provider_label,
                pan,
                expire_time,
                source_used_time,
                card_data
            FROM old_card_pool
            WHERE lock_token = {token}
        """.format(token=_param()), (lock_token,))
        row = cursor.fetchone()
        if row is None:
            return False, "获取旧卡失败"

    try:
        card_data = json.loads(_get_row_value(row, "card_data") or "{}")
    except Exception:
        card_data = {}

    return True, {
        "source_key_id": _get_row_value(row, "source_key_id"),
        "backend_channel_id": _get_row_value(row, "backend_channel_id"),
        "channel_head": _get_row_value(row, "channel_head"),
        "provider": _get_row_value(row, "provider"),
        "provider_label": _get_row_value(row, "provider_label"),
        "pan": _get_row_value(row, "pan"),
        "expire_time": _normalize_iso_to_utc(_get_row_value(row, "expire_time")),
        "source_used_time": _normalize_iso_to_utc(_get_row_value(row, "source_used_time")),
        "card": card_data if isinstance(card_data, dict) else {}
    }


def release_old_card_lock(source_key_id):
    """
    释放旧卡锁，仅对可用状态生效。
    """
    source_key_id = str(source_key_id or "").strip()
    if not source_key_id:
        return False

    _init_db()
    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE old_card_pool
            SET locked = {_param()}, locked_time = NULL, lock_token = NULL
            WHERE source_key_id = {_param()} AND status = 'available'
        """, (locked_reset, source_key_id))
    return True


def mark_old_card_used(source_key_id, used_by_key=None):
    """
    标记旧卡池卡片已被分配使用。
    """
    source_key_id = str(source_key_id or "").strip()
    if not source_key_id:
        return False

    _init_db()
    now_iso = _utc_now_iso()

    with _get_cursor() as cursor:
        locked_reset = 0 if USE_SQLITE else False
        cursor.execute(f"""
            UPDATE old_card_pool
            SET status = {_param()},
                used_time = {_param()},
                used_by_key = {_param()},
                last_error = NULL,
                locked = {_param()},
                locked_time = NULL,
                lock_token = NULL
            WHERE source_key_id = {_param()}
        """, ("used", now_iso, used_by_key, locked_reset, source_key_id))
    return True


def allocate_existing_ids_for_withdraw(token, note, username, card_type, count, bound_channel=None, key_kind="normal"):
    """
    优先提取现有未使用的卡密并标记为隐藏，用于提卡链接
    """
    if count <= 0:
        return []

    _init_db()
    normalized_bound_channel = _normalize_bound_channel(bound_channel)
    normalized_key_kind = _normalize_key_kind(key_kind, default="normal")
    with _get_cursor() as cursor:
        if USE_SQLITE:
            conditions = [
                "used = 0",
                "(destroyed = 0 OR destroyed IS NULL)",
                "(hidden = 0 OR hidden IS NULL)"
            ]
        else:
            conditions = [
                "used = FALSE",
                "(destroyed = FALSE OR destroyed IS NULL)",
                "(hidden = FALSE OR hidden IS NULL)"
            ]
        params = []

        if normalized_bound_channel:
            conditions.append(f"bound_display_channel_id = {_param()}")
            params.append(normalized_bound_channel["display_channel_id"])
        else:
            conditions.append("(bound_display_channel_id IS NULL OR bound_display_channel_id = '')")

        if username:
            conditions.append(f"created_by = {_param()}")
            params.append(username)

        conditions.append(f"key_kind = {_param()}")
        params.append(normalized_key_kind)

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
            "key_kind": _normalize_key_kind(_get_row_value(row, "key_kind"), default="normal"),
            "used_time": used_time,
            "destroyed": bool(_get_row_value(row, "destroyed")),
            "destroyed_time": _get_row_value(row, "destroyed_time"),
            "bound_display_channel_id": _get_row_value(row, "bound_display_channel_id"),
            "bound_display_channel_name": _get_row_value(row, "bound_display_channel_name"),
            "bound_backend_channel_id": _get_row_value(row, "bound_backend_channel_id"),
            "bound_channel_head": _get_row_value(row, "bound_channel_head"),
            "channel_binding_enabled": bool(
                _get_row_value(row, "bound_display_channel_id")
                and _get_row_value(row, "bound_backend_channel_id")
            )
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
                "key_kind": _normalize_key_kind(_get_row_value(row, "key_kind"), default="normal"),
                "used_time": used_time,
                "destroyed": bool(_get_row_value(row, "destroyed")),
                "destroyed_time": _get_row_value(row, "destroyed_time"),
                "bound_display_channel_id": _get_row_value(row, "bound_display_channel_id"),
                "bound_display_channel_name": _get_row_value(row, "bound_display_channel_name"),
                "bound_backend_channel_id": _get_row_value(row, "bound_backend_channel_id"),
                "bound_channel_head": _get_row_value(row, "bound_channel_head"),
                "channel_binding_enabled": bool(
                    _get_row_value(row, "bound_display_channel_id")
                    and _get_row_value(row, "bound_backend_channel_id")
                )
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

        _invalidate_old_card_pool_sources(cursor, [card_id])
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

        _invalidate_old_card_pool_sources(cursor, [card_id], reason="来源卡片已销毁")
        destroyed_val = True if not USE_SQLITE else 1
        cursor.execute(
            f"UPDATE ids SET destroyed = {_param()}, destroyed_time = {_param()} WHERE id = {_param()}",
            (destroyed_val, now_iso, card_id)
        )
        return True, None


def _append_key_kind_condition(conditions, params, key_kind):
    normalized = str(key_kind or "").strip().lower()
    if normalized not in KEY_KIND_TYPES:
        return

    if normalized == "old_card":
        conditions.append(f"key_kind = {_param()}")
        params.append("old_card")
        return

    conditions.append(f"(key_kind = {_param()} OR key_kind IS NULL OR key_kind = '')")
    params.append("normal")


def get_all_ids(username=None, page=None, page_size=100, key_kind=None):
    """
    获取卡密列表（只返回未使用且未销毁的卡密）
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            base_condition = "used = 0 AND (destroyed = 0 OR destroyed IS NULL) AND (hidden = 0 OR hidden IS NULL)"
        else:
            base_condition = "used = FALSE AND (destroyed = FALSE OR destroyed IS NULL) AND (hidden = FALSE OR hidden IS NULL)"

        conditions = [base_condition]
        params = []

        if username:
            conditions.append(f"created_by = {_param()}")
            params.append(username)

        _append_key_kind_condition(conditions, params, key_kind)
        where_clause = " AND ".join(conditions)

        # 获取总数
        cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {where_clause}", params)

        count_row = cursor.fetchone()
        total = count_row[0] if USE_SQLITE else count_row['cnt']

        # 分页查询
        if page is not None and page > 0:
            offset = (page - 1) * page_size
            cursor.execute(
                f"SELECT * FROM ids WHERE {where_clause} ORDER BY created_time DESC LIMIT {_param()} OFFSET {_param()}",
                tuple(params) + (page_size, offset)
            )
        else:
            cursor.execute(
                f"SELECT * FROM ids WHERE {where_clause} ORDER BY created_time DESC",
                params
            )

        rows = cursor.fetchall()
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1

        return {
            "ids": [_row_to_dict(row) for row in rows],
            "total": total,
            "page": page or 1,
            "page_size": page_size,
            "total_pages": total_pages
        }


def get_unused_count_by_type(username=None, card_type=None, key_kind=None):
    """
    获取未使用且未销毁的卡密数量（按类型）
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            base_condition = "used = 0 AND (destroyed = 0 OR destroyed IS NULL) AND (hidden = 0 OR hidden IS NULL)"
        else:
            base_condition = "used = FALSE AND (destroyed = FALSE OR destroyed IS NULL) AND (hidden = FALSE OR hidden IS NULL)"
        conditions = [base_condition]
        params = []

        if username:
            conditions.append(f"created_by = {_param()}")
            params.append(username)

        if card_type:
            conditions.append(f"card_type = {_param()}")
            params.append(card_type)

        _append_key_kind_condition(conditions, params, key_kind)
        where_clause = " AND ".join(conditions)

        cursor.execute(f"SELECT COUNT(*) as cnt FROM ids WHERE {where_clause}", params)
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
            cursor.execute(
                f"SELECT id FROM ids WHERE created_by = {_param()} AND {hidden_cond}",
                (username,)
            )
        else:
            cursor.execute(f"SELECT id FROM ids WHERE {hidden_cond}")
        source_key_ids = [_get_row_value(row, "id") for row in cursor.fetchall()]
        _invalidate_old_card_pool_sources(cursor, source_key_ids)

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
                f"SELECT id FROM ids WHERE id IN ({placeholders}) AND created_by = {_param()}",
                (*id_list, username)
            )
            owned_ids = [_get_row_value(row, "id") for row in cursor.fetchall()]
            _invalidate_old_card_pool_sources(cursor, owned_ids)
            cursor.execute(
                f"DELETE FROM ids WHERE id IN ({placeholders}) AND created_by = {_param()}",
                (*id_list, username)
            )
        else:
            _invalidate_old_card_pool_sources(cursor, id_list)
            cursor.execute(
                f"DELETE FROM ids WHERE id IN ({placeholders})",
                id_list
            )

        return cursor.rowcount


def delete_unused_ids_by_type(card_type=None, username=None, key_kind=None):
    """
    删除未使用的卡密（按类型过滤）
    """
    _init_db()
    with _get_cursor() as cursor:
        if USE_SQLITE:
            conditions = ["used = 0", "(destroyed = 0 OR destroyed IS NULL)", "(hidden = 0 OR hidden IS NULL)"]
        else:
            conditions = ["used = FALSE", "(destroyed = FALSE OR destroyed IS NULL)", "(hidden = FALSE OR hidden IS NULL)"]
        params = []

        if username:
            conditions.append(f"created_by = {_param()}")
            params.append(username)

        if card_type:
            conditions.append(f"card_type = {_param()}")
            params.append(card_type)

        _append_key_kind_condition(conditions, params, key_kind)
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
                "key_kind": _normalize_key_kind(_get_row_value(row, "key_kind"), default="normal"),
                "expire_minutes": _get_row_value(row, "expire_minutes"),
                "used": bool(_get_row_value(row, "used")),
                "used_time": _normalize_iso_to_utc(_get_row_value(row, "used_time")),
                "destroyed": bool(_get_row_value(row, "destroyed")),
                "destroyed_time": _get_row_value(row, "destroyed_time"),
                "bound_display_channel_id": _get_row_value(row, "bound_display_channel_id"),
                "bound_display_channel_name": _get_row_value(row, "bound_display_channel_name"),
                "bound_backend_channel_id": _get_row_value(row, "bound_backend_channel_id"),
                "bound_channel_head": _get_row_value(row, "bound_channel_head"),
                "channel_binding_enabled": bool(
                    _get_row_value(row, "bound_display_channel_id")
                    and _get_row_value(row, "bound_backend_channel_id")
                )
            })

        first = rows[0]
        return {
            "token": token,
            "note": _get_row_value(first, "hidden_note"),
            "created_time": _normalize_iso_to_utc(_get_row_value(first, "created_time")),
            "created_by": _get_row_value(first, "created_by"),
            "card_type": _get_row_value(first, "card_type"),
            "key_kind": _normalize_key_kind(_get_row_value(first, "key_kind"), default="normal"),
            "bound_display_channel_id": _get_row_value(first, "bound_display_channel_id"),
            "bound_display_channel_name": _get_row_value(first, "bound_display_channel_name"),
            "bound_backend_channel_id": _get_row_value(first, "bound_backend_channel_id"),
            "bound_channel_head": _get_row_value(first, "bound_channel_head"),
            "channel_binding_enabled": bool(
                _get_row_value(first, "bound_display_channel_id")
                and _get_row_value(first, "bound_backend_channel_id")
            ),
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
                "key_kind": _normalize_key_kind(_get_row_value(row, "key_kind"), default="normal"),
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

        _invalidate_old_card_pool_sources(cursor, [key_id], reason="来源卡片记录已清除")
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
                SELECT id FROM ids
                WHERE {used_cond} AND redeemed_card IS NOT NULL AND created_by = {_param()}
            ''', (username,))
        else:
            cursor.execute(f'''
                SELECT id FROM ids
                WHERE {used_cond} AND redeemed_card IS NOT NULL
            ''')
        source_key_ids = [_get_row_value(row, "id") for row in cursor.fetchall()]
        _invalidate_old_card_pool_sources(cursor, source_key_ids, reason="来源卡片记录已清除")

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
