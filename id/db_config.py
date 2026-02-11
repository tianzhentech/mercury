"""
PostgreSQL 数据库配置

优先从 settings.json 读取配置，如果不存在则使用环境变量，
最后使用默认值。
"""

import os
import json

# 尝试从 settings.json 读取配置
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")

def _load_settings():
    """加载 settings.json 中的配置"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                return settings.get("db_config", {})
        except:
            pass
    return {}

_settings_db_config = _load_settings()

# 数据库连接配置（优先 settings.json > 环境变量 > 默认值）
DB_CONFIG = {
    "host": _settings_db_config.get("host") or os.environ.get("PG_HOST", "localhost"),
    "port": int(_settings_db_config.get("port") or os.environ.get("PG_PORT", 5432)),
    "database": _settings_db_config.get("database") or os.environ.get("PG_DATABASE", "mercury"),
    "user": _settings_db_config.get("user") or os.environ.get("PG_USER", "mercury"),
    "password": _settings_db_config.get("password") or os.environ.get("PG_PASSWORD", "mercury123"),
    "connect_timeout": 10  # 连接超时 10 秒
}

# 连接池配置
POOL_MIN_CONN = int(_settings_db_config.get("pool_min") or os.environ.get("PG_POOL_MIN", 2))
POOL_MAX_CONN = int(_settings_db_config.get("pool_max") or os.environ.get("PG_POOL_MAX", 20))

# 是否使用 SQLite（用于回滚或本地开发）
# 如果 settings.json 中有 db_config，默认使用 PostgreSQL
USE_SQLITE = os.environ.get("USE_SQLITE", "true" if not _settings_db_config else "false").lower() == "true"
