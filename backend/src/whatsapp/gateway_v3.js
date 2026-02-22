console.error("🚀 DEBUG: GATEWAY VERSION 2.0 STARTING");
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

console.error(`[Gateway V2.1] Using AUTH_DIR: ${AUTH_DIR}`);
if (phoneNumber) console.error(`[Gateway V2.1] Using Pairing Code for phone: ${phoneNumber}`);

// ── FIX: Single-run guard ────────────────────────────────────────────────────
// The old code called startGateway() recursively from inside connection.update.
// This created multiple concurrent Baileys sockets fighting over the same auth
// state. We now exit the process instead and let Python's bridge._health_monitor
// (or _attempt_restart) restart us cleanly as a fresh process.
//
// Rule: Node NEVER calls startGateway() more than once per process lifetime.
// If a reconnect is needed, Node emits { type: "restart_requested" } on stdout
// and calls process.exit(0).  Python owns the restart lifecycle.
// ────────────────────────────────────────────────────────────────────────────

/**
 * Emit a restart request to Python and exit this process.
 * Python's bridge will detect either:
 *   a) the stdout JSON event "restart_requested", OR
 *   b) the process dying (poll() !== null) — whichever arrives first.
 * Either way Python will call _attempt_restart() which calls start().
 *
 * @param {string} reason  Human-readable reason for the restart.
 * @param {number} [delay] Milliseconds to wait before exiting (gives stdout time to flush).
 */
function requestRestartAndExit(reason, delay = 500) {
    console.error(`[Gateway] Requesting restart from Python: ${reason}`);
    // Emit the event synchronously so Python can log the reason
    console.log(JSON.stringify({ type: 'restart_requested', reason }));
    // Give stdout a moment to flush, then exit
    setTimeout(() => process.exit(0), delay);
}

// Connection state management
let sock = null;
let isConnected = false;
let messageQueue = [];
let isProcessingQueue = false;
let contacts = {};

let contactsTimeout = null;
function debouncedSendContacts() {
    if (contactsTimeout) clearTimeout(contactsTimeout);
    contactsTimeout = setTimeout(() => {
        sendContacts();
    }, 5000);
}

async function sendContacts() {
    const raw = Object.values(contacts);
    const cleaned = raw.filter(c => {
        if (!c.id) return false;
        if (c.id.includes('status@') || c.id.includes('broadcast') || c.id.includes('newsletter')) return false;
        const valid = c.id.endsWith('@s.whatsapp.net') || c.id.endsWith('@lid');
        return valid;
    }).map(c => {
        let phoneJid = c.id;
        if (c.id.endsWith('@lid')) {
            phoneJid = c.id;
        }
        return {
            id: phoneJid,
            name: c.name || c.notify || c.pushName || null,
            notify: c.notify || c.pushName || null,
            pushName: c.pushName || null,
            isLid: c.id.endsWith('@lid'),
        };
    });
    console.error(`[Gateway] Sending ${cleaned.length} contacts (filtered from ${raw.length})`);
    cleaned.slice(0, 5).forEach((c, i) => {
        console.error(`[Gateway]   Sample[${i}]: id=${c.id}, name=${c.name || 'NONE'}`);
    });
    console.log(JSON.stringify({
        type: 'contacts',
        data: cleaned
    }));
}

// Ensure directories exist
[MEDIA_DIR].forEach(dir => {
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
});

function getFileHash(buffer) {
    return crypto.createHash('sha256').update(buffer).digest('hex');
}

function findExistingMedia(hash, extension) {
    const files = fs.readdirSync(MEDIA_DIR);
    const matchingFile = files.find(f => f.startsWith(hash));
    return matchingFile ? path.join(MEDIA_DIR, matchingFile) : null;
}

async function processMessageQueue() {
    if (isProcessingQueue || messageQueue.length === 0) return;

    isProcessingQueue = true;

    while (messageQueue.length > 0) {
        const command = messageQueue.shift();

        try {
            await executeCommand(command);
        } catch (err) {
            console.log(JSON.stringify({
                type: 'error',
                id: command.id,
                message: err.message
            }));
        }

        await new Promise(r => setTimeout(r, 300));
    }

    isProcessingQueue = false;
}

