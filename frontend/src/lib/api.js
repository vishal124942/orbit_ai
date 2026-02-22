// API client with auth token injection
import axios from 'axios'
import { create } from 'zustand'

const BASE_URL = import.meta.env.VITE_API_URL || ''

// ── Auth Store (Zustand) ──────────────────────────────────────────────────────

export const useAuthStore = create((set, get) => ({
    user: null,
    token: localStorage.getItem('orbit_token'),
    isLoading: true,

    setAuth: (user, token) => {
        localStorage.setItem('orbit_token', token)
        set({ user, token })
    },

    logout: () => {
        localStorage.removeItem('orbit_token')
        set({ user: null, token: null })
    },

    setLoading: (isLoading) => set({ isLoading }),
}))

// ── Axios instance ────────────────────────────────────────────────────────────

const api = axios.create({ baseURL: BASE_URL })

api.interceptors.request.use((config) => {
    const token = useAuthStore.getState().token
    if (token) {
        config.headers.Authorization = `Bearer ${token}`
    }
    return config
})

api.interceptors.response.use(
    (res) => res,
    (err) => {
        if (err.response?.status === 401) {
            useAuthStore.getState().logout()
            // Redirect to login so the user isn't stuck on a broken page
            if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
                window.location.href = '/login'
            }
        }
        return Promise.reject(err)
    }
)

// ── Auth ──────────────────────────────────────────────────────────────────────

export const authGoogleLogin = async (credential) => {
    const res = await api.post('/auth/google', { credential })
    return res.data
}

export const fetchMe = async () => {
    const res = await api.get('/api/me')
    return res.data
}

// ── WhatsApp ──────────────────────────────────────────────────────────────────

export const startWaAgent = async (phoneNumber = null) => {
    const payload = phoneNumber ? { phone_number: phoneNumber } : {}
    const res = await api.post('/api/whatsapp/start', payload)
    return res.data
}

export const stopWaAgent = async () => {
    const res = await api.post('/api/whatsapp/stop')
    return res.data
}

export const regenerateWaCode = async (phoneNumber = null) => {
    const payload = phoneNumber ? { phone_number: phoneNumber } : {}
    const res = await api.post('/api/whatsapp/regenerate', payload)
    return res.data
}

export const getWaStatus = async () => {
    const res = await api.get('/api/whatsapp/status')
    return res.data
}

// ── Contacts ──────────────────────────────────────────────────────────────────

export const getContacts = async (search = '', isGroup = null) => {
    const params = {}
    if (search) params.search = search
    if (isGroup !== null) params.is_group = isGroup
    const res = await api.get('/api/contacts', { params })
    return res.data
}

export const getTopContacts = async (n = 100) => {
    const res = await api.get('/api/contacts/top', { params: { n } })
    return res.data
}

export const syncContacts = async () => {
    const res = await api.post('/api/contacts/sync')
    return res.data
}

// ── Settings ──────────────────────────────────────────────────────────────────

export const getSettings = async () => {
    const res = await api.get('/api/settings')
    return res.data
}

export const updateAllowlist = async (allowedJids) => {
    const res = await api.post('/api/settings/allowlist', { allowed_jids: allowedJids })
    return res.data
}

export const updateContactTone = async (data) => {
    const res = await api.post('/api/settings/contact-tone', data)
    return res.data
}

export const generateContactSoul = async (contactJid) => {
    const res = await api.post('/api/settings/contact-soul/generate', { contact_jid: contactJid })
    return res.data
}

export const getAnalytics = async () => {
    const res = await api.get('/api/analytics')
    return res.data
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

/**
 * Create a WebSocket with automatic reconnection.
 *
 * FIXES:
 * 1. _pingInterval was stored as a non-standard property on the WebSocket
 *    object — fragile. Now managed via closure.
 * 2. No reconnection: if the WS dropped, it stayed dead. Now reconnects with
 *    exponential back-off (1s → 2s → 4s … up to 30s cap).
 * 3. onMessage is called from ws.onmessage but the old code never cleared the
 *    interval on reconnect — multiple intervals could stack up.
 *
 * Returns a controller object with a `close()` method to permanently stop
 * reconnection (e.g. when the user logs out).
 */
export const createWebSocket = (userId, onMessage) => {
    const wsBase = BASE_URL.replace(/^http/, 'ws') || `ws://${window.location.host}`
    const url = `${wsBase}/ws/${userId}`

    let ws = null
    let pingInterval = null
    let reconnectTimeout = null
    let reconnectDelay = 1000  // ms, doubles on each failure up to 30 s
    let stopped = false        // true after controller.close() — never reconnect

    const clearTimers = () => {
        if (pingInterval) { clearInterval(pingInterval); pingInterval = null }
        if (reconnectTimeout) { clearTimeout(reconnectTimeout); reconnectTimeout = null }
    }

    const connect = () => {
        if (stopped) return

        ws = new WebSocket(url)

        ws.onopen = () => {
            reconnectDelay = 1000  // Reset back-off on successful connect
            clearTimers()
            pingInterval = setInterval(() => {
                if (ws.readyState === WebSocket.OPEN) ws.send('ping')
            }, 30000)
        }

        ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data)
                if (msg.type === 'ping') {
                    if (ws.readyState === WebSocket.OPEN) ws.send('pong')
                    return
                }
                if (msg.type === 'pong') return
                onMessage(msg)
            } catch (err) {
                if (e.data === 'pong' || e.data === 'ping') return
                console.warn('[WS] parse error', err, e.data)
            }
        }

        ws.onerror = (err) => {
            // onerror is always followed by onclose — let onclose handle reconnect
            console.warn('[WS] error', err)
        }

        ws.onclose = (event) => {
            clearTimers()
            if (stopped) return

            // 1001 = going away (page unload) — don't reconnect
            if (event.code === 1001) return

            console.warn(`[WS] closed (code ${event.code}), reconnecting in ${reconnectDelay}ms…`)
            reconnectTimeout = setTimeout(() => {
                reconnectDelay = Math.min(reconnectDelay * 2, 30000)
                connect()
            }, reconnectDelay)
        }
    }

    connect()

    return {
        /** Permanently close and disable reconnection. */
        close: () => {
            stopped = true
            clearTimers()
            if (ws) {
                ws.close(1000, 'client closed')
                ws = null
            }
        },
        /** Expose current socket for callers that need readyState. */
        get socket() { return ws },
    }
}

export default api