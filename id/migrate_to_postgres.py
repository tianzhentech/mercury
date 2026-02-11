#!/usr/bin/env python3
"""
SQLite 到 PostgreSQL 数据迁移脚本

使用方法:
    python3 migrate_to_postgres.py

注意：
    1. 运行前确保 PostgreSQL 已安装并创建好数据库
    2. 确保 db_config.py 中的连接信息正确
    3. 此脚本会将 SQLite 中的所有数据迁移到 PostgreSQL
"""

import os
import sys
import json
import sqlite3

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("错误: 请先安装 psycopg2-binary")
    print("运行: pip install psycopg2-binary")
    sys.exit(1)

from db_config import DB_CONFIG

# SQLite 数据库路径
SQLITE_DB = os.path.join(os.path.dirname(__file__), "id.db")


def migrate():
    """执行迁移"""
    
    # 检查 SQLite 数据库是否存在
    if not os.path.exists(SQLITE_DB):
        print(f"错误: SQLite 数据库不存在: {SQLITE_DB}")
        sys.exit(1)
    
    print(f"[1/5] 连接 SQLite 数据库: {SQLITE_DB}")
    sqlite_conn = sqlite3.connect(SQLITE_DB)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()
    
    # 获取 SQLite 记录数
    sqlite_cursor.execute("SELECT COUNT(*) FROM ids")
    sqlite_count = sqlite_cursor.fetchone()[0]
    print(f"      SQLite 中共有 {sqlite_count:,} 条记录")
    
    if sqlite_count == 0:
        print("      SQLite 数据库为空，无需迁移")
        return
    
    print(f"[2/5] 连接 PostgreSQL 数据库...")
    try:
        pg_conn = psycopg2.connect(**DB_CONFIG)
        pg_cursor = pg_conn.cursor()
        print(f"      已连接到 {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    except Exception as e:
        print(f"错误: 无法连接 PostgreSQL: {e}")
        print("\n请检查:")
        print("  1. PostgreSQL 服务是否运行")
        print("  2. db_config.py 中的连接信息是否正确")
        print("  3. 是否已创建数据库和用户")
        sys.exit(1)
    
    print("[3/5] 创建 PostgreSQL 表结构...")
    pg_cursor.execute('''
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
            hidden_note TEXT
        )
    ''')
    # 创建索引
    pg_cursor.execute('CREATE INDEX IF NOT EXISTS idx_used ON ids(used)')
    pg_cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_by ON ids(created_by)')
    pg_cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_time ON ids(created_time)')
    pg_conn.commit()
    print("      表结构创建完成")
    
    # 检查 PostgreSQL 是否已有数据
    pg_cursor.execute("SELECT COUNT(*) FROM ids")
    pg_count = pg_cursor.fetchone()[0]
    if pg_count > 0:
        print(f"\n警告: PostgreSQL 中已有 {pg_count:,} 条记录")
        response = input("是否清空并重新迁移? (y/N): ").strip().lower()
        if response != 'y':
            print("取消迁移")
            return
        pg_cursor.execute("DELETE FROM ids")
        pg_conn.commit()
        print("      已清空 PostgreSQL 数据")
    
    print(f"[4/5] 迁移数据 (共 {sqlite_count:,} 条)...")
    
    # 批量读取和插入
    batch_size = 5000
    offset = 0
    migrated = 0
    
    while True:
        sqlite_cursor.execute(f"SELECT * FROM ids LIMIT {batch_size} OFFSET {offset}")
        rows = sqlite_cursor.fetchall()
        
        if not rows:
            break
        
        # 准备批量插入数据
        values = []
        for row in rows:
            values.append((
                row["id"],
                row["expire_minutes"],
                row["card_limit"],
                row["card_type"],
                row["created_time"],
                bool(row["used"]),
                row["used_time"],
                row["created_by"],
                row["redeemed_card"],
                bool(row["destroyed"]) if row["destroyed"] is not None else False,
                row["destroyed_time"] if "destroyed_time" in row.keys() else None,
                bool(row["hidden"]) if "hidden" in row.keys() and row["hidden"] is not None else False,
                row["hidden_token"] if "hidden_token" in row.keys() else None,
                row["hidden_note"] if "hidden_note" in row.keys() else None
            ))
        
        # 批量插入
        execute_values(
            pg_cursor,
            """INSERT INTO ids 
               (id, expire_minutes, card_limit, card_type, created_time, used, used_time, 
                created_by, redeemed_card, destroyed, destroyed_time, hidden, hidden_token, hidden_note)
               VALUES %s
               ON CONFLICT (id) DO NOTHING""",
            values
        )
        pg_conn.commit()
        
        migrated += len(rows)
        progress = (migrated / sqlite_count) * 100
        print(f"      进度: {migrated:,}/{sqlite_count:,} ({progress:.1f}%)")
        
        offset += batch_size
    
    print(f"[5/5] 验证迁移结果...")
    pg_cursor.execute("SELECT COUNT(*) FROM ids")
    final_pg_count = pg_cursor.fetchone()[0]
    
    if final_pg_count == sqlite_count:
        print(f"      ✓ 迁移成功! PostgreSQL 中共 {final_pg_count:,} 条记录")
    else:
        print(f"      ⚠ 警告: 数量不一致")
        print(f"        SQLite: {sqlite_count:,}")
        print(f"        PostgreSQL: {final_pg_count:,}")
    
    # 关闭连接
    sqlite_cursor.close()
    sqlite_conn.close()
    pg_cursor.close()
    pg_conn.close()
    
    print("\n迁移完成!")
    print("\n下一步:")
    print("  1. 修改 db_config.py 中的 USE_SQLITE = False")
    print("  2. 重启 web_server.py")
    print("  3. 测试功能是否正常")


if __name__ == "__main__":
    migrate()
