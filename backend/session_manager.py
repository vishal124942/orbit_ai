"""
Multi-User Session Manager — v3.1
===================================

CHANGES in v3.1:
1. start_pairing: passes on_contact_sync_progress callback to UserAgentController
   so the gateway's live count events propagate all the way to the frontend WS.
2. _on_contact_sync_progress: new handler that broadcasts a contacts_progress WS
   event (type, count) to all connected WebSocket clients in real time.
3. _on_contacts: per-contact-batch logging improved; task is created so the
   DB write never blocks the bridge stdout reader.
4. _stop_agent_internal: asyncio.create_subprocess_exec + wait() so the R2
   clear-state call never blocks the event loop (was subprocess.run before).
5. restore_active_sessions: per-session error isolation preserved.
6. Deadlock fix retained: stop_agent and start_pairing acquire action_lock
   independently so wa_regenerate callers never nest locks.
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
    contact_sync_count: int = 0   # running tally of synced contacts


class SessionManager:
    def __init__(self, platform_db, base_config: Dict):
        self.platform_db = platform_db
        self.base_config = base_config
        self.sessions: Dict[str, UserSession] = {}
        self._lock = asyncio.Lock()

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

        Deadlock-safe: acquires action_lock only for its own scope.
        Callers doing stop+start (wa_regenerate) must call them sequentially
        without nesting, as each acquires the lock independently.
        """
        session = await self.get_or_create_session(user_id)

        async with session.action_lock:
            if session.wa_status == "connected" and session.is_running:
                return session

            if session.task or session.controller:
                logger.info(f"[SessionManager] Stopping existing agent for {user_id} before re-pairing")
                await self._stop_agent_internal(user_id, session, clear_auth=False)
                await asyncio.sleep(0.5)

            from backend.user_agent import UserAgentController

            config = await self.get_user_config(user_id)
            if phone_number:
                config["whatsapp"]["phone_number"] = phone_number

            allowed_jids = set(await self.platform_db.get_allowed_jids(user_id))
            session.allowed_jids = allowed_jids
            session.contact_sync_count = 0  # Reset counter on new pairing

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
                on_contact_sync_progress=lambda count: self._on_contact_sync_progress(user_id, count),
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
            # Always clean up — prevents stale controller ref on next start
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
                session.pairing_code = None

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
        Store a batch of contacts and broadcast sync progress to the frontend.

        Uses two-pass upsert (bulk + per-row fallback) in pg_models, so one
        bad row never drops the whole batch.  Broadcasts contacts_synced after
        every batch so the frontend counter stays accurate in real time.
        """
        if not contacts:
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
                f"(batch={len(contacts)}): {e}",
                exc_info=True,
            )

        # Update running tally on session
        session = self.sessions.get(user_id)
        if session:
            session.contact_sync_count += stored_count

        asyncio.create_task(self._broadcast(user_id, {
            "type": "contacts_synced",
            "count": stored_count,
            "received": len(contacts),
            "total_synced": session.contact_sync_count if session else stored_count,
        }))

    def _on_contact_sync_progress(self, user_id: str, count: int):
        """
        Receive live contact count from the gateway and broadcast it immediately
        to all WebSocket clients so the frontend can show a running counter.

        This is called from the bridge thread via call_soon_threadsafe — it must
        only schedule work on the event loop, never do async I/O directly.
        """
        session = self.sessions.get(user_id)
        if session:
            session.contact_sync_count = max(session.contact_sync_count, count)

        # Fire-and-forget broadcast — doesn't block the bridge thread
        asyncio.create_task(self._broadcast(user_id, {
            "type": "contacts_progress",
            "count": count,
        }))

    # ── Stop ──────────────────────────────────────────────────────────────────

    async def stop_agent(self, user_id: str, clear_auth: bool = False):
        """
        Stop a user's agent. Acquires action_lock independently.

        Callers doing stop+start (wa_regenerate) must call this first, await it,
        then call start_pairing — never nesting the two under a shared lock.
        """
        session = self.sessions.get(user_id)
        if not session:
            # For code regeneration we may still need to wipe persisted WA auth
            # even when no controller is currently running in memory.
            if not clear_auth:
                await self.platform_db.set_agent_running(user_id, False)
                await self.platform_db.update_wa_status(user_id, "disconnected")
                return
            session = await self.get_or_create_session(user_id)
        if session.action_lock is None:
            session.action_lock = asyncio.Lock()

        async with session.action_lock:
            await self._stop_agent_internal(user_id, session, clear_auth=clear_auth)

    async def _stop_agent_internal(self, user_id: str, session: UserSession, clear_auth: bool = False):
        """Lock-free internal shutdown — caller must hold action_lock."""

        # 1. Cancel the asyncio task
        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(session.task), timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        session.task = None

        # 2. Stop the bridge (fast — just sends SIGTERM to the Node subprocess)
        if session.controller:
            try:
                await session.controller.stop()
            except Exception as e:
                logger.warning(f"[SessionManager] Controller stop error for {user_id}: {e}")
        session.controller = None

        # 3. Optionally wipe local + remote WhatsApp auth cache (hard reset only)
        wa_auth_dir = os.path.join(self.get_user_data_dir(user_id), "whatsapp")
        if clear_auth:
            try:
                import shutil
                if os.path.exists(wa_auth_dir):
                    await asyncio.to_thread(shutil.rmtree, wa_auth_dir)
                    logger.info(f"[SessionManager] Wiped local auth dir for {user_id}")
            except Exception as e:
                logger.error(f"[SessionManager] Failed to wipe local auth dir: {e}")

            # 4. Wipe Cloudflare R2 session — async subprocess with bounded wait
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
                        await asyncio.wait_for(proc.wait(), timeout=8)
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
        session.contact_sync_count = 0

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
        if not session:
            # New users often open the dashboard WS before starting an agent.
            # Create a lightweight placeholder session so pairing/status events
            # can be pushed immediately after start_pairing().
            session = UserSession(
                user_id=user_id,
                data_dir=self.get_user_data_dir(user_id),
                action_lock=asyncio.Lock(),
            )
            self.sessions[user_id] = session
        if ws not in session.ws_clients:
            session.ws_clients.append(ws)

    def remove_ws_client(self, user_id: str, ws):
        session = self.sessions.get(user_id)
        if session and ws in session.ws_clients:
            session.ws_clients.remove(ws)

    async def _broadcast(self, user_id: str, message: Dict):
        """
        Broadcast a message to all WebSocket clients for a user.
        Dead sockets are pruned silently — send errors are never propagated.
        """
        session = self.sessions.get(user_id)
        if not session or not session.ws_clients:
            return
        dead = []
        for ws in list(session.ws_clients):
            try:
                await ws.send_json(message)
            except Exception:
                # Catches WebSocketDisconnect, ClientDisconnected, RuntimeError, OSError etc.
                # Any send failure means the socket is dead — schedule for removal.
                dead.append(ws)
        for ws in dead:
            try:
                session.ws_clients.remove(ws)
            except ValueError:
                pass  # Already removed by another coroutine

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
                "contact_sync_count": 0,
            }
        return {
            "status": session.wa_status,
            "is_running": session.is_running,
            "pairing_code": session.pairing_code,
            "wa_jid": session.wa_jid,
            "contact_sync_count": session.contact_sync_count,
        }

    async def restore_active_sessions(self):
        """
        On startup: mark ALL previously-running sessions as disconnected.
        Agents only start when the user explicitly taps 'Start Agent'.
        This prevents phantom code generation on server restart.
        """
        try:
            active = await self.platform_db.get_all_connected_sessions()
        except Exception as e:
            logger.error(f"[SessionManager] Could not load active sessions: {e}")
            return

        for s in active:
            try:
                uid = s["user_id"]
                logger.info(f"[SessionManager] Marking session {uid} as disconnected (server restart)")
                await self.platform_db.set_agent_running(uid, False)
                await self.platform_db.update_wa_status(uid, "disconnected")
            except Exception as e:
                logger.error(f"[SessionManager] Failed to reset session for {s['user_id']}: {e}")
