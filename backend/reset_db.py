import asyncio
import os
from dotenv import load_dotenv

load_dotenv("backend/.env")

from src.core.database import Database
# Also, to drop all users from the platform DB:
import asyncpg

async def reset_db():
    print("Clearing Platform PostgreSQL DB...")
    url = os.getenv("DATABASE_URL")
    if not url:
        print("❌ DATABASE_URL not found in environment.")
        return
        
    conn = await asyncpg.connect(url)
    tables = [
        "auth_tokens",
        "contact_settings",
        "contacts",
        "whatsapp_sessions",
        "agent_settings",
        "users",
        "media_descriptions"
    ]
    for table in tables:
        try:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
            print(f" - Truncated {table}")
        except Exception as e:
            print(f" - Error truncating {table}: {e}")
            
    await conn.close()
    print("✅ Done clearing database.")

if __name__ == "__main__":
    asyncio.run(reset_db())
