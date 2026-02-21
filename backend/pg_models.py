"""
Platform Database — PostgreSQL
================================
Replaces the SQLite platform.db with PostgreSQL for production scalability.
Uses asyncpg for async connection pooling — handles 10-100+ concurrent users efficiently.

Setup:
  pip install asyncpg psycopg2-binary
  export DATABASE_URL=postgresql://user:pass@localhost:5432/orbit

Per-user agent data (messages, memory, episodes) stays in per-user SQLite.
Only platform-level data lives here: users, sessions, contacts, settings.
"""

import os
import json
import asyncio
import asyncpg
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

# Load .env from backend directory
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://orbit:orbit@localhost:5432/orbit")

# Global pool — shared across all requests
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,          # 10 connections handle 100+ concurrent requests
            max_inactive_connection_lifetime=300,
            command_timeout=30,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Schema init ───────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS whatsapp_sessions (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    wa_jid TEXT,
    wa_name TEXT,
    wa_number TEXT,
    status TEXT DEFAULT 'disconnected',
    agent_running BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_connected TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    jid TEXT NOT NULL,
    name TEXT,
    number TEXT,
    avatar_url TEXT,
    last_seen TIMESTAMPTZ,
    is_group BOOLEAN DEFAULT FALSE,
    synced_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, jid)
);

CREATE INDEX IF NOT EXISTS idx_contacts_user ON contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts(user_id, name, number);

CREATE TABLE IF NOT EXISTS contact_settings (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    contact_jid TEXT NOT NULL,
    is_allowed BOOLEAN DEFAULT TRUE,
    custom_tone TEXT,
    custom_language TEXT,
    notes TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, contact_jid)
);

CREATE INDEX IF NOT EXISTS idx_contact_settings_user ON contact_settings(user_id);
CREATE INDEX IF NOT EXISTS idx_contact_settings_allowed ON contact_settings(user_id, is_allowed);

CREATE TABLE IF NOT EXISTS agent_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    soul_override TEXT,
    debounce_seconds INTEGER DEFAULT 8,
    auto_respond BOOLEAN DEFAULT TRUE,
    tts_enabled BOOLEAN DEFAULT TRUE,
    model TEXT DEFAULT 'gpt-4o',
    temperature REAL DEFAULT 0.75,
    handoff_intents JSONB DEFAULT '["money","emergency"]'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_expiry ON auth_tokens(expires_at);

-- Media description cache: stores AI-analyzed text from media, NO files
-- Key = SHA-256 of original media bytes, value = extracted text
CREATE TABLE IF NOT EXISTS media_descriptions (
    file_hash TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    access_count INTEGER DEFAULT 1,
    last_accessed TIMESTAMPTZ DEFAULT NOW()
);
"""


async def init_schema():
    """Initialize the database schema. Safe to call multiple times."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


# ── PlatformDB class ──────────────────────────────────────────────────────────

