console.error("🚀 DEBUG: GATEWAY VERSION 3.0 STARTING");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
    isJidGroup,
    downloadMediaMessage
} = require('@whiskeysockets/baileys');
const { writeFile } = require('fs/promises');
const path = require('path');
const fs = require('fs');
const { Boom } = require('@hapi/boom');
const P = require('pino');
const os = require('os');
const crypto = require('crypto');
const util = require('util');

// Configuration
const AUTH_DIR = process.argv[2] || path.join(os.homedir(), '.ai-agent-system', 'credentials', 'whatsapp', 'default');
const phoneNumber = process.argv[3] && process.argv[3] !== '--clear-state' ? process.argv[3].replace(/[^0-9]/g, '') : null;
const isClearStateCmd = process.argv.includes('--clear-state');

const MEDIA_DIR = path.join(__dirname, '../../../data/media');
const logger = P({ level: 'silent' });

// ── CLEAR STATE COMMAND (CLI UTILITY) ───────────────────────────────────────
if (isClearStateCmd) {
    console.error(`[Gateway Utility] Executing --clear-state for ${AUTH_DIR}`);
    const sessionName = process.env.WHATSAPP_SESSION_ID || path.basename(AUTH_DIR);

    (async () => {
        try {
            if (process.env.R2_BUCKET_NAME) {
                console.error(`[Gateway Utility] Wiping Cloudflare R2 Session: ${sessionName}`);
                const { useR2AuthState } = require('./r2_auth_state');
                const auth = await useR2AuthState(sessionName);
                if (auth.clearState) await auth.clearState();
            } else {
                console.error(`[Gateway Utility] Wiping Local Directory: ${AUTH_DIR}`);
                if (fs.existsSync(AUTH_DIR)) {
                    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
                }
            }
            console.error(`[Gateway Utility] Cleanup successful. Exiting.`);
            process.exit(0);
        } catch (err) {
            console.error(`[Gateway Utility] Cleanup failed:`, err);
            process.exit(1);
        }
    })();
    return;
}
// ────────────────────────────────────────────────────────────────────────────

console.error(`[Gateway V3.0] Using AUTH_DIR: ${AUTH_DIR}`);
if (phoneNumber) console.error(`[Gateway V3.0] Using Pairing Code for phone: ${phoneNumber}`);

function requestRestartAndExit(reason, delay = 500) {
    console.error(`[Gateway] Requesting restart from Python: ${reason}`);

    // KILL the socket immediately so the next session doesn't hit a 440 Conflict
    if (sock) {
        try {
            console.error('[Gateway] Closing socket before restart...');
            sock.ev.removeAllListeners();
            sock.end();
            sock.ws.close();
        } catch (e) {
            console.error(`[Gateway] Error closing socket: ${e.message}`);
        }
    }

    console.log(JSON.stringify({ type: 'restart_requested', reason }));
    setTimeout(() => {
        console.error('[Gateway] Exiting now.');
        process.exit(0);
    }, delay);
}

// Connection state management
let sock = null;
let isConnected = false;
let messageQueue = [];
let isProcessingQueue = false;
let contacts = {};

// ── Contact flush strategy ───────────────────────────────────────────────────
//
// ROOT CAUSES OF "8-10 CONTACTS ONLY" BUG:
//
// CAUSE 1 (primary, Node-side): contacts.upsert on a linked device ONLY fires
//   for contacts who appear in the device's recent chat history, NOT the full
//   phone address book.  WhatsApp's multi-device protocol only pushes the
//   address book to the primary phone; linked devices get a subset derived from
//   active conversations.
//
//   FIX: Mine ALL unique JIDs from messaging-history.set messages and inject
//   them into the contacts map with their pushName as a stub entry.  This
//   ensures every JID that has ever sent or received a message is captured as
//   a contact even if WhatsApp never fired contacts.upsert for them.
//
// CAUSE 2: Debounce cancellation race (documented in V2).
// CAUSE 3: Python-side silent failures (fixed in session_manager + pg_models).
// ─────────────────────────────────────────────────────────────────────────────

