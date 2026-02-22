"""
Database — Three-Tier Memory + Pipeline Metrics

FIX: Added threading.Lock around all write operations.
SQLite with check_same_thread=False allows multiple threads to share one
connection, but concurrent writes without coordination cause "database is
locked" errors in WAL mode.  A single RLock serialises writes while allowing
reads to proceed concurrently (SQLite WAL allows parallel reads).
"""

import sqlite3
import os
import json
import threading
from typing import Optional, Dict, Any, List


class Database:
    def __init__(self, db_path: str = "data/agent_system.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL mode: readers don't block writers and vice-versa.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # RLock instead of Lock so the same thread can re-enter (e.g. _init_db
        # calling multiple helpers that each acquire the lock).
        self._write_lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        with self._write_lock:
            c = self.conn.cursor()

            c.execute("""
                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT, description TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_jid TEXT, text TEXT, push_name TEXT,
                    message_id TEXT UNIQUE, from_me INTEGER DEFAULT 0,
                    media_type TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration: Add media_type if missing (for older schemas)
            try:
                c.execute("SELECT media_type FROM messages LIMIT 1")
            except sqlite3.OperationalError:
                print("[Database] Migrating: Adding 'media_type' column to messages table")
                self.conn.execute("ALTER TABLE messages ADD COLUMN media_type TEXT")

            # Tier 2: Long-term memory (key-value facts per JID)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    remote_jid TEXT PRIMARY KEY,
                    summary TEXT,
                    intelligence TEXT,
                    metadata TEXT,
                    last_compacted_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Tier 3: Episodic memory
            c.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_jid TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    importance REAL DEFAULT 0.5,
                    emotion TEXT DEFAULT 'neutral',
                    tags TEXT DEFAULT '[]',
                    message_ids TEXT DEFAULT '[]',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    accessed_count INTEGER DEFAULT 0,
                    last_accessed DATETIME
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_episodes_jid ON episodes(remote_jid)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_episodes_importance ON episodes(importance DESC)")

            # Analysis logs
            c.execute("""
                CREATE TABLE IF NOT EXISTS analysis_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_jid TEXT NOT NULL,
                    sentiment_score REAL, vibe TEXT, toxicity TEXT,
                    intent TEXT, risk TEXT, language TEXT, summary TEXT,
                    route TEXT, route_reason TEXT,
                    message_count INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Pipeline metrics
            c.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_jid TEXT NOT NULL,
                    route TEXT, response_time_ms INTEGER,
                    message_sent INTEGER DEFAULT 0,
                    audio_sent INTEGER DEFAULT 0,
                    sticker_sent INTEGER DEFAULT 0,
                    reaction_sent INTEGER DEFAULT 0,
                    human_handoff INTEGER DEFAULT 0,
                    draft_created INTEGER DEFAULT 0,
                    error_occurred INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Drafts
            c.execute("""
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remote_jid TEXT NOT NULL,
                    reply_text TEXT, sticker_vibe TEXT,
                    reaction_emoji TEXT, analysis_json TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at DATETIME
                )
            """)

            self.conn.commit()

    # ── Messages ──────────────────────────────────────────────────────────────

    def add_message(self, remote_jid, text, push_name=None, message_id=None,
                    from_me=0, media_type=None, msg_timestamp=None):
        from datetime import datetime, timezone
        ts = None
        if msg_timestamp:
            try:
                ts = datetime.fromtimestamp(int(msg_timestamp), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
            except Exception:
                ts = None

        with self._write_lock:
            if ts:
                self.conn.execute(
                    "INSERT OR IGNORE INTO messages "
                    "(remote_jid,text,push_name,message_id,from_me,media_type,timestamp) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (remote_jid, text, push_name, message_id, from_me, media_type, ts)
                )
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO messages "
                    "(remote_jid,text,push_name,message_id,from_me,media_type) "
                    "VALUES (?,?,?,?,?,?)",
                    (remote_jid, text, push_name, message_id, from_me, media_type)
                )
            self.conn.commit()

    def update_message_text(self, message_id, new_text):
        if not message_id:
            return
        with self._write_lock:
            self.conn.execute("UPDATE messages SET text=? WHERE message_id=?", (new_text, message_id))
            self.conn.commit()

    def get_messages(self, remote_jid=None, limit=50):
        # Reads don't need the write lock in WAL mode
        q, p = "SELECT * FROM messages", []
        if remote_jid:
            q += " WHERE remote_jid=?"
            p.append(remote_jid)
        q += " ORDER BY id DESC LIMIT ?"
        p.append(limit)
        return self.conn.execute(q, p).fetchall()

    def prune_messages(self, remote_jid, keep=200):
        with self._write_lock:
            self.conn.execute("""
                DELETE FROM messages WHERE remote_jid = ? AND id NOT IN (
                    SELECT id FROM messages WHERE remote_jid = ? ORDER BY id DESC LIMIT ?
                )
            """, (remote_jid, remote_jid, keep))
            self.conn.commit()

    def add_message_and_prune(self, remote_jid, text, push_name, message_id,
                               from_me=0, media_type=None, keep=200):
        """Atomic insert + prune inside a single transaction to avoid lock contention."""
        with self._write_lock:
            try:
                # Use a single transaction so insert + prune are atomic
                with self.conn:
                    self.conn.execute("""
                        INSERT INTO messages (remote_jid, text, push_name, message_id, from_me, media_type)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(message_id) DO UPDATE SET
                            text=excluded.text,
                            push_name=excluded.push_name
                    """, (remote_jid, text, push_name, message_id, from_me, media_type))

                    self.conn.execute("""
                        DELETE FROM messages WHERE remote_jid = ? AND id NOT IN (
                            SELECT id FROM messages WHERE remote_jid = ? ORDER BY id DESC LIMIT ?
                        )
                    """, (remote_jid, remote_jid, keep))
                return True
            except Exception as e:
                print(f"[Database] Error adding message: {e}")
                return False

    def get_message_stats(self):
        return self.conn.execute("""
            SELECT remote_jid, COUNT(*) as count
            FROM messages
            GROUP BY remote_jid
        """).fetchall()

    # ── Sessions ──────────────────────────────────────────────────────────────

    def update_session(self, remote_jid, summary=None, intelligence=None, metadata=None):
        with self._write_lock:
            self.conn.execute("""
                INSERT INTO sessions (remote_jid,summary,intelligence,metadata,last_compacted_at)
                VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(remote_jid) DO UPDATE SET
                    summary=COALESCE(?,summary),
                    intelligence=COALESCE(?,intelligence),
                    metadata=COALESCE(?,metadata),
                    last_compacted_at=CURRENT_TIMESTAMP
            """, (remote_jid, summary, intelligence, metadata, summary, intelligence, metadata))
            self.conn.commit()

    def get_session(self, remote_jid):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE remote_jid=?", (remote_jid,)
        ).fetchone()

    # ── Episodic Memory ───────────────────────────────────────────────────────

    def store_episode(self, remote_jid: str, summary: str, importance: float,
                      emotion: str, tags: List[str], message_ids: List[str] = None):
        with self._write_lock:
            self.conn.execute("""
                INSERT INTO episodes (remote_jid, summary, importance, emotion, tags, message_ids)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                remote_jid, summary, importance, emotion,
                json.dumps(tags), json.dumps(message_ids or [])
            ))
            self.conn.commit()

    def get_episodes(self, remote_jid: str, limit: int = 50, min_importance: float = 0.0):
        return self.conn.execute("""
            SELECT * FROM episodes
            WHERE remote_jid=? AND importance >= ?
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """, (remote_jid, min_importance, limit)).fetchall()

    def touch_episode(self, episode_id: int):
        with self._write_lock:
            self.conn.execute("""
                UPDATE episodes
                SET accessed_count = accessed_count + 1, last_accessed = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (episode_id,))
            self.conn.commit()

    def get_episode_count(self, remote_jid: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM episodes WHERE remote_jid=?", (remote_jid,)
        ).fetchone()
        return row["cnt"] if row else 0

    def delete_old_episodes(self, remote_jid: str, keep: int = 200):
        with self._write_lock:
            self.conn.execute("""
                DELETE FROM episodes WHERE remote_jid=? AND id NOT IN (
                    SELECT id FROM episodes WHERE remote_jid=?
                    ORDER BY importance DESC, created_at DESC LIMIT ?
                )
            """, (remote_jid, remote_jid, keep))
            self.conn.commit()

    # ── Analysis Logs ─────────────────────────────────────────────────────────

    def log_analysis(self, remote_jid, analysis, route, route_reason, message_count=1):
        with self._write_lock:
            self.conn.execute("""
                INSERT INTO analysis_logs
                (remote_jid,sentiment_score,vibe,toxicity,intent,risk,language,summary,
                 route,route_reason,message_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                remote_jid, analysis.get("sentiment_score"), analysis.get("vibe"),
                analysis.get("toxicity"), analysis.get("intent"), analysis.get("risk"),
                analysis.get("language"), analysis.get("summary"),
                route, route_reason, message_count,
            ))
            self.conn.commit()

    # ── Pipeline Metrics ──────────────────────────────────────────────────────

    def log_pipeline_metric(self, remote_jid, route, response_time_ms,
                             message_sent=False, audio_sent=False,
                             sticker_sent=False, reaction_sent=False,
                             human_handoff=False, draft_created=False,
                             error_occurred=False, error_message=""):
        with self._write_lock:
            self.conn.execute("""
                INSERT INTO pipeline_metrics
                (remote_jid,route,response_time_ms,message_sent,audio_sent,sticker_sent,
                 reaction_sent,human_handoff,draft_created,error_occurred,error_message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                remote_jid, route, response_time_ms,
                int(message_sent), int(audio_sent), int(sticker_sent),
                int(reaction_sent), int(human_handoff), int(draft_created),
                int(error_occurred), error_message,
            ))
            self.conn.commit()

    # ── Drafts ────────────────────────────────────────────────────────────────

    def save_draft(self, remote_jid, reply_text, sticker_vibe="", reaction_emoji="", analysis_json=""):
        with self._write_lock:
            cursor = self.conn.execute("""
                INSERT INTO drafts (remote_jid,reply_text,sticker_vibe,reaction_emoji,analysis_json)
                VALUES (?,?,?,?,?)
            """, (remote_jid, reply_text, sticker_vibe, reaction_emoji, analysis_json))
            self.conn.commit()
            return cursor.lastrowid

    def get_pending_drafts(self, remote_jid=None):
        q, p = "SELECT * FROM drafts WHERE status='pending'", []
        if remote_jid:
            q += " AND remote_jid=?"
            p.append(remote_jid)
        return self.conn.execute(q + " ORDER BY created_at ASC", p).fetchall()

    def approve_draft(self, draft_id):
        with self._write_lock:
            self.conn.execute(
                "UPDATE drafts SET status='approved',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (draft_id,)
            )
            self.conn.commit()

    def reject_draft(self, draft_id):
        with self._write_lock:
            self.conn.execute(
                "UPDATE drafts SET status='rejected',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (draft_id,)
            )
            self.conn.commit()

    # ── Activities ────────────────────────────────────────────────────────────

    def add_activity(self, activity_type, description):
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO activities (type,description) VALUES (?,?)",
                (activity_type, description)
            )
            self.conn.commit()

    def get_recent_activities(self, limit=10):
        return self.conn.execute(
            "SELECT * FROM activities ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self):
        self.conn.close()