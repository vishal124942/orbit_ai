"""
Agent Controller — Two-LLM Stack + Full Media I/O + Three-Tier Memory
=======================================================================

Inbound:  audio, voice note, image, static sticker, animated sticker, video
Outbound: text, voice note (TTS), sticker, reaction emoji

Memory:
  Short-term  → in-RAM sliding window (last ~20 turns)
  Long-term   → persisted facts per JID (SQLite intelligence blob)
  Episodic    → memorable moments per JID (SQLite episodes table)

Pipeline (autonomous):
  Bridge → Filter/Dedup → MediaProcessor → DB + Session
      → Debounce Aggregator
      → LLM-1: IndianAnalyzer  (analysis JSON)
      → PolicyRouter            (route decision)
      → LLM-2: Orchestrator    (action plan JSON incl. response_type)
      → IndianLocalizer         (Hinglish rewrite)
      → MediaResponder          (generate TTS / pick sticker / text)
      → Deterministic Executor  (send everything)
      → MemoryManager.reflect() (episodic + long-term update)
      → DB Metrics
"""

import os
import json
import time
import asyncio
import base64
import hashlib
from typing import List, Dict, Any, Optional

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown

from backend.src.whatsapp.bridge import WhatsAppBridge
from backend.src.core.database import Database
from backend.src.core.indian_analyzer import IndianAnalyzer
from backend.src.core.policy_router import PolicyRouter, ROUTE_AUTO_REPLY, ROUTE_DRAFT_FOR_HUMAN, ROUTE_HANDOFF
from backend.src.core.indian_localizer import IndianLocalizer
from backend.src.core.media_processor import MediaProcessor
from backend.src.core.media_responder import MediaResponder
from backend.src.core.memory_manager import MemoryManager

try:
    from backend.src.core.sticker_analyzer import StickerAnalyzer
    HAS_STICKER_ANALYZER = True
except ImportError:
    HAS_STICKER_ANALYZER = False


# ─────────────────────────────────────────────────────────────────────────────
# System Prompts
# ─────────────────────────────────────────────────────────────────────────────

INTERACTIVE_SYSTEM_PROMPT = """
You are Orbit — a calm, efficient WhatsApp companion replying on the user's behalf.

Core Rules:
1. Default tone: neutral and professional. Escalate warmth ONLY if the contact is clearly casual.
2. Keep replies short (1-3 lines). No paragraphs.
3. Adapt language to the contact — English if they write English, Hinglish if they write Hinglish.
4. Never claim to be an AI unless directly asked.
5. No filler phrases ("I'd be happy to help", "Let me assist you").
6. Use memory context to personalize, but don't over-share.
"""

