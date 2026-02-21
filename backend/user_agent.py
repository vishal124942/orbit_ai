"""
UserAgentController
====================

Wraps the original AIAgentController to provide:
1. Per-user data directory isolation
2. Contact allowlist enforcement
3. Per-contact custom tone/behavior (self-modifying soul)
4. Hot-reload of settings without restart
5. Callbacks to Session Manager for QR, status, contacts
"""

import os
import json
import asyncio
import logging
from typing import Dict, Set, List, Optional, Callable
from openai import OpenAI

logger = logging.getLogger(__name__)


class UserAgentController:
    """
    Isolated agent instance for a single user.
    Delegates message processing to core pipeline while enforcing:
    - Contact allowlist (only allowed JIDs get auto-replies)
    - Per-contact soul files (custom tone per contact)
    - User-scoped data directories
    """

    def __init__(
        self,
        user_id: str,
        config: Dict,
        allowed_jids: Set[str],
        on_qr: Callable,
        on_status: Callable,
        on_contacts: Callable,
    ):
        self.user_id = user_id
        self.config = config
        self.allowed_jids = allowed_jids
        self.on_qr_cb = on_qr
        self.on_status_cb = on_status
        self.on_contacts_cb = on_contacts

        self.data_dir = config["_data_dir"]
        self.soul_override = config.get("_soul_override", "")

        # Lazy import to avoid circular dependencies
        self._controller = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Contact-level soul files cache
        self._contact_souls: Dict[str, str] = {}
        self._load_contact_souls()

        # Contact-level custom tones (from Postgres, pushed live)
        self._contact_tones: Dict[str, str] = {}

    def _load_contact_souls(self):
        """Load per-contact soul overrides from disk."""
        souls_dir = os.path.join(self.data_dir, "souls")
        if not os.path.exists(souls_dir):
            os.makedirs(souls_dir, exist_ok=True)
            return
        for fname in os.listdir(souls_dir):
            if fname.endswith(".md"):
                jid = fname[:-3].replace("_", "@")
                try:
                    with open(os.path.join(souls_dir, fname)) as f:
                        self._contact_souls[jid] = f.read()
                except Exception:
                    pass

    def get_soul_for_contact(self, jid: str) -> str:
        """Get the soul/personality for a specific contact."""
        # Contact-specific soul takes priority
        if jid in self._contact_souls:
            return self._contact_souls[jid]
        # User-level soul override
        if self.soul_override:
            return self.soul_override
        # Default soul.md
        soul_path = os.path.join(os.getcwd(), "soul.md")
        if os.path.exists(soul_path):
            with open(soul_path) as f:
                return f.read()
        return ""

    def update_contact_soul(self, jid: str, soul_content: str):
        """Hot-update the soul for a specific contact."""
        souls_dir = os.path.join(self.data_dir, "souls")
        os.makedirs(souls_dir, exist_ok=True)
        safe_jid = jid.replace("@", "_")
        path = os.path.join(souls_dir, f"{safe_jid}.md")
        with open(path, "w") as f:
            f.write(soul_content)
        self._contact_souls[jid] = soul_content
        # Update live session if running
        if self._controller and jid in self._controller.sessions:
            # Rebuild session with new soul
            del self._controller.sessions[jid]

    def update_allowed_jids(self, allowed_jids: Set[str]):
        """Hot-reload the contact allowlist."""
        self.allowed_jids = allowed_jids
        if self._controller:
            self._controller.allowed_jids = allowed_jids

    def is_jid_allowed(self, jid: str) -> bool:
        """Check if a JID is in the allowlist. Empty allowlist = allow all."""
        if not self.allowed_jids:
            return True  # No allowlist configured yet → allow all contacts
        return jid in self.allowed_jids

    def update_contact_tone_live(self, jid: str, tone: str):
        """Push a tone update live — updates the in-memory dict and invalidates the
        cached session so the next message rebuilds the system prompt."""
        self._contact_tones[jid] = tone
        if self._controller:
            self._controller._contact_tones[jid] = tone
            # Invalidate cached session so next message gets fresh system prompt
            self._controller.sessions.pop(jid, None)

    async def start(self):
        """Start the user's isolated agent."""
        self._loop = asyncio.get_running_loop()

        # Build per-user database path
        db_path = os.path.join(self.data_dir, "agent.db")

        # Import core modules
        from backend.src.core.database import Database
        from backend.src.core.agent_controller import AIAgentController

        db = Database(db_path=db_path)

        # Patch config with user-specific paths
        config = dict(self.config)

        # Create controller with user-scoped callbacks
        controller = _IsolatedAgentController(
            config=config,
            db=db,
            user_id=self.user_id,
            allowed_jids=self.allowed_jids,
            data_dir=self.data_dir,
            get_soul_fn=self.get_soul_for_contact,
            update_soul_fn=self.update_contact_soul,
            has_soul_fn=lambda jid: jid in self._contact_souls,
            contact_tones=self._contact_tones,
            on_qr=self.on_qr_cb,
            on_status=self.on_status_cb,
            on_contacts=self.on_contacts_cb,
            loop=self._loop,
        )

        self._controller = controller

        logger.info(f"[UserAgent:{self.user_id}] Starting agent...")
        await controller.run_headless()

    async def stop(self):
        """Stop the user's agent."""
        if self._controller and hasattr(self._controller, "wa_bridge"):
            try:
                self._controller.wa_bridge.stop()
            except Exception as e:
                logger.warning(f"[UserAgent:{self.user_id}] Stop error: {e}")

    async def _sync_contacts(self):
        """Sync WhatsApp contacts to platform DB."""
        if self._controller:
            await self._controller._sync_contacts()


