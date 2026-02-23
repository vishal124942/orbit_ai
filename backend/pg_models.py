"""
Platform Database — PostgreSQL

FIXES applied:
1. upsert_contacts: derive `number` from the JID for @s.whatsapp.net contacts when
   no explicit number is supplied.
2. get_contacts: relaxed filter so contacts with only a number are also shown.
3. upsert_contacts: two-pass bulk+per-row fallback so one bad row never drops the batch.
4. upsert_contacts: detailed logging.
5. get_contacts: is_group=None default returns ALL contacts.
6. prune_old_media_cache: FIXED SQL bug — asyncpg cannot interpolate $1 inside a
   string literal like INTERVAL '$1 days'. Changed to ($1 || ' days')::INTERVAL.
"""

import os
import json
import asyncio
import asyncpg
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://orbit:orbit@localhost:5432/orbit")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
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
CREATE INDEX IF NOT EXISTS idx_contacts_synced ON contacts(user_id, synced_at);

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
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _derive_number_from_jid(jid: str) -> str:
    if not jid or not jid.endswith("@s.whatsapp.net"):
        return ""
    raw = jid.split("@")[0]
    if not raw.isdigit():
        return ""
    if raw.startswith("91") and len(raw) == 12:
        return f"+91 {raw[2:7]} {raw[7:]}"
    return f"+{raw}"


import logging
_logger = logging.getLogger(__name__)

_CONTACT_UPSERT_SQL = """
    INSERT INTO contacts (user_id, jid, name, number, is_group)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (user_id, jid) DO UPDATE SET
        name = CASE
            WHEN EXCLUDED.name IS NOT NULL AND EXCLUDED.name != ''
                 AND EXCLUDED.name NOT LIKE '+%'
                 AND EXCLUDED.name NOT LIKE '~%'
            THEN EXCLUDED.name
            WHEN contacts.name IS NOT NULL AND contacts.name != ''
                 AND contacts.name NOT LIKE '+%'
                 AND contacts.name NOT LIKE '~%'
            THEN contacts.name
            ELSE COALESCE(EXCLUDED.name, contacts.name)
        END,
        number = COALESCE(EXCLUDED.number, contacts.number),
        synced_at = NOW()
"""


class PlatformDB:
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
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_sessions WHERE agent_running = TRUE"
            )
            return [dict(r) for r in rows]

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def upsert_contacts(self, user_id: str, contacts: List[Dict]):
        if not contacts:
            return

        data = []
        for c in contacts:
            jid = c.get("jid", "")
            if not jid:
                continue
            explicit_number = c.get("number") or ""
            number = explicit_number or _derive_number_from_jid(jid) or None
            name = c.get("name") or None
            data.append((
                user_id,
                jid,
                name,
                number,
                bool(c.get("is_group", False)),
            ))

        if not data:
            return

        pool = await self._pool()

        # Pass 1: Bulk fast path
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(_CONTACT_UPSERT_SQL, data)
            _logger.info(
                f"[PlatformDB] upsert_contacts: bulk OK — {len(data)} rows for {user_id}"
            )
            return
        except Exception as bulk_err:
            _logger.warning(
                f"[PlatformDB] upsert_contacts: bulk failed for {user_id} "
                f"({len(data)} rows): {bulk_err} — falling back to per-row mode"
            )

        # Pass 2: Per-row fallback — never drops the whole batch
        succeeded = 0
        failed = 0
        async with pool.acquire() as conn:
            for row in data:
                try:
                    async with conn.transaction():
                        await conn.execute(_CONTACT_UPSERT_SQL, *row)
                    succeeded += 1
                except Exception as row_err:
                    failed += 1
                    _logger.debug(
                        f"[PlatformDB] upsert_contacts: skipped row jid={row[1]}: {row_err}"
                    )

        _logger.info(
            f"[PlatformDB] upsert_contacts per-row fallback: "
            f"{succeeded} OK, {failed} skipped for {user_id}"
        )

    async def get_contacts(self, user_id: str, search: str = None,
                           is_group: Optional[bool] = None) -> List[Dict]:
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

            if is_group is not None:
                params.append(is_group)
                query += f" AND c.is_group = ${len(params)}"

            query += """
                AND (
                    (c.name   IS NOT NULL AND c.name   != '')
                    OR (c.number IS NOT NULL AND c.number != '')
                    OR cs.id IS NOT NULL
                )
            """

            if search:
                params.append(f"%{search}%")
                n = len(params)
                query += f" AND (c.name ILIKE ${n} OR c.number LIKE ${n} OR c.jid LIKE ${n})"

            query += " ORDER BY c.name ASC NULLS LAST"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def get_contact_sync_status(self, user_id: str, minutes: int = 10) -> Dict:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE synced_at > NOW() - ($2 || ' minutes')::INTERVAL
                    ) AS recent
                FROM contacts
                WHERE user_id = $1
            """, user_id, str(minutes))
            return {
                "total": row["total"] if row else 0,
                "recent_window_minutes": minutes,
                "recently_synced": row["recent"] if row else 0,
            }

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
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE contact_settings SET is_allowed = FALSE, updated_at = NOW() WHERE user_id = $1",
                    user_id
                )
                if not allowed_jids:
                    return
                data = [(user_id, jid) for jid in allowed_jids]
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

    # ── Media Description Cache ───────────────────────────────────────────────

    async def get_media_description(self, file_hash: str) -> Optional[str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT description FROM media_descriptions WHERE file_hash = $1",
                file_hash
            )
            if row:
                asyncio.create_task(self._bump_media_access(file_hash))
                return row["description"]
            return None

    async def save_media_description(self, file_hash: str, media_type: str, description: str):
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
        pool = await self._pool()
        async with pool.acquire() as conn:
            # BUG FIX: The original query was:
            #   WHERE last_accessed < NOW() - INTERVAL '$1 days'
            # asyncpg does NOT interpolate $1 inside a SQL string literal.
            # The '$1' was treated as the literal text "$1 days", making the
            # INTERVAL invalid and crashing the maintenance loop every 24h.
            # Fix: cast the parameter as an interval using string concatenation.
            deleted = await conn.execute("""
                DELETE FROM media_descriptions
                WHERE last_accessed < NOW() - ($1 || ' days')::INTERVAL
                AND access_count < 3
            """, str(keep_days))
            return deleted


# ── Sync wrapper ──────────────────────────────────────────────────────────────

class SyncPlatformDB:
    """Synchronous wrapper. Use sparingly — prefer the async version."""
    def __init__(self):
        self._async_db = PlatformDB()

    def _run(self, coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    def __getattr__(self, name):
        async_method = getattr(self._async_db, name)
        def wrapper(*args, **kwargs):
            return self._run(async_method(*args, **kwargs))
        return wrapper