let contactsDebounceTimer = null;
let periodicSyncTimer = null;

/**
 * Determine if a JID is a real individual contact (not group/broadcast/status).
 */
function isIndividualJid(jid) {
    if (!jid) return false;
    if (jid.includes('broadcast') || jid.includes('status@') || jid.includes('newsletter')) return false;
    if (isJidGroup(jid)) return false;
    return jid.endsWith('@s.whatsapp.net') || jid.endsWith('@lid');
}

/** Serialise and emit the full contacts map on stdout — never throws. */
function sendContactsNow() {
    try {
        const raw = Object.values(contacts);
        const cleaned = raw.filter(c => isIndividualJid(c.id)).map(c => {
            const isLid = c.id.endsWith('@lid');
            const lidRaw = isLid ? c.id.split('@')[0] : null;
            return {
                id: c.id,
                name: c.name || c.notify || c.pushName || null,
                notify: c.notify || c.pushName || null,
                pushName: c.pushName || null,
                isLid,
                lidId: lidRaw,
            };
        });
        console.error(`[Gateway] Sending ${cleaned.length} contacts (filtered from ${raw.length} raw)`);
        cleaned.slice(0, 5).forEach((c, i) =>
            console.error(`[Gateway]   Sample[${i}]: id=${c.id}, name=${c.name || 'NONE'}, isLid=${c.isLid}`)
        );
        console.log(JSON.stringify({ type: 'contacts', data: cleaned }));
    } catch (err) {
        console.error('[Gateway] sendContactsNow error:', err.message);
    }
}

/**
 * Flush NOW and arm a 1 s trailing debounce.
 */
function flushContactsWithTrailingDebounce() {
    sendContactsNow();
    if (contactsDebounceTimer) clearTimeout(contactsDebounceTimer);
    contactsDebounceTimer = setTimeout(() => {
        contactsDebounceTimer = null;
        sendContactsNow();
    }, 1000);
}

function startPeriodicContactSync() {
    stopPeriodicContactSync();
    periodicSyncTimer = setInterval(() => {
        if (isConnected) {
            console.error('[Gateway] Periodic contact sync');
            sendContactsNow();
        }
    }, 5 * 60 * 1000);
}

function stopPeriodicContactSync() {
    if (periodicSyncTimer) { clearInterval(periodicSyncTimer); periodicSyncTimer = null; }
}

/**
 * FIX (CAUSE 1): Mine JIDs from message history and inject them as stub contacts.
 *
 * WHY THIS MATTERS:
 *   WhatsApp's multi-device protocol sends contacts.upsert only for peers who
 *   appear in the server-side "contact book" that was synced to the linked device.
 *   This is typically the last ~30-50 conversations.  Everyone else is invisible.
 *
 *   However, messaging-history.set contains every conversation ever synced.
 *   By walking those messages and inserting any unseen JID into the contacts map
 *   (with pushName as the display label), we capture the full set of people the
 *   user has actually talked to — which is the most useful definition of "contacts"
 *   for the allowlist feature anyway.
 *
 * SAFETY:
 *   We never overwrite an existing entry that already has a real name.  The stub
 *   will be upgraded later if contacts.update fires a proper name for it.
 */
function mineContactsFromMessages(messages) {
    let newCount = 0;
    for (const msg of messages) {
        const jid = msg.key?.remoteJid;
        if (!jid || !isIndividualJid(jid)) continue;

        if (!contacts[jid]) {
            // Stub — will be enriched by contacts.upsert/update later
            contacts[jid] = {
                id: jid,
                notify: msg.pushName || null,
                pushName: msg.pushName || null,
            };
            newCount++;
        } else {
            // Upgrade stub with pushName if we don't yet have any name
            if (!contacts[jid].name && !contacts[jid].notify && msg.pushName) {
                contacts[jid].notify = msg.pushName;
            }
        }

        // Also capture the sender's own JID for group messages where fromMe=false
        // (the real sender is in participant, not remoteJid for groups — skip groups above)
    }
    if (newCount > 0) {
        console.error(`[Gateway] mineContactsFromMessages: added ${newCount} new stubs (Total: ${Object.keys(contacts).length})`);
    }
}

