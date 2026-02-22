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
const phoneNumber = process.argv[3] ? process.argv[3].replace(/[^0-9]/g, '') : null;
const MEDIA_DIR = path.join(__dirname, '../../../data/media');
const logger = P({ level: 'silent' });

console.error(`[Gateway V2.1] Using AUTH_DIR: ${AUTH_DIR}`);
if (phoneNumber) console.error(`[Gateway V2.1] Using Pairing Code for phone: ${phoneNumber}`);

// Connection state management
let sock = null;
let isConnected = false;
let messageQueue = [];
let isProcessingQueue = false;
let contacts = {}; // Track all seen contacts

let contactsTimeout = null;
function debouncedSendContacts() {
    if (contactsTimeout) clearTimeout(contactsTimeout);
    contactsTimeout = setTimeout(() => {
        sendContacts();
    }, 5000); // Wait 5 seconds after last update before sending
}

// ... (downloadAndSaveMedia implementation remains above) ...

async function sendContacts() {
    const raw = Object.values(contacts);
    // Accept @s.whatsapp.net (individuals), @g.us (groups), and @lid (linked identity)
    const cleaned = raw.filter(c => {
        if (!c.id) return false;
        // Skip system/broadcast/newsletter
        if (c.id.includes('status@') || c.id.includes('broadcast') || c.id.includes('newsletter')) return false;
        // REQUIREMENT: REAL CONTACTS ONLY (individuals only)
        const valid = c.id.endsWith('@s.whatsapp.net') || c.id.endsWith('@lid');
        return valid;
    }).map(c => {
        // For @lid contacts, try to extract a phone number from the ID
        // LID format is typically: <number>@lid  
        let phoneJid = c.id;
        if (c.id.endsWith('@lid')) {
            // Keep the LID as-is but mark it; Python side will handle display
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
    // Debug: log a few samples
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

// Calculate file hash for deduplication
function getFileHash(buffer) {
    return crypto.createHash('sha256').update(buffer).digest('hex');
}

// Check if media already exists
function findExistingMedia(hash, extension) {
    const files = fs.readdirSync(MEDIA_DIR);
    const matchingFile = files.find(f => f.startsWith(hash));
    return matchingFile ? path.join(MEDIA_DIR, matchingFile) : null;
}

// Process message queue with retry logic
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

        // Small delay between messages to prevent rate limiting
        await new Promise(r => setTimeout(r, 300));
    }

    isProcessingQueue = false;
}

// Execute a single command with proper error handling
async function executeCommand(command) {
    const target = formatJid(command.to);

    // Verify connection
    if (!isConnected || !sock || !sock.user) {
        throw new Error('Connection not ready');
    }

    // Send typing indicator
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

            // Success - send ack
            console.log(JSON.stringify({
                type: 'ack',
                id: command.id,
                success: true
            }));

            // Revoke typing
            try {
                await sock.sendPresenceUpdate('paused', target);
            } catch (e) { }

            return; // Success!

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

            throw err; // Final failure
        }
    }

    if (lastError) {
        throw lastError;
    }
}

// Send a message (text, media, or sticker)
async function sendMessage(target, command) {
    let options = {};

    if (command.media) {
        const mediaPath = command.media;
        const isUrl = mediaPath.startsWith('http');
        const ext = path.extname(mediaPath).toLowerCase();

        // Determine media type
        if (ext === '.webp' || command.mediaType === 'sticker') {
            // Sticker
            options = {
                sticker: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath)
            };
        } else if (ext === '.mp4' || ext === '.mkv' || ext === '.avi' || command.mediaType === 'video') {
            // Video
            options = {
                video: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath),
                caption: command.text || ''
            };
        } else if (ext === '.ogg' || ext === '.mp3' || ext === '.m4a' || ext === '.opus' || command.mediaType === 'audio') {
            // Audio
            options = {
                audio: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath),
                mimetype: 'audio/mp4',
                ptt: true
            };
        } else {
            // Image (default)
            options = {
                image: isUrl ? { url: mediaPath } : fs.readFileSync(mediaPath),
                caption: command.text || ''
            };
        }
    } else if (command.text) {
        // Text only
        options = { text: command.text };
    } else {
        throw new Error('No text or media provided');
    }

    await sock.sendMessage(target, options);
}

// Send a reaction
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

// Delete a message
async function deleteMessage(target, command) {
    await sock.sendMessage(target, {
        delete: {
            remoteJid: target,
            fromMe: true,
            id: command.messageId
        }
    });
}

// Format JID properly
function formatJid(jid) {
    if (!jid) return jid;
    if (jid.includes('@')) return jid;
    return `${jid.replace(/\D/g, '')}@s.whatsapp.net`;
}

