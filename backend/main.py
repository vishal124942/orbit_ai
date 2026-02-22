"""
Orbit AI — Multi-Tenant FastAPI Backend (v2)
===============================================
- PostgreSQL (asyncpg) for platform data
- /api/whatsapp/pairing-code polling endpoint
- WebSocket as bonus real-time channel
- No media file persistence (EphemeralMediaProcessor)
- Daily maintenance: TTS cache cleanup + media description pruning
"""

import os
import yaml
import logging
import asyncio
from typing import Dict, List, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from backend.pg_models import PlatformDB, close_pool
from backend.session_manager import SessionManager
from backend.auth import verify_google_token, create_session_token, decode_session_token, extract_token_from_header
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Header, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from backend.pg_models import PlatformDB, init_schema, close_pool
from backend.auth import verify_google_token, create_session_token, decode_session_token, extract_token_from_header
from backend.session_manager import SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

platform_db: Optional[PlatformDB] = None
session_manager: Optional[SessionManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global platform_db, session_manager
    config_path = os.getenv("CONFIG_PATH", "agent_config.yaml")
    try:
        with open(config_path) as f:
            base_config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        base_config = {}

    platform_db = PlatformDB()
    await platform_db.ensure_init()
    logger.info("✅ PostgreSQL connected")

    session_manager = SessionManager(platform_db, base_config)
    await session_manager.restore_active_sessions()
    asyncio.create_task(_maintenance_loop())
    logger.info("🚀 Orbit AI Backend ready")
    yield
    await close_pool()


app = FastAPI(title="Orbit AI", version="3.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


async def _maintenance_loop():
    from backend.src.core.ephemerial_media_processor import prune_tts_cache
    while True:
        await asyncio.sleep(86400)
        try:
            await platform_db.prune_old_media_cache(keep_days=30)
            users_dir = "data/users"
            if os.path.exists(users_dir):
                for uid in os.listdir(users_dir):
                    upath = os.path.join(users_dir, uid)
                    if os.path.isdir(upath):
                        await prune_tts_cache(upath, max_age_days=7, max_size_mb=50)
            logger.info("[Maintenance] Cache cleanup done")
        except Exception as e:
            logger.error(f"[Maintenance] {e}")


async def get_current_user(authorization: Optional[str] = Header(None)) -> Dict:
    token = extract_token_from_header(authorization or "")
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = await platform_db.get_user(payload["sub"])
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── Pydantic models ───────────────────────────────────────────────────────────

class GoogleAuthRequest(BaseModel):
    credential: str

class AllowlistUpdate(BaseModel):
    allowed_jids: List[str]

class ContactToneUpdate(BaseModel):
    contact_jid: str
    custom_tone: Optional[str] = None
    custom_language: Optional[str] = None
    soul_content: Optional[str] = None

class AgentSettingsUpdate(BaseModel):
    soul_override: Optional[str] = None
    debounce_seconds: Optional[int] = None
    auto_respond: Optional[bool] = None
    tts_enabled: Optional[bool] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    handoff_intents: Optional[List[str]] = None

class ContactSoulRequest(BaseModel):
    contact_jid: str


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/auth/google")
async def google_auth(req: GoogleAuthRequest):
    user_info = verify_google_token(req.credential)
    if not user_info:
        raise HTTPException(status_code=401, detail="Invalid Google credential")
    if not user_info.get("email_verified"):
        raise HTTPException(status_code=401, detail="Email not verified")

    user = await platform_db.upsert_user(
        user_info["sub"], user_info["email"],
        user_info.get("name", ""), user_info.get("picture", "")
    )
    token_data = create_session_token(user["id"])
    return {
        "user": {"id": user["id"], "email": user["email"],
                 "name": user["name"], "avatar_url": user["avatar_url"]},
        **token_data,
    }


# ── User ──────────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def get_me(user: Dict = Depends(get_current_user)):
    wa = await platform_db.get_wa_session(user["id"]) or {}
    settings = await platform_db.get_agent_settings(user["id"]) or {}
    
    # Check if agent is remotely paused
    data_dir = os.path.expanduser(f'~/.ai-agent-system/data/users/{user["id"]}')
    pause_file = os.path.join(data_dir, "paused.lock")
    is_paused = os.path.exists(pause_file)

    return {
        "user": user,
        "whatsapp": {
            "status": wa.get("status", "disconnected"),
            "wa_jid": wa.get("wa_jid"),
            "wa_name": wa.get("wa_name"),
            "wa_number": wa.get("wa_number"),
            "agent_running": bool(wa.get("agent_running", False)) and not is_paused,
            "is_paused": is_paused
        },
        "settings": settings,
    }


# ── WhatsApp ──────────────────────────────────────────────────────────────────

@app.get("/api/whatsapp/status")
async def wa_status(user: Dict = Depends(get_current_user)):
    return await session_manager.get_session_status(user["id"])


class StartWaRequest(BaseModel):
    phone_number: Optional[str] = None


@app.get("/api/whatsapp/pairing-code")
async def wa_get_pairing_code(user: Dict = Depends(get_current_user)):
    """
    Primary Pairing Code delivery via REST polling.
    Frontend polls this every 2s when status == 'pairing'.
    """
    status = await session_manager.get_session_status(user["id"])
    return {
        "status": status["status"],
        "pairing_code": status.get("pairing_code"),
        "has_pairing_code": bool(status.get("pairing_code")),
    }


@app.post("/api/whatsapp/start")
async def wa_start(req: StartWaRequest = Body(default=None), user: Dict = Depends(get_current_user)):
    status = await session_manager.get_session_status(user["id"])
    if status["status"] == "connected" and status["is_running"]:
        return {"message": "Agent already running", **status}
    
    phone_number = req.phone_number if req else None
    
    # Enforce 1 Account = 1 Phone Rule
    if phone_number:
        wa_session = await platform_db.get_wa_session(user["id"])
        if wa_session and wa_session.get("wa_number"):
            saved_number = wa_session["wa_number"]
            clean_phone = ''.join(filter(str.isdigit, phone_number))
            # Some WA sessions save the number with a device ID suffix like: 917310885365:53
            clean_saved = ''.join(filter(str.isdigit, saved_number.split(':')[0]))
            
            if clean_phone and clean_saved and clean_phone != clean_saved:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Account is already bound to +{clean_saved}. Please use the same number or create a new Orbit AI account for a different number."
                )

    await session_manager.start_pairing(user["id"], phone_number=phone_number)
    return {"message": "Agent starting. Poll /api/whatsapp/pairing-code for pairing code.", "status": "starting"}


@app.post("/api/whatsapp/stop")
async def wa_stop(user: Dict = Depends(get_current_user)):
    await session_manager.stop_agent(user["id"])
    return {"message": "Agent stopped", "status": "disconnected"}


@app.post("/api/whatsapp/regenerate")
async def wa_regenerate(user: Dict = Depends(get_current_user)):
    """
    Forcefully stops the current agent pairing session and spins it up again 
    to fetch a brand new 8-character code.
    """
    wa_session = await platform_db.get_wa_session(user["id"])
    phone_number = wa_session.get("wa_number") if wa_session else None
    
    if not phone_number:
        raise HTTPException(
            status_code=400, 
            detail="No phone number bound to this account. Cannot regenerate pairing code."
        )

    # Clean the phone number of any WhatsApp multi-device suffixes (e.g., :53)
    # Baileys requestPairingCode explicitly requires purely the digits.
    clean_phone = phone_number.split(':')[0]

    # 1. Kill the stale background WhatsApp process
    await session_manager.stop_agent(user["id"])
    
    # 2. Add a tiny debounce to ensure SQLite ports and files are un-locked
    import asyncio
    await asyncio.sleep(1.0)
    
    # 3. Spin it back up with the clean phone string
    await session_manager.start_pairing(user["id"], phone_number=clean_phone)
    return {"message": "Agent restarting to generate fresh pairing code.", "status": "pairing"}


# ── Contacts ──────────────────────────────────────────────────────────────────

@app.get("/api/contacts")
async def get_contacts(
    search: Optional[str] = Query(None),
    is_group: Optional[bool] = Query(None),
    user: Dict = Depends(get_current_user),
):
    contacts = await platform_db.get_contacts(user["id"], search=search, is_group=is_group)
    return {"contacts": contacts, "total": len(contacts)}


@app.get("/api/contacts/top")
async def get_top_contacts(
    n: int = Query(100, ge=1, le=200),
    user: Dict = Depends(get_current_user),
):
    """
    Return top N most active REAL contacts (with phone numbers, not @lid or groups)
    ranked by message count from the local conversation database.
    """
    s = session_manager.sessions.get(user["id"])
    
    # Get all non-group contacts from platform DB
    all_contacts = await platform_db.get_contacts(user["id"], is_group=False)
    real_contacts = [
        c for c in all_contacts
        if (c.get("jid", "").endswith("@s.whatsapp.net") or c.get("jid", "").endswith("@lid")) and c.get("name")
    ]
    
    # If agent is running, rank by actual message count from SQLite
    if s and s.controller:
        db_path = os.path.join(s.controller.data_dir, "agent.db")
        if os.path.exists(db_path):
            from backend.src.core.database import Database
            import sqlite3
            db_conn = sqlite3.connect(db_path)
            db_conn.row_factory = sqlite3.Row
            try:
                rows = db_conn.execute("""
                    SELECT remote_jid, COUNT(*) as msg_count,
                           MAX(timestamp) as last_msg
                    FROM messages
                    WHERE remote_jid NOT LIKE '%@g.us'
                      AND remote_jid NOT LIKE '%broadcast%'
                    GROUP BY remote_jid
                    ORDER BY last_msg DESC
                    LIMIT 100
                """).fetchall()
                
                # Build a lookup of jid → {count, last_msg}
                msg_stats = {r["remote_jid"]: {
                    "msg_count": r["msg_count"],
                    "last_msg": r["last_msg"],
                    "has_history": r["msg_count"] >= 5,
                } for r in rows}
                
                # Enrich contacts with message stats
                for c in real_contacts:
                    jid = c["jid"]
                    stats = msg_stats.get(jid, {"msg_count": 0, "last_msg": None, "has_history": False})
                    c.update(stats)
                
                # Sort by most recently contacted (safe fallback for None)
                real_contacts.sort(key=lambda x: str(x.get("last_msg") or ""), reverse=True)
            finally:
                db_conn.close()
    
    top = real_contacts[:n]
    return {"contacts": top, "total": len(top), "total_contacts": len(all_contacts)}


@app.post("/api/contacts/sync")
async def sync_contacts(user: Dict = Depends(get_current_user)):
    s = session_manager.sessions.get(user["id"])
    if not s or not s.controller:
        raise HTTPException(status_code=400, detail="Agent not running")
    asyncio.create_task(s.controller._sync_contacts())
    return {"message": "Contact sync triggered"}



# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(user: Dict = Depends(get_current_user)):
    settings = await platform_db.get_agent_settings(user["id"])
    allowed_jids = await platform_db.get_allowed_jids(user["id"])
    return {"settings": settings, "allowed_jids": allowed_jids}


@app.post("/api/settings/allowlist")
async def update_allowlist(req: AllowlistUpdate, user: Dict = Depends(get_current_user)):
    await platform_db.bulk_update_allowlist(user["id"], req.allowed_jids)
    session_manager.update_allowed_jids(user["id"], req.allowed_jids)
    return {"message": f"Allowlist updated: {len(req.allowed_jids)} contacts", "count": len(req.allowed_jids)}


@app.post("/api/settings/contact-tone")
async def update_contact_tone(req: ContactToneUpdate, user: Dict = Depends(get_current_user)):
    # Preserve existing is_allowed state — only update tone/language, not allowlist status
    # This prevents saving a tone from re-adding a blocked contact to the allowlist
    pool_conn = await platform_db._pool()
    async with pool_conn.acquire() as conn:
        await conn.execute("""
            INSERT INTO contact_settings (user_id, contact_jid, is_allowed, custom_tone, custom_language)
            VALUES ($1, $2, FALSE, $3, $4)
            ON CONFLICT (user_id, contact_jid) DO UPDATE SET
                custom_tone = COALESCE(EXCLUDED.custom_tone, contact_settings.custom_tone),
                custom_language = COALESCE(EXCLUDED.custom_language, contact_settings.custom_language),
                updated_at = NOW()
        """, user["id"], req.contact_jid, req.custom_tone, req.custom_language)

    if req.soul_content:
        s = session_manager.sessions.get(user["id"])
        if s and s.controller:
            s.controller.update_contact_soul(req.contact_jid, req.soul_content)

    # Push tone/language live to running agent (so next reply uses the new tone immediately)
    if req.custom_tone is not None or req.custom_language is not None:
        s = session_manager.sessions.get(user["id"])
        if s and s.controller and req.custom_tone:
            s.controller.update_contact_tone_live(req.contact_jid, req.custom_tone)

    return {"message": f"Contact settings updated for {req.contact_jid}"}


@app.post("/api/settings/contact-soul/generate")
async def generate_contact_soul(req: ContactSoulRequest, user: Dict = Depends(get_current_user)):
    s = session_manager.sessions.get(user["id"])
    if not s or not s.controller:
        raise HTTPException(status_code=400, detail="Agent must be running")

    data_dir = s.controller.data_dir
    db_path = os.path.join(data_dir, "agent.db")

    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="No conversation database found. Send some messages first.")

    from backend.src.core.database import Database
    from openai import OpenAI

    db = Database(db_path=db_path)

    # Try the provided JID first, then also try converting @lid to @s.whatsapp.net
    contact_jid = req.contact_jid
    found_jid = contact_jid  # Track which JID actually matched
    messages = db.get_messages(remote_jid=contact_jid, limit=50)

    # If no messages found and it's a @lid JID, the messages might be stored under @s.whatsapp.net
    if (not messages or len(messages) < 5) and contact_jid.endswith("@lid"):
        lid_number = contact_jid.split("@")[0]
        if lid_number.isdigit():
            alt_jid = f"{lid_number}@s.whatsapp.net"
            alt_messages = db.get_messages(remote_jid=alt_jid, limit=50)
            if alt_messages and len(alt_messages) >= 5:
                messages = alt_messages
                found_jid = alt_jid

    # Also try @s.whatsapp.net → @lid conversion
    if (not messages or len(messages) < 5) and contact_jid.endswith("@s.whatsapp.net"):
        phone = contact_jid.split("@")[0]
        all_msgs = db.get_messages(limit=500)
        if all_msgs:
            matched = [m for m in all_msgs if phone in (m["remote_jid"] or "")]
            if matched and len(matched) >= 5:
                messages = matched
                found_jid = matched[0]["remote_jid"]

    if not messages or len(messages) < 5:
        raise HTTPException(
            status_code=404,
            detail=f"Need at least 5 messages with this contact. Currently have {len(messages) if messages else 0}. Chat with them first!"
        )

    history = "\n".join([
        f"{'Agent' if m['from_me'] else 'Contact'}: {m['text']}"
        for m in reversed(list(messages))
        if m["text"] and not m["text"].startswith("[")
    ])

    if not history.strip():
        raise HTTPException(status_code=404, detail="No text messages found with this contact. Only text chats can be analyzed.")

    # Use Sarvam AI if available, else fallback to OpenAI
    sarvam_key = os.getenv("SARVAM_API_KEY")
    from openai import OpenAI as _OAI
    if sarvam_key:
        llm = _OAI(api_key=sarvam_key, base_url="https://api.sarvam.ai/v1")
        llm_model = "sarvam-m"
    else:
        llm = _OAI(api_key=os.getenv("OPENAI_API_KEY"))
        llm_model = "gpt-4o"

    PROMPT = """Analyze this WhatsApp conversation and write a concise soul.md-style personality profile for how the AI agent should behave with THIS contact.

Include: communication style, language mix (Hindi%/English%), tone tolerance (gaali level), topics they discuss, response preferences (brief/detailed, voice/sticker/text), and any memorable facts.
Be specific and actionable. Under 250 words. soul.md markdown format."""

    try:
        resp = llm.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": f"Chat history:\n\n{history[-4000:]}"},
            ],
            max_tokens=600, temperature=0.7,
        )
        soul = resp.choices[0].message.content.strip()
        # Strip any markdown fences Sarvam might add
        if soul.startswith("```"):
            soul = soul.split("```")[1].strip()
            if soul.startswith("markdown") or soul.startswith("md"):
                soul = "\n".join(soul.split("\n")[1:])

        s.controller.update_contact_soul(req.contact_jid, soul)
        # DO NOT force is_allowed=True here — preserve the user's allowlist choice

        # Prune old messages — keep only latest 200 for fresh future profiles
        db.prune_messages(found_jid, keep=200)

        return {"soul_content": soul, "contact_jid": req.contact_jid, "message": "Profile generated"}
    except Exception as e:
        logger.error(f"[SoulGen] Error: {e}")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")