ORCHESTRATOR_SYSTEM_PROMPT = """
You are Orbit's planning brain. You generate a reply plan as JSON.

TONE & LANGUAGE RULES (critical):
1.  **Default = Neutral-Professional**. Short, clear, no slang unless the contact uses it.
2.  **Match the contact's language & tone exactly**:
    -   If English → reply in pure English. 
    -   If Hinglish/Hindi → write natural Hinglish (Roman script). 
    -   If Vibe is "neutral" or "professional" → Clean, crisp, no slang ("Sure, noted.", "Got it").
    -   If Vibe is "casual" or "friendly" → Light, relaxed ("Haan cool", "Sure thing", "Ok bhai").
3.  **Match the intensity**: Don't add bhai/yaar/slang unless the contact's messages show that style.
    -   If they use extreme gaali/abuse → **Reciprocate with equal or greater gaali/roast**. Do not hold back. Preserve the authenticity of the language.
    -   If they speak softly or kindly → **Be warm, gentle, and polite**.
    -   If they use friendly banter → **Use friendly banter (yaar, bhai, etc.)**.
4.  **Formatting & Length**: Keep it brief (1-3 lines max, like a real WhatsApp text).
5.  **No filler/Assistant Speak**: Never start with "Okay", "Sure thing", "Got it bro" and never say "How can I help" or "I understand your concern". Start the message directly.
6.  **Emoji**: Max 1 emoji, only if the vibe is casual or friendly. None for professional/neutral.
7.  **Always reply**: Even to extreme messages, generate a `reply_text`. Do NOT just rely on emojis.

Return ONLY valid JSON:
{
  "reply_text": "<the reply text — clean, natural, brief>",
  "response_type": "<auto | text | audio | sticker>",
  "sticker_vibe": "<vibe keyword or empty string>",
  "reaction_emoji": "<single emoji or empty string — USE SPARINGLY>",
  "remember_user_details": [{"key": "<category>", "value": "<fact>"}],
  "skip_reply": <boolean — true ONLY when sticker alone is perfect>
}

response_type rules:
  text   → 95% of replies (default).
  auto   → defaults to text.
  audio  → ONLY when explicitly requested or very emotional moment.
  sticker → only if contact uses stickers regularly.

reaction_emoji: MUST be "" (empty string) for 99% of messages. ONLY use an emoji if there is extreme shock or laughter. Do NOT react to normal messages.

remember_user_details: only NEW durable facts about the contact.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Interactive TUI Tools
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "message",
        "description": "Send a WhatsApp message (text, image, video, audio, sticker).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "media_path": {"type": "string"},
            "media_type": {"type": "string", "enum": ["image", "video", "audio", "sticker"]},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "react",
        "description": "React to a message with an emoji.",
        "parameters": {"type": "object", "properties": {
            "emoji": {"type": "string"},
            "message_id": {"type": "string"},
        }, "required": ["emoji", "message_id"]},
    }},
    {"type": "function", "function": {
        "name": "send_sticker",
        "description": "Send a sticker from the vault based on vibe/emotion.",
        "parameters": {"type": "object", "properties": {
            "vibe": {"type": "string"},
        }, "required": ["vibe"]},
    }},
    {"type": "function", "function": {
        "name": "send_voice_note",
        "description": "Send a voice note (TTS). Great for banter, reactions, emotional moments.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Text to speak as voice note."},
            "vibe": {"type": "string", "description": "Vibe for voice selection: banter/roast/sad/fun/etc."},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "remember_user_details",
        "description": "Store a durable fact about this user.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"},
        }, "required": ["key", "value"]},
    }},
    {"type": "function", "function": {
        "name": "list_stickers",
        "description": "Search sticker vault by vibe.",
        "parameters": {"type": "object", "properties": {
            "vibe": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "recall_memory",
        "description": "Recall what you know about this person — long-term facts and past episodes.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for in memory."},
        }, "required": ["query"]},
    }},
]


# ─────────────────────────────────────────────────────────────────────────────
# Agent Controller
# ─────────────────────────────────────────────────────────────────────────────

class AIAgentController:
    def __init__(self, config: Dict, db: Database):
        self.config = config
        self.db = db
        self.console = Console()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # ── Core pipeline components ───────────────────────────────────────
        self.analyzer       = IndianAnalyzer(config, fallback_client=self.openai_client)
        self.router         = PolicyRouter(config)
        self.localizer      = IndianLocalizer(config, fallback_client=self.openai_client)
        self.memory         = MemoryManager(db, self.openai_client, config)

        # ── Sticker analyzer ───────────────────────────────────────────────
        self.sticker_analyzer = None
        if HAS_STICKER_ANALYZER:
            self.sticker_analyzer = StickerAnalyzer(self.openai_client)

        self.media_processor = MediaProcessor(self.openai_client, sticker_analyzer=self.sticker_analyzer)
        self.media_responder = MediaResponder(self.openai_client, config)

        # ── State ──────────────────────────────────────────────────────────
        self.accounts: Dict = {}
        self.sessions: Dict = {}        # JID → {history, intelligence, last_message_id}
        self.active_session = {"target": None, "account_id": None, "last_message_id": None}
        self.pending_batches: Dict[str, List[Dict]] = {}

        # ── Locks ──────────────────────────────────────────────────────────
        self.session_locks: Dict[str, asyncio.Lock] = {}
        self.response_locks: Dict[str, asyncio.Lock] = {}
        self.debounce_timers: Dict = {}
        self.debounce_lock = asyncio.Lock()

        # ── Media dedup cache ──────────────────────────────────────────────
        self.media_hashes: Dict[str, str] = {}
        self._load_media_cache()

        # ── Soul personality file ──────────────────────────────────────────
        self.soul_content = ""
        soul_path = os.path.join(os.getcwd(), "soul.md")
        if os.path.exists(soul_path):
            try:
                with open(soul_path, "r") as f:
                    self.soul_content = f.read()
            except Exception as e:
                print(f"[Warning] soul.md not loaded: {e}")

        # ── WhatsApp bridge ────────────────────────────────────────────────
        self.wa_bridge = WhatsAppBridge(config.get("whatsapp", {}).get("auth_dir"))
        self._setup_wa()
        self.status = {"whatsapp": "disconnected", "last_qr": None}

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _load_media_cache(self):
        media_dir = "data/media"
        if os.path.exists(media_dir):
            for filename in os.listdir(media_dir):
                file_hash = filename.split(".")[0]
                if len(file_hash) == 64:
                    self.media_hashes[file_hash] = os.path.join(media_dir, filename)

    def _get_session_lock(self, jid):
        if jid not in self.session_locks:
            self.session_locks[jid] = asyncio.Lock()
        return self.session_locks[jid]

    def _get_response_lock(self, jid):
        if jid not in self.response_locks:
            self.response_locks[jid] = asyncio.Lock()
        return self.response_locks[jid]

    def _hash_file(self, path):
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # WhatsApp Bridge Setup
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_wa(self):
        def on_qr(event):
            self.status["last_qr"] = event["data"]
            self.status["whatsapp"] = "pairing"

        def on_connection(event):
            self.status["whatsapp"] = event["status"]
            if event["status"] == "open":
                user = event.get("user", {})
                jid = user.get("id")
                if jid:
                    self.accounts[jid] = {"platform": "whatsapp", "name": user.get("name"), "number": jid.split("@")[0]}
                self.db.add_activity("system", f"WhatsApp connected as {jid}")

        def on_message(event):
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._handle_inbound_event(event))
                )

        def on_error(event):
            self.console.print(f"[red]❌ WA Error: {event.get('message')}[/red]")

        self.wa_bridge.on_event("qr", on_qr)
        self.wa_bridge.on_event("connection", on_connection)
        self.wa_bridge.on_event("error", on_error)
        self.wa_bridge.on_event("message", on_message)

    # ──────────────────────────────────────────────────────────────────────────
    # Inbound Handling
    # ──────────────────────────────────────────────────────────────────────────

    async def _handle_inbound_event(self, event: Dict):
        remote_jid = event.get("from", "")
        async with self._get_session_lock(remote_jid):
            await self._process_inbound_message(event)

    async def _process_inbound_message(self, event: Dict):
        remote_jid = event.get("from", "")

        if not remote_jid or "broadcast" in remote_jid.lower():
            return

        # God Mode: Learn from owner's messages, but don't auto-reply to them
        from_me = event.get("fromMe", False)
        
        # ── Global Kill Switch ────────────────────────────────────────────────
        user_text = event.get("text", "").strip().lower()
        PAUSE_LOCK_FILE = "data/paused.lock"

        if from_me:
            if user_text == "stop orbit":
                with open(PAUSE_LOCK_FILE, "w") as f: f.write("paused")
                self.console.print("[bold red]⛔ SYSTEM PAUSED by Owner[/bold red]")
                return
            elif user_text == "start orbit":
                if os.path.exists(PAUSE_LOCK_FILE):
                    os.remove(PAUSE_LOCK_FILE)
                    self.console.print("[bold green]✅ SYSTEM RESUMED by Owner[/bold green]")
                return


        is_group = event.get("isGroup", False)
        is_active = remote_jid == self.active_session.get("target")
        if is_group and not is_active:
            return

        # Media dedup
        media_path = event.get("mediaPath")
        if media_path:
            fh = self._hash_file(media_path)
            if fh and fh in self.media_hashes:
                event["mediaPath"] = self.media_hashes[fh]
            elif fh:
                self.media_hashes[fh] = media_path

        # DB write (raw)
        self.db.add_activity("whatsapp", f"From {event.get('pushName', '?')}")
        self.db.add_message(
            remote_jid=remote_jid,
            text=event.get("text", ""),
            push_name=event.get("pushName"),
            message_id=event.get("id"),
            from_me=1 if from_me else 0,
            media_type=event.get("mediaType"),
        )

        # ── Media Processing (parallel for video, sequential otherwise) ───
        user_text = event.get("text", "")
        inbound_media_type = event.get("mediaType")

        if event.get("mediaPath") and inbound_media_type:
            self.console.print(f"[dim]🎬 Processing {inbound_media_type}...[/dim]")
            enriched = await self.media_processor.process(
                event["mediaPath"], inbound_media_type
            )
            if enriched:
                user_text = f"{user_text} {enriched}".strip()
                self.db.update_message_text(event.get("id"), user_text)
                event["text"] = user_text
                self.console.print(f"[dim]   → {enriched[:80]}[/dim]")

        # Session + short-term memory update
        session = self._get_session(remote_jid)
        session["last_message_id"] = event.get("id")
        push_name = event.get("pushName", "User")

        turn = {"role": "user", "content": f"[{push_name}]: {user_text}"}
        session["history"].append(turn)
        self.memory.add_to_short_term(remote_jid, "user", f"[{push_name}]: {user_text}")

        # Active target
        if not is_group and not self.active_session.get("target"):
            self.active_session["target"] = remote_jid
            self.active_session["account_id"] = list(self.accounts.keys())[0] if self.accounts else None

        media_indicator = f" [{inbound_media_type}]" if inbound_media_type else ""
        self.console.print(
            f"\n[bold #F6C453]🔔 INBOUND[/bold #F6C453] "
            f"[dim]{push_name}{media_indicator}: {user_text[:80]}[/dim]"
        )

        # Batch accumulation for debounce
        if remote_jid not in self.pending_batches:
            self.pending_batches[remote_jid] = []
        self.pending_batches[remote_jid].append({**event, "text": user_text})

        # ──────────────────────────────────────────────────────────────────────
        # Check if system is paused (skip auto-response if paused)
        PAUSE_LOCK_FILE = "data/paused.lock"
        if os.path.exists(PAUSE_LOCK_FILE):
            return

        if self.config.get("whatsapp", {}).get("auto_respond", True) and not is_group and not from_me:
            await self._schedule_auto_response(remote_jid)

    # ──────────────────────────────────────────────────────────────────────────
    # Debounce
    # ──────────────────────────────────────────────────────────────────────────

    async def _schedule_auto_response(self, remote_jid: str):
        async with self.debounce_lock:
            debounce = self.config.get("whatsapp", {}).get("debounce_seconds", 8)
            if remote_jid in self.debounce_timers:
                self.debounce_timers[remote_jid].cancel()
            self.debounce_timers[remote_jid] = self.loop.call_later(
                debounce,
                lambda: asyncio.create_task(self._process_auto_respond(remote_jid))
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Full Pipeline
    # ──────────────────────────────────────────────────────────────────────────

    async def _process_auto_respond(self, remote_jid: str):
        async with self._get_response_lock(remote_jid):
            async with self.debounce_lock:
                self.debounce_timers.pop(remote_jid, None)

            start = time.time()
            batch = self.pending_batches.pop(remote_jid, [])
            if not batch:
                return

            self.console.print(
                f"\n[bold #F6C453]⚙️  PIPELINE[/bold #F6C453] "
                f"[dim]{remote_jid} — {len(batch)} msg(s)[/dim]"
            )

            metrics = {
                "route": ROUTE_AUTO_REPLY, "message_sent": False, "audio_sent": False,
                "sticker_sent": False, "reaction_sent": False, "human_handoff": False,
                "draft_created": False, "error_occurred": False, "error_message": "",
            }
            inbound_media_type = batch[-1].get("mediaType") if batch else None

            try:
                # ── Step 1: LLM-1 Analyze ────────────────────────────────
                self.console.print("[bold #7B7F87]🧪 LLM-1:[/bold #7B7F87] [dim]Analyzing...[/dim]")
                analysis = await self.analyzer.analyze(batch)
                self.console.print(
                    f"   [dim]vibe=[/dim][#F6C453]{analysis['vibe']}[/#F6C453] | "
                    f"[dim]intent=[/dim][#F6C453]{analysis['intent']}[/#F6C453] | "
                    f"[dim]lang=[/dim]{analysis['language']} | "
                    f"[dim]tox=[/dim]{analysis['toxicity']}"
                )

                # ── Step 2: Route ─────────────────────────────────────────
                route, route_reason = self.router.route(analysis)
                metrics["route"] = route
                self.console.print(f"[bold #7B7F87]🔀 ROUTE:[/bold #7B7F87] [dim]{self.router.describe(route, route_reason)}[/dim]")
                self.db.log_analysis(remote_jid, analysis, route, route_reason, len(batch))

                if route == ROUTE_HANDOFF:
                    metrics["human_handoff"] = True
                    self.console.print(f"[bold red]🚨 HANDOFF — {route_reason}[/bold red]")
                    
                    # Notify user we are signing off
                    handoff_msg = "Thik hai bhai, tu sambhal le ab. Main chalta hu."
                    try:
                        # 1. Localize the persona message
                        localized_msg = await self.localizer.localize(
                            handoff_msg, vibe="casual", language=analysis.get("language", "hinglish")
                        )
                        
                        # 2. Append system details (Time + Contact)
                        import datetime
                        current_time = datetime.datetime.now().strftime("%I:%M %p")
                        contact_info = self.config.get("agent", {}).get("support_contact", "Admin")
                        
                        final_msg = f"{localized_msg}\n\n[System Handoff]\n🕒 Time: {current_time}\n📞 Contact: {contact_info}"
                        
                        await self._send_text(remote_jid, final_msg, metrics)
                    except Exception as e:
                        self.console.print(f"[red]Handoff msg failed: {e}[/red]")
                        pass

                    self.db.add_activity("handoff", f"HANDOFF {remote_jid}: {route_reason}")
                    self._log_metrics(remote_jid, metrics, start)
                    return

                # ── Step 3: LLM-2 Orchestrator ───────────────────────────
                self.console.print("[bold #7B7F87]🧠 LLM-2:[/bold #7B7F87] [dim]Planning...[/dim]")
                current_text = " ".join(m.get("text", "") for m in batch)
                plan = await self._run_orchestrator(remote_jid, analysis, current_text)

                if not plan:
                    metrics["error_occurred"] = True
                    metrics["error_message"] = "No plan from orchestrator"
                    self.console.print("[red]❌ Plan failed[/red]")
                    self._log_metrics(remote_jid, metrics, start)
                    return

                # ── Draft route ───────────────────────────────────────────
                if route == ROUTE_DRAFT_FOR_HUMAN:
                    metrics["draft_created"] = True
                    did = self.db.save_draft(remote_jid, plan.get("reply_text",""),
                                             plan.get("sticker_vibe",""), plan.get("reaction_emoji",""),
                                             json.dumps(analysis))
                    self.console.print(f"[yellow]✏️  DRAFT #{did} saved[/yellow]")
                    self._log_metrics(remote_jid, metrics, start)
                    return

                # ── Step 4: Localize ──────────────────────────────────────
                reply_text = plan.get("reply_text", "")
                if reply_text and not plan.get("skip_reply"):
                    self.console.print("[bold #7B7F87]🌏 LOCALIZING:[/bold #7B7F87] [dim]Hinglish-ifying...[/dim]")
                    reply_text = await self.localizer.localize(
                        reply_text, analysis.get("vibe","neutral"), analysis.get("language","hinglish")
                    )

                # ── Step 5: Media decision ────────────────────────────────
                response_type = self.media_responder.recommend_response_type(
                    analysis, plan, inbound_media_type
                )
                self.console.print(f"[bold #7B7F87]📤 OUTBOUND:[/bold #7B7F87] [dim]{response_type}[/dim]")

                # ── Step 6: Execute ───────────────────────────────────────
                await self._execute_plan(
                    remote_jid=remote_jid,
                    plan=plan,
                    localized_reply=reply_text,
                    response_type=response_type,
                    analysis=analysis,
                    session=self._get_session(remote_jid),
                    metrics=metrics,
                )

                # ── Step 7: Memory reflection (async, non-blocking) ───────
                if self.memory.should_reflect(remote_jid):
                    asyncio.create_task(self._reflect(remote_jid))

                # Prune old episodes periodically
                if self.db.get_episode_count(remote_jid) > 200:
                    self.db.delete_old_episodes(remote_jid, keep=150)

            except Exception as e:
                self.console.print(f"[red]❌ Pipeline error: {e}[/red]")
                metrics["error_occurred"] = True
                metrics["error_message"] = str(e)
            finally:
                self._log_metrics(remote_jid, metrics, start)

    async def _reflect(self, remote_jid: str):
        """Async background reflection — extract episodes + long-term facts."""
        self.console.print(f"[dim]🧠 Memory reflection for {remote_jid}...[/dim]")
        recent_messages = [dict(m) for m in self.db.get_messages(remote_jid, limit=self.memory.REFLECTION_EVERY_N)]
        await self.memory.extract_and_store_episodes(remote_jid, recent_messages)

    def _log_metrics(self, remote_jid, metrics, start_time):
        self.db.log_pipeline_metric(
            remote_jid=remote_jid,
            route=metrics.get("route","UNKNOWN"),
            response_time_ms=int((time.time()-start_time)*1000),
            message_sent=metrics.get("message_sent",False),
            audio_sent=metrics.get("audio_sent",False),
            sticker_sent=metrics.get("sticker_sent",False),
            reaction_sent=metrics.get("reaction_sent",False),
            human_handoff=metrics.get("human_handoff",False),
            draft_created=metrics.get("draft_created",False),
            error_occurred=metrics.get("error_occurred",False),
            error_message=metrics.get("error_message",""),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # LLM-2: Orchestrator
    # ──────────────────────────────────────────────────────────────────────────

    async def _run_orchestrator(self, remote_jid: str, analysis: Dict, current_text: str = "") -> Optional[Dict]:
        session = self._get_session(remote_jid)
        history = session["history"]

        # Build memory context block
        memory_ctx = self.memory.build_memory_context(remote_jid, current_text)

        orchestrator_msg = (
            f"[INCOMING MESSAGE BATCH]:\n{current_text}\n\n"
            f"[LLM-1 ANALYSIS]:\n```json\n{json.dumps(analysis, indent=2)}\n```\n\n"
            + (f"[MEMORY CONTEXT]:\n{memory_ctx}\n\n" if memory_ctx else "")
            + "Create the action plan JSON now."
        )

        messages = [
            {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
            *history[-20:],
            {"role": "user", "content": orchestrator_msg},
        ]

        try:
            response = self.openai_client.chat.completions.create(
                model=self.config.get("openai", {}).get("model", "gpt-4o"),
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=800,
                temperature=self.config.get("openai", {}).get("temperature", 0.75),
            )
            plan = json.loads(response.choices[0].message.content)
            session["history"].append({
                "role": "assistant",
                "content": plan.get("reply_text") or "[media/sticker/reaction only]",
            })
            self.memory.add_to_short_term(remote_jid, "assistant", plan.get("reply_text",""))
            return plan
        except Exception as e:
            self.console.print(f"[red]Orchestrator error: {e}[/red]")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Deterministic Executor
    # ──────────────────────────────────────────────────────────────────────────

    async def _execute_plan(
        self, remote_jid, plan, localized_reply, response_type, analysis, session, metrics
    ):
        """Order: react → sticker → text/audio → memory"""
        last_message_id = session.get("last_message_id")
        vibe = analysis.get("vibe", "neutral")

        # 1. React
        emoji = plan.get("reaction_emoji", "").strip()
        # Fail-safe: LLM sometimes hallucinates text or empty space
        if emoji and len(emoji) > 2:
            emoji = ""
            
        if emoji and last_message_id:
            try:
                self.wa_bridge.react(to=remote_jid, message_id=last_message_id, emoji=emoji)
                metrics["reaction_sent"] = True
                self.console.print(f"[green]✓[/green] React: {emoji}")
            except Exception as e:
                self.console.print(f"[yellow]⚠️  React failed: {e}[/yellow]")

        # 2. Sticker
        sticker_vibe = plan.get("sticker_vibe", "").strip()
        if not sticker_vibe and analysis.get("requires_sticker"):
            sticker_vibe = vibe
        if sticker_vibe:
            metrics["sticker_sent"] = self._send_sticker(remote_jid, sticker_vibe)

        # 3. Reply (text or audio)
        # Fallback 1: If sticker failed (e.g. empty vault), force text reply even if skip_reply=True
        # Fallback 2: If the message is highly toxic/banter, ignore skip_reply and always respond.
        is_extreme = analysis.get("toxicity") in ["banter", "heavy-banter"]
        should_reply = localized_reply and (
            not plan.get("skip_reply") 
            or not metrics.get("sticker_sent") 
            or is_extreme
        )

        if should_reply:
            if response_type == "audio":
                # Generate TTS voice note
                self.console.print("[dim]🎙️  Generating voice note...[/dim]")
                audio_path = await self.media_responder.generate_voice_note(
                    localized_reply, vibe
                )
                if audio_path:
                    try:
                        await self._send_media(
                            jid=remote_jid, media_path=audio_path, media_type="audio", caption=""
                        )
                        self.db.add_message(remote_jid=remote_jid, text=f"[Voice: {localized_reply}]", from_me=1, media_type="audio")
                        # Also send text if needed? No, just voice is usually enough or follows with text
                        # But for now, let's say if voice fails, we send text.
                    except Exception as e:
                        self.console.print(f"[red]Voice send failed: {e}[/red]")
                        await self._send_text(remote_jid, localized_reply, metrics)
                else:
                     # No audio generated, fallback to text
                    await self._send_text(remote_jid, localized_reply, metrics)

            else:
                # Text response
                await self._send_text(remote_jid, localized_reply, metrics)

        # 4. Memory — remember new details
        new_details = plan.get("remember_user_details", [])
        if new_details:
            facts = {item["key"]: item["value"] for item in new_details if item.get("key") and item.get("value")}
            if facts:
                self.memory.update_long_term(remote_jid, facts)
                for k, v in facts.items():
                    session["intelligence"][k] = v
                    self.console.print(f"[blue]ℹ[/blue] Remembered: {k} = {v}")

        # Trim history
        if len(session["history"]) > 30:
            await self.compact_history(remote_jid)

    async def _send_text(self, jid: str, text: str, metrics: Dict = None):
        # Emergency Stop Check: Don't send if paused
        if os.path.exists("data/paused.lock"):
            self.console.print(f"[bold red]⛔ SKIP SEND (Paused): {text[:30]}...[/bold red]")
            return

        try:
            self.wa_bridge.send_message(to=jid, text=text)
            self.db.add_message(remote_jid=jid, text=text, from_me=1)
            if metrics is not None:
                metrics["message_sent"] = True
            self.console.print(f"[green]✓[/green] Text: {text[:70]}")
        except Exception as e:
            self.console.print(f"[red]✗ Send text failed: {e}[/red]")

    async def _send_media(self, jid: str, media_path: str, media_type: str, caption: str = None, metrics: Dict = None):
        # Emergency Stop Check: Don't send if paused
        if os.path.exists("data/paused.lock"):
            self.console.print(f"[bold red]⛔ SKIP MEDIA (Paused): {media_type}[/bold red]")
            return

        try:
            self.wa_bridge.send_message(to=jid, text=caption or "", media=media_path, media_type=media_type)
            self.db.add_message(remote_jid=jid, text=caption or f"[{media_type}]", from_me=1, media_type=media_type)
            if metrics is not None:
                metrics[f"{media_type}_sent"] = True
            self.console.print(f"[green]✓[/green] Media ({media_type}): {media_path[:30]}...")
            return True
        except Exception as e:
            self.console.print(f"[red]✗ Send media failed: {e}[/red]")
            return False

    def _send_sticker(self, remote_jid, vibe):
        stickers = self.list_stickers(vibe, remote_jid=remote_jid)
        if not stickers:
            # Phase 17: Silent handling for empty vault (Collecting Knowledge)
            self.console.print(f"   [dim]Vault: No matching sticker for '{vibe}' yet (Collecting...)[/dim]")
            return False
        try:
            sticker = stickers[0]
            self.wa_bridge.send_message(to=remote_jid, text="", media=sticker["path"], media_type="sticker")
            self.console.print(f"[green]✓[/green] Sticker: {vibe} ([dim]{sticker.get('description','')[:50]}...[/dim])")
            return True
        except Exception as e:
            self.console.print(f"[red]✗ Sticker failed: {e}[/red]")
            return False

    def list_stickers(self, vibe: str = None, remote_jid: str = None, **kwargs) -> List[Dict]:
        """Query sticker vault with advanced filtering based on user tolerance."""
        if not self.sticker_analyzer:
            return []
        
        # Check user intelligence for vulgarity tolerance
        max_vulgarity = 'none'
        if not remote_jid:
            remote_jid = self.active_session.get('target')
            
        if remote_jid:
            session = self._get_session(remote_jid)
            intelligence = session.get('intelligence', {})
            gaali_tolerance = intelligence.get('gaali_tolerance', 'none')
            
            # Map gaali tolerance to vulgarity levels
            vulgarity_map = {
                'none': 'none',
                'low': 'mild',
                'medium': 'moderate',
                'high': 'high'
            }
            max_vulgarity = vulgarity_map.get(gaali_tolerance.lower(), 'none')
        
        return self.sticker_analyzer.search_stickers(
            vibe=vibe,
            max_vulgarity=max_vulgarity,
            **kwargs
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Session Management
    # ──────────────────────────────────────────────────────────────────────────

    def _get_session(self, remote_jid: str) -> Dict:
        if remote_jid not in self.sessions:
            lt_memory = self.memory.format_long_term_context(remote_jid)
            summary = ""
            session_data = self.db.get_session(remote_jid)
            intelligence = {}
            if session_data:
                try:
                    intelligence = json.loads(session_data["intelligence"] or "{}")
                    summary = session_data["summary"] or ""
                except Exception:
                    pass

            summary_str = f"\n[CONVERSATION SUMMARY]: {summary}" if summary else ""
            system_content = (
                f"{INTERACTIVE_SYSTEM_PROMPT}\n\n"
                f"{lt_memory}\n\n"
                f"{summary_str}\n\n"
                f"{self.soul_content}"
            ).strip()

            self.sessions[remote_jid] = {
                "history": [{"role": "system", "content": system_content}],
                "intelligence": intelligence,
                "last_message_id": None,
            }
        return self.sessions[remote_jid]

    def _get_history(self, remote_jid):
        return self._get_session(remote_jid)["history"]

    # ──────────────────────────────────────────────────────────────────────────
    # History Compaction
    # ──────────────────────────────────────────────────────────────────────────

    async def compact_history(self, remote_jid: str):
        if remote_jid not in self.sessions:
            return
        self.console.print(f"[dim]🧹 Compacting {remote_jid}...[/dim]")
        session = self._get_session(remote_jid)
        history = session["history"]
        messages = self.db.get_messages(remote_jid=remote_jid, limit=50)
        history_text = "\n".join([
            f"{'Orbit' if m['from_me'] else 'User'}: {m['text']}" for m in messages
        ])
        try:
            response = self.openai_client.chat.completions.create(
                model=self.config["openai"]["model"],
                messages=[
                    {"role": "system", "content": "Summarize this WhatsApp chat in under 150 words. Preserve tone, key facts, vibe, any memorable moments."},
                    {"role": "user", "content": history_text},
                ],
                max_tokens=200,
            )
            summary = response.choices[0].message.content
            self.db.update_session(remote_jid, summary=summary)
            session["history"] = [history[0]] + history[-10:]
            self.console.print("[dim]✅ Compacted.[/dim]")
        except Exception as e:
            self.console.print(f"[red]❌ Compaction failed: {e}[/red]")

    # ──────────────────────────────────────────────────────────────────────────
    # Interactive TUI Chat (tool-calling)
    # ──────────────────────────────────────────────────────────────────────────

    async def chat(self, user_message: str, remote_jid: Optional[str] = None, autonomous: bool = False):
        if not remote_jid:
            remote_jid = self.active_session.get("target")
        if not remote_jid:
            yield {"type": "error", "data": {"message": "No active target."}}
            return

        session = self._get_session(remote_jid)
        history = session["history"]
        history.append({"role": "user", "content": user_message})
        self.memory.add_to_short_term(remote_jid, "user", user_message)

        try:
            turns, max_turns = 0, 6
            while turns < max_turns:
                turns += 1
                response = self.openai_client.chat.completions.create(
                    model=self.config["openai"].get("model", "gpt-4o"),
                    messages=history, tools=TOOLS, tool_choice="auto",
                )
                msg = response.choices[0].message
                history.append(msg)

                if msg.content:
                    yield {"type": "content", "data": {"text": msg.content, "silent": autonomous}}

                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        fn = tc.function.name
                        args = json.loads(tc.function.arguments)
                        result = await self._execute_tool(fn, args, remote_jid)
                        history.append({"role": "tool", "tool_call_id": tc.id, "name": fn, "content": json.dumps(result)})
                        yield {"type": "tool_executed", "data": {"function": fn, "arguments": args, "result": result}}
                    continue
                else:
                    break

            if len(history) > 30:
                await self.compact_history(remote_jid)
            if self.memory.should_reflect(remote_jid):
                asyncio.create_task(self._reflect(remote_jid))

        except Exception as e:
            self.console.print(f"[red]{e}[/red]")
            yield {"type": "error", "data": {"message": str(e)}}

    async def _execute_tool(self, function_name: str, arguments: Dict, remote_jid: str) -> Dict:
        try:
            if function_name == "message":
                text = arguments.get("text", "")
                media_path = arguments.get("media_path")
                media_type = arguments.get("media_type")

                if media_path and media_type:
                    await self._send_media(remote_jid, media_path, media_type, caption=text)
                    return {"status": "sent", "media": media_type}
                else:
                    await self._send_text(remote_jid, text)
                    return {"status": "sent", "text": text}

            elif function_name == "react":
                # Reacts are fine to skip pause check or can be added if strictness needed
                # For now, allowing reacts as they are low impact, but let's be consistent:
                if not os.path.exists("data/paused.lock"):
                    self.wa_bridge.react(to=remote_jid, message_id=arguments.get("message_id"), emoji=arguments.get("emoji"))
                return {"status": "reacted", "emoji": arguments.get("emoji")}

            elif function_name == "send_sticker":
                vibe = arguments.get("vibe", "happy")
                # Fix: removed duplicate call
                ok = await self._send_sticker(remote_jid, vibe)
                return {"status": "sticker_sent" if ok else "no_sticker_found", "vibe": vibe}

            elif function_name == "send_voice_note":
                text = arguments.get("text", "")
                vibe = arguments.get("vibe", "default")
                
                # Check pause before generating audio to save resources
                if os.path.exists("data/paused.lock"):
                    self.console.print(f"[bold red]⛔ SKIP VOICE (Paused)[/bold red]")
                    return {"status": "skipped_paused"}

                audio_path = await self.media_responder.generate_voice_note(text, vibe)
                if audio_path:
                    await self._send_media(remote_jid, audio_path, "audio", caption="")
                    return {"status": "voice_sent"}
                return {"status": "tts_failed"}

            elif function_name == "remember_user_details":
                key, value = arguments.get("key"), arguments.get("value")
                self.memory.set_long_term_key(remote_jid, key, value)
                session = self._get_session(remote_jid)
                session["intelligence"][key] = value
                self.console.print(f"[blue]ℹ[/blue] Remembered: {key} = {value}")
                return {"status": "remembered", "key": key}

            elif function_name == "list_stickers":
                stickers = self.list_stickers(arguments.get("vibe"), remote_jid=remote_jid)
                compact = [{"description": s["description"], "path": s["path"]} for s in stickers[:5]]
                return {"status": "success", "count": len(stickers), "stickers": compact}

            elif function_name == "recall_memory":
                query = arguments.get("query", "")
                lt = self.memory.format_long_term_context(remote_jid)
                ep = self.memory.format_episodic_context(remote_jid, query)
                return {"status": "success", "long_term": lt, "episodic": ep}

        except Exception as e:
            self.console.print(f"[red]✗ Tool ({function_name}): {e}[/red]")
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Entry Points
    # ──────────────────────────────────────────────────────────────────────────

    def start_services(self):
        self.wa_bridge.start()

    async def run_interactive(self):
        self.loop = asyncio.get_running_loop()
        from prompt_toolkit import PromptSession
        from prompt_toolkit.styles import Style
        from prompt_toolkit.formatted_text import HTML
        from rich.panel import Panel

        self.start_services()
        style = Style.from_dict({"bottom-toolbar": "#F6C453 bg:#2B2F36", "prompt": "#F6C453 bold"})

        def get_toolbar():
            wa = self.status.get("whatsapp", "disconnected")
            c = "green" if wa == "open" else "yellow" if wa == "pairing" else "red"
            jid = self.active_session.get("target", "none")
            ep_count = self.db.get_episode_count(jid) if jid != "none" else 0
            return HTML(
                f'<style color="#7B7F87"> Status: </style>'
                f'<style color="white" bg="#3C414B"> OpenAI: Ready </style> '
                f'<style color="white" bg="{c}"> WA: {wa.capitalize()} </style> '
                f'<style color="white" bg="#3C414B"> 💾 Episodes: {ep_count} </style>'
            )

        sess = PromptSession(style=style, bottom_toolbar=get_toolbar)
        self.console.print(Panel(
            Markdown("# 🤖 ORBIT AI — Full Media + Three-Tier Memory"),
            subtitle="[dim]Analyze → Route → Plan → Localize → MediaRespond → Execute → Remember[/dim]",
            border_style="#F6C453",
        ))

        while True:
            try:
                user_input = await sess.prompt_async("You > ")
                if not user_input.strip(): continue
                if user_input.strip() == "/clear": self.console.clear(); continue
                if user_input.strip() == "/exit": break
                if user_input.strip() == "/drafts":
                    drafts = self.db.get_pending_drafts()
                    self.console.print(f"[bold]Pending drafts: {len(drafts)}[/bold]")
                    for d in drafts:
                        self.console.print(f"  #{d['id']} [{d['remote_jid']}]: {d['reply_text'][:60]}")
                    continue
                if user_input.strip() == "/memory":
                    jid = self.active_session.get("target", "")
                    if jid:
                        lt = self.memory.format_long_term_context(jid)
                        ep = self.memory.format_episodic_context(jid, "")
                        self.console.print(lt or "[No long-term memory]")
                        self.console.print(ep or "[No episodic memory]")
                    continue

                self.console.print("\n[bold #F6C453]Orbit[/bold #F6C453]")
                async for result in self.chat(user_input, remote_jid=self.active_session.get("target")):
                    if result["type"] == "content":
                        self.console.print(result["data"]["text"], end="")
                    elif result["type"] == "tool_executed":
                        t = result["data"]
                        self.console.print(f"\n[dim][{t['function']}] → {t['result'].get('status')}[/dim]")
                self.console.print("\n")

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.console.print(f"\n[red]❌ {e}[/red]")

        self.wa_bridge.stop()

    async def run_headless(self):
        self.loop = asyncio.get_running_loop()
        self.console.print("[bold #F6C453]🚀 ORBIT AI — HEADLESS | Full Media + Memory[/bold #F6C453]")
        self.console.print("[dim]Pipeline: Analyze → Route → Plan → Localize → MediaRespond → Execute → Remember[/dim]\n")
        self.start_services()
        while True:
            await asyncio.sleep(3600)