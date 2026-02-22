"""
Multi-User Session Manager
===========================

FIXES applied:
1. _stop_agent_internal: subprocess.run was blocking the asyncio event loop
   for up to 30 s.  Replaced with asyncio.create_subprocess_exec + wait().
2. _run_controller: session.controller was not cleared on exception, leaving
   a stale ref that caused the next start() to skip cleanup incorrectly.
3. Deadlock in wa_regenerate flow: stop_agent acquires action_lock, then
   start_pairing tried to acquire the same lock — deadlock.  Separated the
   lock scope so stop and start are sequential but not nested.
4. restore_active_sessions: wraps each session restore individually so one
   failure doesn't abort all others.
5. _on_contacts: was called with zero error handling — a single asyncpg
   exception silently dropped ALL contacts with no log entry.  Now wrapped
   in try/except with per-batch logging.  Broadcasts contacts_synced even
   when upsert partially succeeds so the frontend stays in sync.
6. _on_contacts: now called via asyncio.create_task so a slow DB write never
   blocks the stdout reader thread (which would stall incoming events).
"""

import os
import asyncio
import logging
from typing import Dict, Optional, List, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    user_id: str
    data_dir: str
    controller: Optional[object] = None
    task: Optional[asyncio.Task] = None
    pairing_code: Optional[str] = None
    wa_status: str = "disconnected"
    wa_jid: Optional[str] = None
    allowed_jids: Set[str] = field(default_factory=set)
    ws_clients: List = field(default_factory=list)
    is_running: bool = False
    action_lock: Optional[asyncio.Lock] = None


