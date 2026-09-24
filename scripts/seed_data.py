# scripts/seed_data.py
# 执行：python scripts/seed_data.py
# 用途：灌入本地开发测试账号

import asyncio
import uuid
import os
import asyncpg                       # PostgreSQL 异步驱动（脚本直接用它，简单直接）
from passlib.context import CryptContext
from backend.config import get_settings


s = get_settings()  # 获取环境变量中的配置信息  数据库+连接引擎+连接信息
# 用环境变量拼出 asyncpg 的连接串（注意 asyncpg 用的是 postgresql:// 而非 +asyncpg）
DB_DSN = (
    f"postgresql://{s.db_user}:{s.db_password}"
    f"@{s.db_host}:{s.db_port}"
    f"/{s.db_name}"
)

print(f'DB_DSN: {DB_DSN}')
# print(DB_DSN)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
TENANT_ID = "tenant_default"

async def seed_users():
    """灌入 4 个测试账号（已存在则跳过）。"""
    conn = await asyncpg.connect(DB_DSN)             # 连接数据库
    print("✅ 数据库连接成功，开始灌入测试账号...")
    try:
        users = [
            {"username": "admin",     "email": "admin@qq.com",     "pwd": "123456",   "role": "admin"},
            {"username": "Lee01", "email": "Lee01@eduagent.local", "pwd": "Lee@123456", "role": "user"},
            {"username": "Sra01", "email": "Sra01@eduagent.local", "pwd": "Sra@123456", "role": "user"},
            {"username": "Sra02", "email": "Sra02@eduagent.local", "pwd": "Sra@123456", "role": "user"},
        ]
        for u in users:
            await conn.execute(
                """
                INSERT INTO users (id, tenant_id, username, email, password_hash, role)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, email) DO NOTHING
                """,
                str(uuid.uuid4()), TENANT_ID, u["username"], u["email"],
                pwd_context.hash(u["pwd"]),          # 存哈希，绝不存明文
                u["role"],
            )
        print(f"✅ 测试账号灌入完成（{len(users)} 个，已存在则跳过）：")
        print("   admin@eduagent.local      / Admin@123456")
        print("   Lee01@eduagent.local  / Lee@123456")
        print("   Sra01@eduagent.local  / Sra@123456")
        print("   Sra02@eduagent.local  / Sra@123456")
    finally:
        await conn.close()                           # 无论成败都关闭连接


if __name__ == "__main__":
    asyncio.run(seed_users())