// Download and save media with deduplication
async function downloadAndSaveMedia(msg, messageType) {
    try {
        const buffer = await downloadMediaMessage(msg, 'buffer', {});

        // Calculate hash for deduplication
        const hash = getFileHash(buffer);

        // Determine file extension
        const mediaTypeMap = {
            'audio': 'ogg',
            'video': 'mp4',
            'image': 'jpg',
            'sticker': 'webp'
        };
        const mediaType = messageType.replace('Message', '');
        const extension = mediaTypeMap[mediaType] || 'bin';

        // Check if file already exists
        let mediaPath = findExistingMedia(hash, extension);

        if (!mediaPath) {
            // Save new file with hash-based name
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
        // MUST use these specific browser options for Pairing Code to work
        browser: phoneNumber ? ["Ubuntu", "Chrome", "20.0.04"] : ["Orbit AI", "Desktop", "1.0.0"],
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 30000,
        retryRequestDelayMs: 500
    });

    // The pairing code is now requested securely during the 'connection.update' event.

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('contacts.upsert', (newContacts) => {
        newContacts.forEach(c => {
            if (!c.id || c.id.includes('broadcast')) return;
            contacts[c.id] = { ...contacts[c.id], ...c };
        });
        console.error(`[Gateway] contacts.upsert: +${newContacts.length} contacts (Total: ${Object.keys(contacts).length})`);

        // Auto-send update if we already had a connection (dynamic update)
        if (isConnected) {
            debouncedSendContacts();
        }
    });

    sock.ev.on('contacts.update', (updates) => {
        updates.forEach(u => {
            if (!u.id || u.id.includes('broadcast')) return;
            if (contacts[u.id]) {
                // Only update name if the new update actually HAS a name/notify
                // This prevents "random number" overrides if we already have a name
                const hasName = u.name || u.notify || u.pushName;
                if (!contacts[u.id].name || hasName) {
                    contacts[u.id] = { ...contacts[u.id], ...u };
                } else {
                    // Just merge non-name fields
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

        // Also capture message history for AI Auto-Profile
        const MAX_HISTORY_PER_CONTACT = 200;
        const historyMessages = history.messages || [];
        if (historyMessages.length > 0) {
            // Group by contact JID
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

            // Cap each contact at MAX_HISTORY_PER_CONTACT most recent
            const capped = [];
            for (const [jid, msgs] of Object.entries(byContact)) {
                // Sort by timestamp descending, take only the latest
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

        // Auto-send contacts after history sync completes
        debouncedSendContacts();
    });

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;

        // Request pairing code when socket is initialized and needs auth 
        // We detect this when Baileys generates a QR code string or asks for one.
        if (phoneNumber && !state.creds.registered && (qr || update.isNewLogin)) {
            try {
                // Wait a tiny bit for the socket to fully stabilize
                await new Promise(resolve => setTimeout(resolve, 1500));
                let code = await sock.requestPairingCode(phoneNumber);
                code = code?.match(/.{1,4}/g)?.join('-') || code;
                console.error(`[Gateway] Pairing code requested successfully: ${code}`);
                console.log(JSON.stringify({ type: 'pairing_code', code: code }));
            } catch (err) {
                console.error("[Gateway] Pairing Code Request Failed:", err);
            }
        }

        if (connection) {
            console.log(JSON.stringify({
                type: 'connection',
                status: connection,
                user: connection === 'open' ? sock.user : undefined
            }));

            if (connection === 'open') {
                isConnected = true;
                console.error(`[Gateway] Connection opened successfully. Total contacts cached: ${Object.keys(contacts).length}`);
                debouncedSendContacts(); // Send initial contacts
                processMessageQueue(); // Start pumping queued messages
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
                    // If it's a conflict (another session replaced us), wait longer
                    // to let the competing session die before we reconnect.
                    const isConflict = reason && reason.toLowerCase().includes('conflict');
                    const delay = isConflict ? 30000 : 5000;
                    console.error(`[Gateway] Reconnecting in ${delay / 1000}s... (${isConflict ? 'conflict — waiting for competing session to close' : 'normal reconnect'})`);
                    setTimeout(() => startGateway(), delay);
                } else {
                    console.error('[Gateway] Logged out (401). Auto-clearing auth and restarting for fresh session...');
                    // Clear stale auth files so next start requests fresh session
                    try {
                        if (clearStateFunc) {
                            await clearStateFunc();
                            console.error(`[Gateway] Cleared stale auth state for session.`);
                        }
                    } catch (e) {
                        console.error(`[Gateway] Auth cleanup error: ${e.message}`);
                    }
                    // Restart gateway — will request fresh session
                    contacts = {};
                    setTimeout(() => startGateway(), 2000);
                }
            }
        }
    });

    sock.ev.on('messages.upsert', async (m) => {
        // type=append: historical messages from WhatsApp sync (like scrolling up)
        // Store these quietly for AI Auto-Profile without triggering AI pipeline
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

        // type=notify: real-time incoming messages — process through full AI pipeline
        if (m.type === 'notify') {
            const now = Math.floor(Date.now() / 1000);
            for (const msg of m.messages) {
                // Ignore historical messages emitted during sync (older than 60 seconds)
                const msgTime = Number(msg.messageTimestamp) || 0;
                if (now - msgTime > 60) {
                    continue;
                }

                // Allow fromMe messages for God Mode and Stop/Start commands
                const messageType = Object.keys(msg.message || {})[0];
                let text = msg.message?.conversation ||
                    msg.message?.extendedTextMessage?.text ||
                    msg.message?.imageMessage?.caption ||
                    msg.message?.videoMessage?.caption ||
                    "";

                // Check for remote command (stop/start from own number)
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

                // Media Handling with deduplication
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

                // Update contact name from pushName if missing
                if (msg.key.remoteJid && msg.pushName && !msg.key.remoteJid.includes('broadcast')) {
                    if (!contacts[msg.key.remoteJid] || !contacts[msg.key.remoteJid].name) {
                        contacts[msg.key.remoteJid] = {
                            ...contacts[msg.key.remoteJid],
                            id: msg.key.remoteJid,
                            notify: msg.pushName
                        };
                    }
                }

                // Send structured message to Python
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
                    fromMe: msg.key.fromMe // Pass this flag to Python
                };
                console.log(JSON.stringify(messageData));
            }
        }
    });

    // Buffer for incoming data
    let buffer = '';

    // Handle incoming commands from Python (stdin)
    process.stdin.on('data', async (data) => {
        buffer += data.toString();

        // Process line by line
        const lines = buffer.split('\n');

        // The last part might be incomplete, save it back to buffer
        buffer = lines.pop();

        for (const line of lines) {
            if (!line.trim()) continue;

            try {
                const command = JSON.parse(line.trim());

                // Add command to queue
                messageQueue.push(command);

                // Process queue if connected
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