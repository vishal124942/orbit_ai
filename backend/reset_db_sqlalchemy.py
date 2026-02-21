import asyncio
from dotenv import load_dotenv

load_dotenv("backend/.env")

from pg_models import get_pool

async def reset_db():
    print("Truncating all platform tables...")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE users CASCADE")
        await conn.execute("TRUNCATE TABLE media_descriptions CASCADE")
    print("All users and media descriptions truncated.")

if __name__ == "__main__":
    asyncio.run(reset_db())