async function executeCommand(command) {
    const target = formatJid(command.to);

    if (!isConnected || !sock || !sock.user) {
        throw new Error('Connection not ready');
    }

    try {
        await sock.presenceSubscribe(target);
        await sock.sendPresenceUpdate('composing', target);
    } catch (e) {
        // Ignore presence errors
    }

    let retries = 3;
    let lastError = null;

    while (retries > 0) {
        try {
            if (command.type === 'send_message') {
                await sendMessage(target, command);
            } else if (command.type === 'react') {
                await sendReaction(target, command);
            } else if (command.type === 'delete_message') {
                await deleteMessage(target, command);
            } else if (command.type === 'get_contacts') {
                await sendContacts();
            }

            console.log(JSON.stringify({
                type: 'ack',
                id: command.id,
                success: true
            }));

            try {
                await sock.sendPresenceUpdate('paused', target);
            } catch (e) { }

            return;

        } catch (err) {
            lastError = err;
            const isTransient = err.output?.statusCode === 428 ||
                err.message.includes('Connection Closed') ||
                err.message.includes('timed out');

            if (isTransient && retries > 1) {
                retries--;
                await new Promise(r => setTimeout(r, 2000));
                continue;
            }

            throw err;
        }
    }

    if (lastError) {
        throw lastError;
    }
}

async function sendMessage(target, command) {
    let options = {};

    if (command.media) {
        const mediaPath = command.media;
        const isUrl = mediaPath.startsWith('http');
        const ext = path.extname(mediaPath).toLowerCase();

        if (ext === '.webp' || command.mediaType === 'sticker') {
            options = {
                sticker: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath)
            };
        } else if (ext === '.mp4' || ext === '.mkv' || ext === '.avi' || command.mediaType === 'video') {
            options = {
                video: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath),
                caption: command.text || ''
            };
        } else if (ext === '.ogg' || ext === '.mp3' || ext === '.m4a' || ext === '.opus' || command.mediaType === 'audio') {
            options = {
                audio: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath),
                mimetype: 'audio/mp4',
                ptt: true
            };
        } else {
            options = {
                image: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath),
                caption: command.text || ''
            };
        }
    } else if (command.text) {
        options = { text: command.text };
    } else {
        throw new Error('No text or media provided');
    }

    await sock.sendMessage(target, options);
}

async function sendReaction(target, command) {
    await sock.sendMessage(target, {
        react: {
            text: command.emoji,
            key: {
                remoteJid: target,
                id: command.messageId,
                fromMe: false
            }
        }
    });
}

