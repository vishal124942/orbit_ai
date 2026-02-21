"""
Multi-User Session Manager
===========================

Manages completely isolated agent instances per user.
Each user gets:
  - Their own data directory: data/users/{user_id}/
  - Their own SQLite agent DB: data/users/{user_id}/agent.db
  - Their own WhatsApp session: data/users/{user_id}/whatsapp/
  - Their own in-memory agent controller instance
  - Their own asyncio tasks and locks

No cross-user data leakage. Each instance is fully independent.
"""

import os
import json
import asyncio
import logging
from typing import Dict, Optional, List, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    user_id: str
    data_dir: str
    controller: Optional[object] = None         # AIAgentController instance
    task: Optional[asyncio.Task] = None          # Background agent task
    qr_code: Optional[str] = None
    wa_status: str = "disconnected"             # disconnected/pairing/connected
    wa_jid: Optional[str] = None
    allowed_jids: Set[str] = field(default_factory=set)
    ws_clients: List = field(default_factory=list)  # WebSocket connections for this user
    is_running: bool = False


class SessionManager:
    """
    Central manager for all user sessions.
    Thread-safe, asyncio-friendly.
    """

    def __init__(self, platform_db, base_config: Dict):
        self.platform_db = platform_db
        self.base_config = base_config
        self.sessions: Dict[str, UserSession] = {}
        self._lock = asyncio.Lock()

    def get_user_data_dir(self, user_id: str) -> str:
        path = os.path.join("data", "users", user_id)
        os.makedirs(path, exist_ok=True)
        os.makedirs(os.path.join(path, "whatsapp"), exist_ok=True)
        os.makedirs(os.path.join(path, "media"), exist_ok=True)
        os.makedirs(os.path.join(path, "tts"), exist_ok=True)
        return path

    async def get_user_config(self, user_id: str) -> Dict:
        """Build per-user config merging base config with user-specific settings."""
        settings = await self.platform_db.get_agent_settings(user_id) or {}
        config = dict(self.base_config)

        # Override with user-specific settings
        config["openai"] = {
            **config.get("openai", {}),
            "model": settings.get("model", config.get("openai", {}).get("model", "gpt-4o")),
            "temperature": settings.get("temperature", 0.75),
            "max_tokens": 2000,
        }

        config["whatsapp"] = {
            **config.get("whatsapp", {}),
            "auto_respond": bool(settings.get("auto_respond", 1)),
            "debounce_seconds": settings.get("debounce_seconds", 8),
            "auth_dir": self.get_user_data_dir(user_id) + "/whatsapp",
            "session_name": user_id,
        }

        config["tts"] = {
            **config.get("tts", {}),
            "enabled": bool(settings.get("tts_enabled", 1)),
        }

        config["policy"] = {
            "handoff_intents": settings.get("handoff_intents", ["money", "emergency"]),
            "draft_intents": [],
        }

        config["_user_id"] = user_id
        config["_data_dir"] = self.get_user_data_dir(user_id)
        config["_soul_override"] = settings.get("soul_override", "")

        return config

    async def get_or_create_session(self, user_id: str) -> UserSession:
        async with self._lock:
            if user_id not in self.sessions:
                data_dir = self.get_user_data_dir(user_id)
                session = UserSession(user_id=user_id, data_dir=data_dir)
                # Load current allowlist
                session.allowed_jids = set(await self.platform_db.get_allowed_jids(user_id))
                self.sessions[user_id] = session
            return self.sessions[user_id]

    async def start_pairing(self, user_id: str) -> UserSession:
        """Initialize WhatsApp pairing for a user."""
        session = await self.get_or_create_session(user_id)

        if session.wa_status == "connected" and session.is_running:
            return session

        # Import here to avoid circular deps
        from backend.user_agent import UserAgentController

        config = await self.get_user_config(user_id)
        allowed_jids = set(await self.platform_db.get_allowed_jids(user_id))

        controller = UserAgentController(
            user_id=user_id,
            config=config,
            allowed_jids=allowed_jids,
            on_qr=lambda qr: self._on_qr(user_id, qr),
            on_status=lambda s, jid=None, name=None, number=None: asyncio.create_task(
                self._on_status(user_id, s, jid, name, number)
            ),
            on_contacts=lambda contacts: asyncio.create_task(self._on_contacts(user_id, contacts)),
        )

        session.controller = controller
        session.wa_status = "pairing"
        await self.platform_db.update_wa_status(user_id, "pairing")

        # Start controller in background
        session.task = asyncio.create_task(self._run_controller(user_id, controller))
        session.is_running = True

        return session

    async def _run_controller(self, user_id: str, controller):
        """Run the user's agent controller as a background task."""
        try:
            await controller.start()
        except asyncio.CancelledError:
            logger.info(f"[SessionManager] Agent for {user_id} cancelled")
        except Exception as e:
            logger.error(f"[SessionManager] Agent error for {user_id}: {e}")
            session = self.sessions.get(user_id)
            if session:
                session.is_running = False
                session.wa_status = "disconnected"
            await self.platform_db.update_wa_status(user_id, "disconnected")
            await self.platform_db.set_agent_running(user_id, False)

    def _on_qr(self, user_id: str, qr: str):
        """Called when WhatsApp generates a QR code for this user."""
        session = self.sessions.get(user_id)
        if session:
            session.qr_code = qr
            session.wa_status = "pairing"
            asyncio.create_task(self._broadcast(user_id, {
                "type": "qr",
                "data": qr,
                "status": "pairing"
            }))

    async def _on_status(self, user_id: str, status: str, wa_jid: str = None,
                   wa_name: str = None, wa_number: str = None):
        """Called when WhatsApp connection status changes."""
        session = self.sessions.get(user_id)
        if session:
            session.wa_status = status
            if wa_jid:
                session.wa_jid = wa_jid
                session.qr_code = None  # Clear QR once connected

        await self.platform_db.update_wa_status(user_id, status, wa_jid, wa_name, wa_number)
        if status == "connected":
            await self.platform_db.set_agent_running(user_id, True)

        asyncio.create_task(self._broadcast(user_id, {
            "type": "status",
            "status": status,
            "wa_jid": wa_jid,
            "wa_name": wa_name,
        }))

    async def _on_contacts(self, user_id: str, contacts: List[Dict]):
        """Called when WhatsApp syncs contacts for this user."""
        await self.platform_db.upsert_contacts(user_id, contacts)
        asyncio.create_task(self._broadcast(user_id, {
            "type": "contacts_synced",
            "count": len(contacts),
        }))

    async def stop_agent(self, user_id: str):
        """Stop a user's agent."""
        session = self.sessions.get(user_id)
        if not session:
            return

        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await asyncio.wait_for(session.task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if session.controller:
            try:
                await session.controller.stop()
            except Exception:
                pass

        session.is_running = False
        session.wa_status = "disconnected"
        session.controller = None
        await self.platform_db.set_agent_running(user_id, False)
        await self.platform_db.update_wa_status(user_id, "disconnected")

    def update_allowed_jids(self, user_id: str, allowed_jids: List[str]):
        """Hot-reload contact allowlist without restarting agent."""
        session = self.sessions.get(user_id)
        if session:
            session.allowed_jids = set(allowed_jids)
            if session.controller:
                session.controller.update_allowed_jids(session.allowed_jids)

    def add_ws_client(self, user_id: str, ws):
        session = self.sessions.get(user_id)
        if session and ws not in session.ws_clients:
            session.ws_clients.append(ws)

    def remove_ws_client(self, user_id: str, ws):
        session = self.sessions.get(user_id)
        if session and ws in session.ws_clients:
            session.ws_clients.remove(ws)

    async def _broadcast(self, user_id: str, message: Dict):
        """Broadcast a message to all WebSocket clients for a user."""
        session = self.sessions.get(user_id)
        if not session:
            return
        dead = []
        for ws in session.ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            session.ws_clients.remove(ws)

    async def get_session_status(self, user_id: str) -> Dict:
        session = self.sessions.get(user_id)
        if not session:
            db_session = await self.platform_db.get_wa_session(user_id)
            return {
                "status": db_session["status"] if db_session else "disconnected",
                "is_running": False,
                "qr_code": None,
                "wa_jid": db_session.get("wa_jid") if db_session else None,
            }
        return {
            "status": session.wa_status,
            "is_running": session.is_running,
            "qr_code": session.qr_code,
            "wa_jid": session.wa_jid,
        }

    async def restore_active_sessions(self):
        """On startup, restore agents for users that were previously connected."""
        try:
            active = await self.platform_db.get_all_connected_sessions()
            for s in active:
                logger.info(f"[SessionManager] Restoring session for {s['user_id']}")
                await self.start_pairing(s["user_id"])
        except Exception as e:
            logger.error(f"[SessionManager] Session restore error: {e}")