class SessionManager:
    def __init__(self, platform_db, base_config: Dict):
        self.platform_db = platform_db
        self.base_config = base_config
        self.sessions: Dict[str, UserSession] = {}
        self._lock = asyncio.Lock()  # Protects self.sessions dict

    # ── Directories ───────────────────────────────────────────────────────────

    def get_user_data_dir(self, user_id: str) -> str:
        path = os.path.join("data", "users", user_id)
        for sub in ["", "whatsapp", "media", "tts"]:
            os.makedirs(os.path.join(path, sub) if sub else path, exist_ok=True)
        return path

    # ── Config ────────────────────────────────────────────────────────────────

    async def get_user_config(self, user_id: str) -> Dict:
        settings = await self.platform_db.get_agent_settings(user_id) or {}
        config = dict(self.base_config)

        config["openai"] = {
            **config.get("openai", {}),
            "model": settings.get("model", config.get("openai", {}).get("model", "gpt-4o")),
            "temperature": settings.get("temperature", 0.75),
            "max_tokens": 2000,
        }
        config["whatsapp"] = {
            **config.get("whatsapp", {}),
            "auto_respond": bool(settings.get("auto_respond", 1)),
            "debounce_seconds": settings.get("debounce_seconds", 1.5),
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

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def get_or_create_session(self, user_id: str) -> UserSession:
        async with self._lock:
            if user_id not in self.sessions:
                data_dir = self.get_user_data_dir(user_id)
                session = UserSession(user_id=user_id, data_dir=data_dir)
                session.allowed_jids = set(await self.platform_db.get_allowed_jids(user_id))
                session.action_lock = asyncio.Lock()
                self.sessions[user_id] = session
            else:
                if self.sessions[user_id].action_lock is None:
                    self.sessions[user_id].action_lock = asyncio.Lock()
            return self.sessions[user_id]

    async def start_pairing(self, user_id: str, phone_number: str = None) -> UserSession:
        """
        Initialize WhatsApp pairing.

        FIX (deadlock): The original code held action_lock across the entire
        stop + start sequence.  wa_regenerate calls stop_agent (which acquires
        action_lock) and then start_pairing (which tried to acquire it again) —
        deadlock.  Now start_pairing only acquires the lock for its own work;
        callers that need stop+start must sequence them without nesting locks.
        """
        session = await self.get_or_create_session(user_id)

        async with session.action_lock:
            if session.wa_status == "connected" and session.is_running:
                return session

            # Tear down any existing process before spawning a new one
            if session.task or session.controller:
                logger.info(f"[SessionManager] Stopping existing agent for {user_id} before re-pairing")
                await self._stop_agent_internal(user_id, session)
                await asyncio.sleep(0.5)

            from backend.user_agent import UserAgentController

            config = await self.get_user_config(user_id)
            if phone_number:
                config["whatsapp"]["phone_number"] = phone_number

            allowed_jids = set(await self.platform_db.get_allowed_jids(user_id))

            controller = UserAgentController(
                user_id=user_id,
                config=config,
                allowed_jids=allowed_jids,
                on_status=lambda s, jid=None, name=None, number=None: asyncio.create_task(
                    self._on_status(user_id, s, jid, name, number)
                ),
                on_contacts=lambda contacts: asyncio.create_task(
                    self._on_contacts(user_id, contacts)
                ),
                on_pairing_code=lambda code: self._on_pairing_code(user_id, code),
            )

            session.controller = controller
            session.wa_status = "pairing"
            await self.platform_db.update_wa_status(user_id, "pairing")

            session.task = asyncio.create_task(self._run_controller(user_id, controller))
            session.is_running = True

            return session

    async def _run_controller(self, user_id: str, controller):
        try:
            await controller.start()
        except asyncio.CancelledError:
            logger.info(f"[SessionManager] Agent for {user_id} cancelled")
        except Exception as e:
            logger.error(f"[SessionManager] Agent error for {user_id}: {e}", exc_info=True)
        finally:
            # FIX: Always clean up session state, whether we exited normally
            # or crashed — prevents stale controller ref on next start.
            session = self.sessions.get(user_id)
            if session:
                session.is_running = False
                session.wa_status = "disconnected"
                session.controller = None
                session.task = None
            await self.platform_db.update_wa_status(user_id, "disconnected")
            await self.platform_db.set_agent_running(user_id, False)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_pairing_code(self, user_id: str, code: str):
        session = self.sessions.get(user_id)
        if session:
            session.pairing_code = code
            session.wa_status = "pairing"
            asyncio.create_task(self._broadcast(user_id, {
                "type": "pairing_code",
                "data": code,
                "status": "pairing",
            }))

    async def _on_status(self, user_id: str, status: str, wa_jid: str = None,
                          wa_name: str = None, wa_number: str = None):
        session = self.sessions.get(user_id)
        if session:
            session.wa_status = status
            if wa_jid:
                session.wa_jid = wa_jid
                session.pairing_code = None  # Code consumed — clear it

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
        """
        FIX (CAUSE 2 — silent failure): The original code had zero error handling.
        A single asyncpg exception (connection blip, constraint violation, etc.)
        caused ALL contacts in the batch to be silently dropped with no log entry,
        no retry, and no feedback to the frontend.

        Now:
        1. The entire upsert is wrapped in try/except with detailed logging.
        2. We broadcast contacts_synced regardless — even if DB write fails,
           the in-memory session state stays consistent.
        3. The batch size is logged so we can tell at a glance whether the
           gateway is sending a reasonable number of contacts.
        """
        if not contacts:
            logger.warning(f"[SessionManager] _on_contacts called with empty list for {user_id}")
            return

        logger.info(f"[SessionManager] Storing {len(contacts)} contacts for {user_id}")
        stored_count = 0
        try:
            await self.platform_db.upsert_contacts(user_id, contacts)
            stored_count = len(contacts)
            logger.info(f"[SessionManager] ✅ Stored {stored_count} contacts for {user_id}")
        except Exception as e:
            logger.error(
                f"[SessionManager] ❌ upsert_contacts failed for {user_id} "
                f"(batch size={len(contacts)}): {e}",
                exc_info=True,
            )
            # Don't re-raise — we still want to broadcast so the frontend
            # knows a sync was attempted, and the 5-min periodic re-flush
            # in the gateway will retry automatically.

        asyncio.create_task(self._broadcast(user_id, {
            "type": "contacts_synced",
            "count": stored_count,
            "received": len(contacts),
        }))

    # ── Stop ──────────────────────────────────────────────────────────────────

    async def stop_agent(self, user_id: str):
        """
        Stop a user's agent.

        FIX (deadlock): This acquires action_lock independently.  Callers that
        need stop + start (e.g. wa_regenerate) must call stop_agent(), await it,
        then call start_pairing() — never nesting them under a shared lock.
        """
        session = self.sessions.get(user_id)
        if not session:
            return
        if session.action_lock is None:
            session.action_lock = asyncio.Lock()

        async with session.action_lock:
            await self._stop_agent_internal(user_id, session)

    async def _stop_agent_internal(self, user_id: str, session: UserSession):
        """Lock-free internal shutdown — caller must hold action_lock."""

        # 1. Cancel the asyncio task
        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(session.task), timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        session.task = None

        # 2. Stop the bridge (synchronous but fast — just sends SIGTERM)
        if session.controller:
            try:
                await session.controller.stop()
            except Exception as e:
                logger.warning(f"[SessionManager] Controller stop error for {user_id}: {e}")
        session.controller = None

        # 3. Wipe local WhatsApp auth cache
        wa_auth_dir = os.path.join(self.get_user_data_dir(user_id), "whatsapp")
        try:
            import shutil
            if os.path.exists(wa_auth_dir):
                shutil.rmtree(wa_auth_dir)
                logger.info(f"[SessionManager] Wiped local auth dir for {user_id}")
        except Exception as e:
            logger.error(f"[SessionManager] Failed to wipe local auth dir: {e}")

        # 4. Wipe Cloudflare R2 session (non-blocking)
        try:
            gateway_script = os.path.join(
                os.getcwd(), "backend", "src", "whatsapp", "gateway_v3.js"
            )
            if os.path.exists(gateway_script):
                env = os.environ.copy()
                env["WHATSAPP_SESSION_ID"] = user_id
                proc = await asyncio.create_subprocess_exec(
                    "node", gateway_script, wa_auth_dir, "--clear-state",
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=15)
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning(f"[SessionManager] R2 clear-state timed out for {user_id}")
        except Exception as e:
            logger.error(f"[SessionManager] R2 state cleanup failed for {user_id}: {e}")

        # 5. Update in-memory + DB state
        session.is_running = False
        session.wa_status = "disconnected"
        session.pairing_code = None
        session.wa_jid = None

        await self.platform_db.set_agent_running(user_id, False)
        await self.platform_db.update_wa_status(user_id, "disconnected")

    # ── Allowlist hot-reload ──────────────────────────────────────────────────

    def update_allowed_jids(self, user_id: str, allowed_jids: List[str]):
        session = self.sessions.get(user_id)
        if session:
            session.allowed_jids = set(allowed_jids)
            if session.controller:
                session.controller.update_allowed_jids(session.allowed_jids)

    # ── WebSocket management ──────────────────────────────────────────────────

    def add_ws_client(self, user_id: str, ws):
        session = self.sessions.get(user_id)
        if session and ws not in session.ws_clients:
            session.ws_clients.append(ws)

    def remove_ws_client(self, user_id: str, ws):
        session = self.sessions.get(user_id)
        if session and ws in session.ws_clients:
            session.ws_clients.remove(ws)

    async def _broadcast(self, user_id: str, message: Dict):
        session = self.sessions.get(user_id)
        if not session:
            return
        dead = []
        for ws in list(session.ws_clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in session.ws_clients:
                session.ws_clients.remove(ws)

    # ── Status ────────────────────────────────────────────────────────────────

    async def get_session_status(self, user_id: str) -> Dict:
        session = self.sessions.get(user_id)
        if not session:
            db_session = await self.platform_db.get_wa_session(user_id)
            return {
                "status": db_session["status"] if db_session else "disconnected",
                "is_running": False,
                "pairing_code": db_session.get("pairing_code") if db_session else None,
                "wa_jid": db_session.get("wa_jid") if db_session else None,
            }
        return {
            "status": session.wa_status,
            "is_running": session.is_running,
            "pairing_code": session.pairing_code,
            "wa_jid": session.wa_jid,
        }

    async def restore_active_sessions(self):
        """
        On startup: restore agents for users that were previously connected.

        FIX: Each session is restored independently so one failure doesn't
        abort all others.
        """
        try:
            active = await self.platform_db.get_all_connected_sessions()
        except Exception as e:
            logger.error(f"[SessionManager] Could not load active sessions: {e}")
            return

        for s in active:
            try:
                logger.info(f"[SessionManager] Restoring session for {s['user_id']}")
                await self.start_pairing(s["user_id"])
            except Exception as e:
                logger.error(
                    f"[SessionManager] Failed to restore session for {s['user_id']}: {e}"
                )