class PlatformDB:
    """
    Async PostgreSQL-backed platform database.
    Drop-in replacement for the SQLite version but fully async.
    """

    def __init__(self):
        self._init_done = False

    async def ensure_init(self):
        if not self._init_done:
            await init_schema()
            self._init_done = True

    async def _pool(self) -> asyncpg.Pool:
        return await get_pool()

    # ── Users ─────────────────────────────────────────────────────────────────

    async def upsert_user(self, google_sub: str, email: str, name: str, avatar_url: str) -> Dict:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO users (id, email, name, avatar_url)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE SET
                    email = EXCLUDED.email,
                    name = EXCLUDED.name,
                    avatar_url = EXCLUDED.avatar_url,
                    last_login = NOW()
                RETURNING *
            """, google_sub, email, name, avatar_url)

            # Init dependent records
            await conn.execute("""
                INSERT INTO whatsapp_sessions (user_id) VALUES ($1)
                ON CONFLICT DO NOTHING
            """, google_sub)
            await conn.execute("""
                INSERT INTO agent_settings (user_id) VALUES ($1)
                ON CONFLICT DO NOTHING
            """, google_sub)

            return dict(row)

    async def get_user(self, user_id: str) -> Optional[Dict]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            return dict(row) if row else None

    # ── WhatsApp Sessions ─────────────────────────────────────────────────────

    async def update_wa_status(self, user_id: str, status: str, wa_jid: str = None,
                                wa_name: str = None, wa_number: str = None):
        pool = await self._pool()
        async with pool.acquire() as conn:
            if wa_jid:
                await conn.execute("""
                    UPDATE whatsapp_sessions SET
                        status = $2, wa_jid = $3, wa_name = $4, wa_number = $5,
                        last_connected = NOW()
                    WHERE user_id = $1
                """, user_id, status, wa_jid, wa_name, wa_number)
            else:
                await conn.execute(
                    "UPDATE whatsapp_sessions SET status = $2 WHERE user_id = $1",
                    user_id, status
                )

    async def set_agent_running(self, user_id: str, running: bool):
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE whatsapp_sessions SET agent_running = $2 WHERE user_id = $1",
                user_id, running
            )

    async def get_wa_session(self, user_id: str) -> Optional[Dict]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM whatsapp_sessions WHERE user_id = $1", user_id
            )
            return dict(row) if row else None

    async def get_all_connected_sessions(self) -> List[Dict]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            # Restore based on agent_running flag rather than status='connected'
            # because status might be 'close' if the previous shutdown was messy
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_sessions WHERE agent_running = TRUE"
            )
            return [dict(r) for r in rows]

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def upsert_contacts(self, user_id: str, contacts: List[Dict]):
        if not contacts:
            return
        
        # Prepare data for executemany
        data = [
            (
                user_id, 
                c["jid"], 
                c.get("name"), 
                c.get("number"), 
                bool(c.get("is_group"))
            ) for c in contacts
        ]

        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany("""
                    INSERT INTO contacts (user_id, jid, name, number, is_group)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id, jid) DO UPDATE SET
                        name = CASE 
                            WHEN EXCLUDED.name IS NOT NULL AND EXCLUDED.name != '' 
                                 AND EXCLUDED.name NOT LIKE '+%'
                            THEN EXCLUDED.name
                            WHEN contacts.name IS NOT NULL AND contacts.name != ''
                            THEN contacts.name
                            ELSE EXCLUDED.name
                        END,
                        number = COALESCE(EXCLUDED.number, contacts.number),
                        synced_at = NOW()
                """, data)


    async def get_contacts(self, user_id: str, search: str = None,
                           is_group: bool = False) -> List[Dict]:
        """
        Get contacts for a user.
        Excludes groups by default. Displays real people.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            query = """
                SELECT c.*, 
                       COALESCE(cs.is_allowed, FALSE) AS is_allowed,
                       cs.custom_tone, cs.custom_language
                FROM contacts c
                LEFT JOIN contact_settings cs 
                    ON cs.user_id = c.user_id AND cs.contact_jid = c.jid
                WHERE c.user_id = $1
            """
            params = [user_id]

            # Requirement: No groups by default
            if is_group is not None:
                params.append(is_group)
                query += f" AND c.is_group = ${len(params)}"
            
            # Show if they have a name OR if they are explicitly allowed/configured
            # This allows managing "unsaved" contacts that you want the agent to handle
            query += " AND (c.name IS NOT NULL AND c.name != '' OR cs.id IS NOT NULL)"

            if search:
                params.append(f"%{search}%")
                n = len(params)
                query += f" AND (c.name ILIKE ${n} OR c.number LIKE ${n} OR c.jid LIKE ${n})"

            query += " ORDER BY c.name ASC NULLS LAST"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    # ── Contact Settings (Allowlist) ──────────────────────────────────────────

    async def update_contact_setting(self, user_id: str, contact_jid: str,
                                      is_allowed: bool, custom_tone: str = None,
                                      custom_language: str = None):
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO contact_settings (user_id, contact_jid, is_allowed, custom_tone, custom_language)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, contact_jid) DO UPDATE SET
                    is_allowed = EXCLUDED.is_allowed,
                    custom_tone = COALESCE(EXCLUDED.custom_tone, contact_settings.custom_tone),
                    custom_language = COALESCE(EXCLUDED.custom_language, contact_settings.custom_language),
                    updated_at = NOW()
            """, user_id, contact_jid, is_allowed, custom_tone, custom_language)

    async def bulk_update_allowlist(self, user_id: str, allowed_jids: List[str]):
        """Atomically replace the entire allowlist."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Set all existing to blocked
                await conn.execute(
                    "UPDATE contact_settings SET is_allowed = FALSE, updated_at = NOW() WHERE user_id = $1",
                    user_id
                )
                
                if not allowed_jids:
                    return

                # Prepare data for executemany
                data = [(user_id, jid) for jid in allowed_jids]

                # Upsert allowed ones in bulk
                await conn.executemany("""
                    INSERT INTO contact_settings (user_id, contact_jid, is_allowed)
                    VALUES ($1, $2, TRUE)
                    ON CONFLICT (user_id, contact_jid) DO UPDATE SET
                        is_allowed = TRUE, updated_at = NOW()
                """, data)

    async def get_allowed_jids(self, user_id: str) -> List[str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT contact_jid FROM contact_settings WHERE user_id = $1 AND is_allowed = TRUE",
                user_id
            )
            return [r["contact_jid"] for r in rows]

    # ── Agent Settings ────────────────────────────────────────────────────────

    async def get_agent_settings(self, user_id: str) -> Dict:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_settings WHERE user_id = $1", user_id
            )
            if not row:
                return {}
            d = dict(row)
            # asyncpg returns JSONB as dict/list natively
            if isinstance(d.get("handoff_intents"), str):
                try:
                    d["handoff_intents"] = json.loads(d["handoff_intents"])
                except Exception:
                    d["handoff_intents"] = ["money", "emergency"]
            return d

    async def update_agent_settings(self, user_id: str, settings: Dict):
        allowed_fields = {
            "soul_override", "debounce_seconds", "auto_respond",
            "tts_enabled", "model", "temperature", "handoff_intents"
        }
        fields = {k: v for k, v in settings.items() if k in allowed_fields}
        if not fields:
            return

        pool = await self._pool()
        async with pool.acquire() as conn:
            set_parts = []
            vals = []
            for i, (k, v) in enumerate(fields.items(), start=2):
                if k == "handoff_intents" and isinstance(v, list):
                    set_parts.append(f"{k} = ${i}::jsonb")
                    vals.append(json.dumps(v))
                else:
                    set_parts.append(f"{k} = ${i}")
                    vals.append(v)

            await conn.execute(
                f"UPDATE agent_settings SET {', '.join(set_parts)}, updated_at = NOW() WHERE user_id = $1",
                user_id, *vals
            )

    # ── Media Description Cache (replaces filesystem cache) ───────────────────

    async def get_media_description(self, file_hash: str) -> Optional[str]:
        """Look up a cached AI description by file hash."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT description FROM media_descriptions WHERE file_hash = $1",
                file_hash
            )
            if row:
                # Update access stats non-blocking
                asyncio.create_task(self._bump_media_access(file_hash))
                return row["description"]
            return None

    async def save_media_description(self, file_hash: str, media_type: str, description: str):
        """Cache an AI description for a media file."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO media_descriptions (file_hash, media_type, description)
                VALUES ($1, $2, $3)
                ON CONFLICT (file_hash) DO UPDATE SET
                    access_count = media_descriptions.access_count + 1,
                    last_accessed = NOW()
            """, file_hash, media_type, description)

    async def _bump_media_access(self, file_hash: str):
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE media_descriptions SET access_count = access_count + 1, last_accessed = NOW() WHERE file_hash = $1",
                file_hash
            )

    async def prune_old_media_cache(self, keep_days: int = 30):
        """Clean up old rarely-accessed media descriptions."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            deleted = await conn.execute("""
                DELETE FROM media_descriptions
                WHERE last_accessed < NOW() - INTERVAL '$1 days'
                AND access_count < 3
            """, keep_days)
            return deleted


# ── Sync wrapper for compatibility with non-async code ────────────────────────

class SyncPlatformDB:
    """
    Synchronous wrapper around PlatformDB for places where async isn't available.
    Creates its own event loop. Use sparingly — prefer async version.
    """
    def __init__(self):
        self._async_db = PlatformDB()

    def _run(self, coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In async context — use concurrent future
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, coro)
                    return future.result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    def __getattr__(self, name):
        async_method = getattr(self._async_db, name)
        def wrapper(*args, **kwargs):
            return self._run(async_method(*args, **kwargs))
        return wrapper