async function deleteMessage(target, command) {
    await sock.sendMessage(target, {
        delete: {
            remoteJid: target,
            fromMe: true,
            id: command.messageId
        }
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

        const mediaTypeMap = {
            'audio': 'ogg',
            'video': 'mp4',
            'image': 'jpg',
            'sticker': 'webp'
        };
        const mediaType = messageType.replace('Message', '');
        const extension = mediaTypeMap[mediaType] || 'bin';

        let mediaPath = findExistingMedia(hash, extension);

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
        state = r2Auth.state;
        saveCreds = r2Auth.saveCreds;
        clearStateFunc = r2Auth.clearState;
    } else {
        console.error(`[Gateway] Using Local File System for Auth State (Dir: ${AUTH_DIR})`);
        const localAuth = await useMultiFileAuthState(AUTH_DIR);
        state = localAuth.state;
        saveCreds = localAuth.saveCreds;

        clearStateFunc = () => {
            const files = fs.readdirSync(AUTH_DIR);
            for (const file of files) {
                fs.unlinkSync(path.join(AUTH_DIR, file));
            }
            console.error(`[Gateway] Cleared ${files.length} stale auth files from ${AUTH_DIR}`);
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
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        logger,
        browser: ["Ubuntu", "Chrome", "20.0.04"],
        printQRInTerminal: false,
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 30000,
        retryRequestDelayMs: 500
    });

    let isPairingCodeRequested = false;

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('contacts.upsert', (newContacts) => {
        newContacts.forEach(c => {
            if (!c.id || c.id.includes('broadcast')) return;
            contacts[c.id] = { ...contacts[c.id], ...c };
        });
        console.error(`[Gateway] contacts.upsert: +${newContacts.length} contacts (Total: ${Object.keys(contacts).length})`);

        if (isConnected) {
            debouncedSendContacts();
        }
    });

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
    });

    sock.ev.on('messaging-history.set', (history) => {
        const historyContacts = history.contacts || [];
        historyContacts.forEach(c => {
            if (!c.id || c.id.includes('broadcast')) return;
            contacts[c.id] = { ...contacts[c.id], ...c };
        });
        console.error(`[Gateway] History sync: captured ${historyContacts.length} contacts (Total: ${Object.keys(contacts).length})`);

        const MAX_HISTORY_PER_CONTACT = 200;
        const historyMessages = history.messages || [];
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
                    id: msg.key.id,
                    from: jid,
                    pushName: msg.pushName || '',
                    text: text,
                    fromMe: msg.key.fromMe || false,
                    timestamp: msg.messageTimestamp,
                });
            }

            const capped = [];
            for (const [jid, msgs] of Object.entries(byContact)) {
                msgs.sort((a, b) => (Number(b.timestamp) || 0) - (Number(a.timestamp) || 0));
                capped.push(...msgs.slice(0, MAX_HISTORY_PER_CONTACT));
            }

            if (capped.length > 0) {
                console.error(`[Gateway] History sync: emitting ${capped.length} msgs (capped at ${MAX_HISTORY_PER_CONTACT}/contact from ${historyMessages.length} raw)`);
                console.log(JSON.stringify({
                    type: 'history_messages',
                    data: capped
                }));
            }
        }

        debouncedSendContacts();
    });

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (phoneNumber && !state.creds.registered && (qr || update.isNewLogin) && !isPairingCodeRequested) {
            try {
                isPairingCodeRequested = true;
                await new Promise(resolve => setTimeout(resolve, 1500));
                let code = await sock.requestPairingCode(phoneNumber);
                code = code?.match(/.{1,4}/g)?.join('-') || code;
                console.error(`[Gateway] Pairing code requested successfully: ${code}`);
                console.log(JSON.stringify({ type: 'pairing_code', code: code }));
            } catch (err) {
                isPairingCodeRequested = false;
                console.error("[Gateway] Pairing Code Request Failed:", err);
            }
        }

        if (connection) {
            let emitStatus = connection;

            if (connection === 'close') {
                const statusCode = lastDisconnect?.error?.output?.statusCode || lastDisconnect?.error?.code;
                if (statusCode === 401 || statusCode !== DisconnectReason.loggedOut) {
                    emitStatus = 'pairing';
                }
            }

            console.log(JSON.stringify({
                type: 'connection',
                status: emitStatus,
                user: connection === 'open' ? sock.user : undefined
            }));

            if (connection === 'open') {
                isConnected = true;
                console.error(`[Gateway] Connection opened successfully. Total contacts cached: ${Object.keys(contacts).length}`);
                debouncedSendContacts();
                processMessageQueue();
            }

            if (connection === 'close') {
                isConnected = false;
                const error = lastDisconnect?.error;
                const statusCode = error?.output?.statusCode || error?.code || 'N/A';
                const reason = error?.message || 'Unknown';

                console.error(`[Gateway] Connection closed (Status: ${statusCode}), Error: ${reason}`);
                console.error(`[Gateway] Connection Update Object: ${util.inspect(update, { depth: null, colors: false })}`);
                if (error) {
                    console.error(`[Gateway] Full error context: ${util.inspect(error, { depth: null, colors: false })}`);
                }

                const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

                if (shouldReconnect) {
                    // ── FIX: Do NOT call startGateway() here. ──────────────────
                    // Calling startGateway() recursively creates a second Baileys
                    // socket inside the same process, fighting the first for the
                    // same auth state and pairing code slot.
                    //
                    // Instead: emit restart_requested and exit cleanly.
                    // Python's bridge._health_monitor will detect the exit and
                    // call _attempt_restart(), which spawns a brand-new process.
                    //
                    // If it's a conflict (another session), wait slightly longer
                    // before telling Python we want a restart, so the competing
                    // session gets a chance to die first.
                    const isConflict = reason && reason.toLowerCase().includes('conflict');
                    const delay = isConflict ? 5000 : 500;
                    console.error(`[Gateway] Scheduling restart via Python in ${delay}ms (${isConflict ? 'conflict backoff' : 'normal'})`);
                    setTimeout(() => requestRestartAndExit(reason, 200), delay);
                } else {
                    // Logged out (401): clear auth state and ask Python to restart
                    console.error('[Gateway] Logged out (401). Clearing auth state and requesting fresh restart from Python...');
                    try {
                        if (clearStateFunc) {
                            await clearStateFunc();
                            console.error(`[Gateway] Cleared stale auth state for session.`);
                        }
                    } catch (e) {
                        console.error(`[Gateway] Auth cleanup error: ${e.message}`);
                    }
                    contacts = {};
                    // ── FIX: Exit and let Python restart, not setTimeout(startGateway) ──
                    console.error('[Gateway] Auth cleared. Requesting Python restart after 10s throttle...');
                    setTimeout(() => requestRestartAndExit('logged_out_401_cleared', 200), 10000);
                }
            }
        }
    });

    sock.ev.on('messages.upsert', async (m) => {
        if (m.type === 'append') {
            const MAX_PER_CONTACT = 200;
            const byContact = {};
            for (const msg of m.messages) {
                const jid = msg.key?.remoteJid;
                if (!jid || jid.includes('broadcast') || jid.includes('status@')) continue;

                const text = msg.message?.conversation ||
                    msg.message?.extendedTextMessage?.text ||
                    msg.message?.imageMessage?.caption ||
                    msg.message?.videoMessage?.caption || "";
                if (!text) continue;

                if (!byContact[jid]) byContact[jid] = [];
                byContact[jid].push({
                    id: msg.key.id,
                    from: jid,
                    pushName: msg.pushName || '',
                    text: text,
                    fromMe: msg.key.fromMe || false,
                    timestamp: msg.messageTimestamp,
                });
            }

            const capped = [];
            for (const [jid, msgs] of Object.entries(byContact)) {
                msgs.sort((a, b) => (Number(b.timestamp) || 0) - (Number(a.timestamp) || 0));
                capped.push(...msgs.slice(0, MAX_PER_CONTACT));
            }

            if (capped.length > 0) {
                console.error(`[Gateway] messages.upsert(append): storing ${capped.length} history msgs from ${Object.keys(byContact).length} contacts`);
                console.log(JSON.stringify({
                    type: 'history_messages',
                    data: capped
                }));
            }
        }

        if (m.type === 'notify') {
            const now = Math.floor(Date.now() / 1000);
            for (const msg of m.messages) {
                const msgTime = Number(msg.messageTimestamp) || 0;
                if (now - msgTime > 60) {
                    continue;
                }

                const messageType = Object.keys(msg.message || {})[0];
                let text = msg.message?.conversation ||
                    msg.message?.extendedTextMessage?.text ||
                    msg.message?.imageMessage?.caption ||
                    msg.message?.videoMessage?.caption ||
                    "";

                if (msg.key.fromMe && text.trim().toLowerCase().match(/^(stop|start)$/)) {
                    console.error(`[Gateway] Detected remote ${text.trim().toLowerCase()} command`);
                    console.log(JSON.stringify({
                        type: 'agent_control',
                        command: text.trim().toLowerCase(),
                        from: msg.key.remoteJid
                    }));
                }

                let mediaPath = null;
                let mediaType = null;

                if (['imageMessage', 'videoMessage', 'audioMessage', 'stickerMessage'].includes(messageType)) {
                    const result = await downloadAndSaveMedia(msg, messageType);
                    mediaPath = result.mediaPath;
                    mediaType = result.mediaType;

                    if (mediaType === 'sticker') {
                        text = text || "[Sticker]";
                    } else if (mediaType === 'audio' || mediaType === 'voice') {
                        text = text || "[Voice Note]";
                    } else if (!text && mediaType) {
                        text = `[Sent a ${mediaType}]`;
                    }
                }

                if (msg.key.remoteJid && msg.pushName && !msg.key.remoteJid.includes('broadcast')) {
                    if (!contacts[msg.key.remoteJid] || !contacts[msg.key.remoteJid].name) {
                        contacts[msg.key.remoteJid] = {
                            ...contacts[msg.key.remoteJid],
                            id: msg.key.remoteJid,
                            notify: msg.pushName
                        };
                    }
                }

                const messageData = {
                    type: 'message',
                    id: msg.key.id,
                    from: msg.key.remoteJid,
                    pushName: msg.pushName,
                    text: text,
                    mediaPath: mediaPath,
                    mediaType: mediaType,
                    timestamp: msg.messageTimestamp,
                    isGroup: isJidGroup(msg.key.remoteJid),
                    fromMe: msg.key.fromMe
                };
                console.log(JSON.stringify(messageData));
            }
        }
    });

    // Buffer for incoming data
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

                if (isConnected) {
                    processMessageQueue();
                }

            } catch (err) {
                console.error("[Gateway] Error parsing command:", err.message);
            }
        }
    });

    // Health check ping
    setInterval(() => {
        if (isConnected && sock) {
            console.error('[Health] Gateway alive, queue:', messageQueue.length);
        }
    }, 60000);
}

// Graceful shutdown
process.on('SIGINT', () => {
    console.error('[Gateway] Received SIGINT. Shutting down gracefully...');
    if (sock) {
        sock.end();
    }
    process.exit(0);
});

process.on('SIGTERM', () => {
    console.error('[Gateway] Received SIGTERM. Shutting down gracefully...');
    if (sock) {
        sock.end();
    }
    process.exit(0);
});

startGateway().catch(err => {
    console.error("Critical Gateway Error:", err);
    process.exit(1);
});