// Ensure directories exist
[MEDIA_DIR].forEach(dir => {
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
});

function getFileHash(buffer) {
    return crypto.createHash('sha256').update(buffer).digest('hex');
}

function findExistingMedia(hash) {
    const files = fs.readdirSync(MEDIA_DIR);
    const match = files.find(f => f.startsWith(hash));
    return match ? path.join(MEDIA_DIR, match) : null;
}

async function processMessageQueue() {
    if (isProcessingQueue || messageQueue.length === 0) return;
    isProcessingQueue = true;
    while (messageQueue.length > 0) {
        const command = messageQueue.shift();
        try {
            await executeCommand(command);
        } catch (err) {
            console.log(JSON.stringify({ type: 'error', id: command.id, message: err.message }));
        }
        await new Promise(r => setTimeout(r, 300));
    }
    isProcessingQueue = false;
}

async function executeCommand(command) {
    const target = formatJid(command.to);
    if (!isConnected || !sock || !sock.user) throw new Error('Connection not ready');
    try {
        await sock.presenceSubscribe(target);
        await sock.sendPresenceUpdate('composing', target);
    } catch (e) { }

    let retries = 3;
    let lastError = null;
    while (retries > 0) {
        try {
            if (command.type === 'send_message') await sendMessage(target, command);
            else if (command.type === 'react') await sendReaction(target, command);
            else if (command.type === 'delete_message') await deleteMessage(target, command);
            else if (command.type === 'get_contacts') sendContactsNow();

            console.log(JSON.stringify({ type: 'ack', id: command.id, success: true }));
            try { await sock.sendPresenceUpdate('paused', target); } catch (e) { }
            return;
        } catch (err) {
            lastError = err;
            const isTransient = err.output?.statusCode === 428 ||
                err.message.includes('Connection Closed') || err.message.includes('timed out');
            if (isTransient && retries > 1) { retries--; await new Promise(r => setTimeout(r, 2000)); continue; }
            throw err;
        }
    }
    if (lastError) throw lastError;
}

async function sendMessage(target, command) {
    let options = {};
    if (command.media) {
        const mediaPath = command.media;
        const isUrl = mediaPath.startsWith('http');
        const ext = path.extname(mediaPath).toLowerCase();
        if (ext === '.webp' || command.mediaType === 'sticker')
            options = { sticker: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath) };
        else if (ext === '.mp4' || ext === '.mkv' || ext === '.avi' || command.mediaType === 'video')
            options = { video: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath), caption: command.text || '' };
        else if (ext === '.ogg' || ext === '.mp3' || ext === '.m4a' || ext === '.opus' || command.mediaType === 'audio')
            options = { audio: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath), mimetype: 'audio/mp4', ptt: true };
        else
            options = { image: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath), caption: command.text || '' };
    } else if (command.text) {
        options = { text: command.text };
    } else {
        throw new Error('No text or media provided');
    }
    await sock.sendMessage(target, options);
}

async function sendReaction(target, command) {
    await sock.sendMessage(target, {
        react: { text: command.emoji, key: { remoteJid: target, id: command.messageId, fromMe: false } }
    });
}

async function deleteMessage(target, command) {
    await sock.sendMessage(target, {
        delete: { remoteJid: target, fromMe: true, id: command.messageId }
    });
}

function formatJid(jid) {
    if (!jid) return jid;
    if (jid.includes('@')) return jid;
    return `${jid.replace(/\D/g, '')}@s.whatsapp.net`;
}

