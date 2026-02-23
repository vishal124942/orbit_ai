"""
UserAgentController
====================

CHANGES in v3.1:
1. Contacts are emitted in chunks of 50 so the frontend gets live updates
   rather than waiting for one giant batch at the end.
2. _emitted_history_jids is a module-level set per controller instance so it
   persists across multiple on_history_messages calls in the same session.
3. on_contacts: deduplication via seen_jids set before calling on_contacts_cb,
   preventing duplicate DB writes when the gateway emits overlapping batches.
4. contact_sync_progress events from the gateway are forwarded as live WS
   broadcasts so the frontend can display a running counter.
5. asyncio.get_running_loop() used everywhere — no get_event_loop() calls.
6. on_agent_control: pathlib.Path.touch() for pause file (no resource leak).
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Set, List, Optional, Callable
from openai import OpenAI

logger = logging.getLogger(__name__)

# Chunk size for streaming contacts to the frontend
CONTACT_CHUNK_SIZE = 50


class UserAgentController:
    def __init__(
        self,
        user_id: str,
        config: Dict,
        allowed_jids: Set[str],
        on_status: Callable,
        on_contacts: Callable,
        on_pairing_code: Callable = None,
        on_contact_sync_progress: Callable = None,
    ):
        self.user_id = user_id
        self.config = config
        self.allowed_jids = allowed_jids
        self.on_status_cb = on_status
        self.on_contacts_cb = on_contacts
        self.on_pairing_code_cb = on_pairing_code
        self.on_contact_sync_progress_cb = on_contact_sync_progress  # optional live-count callback

        self.data_dir = config["_data_dir"]
        self.soul_override = config.get("_soul_override", "")

        self._controller = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._contact_souls: Dict[str, str] = {}
        self._load_contact_souls()

        self._contact_tones: Dict[str, str] = {}

    def _load_contact_souls(self):
        souls_dir = os.path.join(self.data_dir, "souls")
        os.makedirs(souls_dir, exist_ok=True)
        for fname in os.listdir(souls_dir):
            if fname.endswith(".md"):
                jid = fname[:-3].replace("_", "@")
                try:
                    with open(os.path.join(souls_dir, fname)) as f:
                        self._contact_souls[jid] = f.read()
                except Exception:
                    pass

    def get_soul_for_contact(self, jid: str) -> str:
        if jid in self._contact_souls:
            return self._contact_souls[jid]
        if self.soul_override:
            return self.soul_override
        soul_path = os.path.join(os.getcwd(), "soul.md")
        if os.path.exists(soul_path):
            with open(soul_path) as f:
                return f.read()
        return ""

    def update_contact_soul(self, jid: str, soul_content: str):
        souls_dir = os.path.join(self.data_dir, "souls")
        os.makedirs(souls_dir, exist_ok=True)
        safe_jid = jid.replace("@", "_")
        path = os.path.join(souls_dir, f"{safe_jid}.md")
        with open(path, "w") as f:
            f.write(soul_content)
        self._contact_souls[jid] = soul_content
        if self._controller and jid in self._controller.sessions:
            del self._controller.sessions[jid]

    def update_allowed_jids(self, allowed_jids: Set[str]):
        self.allowed_jids = allowed_jids
        if self._controller:
            self._controller.allowed_jids = allowed_jids

    def is_jid_allowed(self, jid: str) -> bool:
        if not self.allowed_jids:
            return True
        return jid in self.allowed_jids

    def update_contact_tone_live(self, jid: str, tone: str):
        self._contact_tones[jid] = tone
        if self._controller:
            self._controller._contact_tones[jid] = tone
            self._controller.sessions.pop(jid, None)

    async def start(self):
        self._loop = asyncio.get_running_loop()

        db_path = os.path.join(self.data_dir, "agent.db")

        from backend.src.core.database import Database
        db = Database(db_path=db_path)
        config = dict(self.config)

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
            on_status=self.on_status_cb,
            on_contacts=self.on_contacts_cb,
            on_pairing_code=self.on_pairing_code_cb,
            on_contact_sync_progress=self.on_contact_sync_progress_cb,
            loop=self._loop,
        )

        self._controller = controller
        logger.info(f"[UserAgent:{self.user_id}] Starting agent...")
        await controller.run_headless()

    async def stop(self):
        if self._controller and hasattr(self._controller, "wa_bridge"):
            try:
                # wa_bridge.stop() performs subprocess waits/thread joins and can
                # block for seconds; run it off the event loop.
                await asyncio.to_thread(self._controller.wa_bridge.stop)
            except Exception as e:
                logger.warning(f"[UserAgent:{self.user_id}] Stop error: {e}")

    async def _sync_contacts(self):
        try:
            if self._controller:
                self._controller.wa_bridge.get_contacts()
        except Exception as e:
            logger.warning(f"[UserAgent:{self.user_id}] Contact sync failed: {e}")


class _IsolatedAgentController:
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
        on_status: Callable,
        on_contacts: Callable,
        on_pairing_code: Callable,
        on_contact_sync_progress: Callable,
        loop: asyncio.AbstractEventLoop,
    ):
        import os
        import json
        import asyncio
        from openai import OpenAI
        from rich.console import Console

        from backend.src.whatsapp.bridge import WhatsAppBridge
        from backend.src.core.indian_analyzer import IndianAnalyzer
        from backend.src.core.policy_router import PolicyRouter
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
        self._contact_tones = contact_tones
        self.on_status_cb = on_status
        self.on_contacts_cb = on_contacts
        self.on_pairing_code_cb = on_pairing_code
        self.on_contact_sync_progress_cb = on_contact_sync_progress
        self.console = Console()
        self.loop = loop

        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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

        auth_dir = config.get("whatsapp", {}).get("auth_dir", os.path.join(data_dir, "whatsapp"))
        phone_number = config.get("whatsapp", {}).get("phone_number")
        self.wa_bridge = WhatsAppBridge(auth_dir, phone_number=phone_number, session_id=self.user_id)
        self._setup_wa()
        self.status = {"whatsapp": "disconnected", "pairing_code": None}

        from backend.src.core.agent_controller import (
            ORCHESTRATOR_SYSTEM_PROMPT, INTERACTIVE_SYSTEM_PROMPT
        )
        self.ORCHESTRATOR_SYSTEM_PROMPT = ORCHESTRATOR_SYSTEM_PROMPT
        self.INTERACTIVE_SYSTEM_PROMPT = INTERACTIVE_SYSTEM_PROMPT

    def _setup_wa(self):
        loop = self.loop

        # ── Session-level deduplication set for contacts ────────────────────
        # Tracks which JIDs have already been emitted to avoid duplicate DB writes
        # when the gateway re-emits overlapping contact batches.
        _seen_jids: Set[str] = set()
        # Tracks JIDs emitted specifically from history messages (cause-4 fix)
        _emitted_history_jids: Set[str] = set()

        def on_pairing_code(event):
            self.status["pairing_code"] = event["code"]
            self.status["whatsapp"] = "pairing"
            if self.on_pairing_code_cb:
                loop.call_soon_threadsafe(lambda: self.on_pairing_code_cb(event["code"]))

        def on_connection(event):
            status = event["status"]
            self.status["whatsapp"] = status
            user = event.get("user", {})
            jid = user.get("id")

            if status == "open":
                # Reset reconnect counter on successful connection
                self.wa_bridge.reconnect_attempts = 0
                loop.call_soon_threadsafe(
                    lambda: self.on_status_cb(
                        "connected", jid,
                        user.get("name"),
                        jid.split("@")[0] if jid else None
                    )
                )
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._sync_contacts())
                )
            elif status == "closed":
                loop.call_soon_threadsafe(lambda: self.on_status_cb("disconnected"))
            else:
                loop.call_soon_threadsafe(lambda: self.on_status_cb(status))

        def on_message(event):
            if loop and loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._handle_inbound_event(event))
                )

        def _format_contact(c: dict) -> Optional[dict]:
            """
            Normalise a single raw contact dict from the gateway.
            Returns None if the JID is unusable.
            """
            jid = c.get("id") or c.get("jid", "")
            if not jid:
                return None
            is_individual = jid.endswith("@s.whatsapp.net") or jid.endswith("@lid")
            if not is_individual:
                return None

            name = c.get("name") or c.get("notify") or c.get("pushName")
            raw_id = jid.split("@")[0] if "@" in jid else jid
            display_number = ""

            if jid.endswith("@s.whatsapp.net") and raw_id.isdigit():
                if raw_id.startswith("91") and len(raw_id) == 12:
                    display_number = f"+91 {raw_id[2:7]} {raw_id[7:]}"
                else:
                    display_number = f"+{raw_id}"
            elif jid.endswith("@lid"):
                lid_id = c.get("lidId") or raw_id
                display_number = f"~{lid_id}" if lid_id else f"~{raw_id}"

            final_name = name or display_number or jid

            return {
                "jid": jid,
                "name": final_name,
                "number": display_number,
                "is_group": False,
            }

        def on_contacts(event):
            """
            Receive contacts from gateway, deduplicate, emit in chunks of 50
            for streaming updates to the frontend.
            """
            raw_contacts = event.get("data", [])
            logger.info(f"[UserAgent:{self.user_id}] Received {len(raw_contacts)} raw contacts")

            formatted = []
            for c in raw_contacts:
                jid = c.get("id") or c.get("jid", "")
                if not jid or jid in _seen_jids:
                    continue
                result = _format_contact(c)
                if result:
                    formatted.append(result)
                    _seen_jids.add(jid)

            if not formatted:
                logger.debug(f"[UserAgent:{self.user_id}] No new contacts after dedup")
                return

            logger.info(f"[UserAgent:{self.user_id}] {len(formatted)} new contacts after dedup (total seen: {len(_seen_jids)})")

            # Emit in chunks so the frontend gets progressive updates
            for i in range(0, len(formatted), CONTACT_CHUNK_SIZE):
                chunk = formatted[i:i + CONTACT_CHUNK_SIZE]
                loop.call_soon_threadsafe(lambda ch=chunk: self.on_contacts_cb(ch))

        def on_contact_sync_progress(event):
            """Forward gateway's running count to the platform as a WS broadcast."""
            count = event.get("count", 0)
            if self.on_contact_sync_progress_cb:
                loop.call_soon_threadsafe(lambda: self.on_contact_sync_progress_cb(count))

        def on_agent_control(event):
            cmd = event.get("command", "").lower()
            pause_file = Path(self.data_dir) / "paused.lock"
            if cmd == "stop":
                pause_file.touch()
                logger.info(f"[UserAgent:{self.user_id}] PAUSED via WhatsApp command")
                self.wa_bridge.send_message(to=event.get("from"), text="⏹️ Orbit AI Paused.")
            elif cmd == "start":
                if pause_file.exists():
                    pause_file.unlink()
                logger.info(f"[UserAgent:{self.user_id}] RESUMED via WhatsApp command")
                self.wa_bridge.send_message(to=event.get("from"), text="▶️ Orbit AI Resumed.")

        history_buffer: Dict[str, list] = {}
        profiled_jids: set = set()

        def on_history_messages(event):
            """
            Save history to DB and emit new contact stubs from message senders.
            Deduplication via _emitted_history_jids prevents repeat DB writes.
            """
            messages = event.get("data", [])
            if not messages:
                return

            # Collect new sender JIDs not yet emitted
            new_contact_stubs = []
            seen_in_batch: Set[str] = set()

            for m in messages:
                remote_jid = m.get("from", "")
                if not remote_jid:
                    continue
                if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
                    continue
                if not (remote_jid.endswith("@s.whatsapp.net") or remote_jid.endswith("@lid")):
                    continue
                if remote_jid in _emitted_history_jids or remote_jid in seen_in_batch:
                    continue

                seen_in_batch.add(remote_jid)

                raw_id = remote_jid.split("@")[0]
                display_number = ""
                if remote_jid.endswith("@s.whatsapp.net") and raw_id.isdigit():
                    if raw_id.startswith("91") and len(raw_id) == 12:
                        display_number = f"+91 {raw_id[2:7]} {raw_id[7:]}"
                    else:
                        display_number = f"+{raw_id}"
                elif remote_jid.endswith("@lid"):
                    display_number = f"~{raw_id}"

                push_name = m.get("pushName") or ""
                final_name = push_name or display_number or remote_jid

                new_contact_stubs.append({
                    "jid": remote_jid,
                    "name": final_name,
                    "number": display_number,
                    "is_group": False,
                })
                _emitted_history_jids.add(remote_jid)
                _seen_jids.add(remote_jid)  # also deduplicate against on_contacts

            if new_contact_stubs:
                logger.info(
                    f"[UserAgent:{self.user_id}] history_messages: "
                    f"{len(new_contact_stubs)} new contact stubs from senders"
                )
                # Emit in chunks for streaming
                for i in range(0, len(new_contact_stubs), CONTACT_CHUNK_SIZE):
                    chunk = new_contact_stubs[i:i + CONTACT_CHUNK_SIZE]
                    loop.call_soon_threadsafe(lambda ch=chunk: self.on_contacts_cb(ch))

            # ── Save messages to DB ──────────────────────────────────────────
            for m in messages:
                remote_jid = m.get("from", "")
                text = m.get("text", "")
                if not remote_jid or not text or remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
                    continue

                push_name = m.get("pushName") or "User"
                msg_id = m.get("id") or f"hist_{hash(text)}"
                from_me = 1 if m.get("fromMe") else 0

                try:
                    self.db.add_message_and_prune(
                        remote_jid=remote_jid,
                        text=text,
                        push_name=push_name,
                        message_id=msg_id,
                        from_me=from_me,
                        media_type="text",
                        keep=200,
                    )
                except Exception as e:
                    logger.warning(f"[UserAgent:{self.user_id}] Failed to save history msg: {e}")

                if remote_jid not in history_buffer:
                    history_buffer[remote_jid] = []
                if len(history_buffer[remote_jid]) < 50:
                    history_buffer[remote_jid].append({
                        "role": "assistant" if m.get("fromMe") else "user",
                        "content": text,
                    })

            # ── Auto-generate souls for contacts with enough history ──────────
            MIN_MESSAGES = 5
            to_profile = [
                jid for jid, msgs in history_buffer.items()
                if len(msgs) >= MIN_MESSAGES
                and jid not in profiled_jids
                and not self.has_soul_fn(jid)
            ]

            if to_profile:
                async def _generate_souls_bg():
                    sarvam_key = os.getenv("SARVAM_API_KEY")
                    from openai import OpenAI as _OAI
                    if not sarvam_key:
                        return
                    llm = _OAI(api_key=sarvam_key, base_url="https://api.sarvam.ai/v1")
                    llm_model = "sarvam-m"

                    PROFILE_PROMPT = (
                        "Analyze this WhatsApp conversation and write a concise personality "
                        "profile for how the AI agent should behave with THIS specific contact.\n"
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
                                for m in msgs[-30:]
                            ])
                            running_loop = asyncio.get_running_loop()
                            resp = await running_loop.run_in_executor(
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
                            self.update_soul_fn(jid, soul)
                            profiled_jids.add(jid)
                            history_buffer.pop(jid, None)
                            logger.info(f"[UserAgent:{self.user_id}] 🧠 Auto-profiled {jid} ({len(msgs)} msgs)")
                        except Exception as e:
                            logger.debug(f"[UserAgent:{self.user_id}] Auto-profile failed for {jid}: {e}")

                asyncio.run_coroutine_threadsafe(_generate_souls_bg(), loop)

        self.wa_bridge.on_event("pairing_code", on_pairing_code)
        self.wa_bridge.on_event("connection", on_connection)
        self.wa_bridge.on_event("message", on_message)
        self.wa_bridge.on_event("contacts", on_contacts)
        self.wa_bridge.on_event("contact_sync_progress", on_contact_sync_progress)
        self.wa_bridge.on_event("history_messages", on_history_messages)
        self.wa_bridge.on_event("agent_control", on_agent_control)

    async def _sync_contacts(self):
        try:
            if self.wa_bridge:
                self.wa_bridge.get_contacts()
        except Exception as e:
            logger.error(f"[UserAgent] Sync contacts error: {e}")

    async def _handle_inbound_event(self, event: Dict):
        remote_jid = event.get("from", "")
        if not remote_jid:
            return

        logger.info(
            f"[UserAgent:{self.user_id}] 📩 Inbound from {remote_jid} "
            f"(allowlist size: {len(self.allowed_jids)})"
        )

        if not event.get("fromMe", False) and self.allowed_jids:
            if remote_jid in self.allowed_jids:
                pass
            elif remote_jid.endswith("@lid"):
                logger.info(f"[UserAgent:{self.user_id}] ✅ @lid {remote_jid} allowed (bypass)")
            else:
                logger.info(f"[UserAgent:{self.user_id}] ⛔ Blocked: {remote_jid}")
                return

        async with self._get_session_lock(remote_jid):
            await self._process_inbound_message(event)

    async def _process_inbound_message(self, event: Dict):
        remote_jid = event.get("from", "")
        if "broadcast" in remote_jid.lower():
            return

        from_me = event.get("fromMe", False)
        is_group = event.get("isGroup", False)
        user_text = event.get("text", "").strip().lower()
        pause_file = Path(self.data_dir) / "paused.lock"

        if from_me:
            if user_text == "stop orbit":
                pause_file.touch()
                return
            elif user_text == "start orbit":
                if pause_file.exists():
                    pause_file.unlink()
                return
            return

        if is_group and remote_jid not in self.allowed_jids:
            return

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
            keep=200,
        )

        if event.get("mediaPath") and inbound_media_type != "sticker":
            import threading
            def delayed_cleanup(p):
                import time
                time.sleep(10)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            threading.Thread(target=delayed_cleanup, args=(event["mediaPath"],), daemon=True).start()

        session = self._get_session(remote_jid)
        session["last_message_id"] = event.get("id")
        push_name = event.get("pushName", "User")

        self.memory.add_to_short_term(remote_jid, "user", f"[{push_name}]: {user_text}")

        if remote_jid not in self.pending_batches:
            self.pending_batches[remote_jid] = []
        self.pending_batches[remote_jid].append({**event, "text": user_text})

        if pause_file.exists():
            return

        if self.config.get("whatsapp", {}).get("auto_respond", True) and not from_me:
            await self._schedule_auto_response(remote_jid)

    async def _schedule_auto_response(self, remote_jid: str):
        async with self.debounce_lock:
            debounce = self.config.get("whatsapp", {}).get("debounce_seconds", 3)
            if remote_jid in self.debounce_timers:
                self.debounce_timers[remote_jid].cancel()
            self.debounce_timers[remote_jid] = self.loop.call_later(
                debounce,
                lambda: asyncio.create_task(self._process_auto_respond(remote_jid)),
            )

    async def _background_soul_refresh(self, remote_jid: str):
        try:
            msgs = self.db.get_messages(remote_jid, limit=50)
            if not msgs or len(msgs) < 10:
                return

            history_text = "\n".join([
                f"{'Agent' if m.get('from_me') else m.get('push_name', 'Contact')}: {m.get('text', '')}"
                for m in reversed(msgs)
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
            self.update_soul_fn(remote_jid, soul)
            logger.info(f"[UserAgent:{self.user_id}] 🧠 Background soul refresh for {remote_jid}")
        except Exception as e:
            logger.error(f"[UserAgent:{self.user_id}] Soul refresh failed: {e}")

    async def _process_auto_respond(self, remote_jid: str):
        from backend.src.core.policy_router import ROUTE_AUTO_REPLY, ROUTE_HANDOFF, ROUTE_DRAFT_FOR_HUMAN

        async with self._get_response_lock(remote_jid):
            async with self.debounce_lock:
                self.debounce_timers.pop(remote_jid, None)

            batch = self.pending_batches.pop(remote_jid, [])
            if not batch:
                return

            session = self._get_session(remote_jid)
            session.setdefault("msg_count_since_profile", 0)
            session["msg_count_since_profile"] += len(batch)

            if session["msg_count_since_profile"] >= 30:
                session["msg_count_since_profile"] = 0
                asyncio.create_task(self._background_soul_refresh(remote_jid))

            full_text = " ".join(m.get("text", "") for m in batch).lower()
            emergency_keywords = ["emergency", "urgent", "help", "hospital", "police", "fire", "accident", "dying"]
            money_keywords = ["pay", "payment", "upi", "gpay", "transfer", "rupees", "account", "bank", "amount"]

            is_emergency = any(k in full_text for k in emergency_keywords)
            is_money = any(k in full_text for k in money_keywords)

            if is_emergency or is_money:
                reason = "Emergency" if is_emergency else "Payment/Money"
                feedback = ("I've seen your message. I'll get back to you immediately."
                            if is_emergency else
                            "Wait a bit, I'll get back to you shortly.")
                await self._send_text(remote_jid, feedback)
                self.db.log_analysis(remote_jid, {"vibe": "serious", "intent": reason}, "HANDOFF", f"Detected {reason} keywords", len(batch))
                return

            inbound_media_type = batch[-1].get("mediaType")

            if inbound_media_type == "sticker":
                import random
                await self._send_text(remote_jid, random.choice([
                    "Haha nice sticker 😂", "lol crazy sticker", "nice one 🤣",
                    "lmao", "where do you even find these stickers 😂",
                ]))
                return

            try:
                if not inbound_media_type:
                    analysis = {
                        "vibe": "neutral", "sentiment_score": 0.0, "toxicity": "safe",
                        "intent": "casual", "risk": "low", "language": "mixed",
                        "requires_sticker": False, "requires_reaction": False,
                        "summary": "Text message",
                    }
                else:
                    analysis = await self.analyzer.analyze(batch)

                route, route_reason = self.router.route(analysis)
                self.db.log_analysis(remote_jid, analysis, route, route_reason, len(batch))

                if route == ROUTE_HANDOFF:
                    handoff_msg = self.config.get("agent", {}).get("support_contact", "Thik hai bhai, operator se contact karo.")
                    await self._send_text(remote_jid, handoff_msg)
                    return

                current_text = " ".join(m.get("text", "") for m in batch)
                plan = await self._run_orchestrator(remote_jid, analysis, current_text)
                if not plan:
                    return

                if route == ROUTE_DRAFT_FOR_HUMAN:
                    self.db.save_draft(remote_jid, plan.get("reply_text", ""), "", "", "")
                    return

                reply_text = plan.get("reply_text", "")
                response_type = self.media_responder.recommend_response_type(analysis, plan, inbound_media_type)

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
                logger.error(f"[UserAgent:{self.user_id}] Pipeline error for {remote_jid}: {e}", exc_info=True)

    async def _run_orchestrator(self, remote_jid: str, analysis: Dict, current_text: str) -> Optional[Dict]:
        session = self._get_session(remote_jid)
        history = session["history"]
        memory_ctx = self.memory.build_memory_context(remote_jid, current_text)

        orchestrator_msg = (
            f"[INCOMING MESSAGE BATCH]:\n{current_text}\n\n"
            f"[LLM-1 ANALYSIS]:\n```json\n{json.dumps(analysis, indent=2)}\n```\n\n"
            + (f"[MEMORY CONTEXT]:\n{memory_ctx}\n\n" if memory_ctx else "")
            + "Create the action plan JSON now."
        )

        clean_history = [m for m in history[-20:] if m.get("role") != "system"]
        while clean_history and clean_history[0].get("role") == "assistant":
            clean_history = clean_history[1:]

        messages = [
            {"role": "system", "content": self.ORCHESTRATOR_SYSTEM_PROMPT},
            *clean_history,
            {"role": "user", "content": orchestrator_msg},
        ]

        try:
            client = self.sarvam_client or self.openai_client
            model = "sarvam-m" if self.sarvam_client else self.config.get("openai", {}).get("model", "gpt-4o")
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 800,
                "temperature": self.config.get("openai", {}).get("temperature", 0.75),
            }
            if not self.sarvam_client:
                kwargs["response_format"] = {"type": "json_object"}

            response = client.chat.completions.create(**kwargs)
            raw_content = response.choices[0].message.content
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

        emoji = plan.get("reaction_emoji", "").strip()
        if emoji and last_message_id:
            try:
                self.wa_bridge.react(to=remote_jid, message_id=last_message_id, emoji=emoji)
            except Exception:
                pass

        sticker_vibe = plan.get("sticker_vibe", "").strip()
        if not sticker_vibe and analysis.get("requires_sticker"):
            sticker_vibe = vibe
        if sticker_vibe and self.sticker_analyzer:
            self._send_sticker(remote_jid, sticker_vibe)

        if localized_reply and not plan.get("skip_reply"):
            if response_type == "audio":
                audio_path = await self.media_responder.generate_voice_note(localized_reply, vibe)
                if audio_path:
                    self.wa_bridge.send_message(to=remote_jid, text="", media=audio_path, media_type="audio")
                    self.db.add_message(remote_jid=remote_jid, text="[Voice]", from_me=1, media_type="audio")
                else:
                    await self._send_text(remote_jid, localized_reply)
            else:
                await self._send_text(remote_jid, localized_reply)

        new_details = plan.get("remember_user_details", [])
        if new_details:
            facts = {item["key"]: item["value"] for item in new_details if item.get("key")}
            if facts:
                self.memory.update_long_term(remote_jid, facts)

        if len(session["history"]) > 30:
            session["history"] = [session["history"][0]] + session["history"][-10:]

    async def _send_text(self, jid: str, text: str):
        pause_file = Path(self.data_dir) / "paused.lock"
        if pause_file.exists():
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
            lt_memory = self.memory.format_long_term_context(remote_jid)
            soul = self.get_soul_fn(remote_jid)
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
