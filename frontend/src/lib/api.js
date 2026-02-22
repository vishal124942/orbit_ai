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

export const createWebSocket = (userId, onMessage) => {
    const wsBase = BASE_URL.replace('http', 'ws') || `ws://${window.location.host}`
    const ws = new WebSocket(`${wsBase}/ws/${userId}`)

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
            // Silence raw pong/ping if any remain
            if (e.data === 'pong' || e.data === 'ping') return
            console.warn('WS parse error', err, e.data)
        }
    }

    ws.onopen = () => {
        // Keep alive ping
        const ping = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send('ping')
        }, 30000)
        ws._pingInterval = ping
    }

    ws.onclose = () => {
        if (ws._pingInterval) clearInterval(ws._pingInterval)
    }

    return ws
}

export default api