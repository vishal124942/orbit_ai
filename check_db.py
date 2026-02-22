import asyncio
import os
from backend.pg_models import PlatformDB

async def main():
    db = PlatformDB()
    # Manual init since we aren't using the pool global
    import asyncpg
    from dotenv import load_dotenv
    load_dotenv("backend/.env")
    url = os.getenv("DATABASE_URL")
    print(f"Connecting to {url.split('@')[-1]}")
    conn = await asyncpg.connect(url)
    
    rows = await conn.fetch("SELECT * FROM contacts")
    print(f"\nTotal contacts in DB: {len(rows)}")
    for r in rows[:20]:
        print(f" - {r['jid']}: {r['name']} (Group: {r['is_group']})")
    
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
