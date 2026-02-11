"""
迁移脚本：为现有记录填充 pan 字段（仅 PostgreSQL）

运行方式:
    cd id/
    python3 migrate_pan.py

此脚本会扫描所有已使用的卡密记录，从 redeemed_card JSON 中提取 pan 并更新到 pan 列。
连接信息从 settings.json 读取。
"""

import json
import os
import sys

# 直接从同目录导入
from db_config import DB_CONFIG

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor


def migrate_pan():
    """迁移现有记录的 pan 字段"""
    
    print(f"连接到 PostgreSQL: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    print(f"用户: {DB_CONFIG['user']}")
    print()
    
    pg_conn_pool = pg_pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        **DB_CONFIG
    )
    conn = pg_conn_pool.getconn()
    
    try:
        # 使用 autocommit 执行 DDL 操作
        conn.autocommit = True
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # 先创建 pan 列（如果不存在）
        print("[准备] 检查并创建 pan 列...")
        try:
            cursor.execute("ALTER TABLE ids ADD COLUMN pan TEXT")
            print("[准备] pan 列已创建")
        except Exception as e:
            if "already exists" in str(e) or "已经存在" in str(e):
                print("[准备] pan 列已存在")
            else:
                print(f"[准备] 创建列时出错: {e}")
        
        # 创建索引（如果不存在）
        print("[准备] 检查并创建 pan 索引...")
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pan ON ids(pan)")
            print("[准备] pan 索引已就绪")
        except Exception as e:
            print(f"[准备] 创建索引时出错: {e}")
        
        cursor.close()
        
        # 关闭 autocommit 进行数据迁移
        conn.autocommit = False
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        print()
        # 查找所有 pan 为空但有 redeemed_card 的记录
        cursor.execute("""
            SELECT id, redeemed_card FROM ids 
            WHERE used = TRUE 
            AND redeemed_card IS NOT NULL 
            AND (pan IS NULL OR pan = '')
        """)
        
        rows = cursor.fetchall()
        total = len(rows)
        updated = 0
        failed = 0
        
        print(f"[迁移] 找到 {total} 条需要迁移的记录")
        
        for i, row in enumerate(rows):
            record_id = row['id']
            redeemed_card_raw = row['redeemed_card']
            
            try:
                card = json.loads(redeemed_card_raw)
                raw_pan = card.get("pan", "")
                clean_pan = ''.join(c for c in str(raw_pan) if c.isdigit())
                
                if clean_pan:
                    cursor.execute(
                        "UPDATE ids SET pan = %s WHERE id = %s",
                        (clean_pan, record_id)
                    )
                    updated += 1
                else:
                    failed += 1
                    
            except Exception as e:
                failed += 1
                print(f"[迁移] 记录 {record_id} 处理失败: {e}")
            
            # 每 1000 条提交一次
            if (i + 1) % 1000 == 0:
                conn.commit()
                print(f"[迁移] 进度: {i + 1}/{total}")
        
        conn.commit()
        print(f"\n[迁移完成] 成功更新 {updated} 条记录，失败 {failed} 条")
        
    finally:
        cursor.close()
        pg_conn_pool.putconn(conn)
        pg_conn_pool.closeall()


if __name__ == "__main__":
    print("=" * 50)
    print("PAN 字段迁移脚本 (PostgreSQL)")
    print("=" * 50)
    print()
    
    migrate_pan()
