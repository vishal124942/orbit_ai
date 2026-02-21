import asyncio
import os
from dotenv import load_dotenv

load_dotenv("backend/.env")

from src.core.database import Database
# Also, to drop all users from the platform DB:
import asyncpg

async def reset_db():
    print("Clearing Neon DB...")
    conn = await asyncpg.connect(os.getenv("PLATFORM_DB_URL"))
    await conn.execute("TRUNCATE TABLE users CASCADE")
    await conn.execute("TRUNCATE TABLE agent_sessions CASCADE")
    await conn.close()
    print("Done clearing Neon DB. All users deleted.")

if __name__ == "__main__":
    asyncio.run(reset_db())
