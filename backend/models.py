"""
Multi-Tenant Database Models
Each user gets isolated data: their own SQLite agent DB, WhatsApp session, and memory.
Platform-level data (users, settings, contacts) lives in a central SQLite DB.
"""

import sqlite3
import os
import json
from datetime import datetime
from typing import Optional, List, Dict, Any


PLATFORM_DB = "data/platform.db"


class PlatformDB:
    """Central platform database — users, sessions, contact settings."""

    def __init__(self, db_path: str = PLATFORM_DB):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        c = self.conn.cursor()

        # ── Users ─────────────────────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,               -- Google sub (unique)
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                avatar_url TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        # ── WhatsApp sessions (per user) ───────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_sessions (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                wa_jid TEXT,                        -- user's WhatsApp JID after pairing
                wa_name TEXT,
                wa_number TEXT,
                status TEXT DEFAULT 'disconnected', -- disconnected/pairing/connected
                agent_running INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_connected DATETIME
            )
        """)

        # ── Contacts (per user, synced from WhatsApp) ─────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                jid TEXT NOT NULL,                  -- WhatsApp JID
                name TEXT,
                number TEXT,
                avatar_url TEXT,
                last_seen DATETIME,
                is_group INTEGER DEFAULT 0,
                synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, jid)
            )
        """)

        # ── Contact allowlist (which contacts the agent is allowed to talk to)
        c.execute("""
            CREATE TABLE IF NOT EXISTS contact_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                contact_jid TEXT NOT NULL,
                is_allowed INTEGER DEFAULT 1,       -- agent can auto-reply
                custom_tone TEXT,                   -- override soul for this contact
                custom_language TEXT,               -- preferred language
                notes TEXT,                         -- operator notes
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, contact_jid)
            )
        """)

        # ── Agent settings per user ────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_settings (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                soul_override TEXT,                 -- custom soul.md content
                debounce_seconds INTEGER DEFAULT 8,
                auto_respond INTEGER DEFAULT 1,
                tts_enabled INTEGER DEFAULT 1,
                model TEXT DEFAULT 'gpt-4o',
                temperature REAL DEFAULT 0.75,
                handoff_intents TEXT DEFAULT '["money","emergency"]',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── API tokens (JWT sessions) ──────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                expires_at DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.commit()

    # ── Users ──────────────────────────────────────────────────────────────

    def upsert_user(self, google_sub: str, email: str, name: str, avatar_url: str) -> Dict:
        existing = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (google_sub,)
        ).fetchone()

        if existing:
            self.conn.execute(
                "UPDATE users SET email=?, name=?, avatar_url=?, last_login=CURRENT_TIMESTAMP WHERE id=?",
                (email, name, avatar_url, google_sub)
            )
        else:
            self.conn.execute(
                "INSERT INTO users (id, email, name, avatar_url) VALUES (?,?,?,?)",
                (google_sub, email, name, avatar_url)
            )
            # Init agent settings
            self.conn.execute(
                "INSERT OR IGNORE INTO agent_settings (user_id) VALUES (?)", (google_sub,)
            )
            # Init WA session
            self.conn.execute(
                "INSERT OR IGNORE INTO whatsapp_sessions (user_id) VALUES (?)", (google_sub,)
            )

        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM users WHERE id=?", (google_sub,)).fetchone())

    def get_user(self, user_id: str) -> Optional[Dict]:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    # ── WhatsApp Sessions ──────────────────────────────────────────────────

    def update_wa_status(self, user_id: str, status: str, wa_jid: str = None,
                         wa_name: str = None, wa_number: str = None):
        updates = ["status=?"]
        params = [status]
        if wa_jid:
            updates.extend(["wa_jid=?", "wa_name=?", "wa_number=?", "last_connected=CURRENT_TIMESTAMP"])
            params.extend([wa_jid, wa_name, wa_number])
        params.append(user_id)
        self.conn.execute(
            f"UPDATE whatsapp_sessions SET {', '.join(updates)} WHERE user_id=?", params
        )
        self.conn.commit()

    def set_agent_running(self, user_id: str, running: bool):
        self.conn.execute(
            "UPDATE whatsapp_sessions SET agent_running=? WHERE user_id=?",
            (1 if running else 0, user_id)
        )
        self.conn.commit()

    def get_wa_session(self, user_id: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM whatsapp_sessions WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_connected_sessions(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM whatsapp_sessions WHERE status='connected' AND agent_running=1"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Contacts ───────────────────────────────────────────────────────────

    def upsert_contacts(self, user_id: str, contacts: List[Dict]):
        for c in contacts:
            self.conn.execute("""
                INSERT INTO contacts (user_id, jid, name, number, is_group)
                VALUES (?,?,?,?,?)
                ON CONFLICT(user_id, jid) DO UPDATE SET
                    name=excluded.name, number=excluded.number, synced_at=CURRENT_TIMESTAMP
            """, (user_id, c["jid"], c.get("name"), c.get("number"), 1 if c.get("is_group") else 0))
        self.conn.commit()

    def get_contacts(self, user_id: str, search: str = None, is_group: int = None) -> List[Dict]:
        q = "SELECT c.*, COALESCE(cs.is_allowed, 0) as is_allowed, cs.custom_tone, cs.custom_language FROM contacts c LEFT JOIN contact_settings cs ON cs.user_id=c.user_id AND cs.contact_jid=c.jid WHERE c.user_id=?"
        params = [user_id]
        if search:
            q += " AND (c.name LIKE ? OR c.number LIKE ? OR c.jid LIKE ?)"
            s = f"%{search}%"
            params.extend([s, s, s])
        if is_group is not None:
            q += " AND c.is_group=?"
            params.append(is_group)
        q += " ORDER BY c.name ASC"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # ── Contact Settings (Allowlist) ───────────────────────────────────────

    def update_contact_setting(self, user_id: str, contact_jid: str, is_allowed: bool,
                                custom_tone: str = None, custom_language: str = None):
        self.conn.execute("""
            INSERT INTO contact_settings (user_id, contact_jid, is_allowed, custom_tone, custom_language)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id, contact_jid) DO UPDATE SET
                is_allowed=excluded.is_allowed,
                custom_tone=COALESCE(excluded.custom_tone, custom_tone),
                custom_language=COALESCE(excluded.custom_language, custom_language),
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, contact_jid, 1 if is_allowed else 0, custom_tone, custom_language))
        self.conn.commit()

    def bulk_update_allowlist(self, user_id: str, allowed_jids: List[str]):
        """Set allowlist: all provided JIDs become allowed, rest get denied."""
        # Deny all first
        self.conn.execute(
            "UPDATE contact_settings SET is_allowed=0, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (user_id,)
        )
        # Allow selected
        for jid in allowed_jids:
            self.conn.execute("""
                INSERT INTO contact_settings (user_id, contact_jid, is_allowed)
                VALUES (?,?,1)
                ON CONFLICT(user_id, contact_jid) DO UPDATE SET is_allowed=1, updated_at=CURRENT_TIMESTAMP
            """, (user_id, jid))
        self.conn.commit()

    def get_allowed_jids(self, user_id: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT contact_jid FROM contact_settings WHERE user_id=? AND is_allowed=1",
            (user_id,)
        ).fetchall()
        return [r["contact_jid"] for r in rows]

    # ── Agent Settings ─────────────────────────────────────────────────────

    def get_agent_settings(self, user_id: str) -> Dict:
        row = self.conn.execute(
            "SELECT * FROM agent_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return {}
        d = dict(row)
        try:
            d["handoff_intents"] = json.loads(d.get("handoff_intents", '["money","emergency"]'))
        except Exception:
            d["handoff_intents"] = ["money", "emergency"]
        return d

    def update_agent_settings(self, user_id: str, settings: Dict):
        if "handoff_intents" in settings and isinstance(settings["handoff_intents"], list):
            settings["handoff_intents"] = json.dumps(settings["handoff_intents"])
        fields = [k for k in settings if k in (
            "soul_override", "debounce_seconds", "auto_respond",
            "tts_enabled", "model", "temperature", "handoff_intents"
        )]
        if not fields:
            return
        set_clause = ", ".join(f"{f}=?" for f in fields)
        vals = [settings[f] for f in fields] + [user_id]
        self.conn.execute(
            f"UPDATE agent_settings SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            vals
        )
        self.conn.commit()