# ── Analytics ─────────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def get_analytics(user: Dict = Depends(get_current_user)):
    s = session_manager.sessions.get(user["id"])
    wa = await platform_db.get_wa_session(user["id"]) or {}
    contacts = await platform_db.get_contacts(user["id"])
    allowed = await platform_db.get_allowed_jids(user["id"])

    total_messages = 0
    if s and s.controller:
        db_path = os.path.join(s.controller.data_dir, "agent.db")
        if os.path.exists(db_path):
            from backend.src.core.database import Database
            db = Database(db_path=db_path)
            msgs = db.get_messages(limit=9999)
            total_messages = len(msgs) if msgs else 0

    return {
        "agent_status": wa.get("status", "disconnected"),
        "agent_running": bool(wa.get("agent_running", False)),
        "total_contacts": len(contacts),
        "allowed_contacts": len(allowed),
        "total_messages": total_messages,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    session_manager.add_ws_client(user_id, websocket)

    # Send current state immediately
    status = await session_manager.get_session_status(user_id)
    await websocket.send_json({"type": "status", **status})
    if status.get("pairing_code"):
        await websocket.send_json({"type": "pairing_code", "data": status["pairing_code"]})

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=45)
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
                elif data == "status":
                    s = await session_manager.get_session_status(user_id)
                    await websocket.send_json({"type": "status", **s})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        session_manager.remove_ws_client(user_id, websocket)


@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(session_manager.sessions) if session_manager else 0}


# ── SPA frontend ──────────────────────────────────────────────────────────────

frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(_full_path: str):
        with open(os.path.join(frontend_dist, "index.html")) as f:
            return HTMLResponse(f.read())