class _IsolatedAgentController:
    """
    Modified AIAgentController that:
    1. Uses per-user data dir for all file I/O
    2. Enforces contact allowlist
    3. Uses per-contact soul files
    4. Fires callbacks to SessionManager
    """

    def __init__(
        self,
        config: Dict,
        db,
        user_id: str,
        allowed_jids: Set[str],
        data_dir: str,
        get_soul_fn: Callable,
        update_soul_fn: Callable,
        has_soul_fn: Callable,
        contact_tones: Dict,
        on_qr: Callable,
        on_status: Callable,
        on_contacts: Callable,
        loop: asyncio.AbstractEventLoop,
    ):
        import os
        import json
        import time
        import asyncio
        import hashlib
        from openai import OpenAI
        from rich.console import Console

        from backend.src.whatsapp.bridge import WhatsAppBridge
        from backend.src.core.indian_analyzer import IndianAnalyzer
        from backend.src.core.policy_router import PolicyRouter, ROUTE_AUTO_REPLY, ROUTE_DRAFT_FOR_HUMAN, ROUTE_HANDOFF
        from backend.src.core.indian_localizer import IndianLocalizer
        from backend.src.core.media_processor import MediaProcessor
        from backend.src.core.media_responder import MediaResponder
        from backend.src.core.memory_manager import MemoryManager

        self.config = config
        self.db = db
        self.user_id = user_id
        self.allowed_jids = allowed_jids
        self.data_dir = data_dir
        self.get_soul_fn = get_soul_fn
        self.update_soul_fn = update_soul_fn
        self.has_soul_fn = has_soul_fn
        self._contact_tones = contact_tones  # Shared dict reference with UserAgentController
        self.on_qr_cb = on_qr
        self.on_status_cb = on_status
        self.on_contacts_cb = on_contacts
        self.console = Console()
        self.loop = loop

        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        # Initialize Sarvam client for LLM-2 and fallbacks
        sarvam_key = os.getenv("SARVAM_API_KEY")
        self.sarvam_client = None
        if sarvam_key:
            try:
                self.sarvam_client = OpenAI(
                    api_key=sarvam_key,
                    base_url=config.get("sarvam", {}).get("api_base", "https://api.sarvam.ai/v1"),
                )
                logger.info(f"[UserAgent:{self.user_id}] ✅ Sarvam client initialized")
            except Exception as e:
                logger.warning(f"[UserAgent:{self.user_id}] ⚠️ Sarvam init failed: {e}")

        self.analyzer = IndianAnalyzer(config, fallback_client=self.openai_client)
        self.router = PolicyRouter(config)
        self.localizer = IndianLocalizer(config, fallback_client=self.openai_client)
        self.memory = MemoryManager(db, self.openai_client, config)

        try:
            from backend.src.core.sticker_analyzer import StickerAnalyzer
            self.sticker_analyzer = StickerAnalyzer(self.openai_client)
        except ImportError:
            self.sticker_analyzer = None

        self.media_processor = MediaProcessor(self.openai_client, sticker_analyzer=self.sticker_analyzer)

        # Use user-scoped TTS dir
        tts_dir = os.path.join(data_dir, "tts")
        self.media_responder = MediaResponder(self.openai_client, config)
        self.media_responder.tts_dir = tts_dir

        self.accounts: Dict = {}
        self.sessions: Dict = {}
        self.pending_batches: Dict[str, List[Dict]] = {}
        self.session_locks: Dict[str, asyncio.Lock] = {}
        self.response_locks: Dict[str, asyncio.Lock] = {}
        self.debounce_timers: Dict = {}
        self.debounce_lock = asyncio.Lock()
        self.media_hashes: Dict[str, str] = {}

        # Setup WhatsApp bridge with user-scoped auth dir
        auth_dir = config.get("whatsapp", {}).get("auth_dir", os.path.join(data_dir, "whatsapp"))
        self.wa_bridge = WhatsAppBridge(auth_dir)
        self._setup_wa()
        self.status = {"whatsapp": "disconnected", "last_qr": None}

        # Import pipeline constants
        from backend.src.core.agent_controller import (
            ORCHESTRATOR_SYSTEM_PROMPT, INTERACTIVE_SYSTEM_PROMPT
        )
        self.ORCHESTRATOR_SYSTEM_PROMPT = ORCHESTRATOR_SYSTEM_PROMPT
        self.INTERACTIVE_SYSTEM_PROMPT = INTERACTIVE_SYSTEM_PROMPT

    def _setup_wa(self):
        def on_qr(event):
            self.status["last_qr"] = event["data"]
            self.status["whatsapp"] = "pairing"
            self.loop.call_soon_threadsafe(lambda: self.on_qr_cb(event["data"]))

        def on_connection(event):
            status = event["status"]
            self.status["whatsapp"] = status
            user = event.get("user", {})
            jid = user.get("id")

            if status == "open":
                self.loop.call_soon_threadsafe(
                    lambda: self.on_status_cb("connected", jid, user.get("name"), jid.split("@")[0] if jid else None)
                )
                # Sync contacts
                self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._sync_contacts()))
            elif status == "closed":
                self.loop.call_soon_threadsafe(lambda: self.on_status_cb("disconnected"))
            else:
                self.loop.call_soon_threadsafe(lambda: self.on_status_cb(status))

        def on_message(event):
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._handle_inbound_event(event))
                )

        def on_contacts(event):
            raw_contacts = event.get("data", [])
            logger.info(f"[UserAgent:{self.user_id}] Received {len(raw_contacts)} raw contacts from bridge")
            formatted = []
            for c in raw_contacts:
                jid = c.get("id") or c.get("jid", "")
                if not jid:
                    continue

                # REQUIREMENT 1: REAL CONTACTS ONLY (individuals only)
                is_individual = jid.endswith("@s.whatsapp.net") or jid.endswith("@lid")
                if not is_individual:
                    continue
                
                # Resolve name: name > notify > pushName
                name = c.get("name") or c.get("notify") or c.get("pushName")

                # Extract raw ID part before @
                raw_id = jid.split("@")[0] if "@" in jid else jid
                display_number = ""
                if jid.endswith("@s.whatsapp.net") and raw_id.isdigit():
                    if raw_id.startswith("91") and len(raw_id) == 12:
                        display_number = f"+91 {raw_id[2:7]} {raw_id[7:]}"
                    else:
                        display_number = f"+{raw_id}"
                
                formatted.append({
                    "jid": jid,
                    "name": name or display_number,
                    "number": display_number,
                    "is_group": False, 
                })

            logger.info(f"[UserAgent:{self.user_id}] Passing {len(formatted)} clean contacts to platform")
            self.loop.call_soon_threadsafe(lambda: self.on_contacts_cb(formatted))

        def on_agent_control(event):
            """Handle Start/Stop commands from own WhatsApp"""
            cmd = event.get("command", "").lower()
            pause_file = os.path.join(self.data_dir, "paused.lock")
            if cmd == "stop":
                open(pause_file, "w").close()
                logger.info(f"[UserAgent:{self.user_id}] PAUSED via WhatsApp command")
                self.wa_bridge.send_message(to=event.get("from"), text="⏹️ Orbit AI Paused.")
            elif cmd == "start":
                if os.path.exists(pause_file):
                    os.remove(pause_file)
                logger.info(f"[UserAgent:{self.user_id}] RESUMED via WhatsApp command")
                self.wa_bridge.send_message(to=event.get("from"), text="▶️ Orbit AI Resumed.")

        # In-RAM buffer for history messages per contact (never persisted)
        # Key: JID, Value: list of {"role": "user"/"assistant", "content": text}
        history_buffer: Dict[str, list] = {}
        # Track which JIDs have already been profiled this session
        profiled_jids: set = set()

        def on_history_messages(event):
            """On-the-fly auto-profiler: buffer messages in RAM and generate soul profiles
            immediately using Sarvam AI — no SQLite storage needed."""
            messages = event.get("data", [])
            if not messages:
                return
            # logger.info(f"[UserAgent:{self.user_id}] Received {len(messages)} history messages from bridge")

            # Group incoming messages by contact into the in-RAM buffer
            for m in messages:
                remote_jid = m.get("from", "")
                text = m.get("text", "")
                if not remote_jid or not text or remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
                    continue
                
                # IMPORTANT FIX: Save history message to the SQLite database
                # so the frontend UI knows this contact `has_history` >= 5
                push_name = m.get("pushName") or "User"
                msg_id = m.get("id") or f"hist_{hash(text)}"
                from_me = 1 if m.get("fromMe") else 0
                timestamp = m.get("timestamp")
                
                try:
                    self.db.add_message_and_prune(
                        remote_jid=remote_jid,
                        text=text,
                        push_name=push_name,
                        message_id=msg_id,
                        from_me=from_me,
                        media_type="text",
                        keep=200
                    )
                except Exception as e:
                    logger.warning(f"[UserAgent:{self.user_id}] Failed to save history msg: {e}")

                # Keep only real phone contacts (@s.whatsapp.net) but also allow @lid
                if remote_jid not in history_buffer:
                    history_buffer[remote_jid] = []
                # Keep buffer lean: max 50 messages per contact
                if len(history_buffer[remote_jid]) < 50:
                    history_buffer[remote_jid].append({
                        "role": "assistant" if m.get("fromMe") else "user",
                        "content": text,
                    })

            # For each contact that now has ≥5 messages and hasn't been profiled yet,
            # fire a background soul-generation task
            MIN_MESSAGES = 5
            to_profile = [
                jid for jid, msgs in history_buffer.items()
                if len(msgs) >= MIN_MESSAGES and jid not in profiled_jids
                and not self.has_soul_fn(jid)  # Skip if already has a profile
            ]

            if to_profile:
                async def _generate_souls_bg():
                    sarvam_key = os.getenv("SARVAM_API_KEY")
                    from openai import OpenAI as _OAI
                    if sarvam_key:
                        llm = _OAI(api_key=sarvam_key, base_url="https://api.sarvam.ai/v1")
                        llm_model = "sarvam-m"
                    else:
                        return  # No LLM available — skip silently

                    PROFILE_PROMPT = (
                        "Analyze this WhatsApp conversation and write a concise personality profile "
                        "for how the AI agent should behave with THIS specific contact.\n"
                        "Include: communication style, language mix (Hindi%/English%), tone, "
                        "topics they discuss, and response preferences.\n"
                        "Be specific and actionable. Under 200 words. Markdown format."
                    )

                    for jid in to_profile:
                        if jid in profiled_jids:
                            continue
                        msgs = history_buffer.get(jid, [])
                        if len(msgs) < MIN_MESSAGES:
                            continue
                        try:
                            history_text = "\n".join([
                                f"{'Agent' if m['role'] == 'assistant' else 'Contact'}: {m['content']}"
                                for m in msgs[-30:]  # Use last 30 messages max
                            ])
                            resp = await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda h=history_text: llm.chat.completions.create(
                                    model=llm_model,
                                    messages=[
                                        {"role": "system", "content": PROFILE_PROMPT},
                                        {"role": "user", "content": f"Chat history:\n\n{h}"},
                                    ],
                                    max_tokens=400,
                                    temperature=0.6,
                                )
                            )
                            soul = resp.choices[0].message.content.strip()
                            # Strip markdown fences if present
                            if soul.startswith("```"):
                                soul = "\n".join(soul.split("\n")[1:]).rstrip("`").strip()
                            self.update_soul_fn(jid, soul)
                            profiled_jids.add(jid)
                            # Free RAM — raw messages are no longer needed after profiling
                            history_buffer.pop(jid, None)
                            logger.info(f"[UserAgent:{self.user_id}] 🧠 Auto-profiled {jid} on-the-fly ({len(msgs)} msgs)")
                        except Exception as e:
                            logger.debug(f"[UserAgent:{self.user_id}] Auto-profile failed for {jid}: {e}")

                if self.loop:
                    asyncio.run_coroutine_threadsafe(_generate_souls_bg(), self.loop)

        self.wa_bridge.on_event("qr", on_qr)
        self.wa_bridge.on_event("connection", on_connection)
        self.wa_bridge.on_event("message", on_message)
        self.wa_bridge.on_event("contacts", on_contacts)
        self.wa_bridge.on_event("history_messages", on_history_messages)
        self.wa_bridge.on_event("agent_control", on_agent_control)


    async def _sync_contacts(self):
        """Sync WhatsApp contacts to platform DB via gateway command."""
        try:
            if self.wa_bridge:
                self.wa_bridge.get_contacts()
        except Exception as e:
            logger.error(f"[UserAgent] Sync contacts error: {e}")
        except Exception as e:
            logger.warning(f"[UserAgent:{self.user_id}] Contact sync failed: {e}")

    async def _handle_inbound_event(self, event: Dict):
        remote_jid = event.get("from", "")
        if not remote_jid:
            return

        logger.info(f"[UserAgent:{self.user_id}] 📩 Inbound from {remote_jid} (allowlist size: {len(self.allowed_jids)})")

        # ── ALLOWLIST GATE ──────────────────────────────────────────────
        # Empty allowlist = allow all contacts (user hasn't configured yet)
        if not event.get("fromMe", False) and self.allowed_jids:
            # Direct check
            if remote_jid in self.allowed_jids:
                pass  # Allowed
            else:
                # @lid JIDs don't have a phone number, so they won't be in
                # the allowlist that was built from @s.whatsapp.net contacts.
                # If the JID is @lid, allow it through — the user's dashboard
                # allowlist only tracks @s.whatsapp.net contacts.
                if remote_jid.endswith("@lid"):
                    logger.info(f"[UserAgent:{self.user_id}] ✅ @lid JID {remote_jid} allowed (LID bypass)")
                else:
                    logger.info(f"[UserAgent:{self.user_id}] ⛔ Blocked (not in allowlist): {remote_jid}")
                    return

        async with self._get_session_lock(remote_jid):
            await self._process_inbound_message(event)

    async def _process_inbound_message(self, event: Dict):
        """Identical to original but uses per-user soul and data paths."""
        remote_jid = event.get("from", "")

        if "broadcast" in remote_jid.lower():
            return

        from_me = event.get("fromMe", False)
        is_group = event.get("isGroup", False)

        # Kill switch
        user_text = event.get("text", "").strip().lower()
        pause_file = os.path.join(self.data_dir, "paused.lock")

        if from_me:
            if user_text == "stop orbit":
                open(pause_file, "w").close()
                return
            elif user_text == "start orbit":
                if os.path.exists(pause_file):
                    os.remove(pause_file)
                return
            return  # Don't process owner's messages further

        # Groups: only process if explicitly allowed
        if is_group and remote_jid not in self.allowed_jids:
            return

        # Media processing
        user_text = event.get("text", "")
        inbound_media_type = event.get("mediaType")
        if event.get("mediaPath") and inbound_media_type:
            enriched = await self.media_processor.process(event["mediaPath"], inbound_media_type)
            if enriched:
                user_text = f"{user_text} {enriched}".strip()
                event["text"] = user_text

        self.db.add_activity("whatsapp", f"From {event.get('pushName', '?')}")
        self.db.add_message_and_prune(
            remote_jid=remote_jid,
            text=user_text,
            push_name=event.get("pushName"),
            message_id=event.get("id"),
            from_me=0,
            media_type=inbound_media_type,
            keep=200
        )
        
        # Cleanup media files after storage (Stickers are kept, others deleted after process)
        if event.get("mediaPath") and inbound_media_type != 'sticker':
            def delayed_cleanup(path):
                import time
                time.sleep(10) # Wait for processing
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"Media cleaned up: {path}")

            import threading
            threading.Thread(target=delayed_cleanup, args=(event["mediaPath"],), daemon=True).start()

        session = self._get_session(remote_jid)
        session["last_message_id"] = event.get("id")
        push_name = event.get("pushName", "User")

        self.memory.add_to_short_term(remote_jid, "user", f"[{push_name}]: {user_text}")

        if remote_jid not in self.pending_batches:
            self.pending_batches[remote_jid] = []
        self.pending_batches[remote_jid].append({**event, "text": user_text})

        if os.path.exists(pause_file):
            return

        if self.config.get("whatsapp", {}).get("auto_respond", True) and not from_me:
            await self._schedule_auto_response(remote_jid)

    async def _schedule_auto_response(self, remote_jid: str):
        import asyncio
        async with self.debounce_lock:
            debounce = self.config.get("whatsapp", {}).get("debounce_seconds", 8)
            if remote_jid in self.debounce_timers:
                self.debounce_timers[remote_jid].cancel()
            self.debounce_timers[remote_jid] = self.loop.call_later(
                debounce,
                lambda: asyncio.create_task(self._process_auto_respond(remote_jid))
            )

    async def _background_soul_refresh(self, remote_jid: str):
        """Periodically regenerate the soul profile using the last 50 messages."""
        try:
            # Get last 50 messages from SQLite for context
            msgs = self.db.get_messages(remote_jid, limit=50)
            if not msgs or len(msgs) < 10:
                return

            history_text = "\n".join([
                f"{'Agent' if m.get('from_me') else m.get('push_name', 'Contact')}: {m.get('text', '')}"
                for m in reversed(msgs) # messages come back DESC, we want chronological
            ])

            sarvam_key = os.getenv("SARVAM_API_KEY")
            from openai import OpenAI as _OAI
            llm_model = self.config.get("openai", {}).get("model", "gpt-4o")
            llm = self.openai_client
            
            if sarvam_key:
                llm = _OAI(api_key=sarvam_key, base_url="https://api.sarvam.ai/v1")
                llm_model = "sarvam-m"

            PROFILE_PROMPT = (
                "Analyze this WhatsApp conversation and write a concise personality profile "
                "for how the AI agent should behave with THIS specific contact.\n"
                "Include: communication style, language mix (Hindi%/English%), tone, "
                "topics they discuss, and response preferences.\n"
                "Be specific and actionable. Under 200 words. Markdown format."
            )

            resp = await self.loop.run_in_executor(
                None,
                lambda h=history_text: llm.chat.completions.create(
                    model=llm_model,
                    messages=[
                        {"role": "system", "content": PROFILE_PROMPT},
                        {"role": "user", "content": f"Chat history:\n\n{h}"},
                    ],
                    max_tokens=400,
                    temperature=0.6,
                )
            )
            soul = resp.choices[0].message.content.strip()
            if soul.startswith("```"):
                soul = "\n".join(soul.split("\n")[1:]).rstrip("`").strip()
            
            # Save the new soul
            self.update_contact_soul(remote_jid, soul)
            logger.info(f"[UserAgent:{self.user_id}] 🧠 Background soul refresh complete for {remote_jid}")
        except Exception as e:
            logger.error(f"[UserAgent:{self.user_id}] Background soul refresh failed for {remote_jid}: {e}")

    async def _process_auto_respond(self, remote_jid: str):
        """Full pipeline — same as original but with per-user soul."""
        import time
        from backend.src.core.policy_router import ROUTE_AUTO_REPLY, ROUTE_HANDOFF, ROUTE_DRAFT_FOR_HUMAN

        async with self._get_response_lock(remote_jid):
            async with self.debounce_lock:
                self.debounce_timers.pop(remote_jid, None)

            batch = self.pending_batches.pop(remote_jid, [])
            if not batch:
                return

            # Keep track of messages for periodic auto-profiling
            session = self._get_session(remote_jid)
            if "msg_count_since_profile" not in session:
                session["msg_count_since_profile"] = 0
            
            session["msg_count_since_profile"] += len(batch)
            
            # Every 30 messages, trigger a background soul regeneration
            if session["msg_count_since_profile"] >= 30:
                logger.info(f"[UserAgent:{self.user_id}] 🧠 Triggering periodic soul refresh for {remote_jid}")
                session["msg_count_since_profile"] = 0 # Reset counter
                if self.loop:
                    asyncio.create_task(self._background_soul_refresh(remote_jid))

            # EMERGENCY / MONEY HANDOFF DETECTION
            full_text = " ".join(m.get("text", "") for m in batch).lower()
            emergency_keywords = ["emergency", "urgent", "help", "hospital", "police", "fire", "accident", "dying"]
            money_keywords = ["pay", "payment", "upi", "gpay", "transfer", "rupees", "account", "bank", "amount"]
            
            
            is_emergency = any(k in full_text for k in emergency_keywords)
            is_money = any(k in full_text for k in money_keywords)

            if is_emergency or is_money:
                reason = "Emergency" if is_emergency else "Payment/Money"
                logger.info(f"[Handoff] Detecting {reason} in message from {remote_jid}")
                feedback = "Wait a bit, I'll get back to you shortly."
                if is_emergency:
                    feedback = "I've seen your message. I'll get back to you immediately."
                
                await self._send_text(remote_jid, feedback)
                # Log as handoff for dashboard notification
                self.db.log_analysis(remote_jid, {"vibe": "serious", "intent": reason}, "HANDOFF", f"Detected {reason} keywords", len(batch))
                return

            inbound_media_type = batch[-1].get("mediaType")
            
            # --- STICKER FALLBACK (No Vision Model Available) ---
            if inbound_media_type == "sticker":
                logger.info(f"[UserAgent:{self.user_id}] Sticker received from {remote_jid}, skipping LLM and sending light response.")
                import random
                light_responses = [
                    "Haha nice sticker 😂", 
                    "lol crazy sticker", 
                    "nice one 🤣", 
                    "lmao", 
                    "where do you even find these stickers 😂"
                ]
                await self._send_text(remote_jid, random.choice(light_responses))
                return

            start = time.time()

            try:
                # LLM-1 analyze
                analysis = await self.analyzer.analyze(batch)

                # Route
                route, route_reason = self.router.route(analysis)
                self.db.log_analysis(remote_jid, analysis, route, route_reason, len(batch))

                if route == ROUTE_HANDOFF:
                    handoff_msg = self.config.get("agent", {}).get(
                        "support_contact",
                        "Thik hai bhai, operator se contact karo."
                    )
                    await self._send_text(remote_jid, handoff_msg)
                    return

                # LLM-2 plan
                current_text = " ".join(m.get("text", "") for m in batch)
                plan = await self._run_orchestrator(remote_jid, analysis, current_text)
                if not plan:
                    return

                if route == ROUTE_DRAFT_FOR_HUMAN:
                    self.db.save_draft(remote_jid, plan.get("reply_text", ""), "", "", "")
                    return

                # Localize
                reply_text = plan.get("reply_text", "")
                if reply_text and not plan.get("skip_reply"):
                    reply_text = await self.localizer.localize(
                        reply_text, analysis.get("vibe", "neutral"), analysis.get("language", "hinglish")
                    )

                # Media decision + execute
                response_type = self.media_responder.recommend_response_type(
                    analysis, plan, inbound_media_type
                )

                await self._execute_plan(
                    remote_jid=remote_jid,
                    plan=plan,
                    localized_reply=reply_text,
                    response_type=response_type,
                    analysis=analysis,
                    session=self._get_session(remote_jid),
                )

                if self.memory.should_reflect(remote_jid):
                    asyncio.create_task(self._reflect(remote_jid))

            except Exception as e:
                logger.error(f"[UserAgent:{self.user_id}] Pipeline error for {remote_jid}: {e}")

    async def _run_orchestrator(self, remote_jid: str, analysis: Dict, current_text: str) -> Optional[Dict]:
        import json
        session = self._get_session(remote_jid)
        history = session["history"]
        memory_ctx = self.memory.build_memory_context(remote_jid, current_text)

        orchestrator_msg = (
            f"[INCOMING MESSAGE BATCH]:\n{current_text}\n\n"
            f"[LLM-1 ANALYSIS]:\n```json\n{json.dumps(analysis, indent=2)}\n```\n\n"
            + (f"[MEMORY CONTEXT]:\n{memory_ctx}\n\n" if memory_ctx else "")
            + "Create the action plan JSON now."
        )

        # Filter history to remove any system messages (Sarvam is strict: 1 system msg at start)
        clean_history = [m for m in history[-20:] if m.get("role") != "system"]
        # Sarvam also requires the first message after system to be from user
        # Drop leading assistant messages if any
        while clean_history and clean_history[0].get("role") == "assistant":
            clean_history = clean_history[1:]

        messages = [
            {"role": "system", "content": self.ORCHESTRATOR_SYSTEM_PROMPT},
            *clean_history,
            {"role": "user", "content": orchestrator_msg},
        ]

        try:
            # Use Sarvam if available, else fallback to OpenAI
            client = self.sarvam_client or self.openai_client
            model = "sarvam-m" if self.sarvam_client else self.config.get("openai", {}).get("model", "gpt-4o")
            
            # Sarvam doesn't strictly support response_format={"type": "json_object"} in the same way,
            # but we can try or just rely on the prompt which asks for JSON.
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 800,
                "temperature": self.config.get("openai", {}).get("temperature", 0.75),
            }
            
            # Only add json_object if using OpenAI
            if not self.sarvam_client:
                kwargs["response_format"] = {"type": "json_object"}
            
            response = client.chat.completions.create(**kwargs)
            
            raw_content = response.choices[0].message.content
            # Clean JSON fences if Sarvam adds them
            if "```json" in raw_content:
                raw_content = raw_content.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_content:
                raw_content = raw_content.split("```")[1].split("```")[0].strip()
                
            plan = json.loads(raw_content)
            session["history"].append({
                "role": "assistant",
                "content": plan.get("reply_text") or "[media/sticker/reaction only]",
            })
            self.memory.add_to_short_term(remote_jid, "assistant", plan.get("reply_text", ""))
            return plan
        except Exception as e:
            logger.error(f"[UserAgent:{self.user_id}] Orchestrator error: {e}")
            return None

    async def _execute_plan(self, remote_jid, plan, localized_reply, response_type, analysis, session):
        vibe = analysis.get("vibe", "neutral")
        last_message_id = session.get("last_message_id")

        # React
        emoji = plan.get("reaction_emoji", "").strip()
        if emoji and last_message_id:
            try:
                self.wa_bridge.react(to=remote_jid, message_id=last_message_id, emoji=emoji)
            except Exception:
                pass

        # Sticker
        sticker_vibe = plan.get("sticker_vibe", "").strip()
        if not sticker_vibe and analysis.get("requires_sticker"):
            sticker_vibe = vibe
        if sticker_vibe and self.sticker_analyzer:
            self._send_sticker(remote_jid, sticker_vibe)

        # Text/audio reply
        should_reply = localized_reply and (not plan.get("skip_reply"))
        if should_reply:
            if response_type == "audio":
                audio_path = await self.media_responder.generate_voice_note(localized_reply, vibe)
                if audio_path:
                    self.wa_bridge.send_message(
                        to=remote_jid, text="", media=audio_path, media_type="audio"
                    )
                    self.db.add_message(remote_jid=remote_jid, text=f"[Voice]", from_me=1, media_type="audio")
                else:
                    await self._send_text(remote_jid, localized_reply)
            else:
                await self._send_text(remote_jid, localized_reply)

        # Memory
        new_details = plan.get("remember_user_details", [])
        if new_details:
            facts = {item["key"]: item["value"] for item in new_details if item.get("key")}
            if facts:
                self.memory.update_long_term(remote_jid, facts)

        if len(session["history"]) > 30:
            session["history"] = [session["history"][0]] + session["history"][-10:]

    async def _send_text(self, jid: str, text: str):
        pause_file = os.path.join(self.data_dir, "paused.lock")
        if os.path.exists(pause_file):
            return
        try:
            self.wa_bridge.send_message(to=jid, text=text)
            self.db.add_message(remote_jid=jid, text=text, from_me=1)
        except Exception as e:
            logger.error(f"[UserAgent:{self.user_id}] Send error: {e}")

    def _send_sticker(self, jid: str, vibe: str) -> bool:
        try:
            stickers = self.sticker_analyzer.search_stickers(vibe=vibe)
            if stickers:
                self.wa_bridge.send_message(to=jid, text="", media=stickers[0]["path"], media_type="sticker")
                return True
        except Exception:
            pass
        return False

    async def _reflect(self, remote_jid: str):
        recent = [dict(m) for m in self.db.get_messages(remote_jid, limit=self.memory.REFLECTION_EVERY_N)]
        await self.memory.extract_and_store_episodes(remote_jid, recent)

    def _get_session(self, remote_jid: str) -> Dict:
        if remote_jid not in self.sessions:
            import json
            lt_memory = self.memory.format_long_term_context(remote_jid)
            soul = self.get_soul_fn(remote_jid)
            # Custom tone for this specific contact (e.g. 'friendly and humorous')
            custom_tone = self._contact_tones.get(remote_jid, "")
            session_data = self.db.get_session(remote_jid)
            intelligence = {}
            summary = ""
            if session_data:
                try:
                    intelligence = json.loads(session_data["intelligence"] or "{}")
                    summary = session_data.get("summary", "")
                except Exception:
                    pass

            summary_str = f"\n[CONVERSATION SUMMARY]: {summary}" if summary else ""
            tone_str = f"\n\n[CUSTOM TONE FOR THIS CONTACT]: {custom_tone}" if custom_tone else ""
            system_content = (
                f"{self.INTERACTIVE_SYSTEM_PROMPT}\n\n"
                f"{lt_memory}\n\n{summary_str}\n\n{soul}{tone_str}"
            ).strip()

            self.sessions[remote_jid] = {
                "history": [{"role": "system", "content": system_content}],
                "intelligence": intelligence,
                "last_message_id": None,
            }
        return self.sessions[remote_jid]

    def _get_session_lock(self, jid: str) -> asyncio.Lock:
        if jid not in self.session_locks:
            self.session_locks[jid] = asyncio.Lock()
        return self.session_locks[jid]

    def _get_response_lock(self, jid: str) -> asyncio.Lock:
        if jid not in self.response_locks:
            self.response_locks[jid] = asyncio.Lock()
        return self.response_locks[jid]

    async def run_headless(self):
        logger.info(f"[UserAgent:{self.user_id}] Running headless...")
        self.wa_bridge.start()
        while True:
            await asyncio.sleep(3600)