async function downloadAndSaveMedia(msg, messageType) {
    try {
        const buffer = await downloadMediaMessage(msg, 'buffer', {});
        const hash = getFileHash(buffer);
        const mediaTypeMap = { audio: 'ogg', video: 'mp4', image: 'jpg', sticker: 'webp' };
        const mediaType = messageType.replace('Message', '');
        const extension = mediaTypeMap[mediaType] || 'bin';

        let mediaPath = findExistingMedia(hash);
        if (!mediaPath) {
            const fileName = `${hash}.${extension}`;
            mediaPath = path.join(MEDIA_DIR, fileName);
            await writeFile(mediaPath, buffer);
            console.error(`[Media] Saved new ${mediaType}: ${fileName}`);
        } else {
            console.error(`[Media] Using existing ${mediaType}: ${path.basename(mediaPath)}`);
        }
        return { mediaPath, mediaType };
    } catch (err) {
        console.error("[Media Error] Download failed:", err.message);
        return { mediaPath: null, mediaType: null };
    }
}

const { useR2AuthState } = require('./r2_auth_state');

async function startGateway() {
    console.log(JSON.stringify({ type: 'system', message: 'Gateway starting...' }));

    let state, saveCreds, clearStateFunc;
    const sessionName = process.env.WHATSAPP_SESSION_ID || path.basename(AUTH_DIR);

    if (process.env.R2_BUCKET_NAME) {
        console.error(`[Gateway] Using Cloudflare R2 for Auth State (Session: ${sessionName})`);
        const r2Auth = await useR2AuthState(sessionName);
        state = r2Auth.state; saveCreds = r2Auth.saveCreds; clearStateFunc = r2Auth.clearState;
    } else {
        console.error(`[Gateway] Using Local File System for Auth State (Dir: ${AUTH_DIR})`);
        const localAuth = await useMultiFileAuthState(AUTH_DIR);
        state = localAuth.state; saveCreds = localAuth.saveCreds;
        clearStateFunc = () => {
            const files = fs.readdirSync(AUTH_DIR);
            files.forEach(f => fs.unlinkSync(path.join(AUTH_DIR, f)));
            console.error(`[Gateway] Cleared ${files.length} stale auth files from ${AUTH_DIR}`);
        };
    }

    if (phoneNumber && !state.creds.registered) {
        const hasStalePartialCreds = !!(
            state.creds.me || state.creds.account ||
            state.creds.signedPreKey || state.creds.registrationId
        );
        if (hasStalePartialCreds) {
            console.error('[Gateway] Pairing mode: stale partial creds detected — wiping...');
            try {
                await clearStateFunc();
                if (process.env.R2_BUCKET_NAME) {
                    const { useR2AuthState: useR2Fresh } = require('./r2_auth_state');
                    const fresh = await useR2Fresh(sessionName);
                    state = fresh.state; saveCreds = fresh.saveCreds; clearStateFunc = fresh.clearState;
                } else {
                    const { useMultiFileAuthState: useLocalFresh } = require('@whiskeysockets/baileys');
                    const fresh = await useLocalFresh(AUTH_DIR);
                    state = fresh.state; saveCreds = fresh.saveCreds;
                }
                console.error('[Gateway] Stale creds cleared.');
            } catch (wipeErr) {
                console.error('[Gateway] Warning: could not wipe stale creds:', wipeErr.message);
            }
        } else {
            console.error('[Gateway] Pairing mode: no stale creds found.');
        }
    }

    let version;
    try {
        const v = await fetchLatestBaileysVersion().catch(() => ({ version: [2, 3000, 1015901307] }));
        version = v.version;
    } catch (e) {
        version = [2, 3000, 1015901307];
    }

    sock = makeWASocket({
        version,
        auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
        logger,
        browser: phoneNumber ? ["Mac OS", "Chrome", "121.0.0.0"] : ["Orbit AI", "Desktop", "1.0.0"],
        printQRInTerminal: false,
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 30000,
        retryRequestDelayMs: 500,
        // FIX: Keep false for performance, but we now mine contacts from history messages
        // so we get full coverage without the heavy full-history sync.
        syncFullHistory: false,
        markOnlineOnConnect: false,
    });

    let isPairingCodeRequested = false;
    sock.ev.on('creds.update', saveCreds);

    // ── contacts.upsert: server address book entries ──────────────────────────
    sock.ev.on('contacts.upsert', (newContacts) => {
        newContacts.forEach(c => {
            if (!c.id || c.id.includes('broadcast')) return;
            contacts[c.id] = { ...contacts[c.id], ...c };
        });
        console.error(`[Gateway] contacts.upsert: +${newContacts.length} (Total: ${Object.keys(contacts).length})`);
        flushContactsWithTrailingDebounce();
    });

    // ── contacts.update: name/notify changes ─────────────────────────────────
    sock.ev.on('contacts.update', (updates) => {
        updates.forEach(u => {
            if (!u.id || u.id.includes('broadcast')) return;
            if (contacts[u.id]) {
                const hasName = u.name || u.notify || u.pushName;
                if (!contacts[u.id].name || hasName) {
                    contacts[u.id] = { ...contacts[u.id], ...u };
                } else {
                    const { name, notify, pushName, ...rest } = u;
                    contacts[u.id] = { ...contacts[u.id], ...rest };
                }
            } else {
                contacts[u.id] = u;
            }
        });
        console.error(`[Gateway] contacts.update: ${updates.length} updates (Total: ${Object.keys(contacts).length})`);
        flushContactsWithTrailingDebounce();
    });

    // ── messaging-history.set: the richest contact source ────────────────────
    sock.ev.on('messaging-history.set', (history) => {
        // 1. Capture explicit contacts from history metadata
        const historyContacts = history.contacts || [];
        historyContacts.forEach(c => {
            if (!c.id || c.id.includes('broadcast')) return;
            contacts[c.id] = { ...contacts[c.id], ...c };
        });
        console.error(`[Gateway] History sync: ${historyContacts.length} explicit contacts (Total before mining: ${Object.keys(contacts).length})`);

        // 2. FIX (CAUSE 1): Mine ALL message senders/recipients as stub contacts.
        //    This is the primary fix for the "8-10 contacts only" bug.
        //    contacts.upsert on a linked device only covers recent chats (~30-50).
        //    Message history covers EVERY conversation ever synced to this device.
        const historyMessages = history.messages || [];
        mineContactsFromMessages(historyMessages);
        console.error(`[Gateway] After mining messages: ${Object.keys(contacts).length} total contacts`);

        // Flush immediately — data is now in stdout buffer before any timer can be cancelled
        flushContactsWithTrailingDebounce();

        // 3. Emit history messages for context
        const MAX_HISTORY_PER_CONTACT = 200;
        if (historyMessages.length > 0) {
            const byContact = {};
            for (const msg of historyMessages) {
                const jid = msg.key?.remoteJid;
                if (!jid || jid.includes('broadcast') || jid.includes('status@')) continue;
                const text = msg.message?.conversation ||
                    msg.message?.extendedTextMessage?.text ||
                    msg.message?.imageMessage?.caption ||
                    msg.message?.videoMessage?.caption || "";
                if (!text) continue;
                if (!byContact[jid]) byContact[jid] = [];
                byContact[jid].push({
                    id: msg.key.id, from: jid, pushName: msg.pushName || '',
                    text, fromMe: msg.key.fromMe || false, timestamp: msg.messageTimestamp,
                });
            }
            const capped = [];
            for (const [jid, msgs] of Object.entries(byContact)) {
                msgs.sort((a, b) => (Number(b.timestamp) || 0) - (Number(a.timestamp) || 0));
                capped.push(...msgs.slice(0, MAX_HISTORY_PER_CONTACT));
            }
            if (capped.length > 0) {
                console.error(`[Gateway] History sync: emitting ${capped.length} msgs (from ${historyMessages.length} raw)`);
                console.log(JSON.stringify({ type: 'history_messages', data: capped }));
            }
        }
    });

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect } = update;

        if (phoneNumber && !state.creds.registered && connection === 'connecting' && !isPairingCodeRequested) {
            isPairingCodeRequested = true;
            await new Promise(resolve => setTimeout(resolve, 2000));
            try {
                const cleanPhone = phoneNumber.replace(/[^0-9]/g, '');
                console.error(`[Gateway] Requesting pairing code for: ${cleanPhone}`);
                let code = await sock.requestPairingCode(cleanPhone);
                code = code?.match(/.{1,4}/g)?.join('-') || code;
                console.error(`[Gateway] Pairing code requested successfully: ${code}`);
                console.log(JSON.stringify({ type: 'pairing_code', code }));
            } catch (err) {
                isPairingCodeRequested = false;
                console.error("[Gateway] Pairing Code Request Failed:", err);
                requestRestartAndExit('pairing_code_request_failed');
            }
        }

        if (connection) {
            let emitStatus = connection;
            if (connection === 'close') {
                const statusCode = lastDisconnect?.error?.output?.statusCode || lastDisconnect?.error?.code;
                if (statusCode === 401 || statusCode !== DisconnectReason.loggedOut) emitStatus = 'pairing';
            }
            console.log(JSON.stringify({
                type: 'connection', status: emitStatus,
                user: connection === 'open' ? sock.user : undefined,
            }));

            if (connection === 'open') {
                isConnected = true;
                console.error(`[Gateway] Connection opened. Cached contacts: ${Object.keys(contacts).length}`);

                // Flush immediately — no debounce at open time
                sendContactsNow();
                flushContactsWithTrailingDebounce();

                startPeriodicContactSync();
                processMessageQueue();
            }

            if (connection === 'close') {
                isConnected = false;
                stopPeriodicContactSync();

                const error = lastDisconnect?.error;
                const statusCode = error?.output?.statusCode || error?.code || 'N/A';
                const reason = error?.message || 'Unknown';

                console.error(`[Gateway] Connection closed (Status: ${statusCode}), Error: ${reason}`);
                console.error(`[Gateway] Connection Update Object: ${util.inspect(update, { depth: null, colors: false })}`);
                if (error) console.error(`[Gateway] Full error context: ${util.inspect(error, { depth: null, colors: false })}`);

                const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
                if (shouldReconnect) {
                    const isConflict = reason && reason.toLowerCase().includes('conflict');
                    const isRestartRequired = statusCode === 515 || statusCode === '515';
                    const delay = isConflict || isRestartRequired ? 5000 : 500;
                    console.error(`[Gateway] Scheduling restart via Python in ${delay}ms`);
                    setTimeout(() => requestRestartAndExit(reason, 500), delay);
                } else {
                    console.error('[Gateway] Logged out (401). Clearing auth state...');
                    try {
                        if (clearStateFunc) { await clearStateFunc(); console.error('[Gateway] Cleared stale auth state.'); }
                    } catch (e) { console.error(`[Gateway] Auth cleanup error: ${e.message}`); }
                    contacts = {};
                    setTimeout(() => requestRestartAndExit('logged_out_401_cleared', 200), 10000);
                }
            }
        }
    });

    sock.ev.on('messages.upsert', async (m) => {
        if (m.type === 'append') {
            // FIX: Mine contacts from appended history messages too
            mineContactsFromMessages(m.messages);

            const MAX_PER_CONTACT = 200;
            const byContact = {};
            for (const msg of m.messages) {
                const jid = msg.key?.remoteJid;
                if (!jid || jid.includes('broadcast') || jid.includes('status@')) continue;
                const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text ||
                    msg.message?.imageMessage?.caption || msg.message?.videoMessage?.caption || "";
                if (!text) continue;
                if (!byContact[jid]) byContact[jid] = [];
                byContact[jid].push({
                    id: msg.key.id, from: jid, pushName: msg.pushName || '',
                    text, fromMe: msg.key.fromMe || false, timestamp: msg.messageTimestamp
                });
            }
            const capped = [];
            for (const [jid, msgs] of Object.entries(byContact)) {
                msgs.sort((a, b) => (Number(b.timestamp) || 0) - (Number(a.timestamp) || 0));
                capped.push(...msgs.slice(0, MAX_PER_CONTACT));
            }
            if (capped.length > 0) {
                console.error(`[Gateway] messages.upsert(append): ${capped.length} history msgs`);
                console.log(JSON.stringify({ type: 'history_messages', data: capped }));
            }
        }

        if (m.type === 'notify') {
            const now = Math.floor(Date.now() / 1000);
            for (const msg of m.messages) {
                const msgTime = Number(msg.messageTimestamp) || 0;
                if (now - msgTime > 60) continue;

                const messageType = Object.keys(msg.message || {})[0];
                let text = msg.message?.conversation || msg.message?.extendedTextMessage?.text ||
                    msg.message?.imageMessage?.caption || msg.message?.videoMessage?.caption || "";

                if (msg.key.fromMe && text.trim().toLowerCase().match(/^(stop|start)$/)) {
                    console.error(`[Gateway] Detected remote ${text.trim().toLowerCase()} command`);
                    console.log(JSON.stringify({
                        type: 'agent_control', command: text.trim().toLowerCase(), from: msg.key.remoteJid,
                    }));
                }

                let mediaPath = null, mediaType = null;
                if (['imageMessage', 'videoMessage', 'audioMessage', 'stickerMessage'].includes(messageType)) {
                    const result = await downloadAndSaveMedia(msg, messageType);
                    mediaPath = result.mediaPath; mediaType = result.mediaType;
                    if (mediaType === 'sticker') text = text || "[Sticker]";
                    else if (mediaType === 'audio' || mediaType === 'voice') text = text || "[Voice Note]";
                    else if (!text && mediaType) text = `[Sent a ${mediaType}]`;
                }

                // Update in-memory contact map for any new senders
                if (msg.key.remoteJid && !msg.key.remoteJid.includes('broadcast')) {
                    if (!contacts[msg.key.remoteJid]) {
                        contacts[msg.key.remoteJid] = {
                            id: msg.key.remoteJid,
                            notify: msg.pushName || null,
                        };
                    } else if (!contacts[msg.key.remoteJid].name && msg.pushName) {
                        contacts[msg.key.remoteJid].notify = msg.pushName;
                    }
                }

                console.log(JSON.stringify({
                    type: 'message', id: msg.key.id, from: msg.key.remoteJid,
                    pushName: msg.pushName, text, mediaPath, mediaType,
                    timestamp: msg.messageTimestamp,
                    isGroup: isJidGroup(msg.key.remoteJid), fromMe: msg.key.fromMe,
                }));
            }
        }
    });

    // Buffer for incoming stdin commands
    let buffer = '';
    process.stdin.on('data', async (data) => {
        buffer += data.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            if (!line.trim()) continue;
            try {
                const command = JSON.parse(line.trim());
                messageQueue.push(command);
                if (isConnected) processMessageQueue();
            } catch (err) {
                console.error("[Gateway] Error parsing command:", err.message);
            }
        }
    });

    // Health check ping
    setInterval(() => {
        if (isConnected && sock) {
            console.error('[Health] Gateway alive, queue:', messageQueue.length,
                'contacts:', Object.keys(contacts).length);
        }
    }, 60000);
}

// Graceful shutdown
process.on('SIGINT', () => {
    console.error('[Gateway] Received SIGINT. Shutting down gracefully...');
    stopPeriodicContactSync();
    if (sock) sock.end();
    process.exit(0);
});
process.on('SIGTERM', () => {
    console.error('[Gateway] Received SIGTERM. Shutting down gracefully...');
    stopPeriodicContactSync();
    if (sock) sock.end();
    process.exit(0);
});

startGateway().catch(err => {
    console.error("Critical Gateway Error:", err);
    process.exit(1);
});