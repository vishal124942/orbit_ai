import { useState, useEffect, useRef, useCallback } from 'react'
import { Play, Square, Smartphone, CheckCircle, AlertCircle, Loader, RefreshCw, Users } from 'lucide-react'
import { startWaAgent, stopWaAgent, regenerateWaCode, useAuthStore } from '../lib/api'
import axios from 'axios'

/**
 * Pairing Code Poller — REST polling as primary delivery mechanism.
 *
 * Uses stable refs for callbacks so the effect only remounts on
 * enabled/hasActiveCode changes, never on callback reference changes.
 */
function usePairingCodePoller(enabled, hasActiveCode, onPairingCode, onConnected) {
    const timerRef = useRef(null)
    const onPairingCodeRef = useRef(onPairingCode)
    const onConnectedRef = useRef(onConnected)
    useEffect(() => { onPairingCodeRef.current = onPairingCode }, [onPairingCode])
    useEffect(() => { onConnectedRef.current = onConnected }, [onConnected])

    useEffect(() => {
        if (!enabled || hasActiveCode) {
            if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
            return
        }

        const poll = async () => {
            try {
                const token = useAuthStore.getState().token
                const res = await axios.get('/api/whatsapp/pairing-code', {
                    headers: { Authorization: `Bearer ${token}` },
                    timeout: 5000,
                })
                if (res.data.has_pairing_code) {
                    onPairingCodeRef.current(res.data.pairing_code)
                    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
                } else if (res.data.status === 'connected') {
                    onConnectedRef.current()
                    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
                }
            } catch (_) { /* network hiccup — retry */ }
        }

        poll()
        timerRef.current = setInterval(poll, 2500)
        return () => { if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null } }
    }, [enabled, hasActiveCode])
}

/**
 * ContactSyncBadge — live counter that updates via WebSocket contact_sync_progress
 * events and also polls the sync-status endpoint every 15s as fallback.
 */
function ContactSyncBadge({ isVisible, wsContactCount, userId }) {
    const [count, setCount] = useState(wsContactCount || 0)
    const [recentlySynced, setRecentlySynced] = useState(0)

    // Keep count in sync with WS pushes
    useEffect(() => {
        if (wsContactCount > count) setCount(wsContactCount)
    }, [wsContactCount])

    // Poll sync-status endpoint as fallback / for recently_synced info
    useEffect(() => {
        if (!isVisible || !userId) return
        const fetchStatus = async () => {
            try {
                const token = useAuthStore.getState().token
                const res = await axios.get('/api/contacts/sync-status', {
                    headers: { Authorization: `Bearer ${token}` },
                    timeout: 4000,
                })
                const { total, recently_synced, live_count } = res.data
                setCount(c => Math.max(c, live_count || 0, total || 0))
                setRecentlySynced(recently_synced || 0)
            } catch (_) { /* silent */ }
        }
        fetchStatus()
        const interval = setInterval(fetchStatus, 15000)
        return () => clearInterval(interval)
    }, [isVisible, userId])

    if (!isVisible || count === 0) return null

    return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-board-700/80 border border-board-600/50 text-xs">
            <Users size={11} className="text-board-accent flex-shrink-0" />
            <span className="text-gray-300">
                <span className="text-white font-semibold tabular-nums">{count.toLocaleString()}</span>
                {' '}contacts synced
                {recentlySynced > 0 && recentlySynced < count && (
                    <span className="text-gray-500 ml-1">(+{recentlySynced} new)</span>
                )}
            </span>
            <span className="w-1.5 h-1.5 rounded-full bg-board-accent animate-pulse flex-shrink-0" />
        </div>
    )
}

export default function WhatsAppPanel({
    waStatus,
    pairingCode: wsPairingCode,
    wsContactCount = 0,        // live count pushed via WebSocket contacts_progress event
    onStatusChange,
    onClearPairingCode,
    onAnalyticsRefresh,
    userId,
}) {
    const [loading, setLoading] = useState(false)
    const [message, setMessage] = useState('')
    const [polledPairingCode, setPolledPairingCode] = useState(null)
    const [phoneNumber, setPhoneNumber] = useState('')

    const requestInFlight = useRef(false)

    /**
     * Wrap async actions with an in-flight lock.
     * Both the ref (synchronous gate) and state (visual) are updated together
     * so the button is disabled correctly in all render paths.
     */
    const withLock = useCallback(async (fn) => {
        if (requestInFlight.current) {
            console.warn('[WhatsAppPanel] Request already in flight — ignoring duplicate')
            return
        }
        requestInFlight.current = true
        setLoading(true)
        setMessage('')
        try {
            await fn()
        } finally {
            requestInFlight.current = false
            setLoading(false)
        }
    }, [])

    const activePairingCode = polledPairingCode || wsPairingCode

    usePairingCodePoller(
        waStatus === 'pairing',
        !!activePairingCode,
        setPolledPairingCode,
        () => {
            onStatusChange('connected')
            onAnalyticsRefresh?.()
        },
    )

    useEffect(() => {
        if (waStatus === 'connected') setPolledPairingCode(null)
    }, [waStatus])

    const isConnected = waStatus === 'connected'
    const isPairing = waStatus === 'pairing'
    const isRegenerating = waStatus === 'regenerating'
    const isDisconnected = waStatus === 'disconnected'
    const isLocked = loading

    const handlePhoneChange = (e) => {
        const val = e.target.value
        if (!val) { setPhoneNumber(''); return }
        let digits = val.replace(/\D/g, '')
        if (digits.length > 0 && digits.length <= 10 && !digits.startsWith('91')) {
            digits = '91' + digits
        }
        if (digits.length > 2) {
            setPhoneNumber(`+${digits.substring(0, 2)}-${digits.substring(2, 12)}`)
        } else if (digits.length > 0) {
            setPhoneNumber(`+${digits}`)
        } else {
            setPhoneNumber('')
        }
    }

    const handleStart = () => withLock(async () => {
        if (!phoneNumber.trim() || phoneNumber.replace(/\D/g, '').length < 10) {
            setMessage('Please enter a valid phone number with country code (e.g. +917310885365).')
            return
        }
        try {
            await startWaAgent(phoneNumber.trim())
            onStatusChange('pairing')
            setMessage('Starting… Pairing code will appear shortly.')
        } catch (e) {
            setMessage(e.response?.data?.detail || 'Failed to start agent')
        }
    })

    const handleStop = () => withLock(async () => {
        try {
            await stopWaAgent()
            onStatusChange('disconnected')
            setPolledPairingCode(null)
            setMessage('Agent stopped.')
            onAnalyticsRefresh?.()
        } catch (e) {
            setMessage(e.response?.data?.detail || 'Failed to stop')
        }
    })

    const handleRegenerate = () => withLock(async () => {
        setPolledPairingCode(null)
        onClearPairingCode?.()
        onStatusChange('regenerating')
        try {
            await regenerateWaCode(phoneNumber)
            onStatusChange('pairing')
            setMessage('Generating fresh pairing code…')
        } catch (e) {
            onStatusChange('pairing')
            setMessage(e.response?.data?.detail || 'Failed to regenerate code')
        }
    })

    return (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Connection card */}
            <div className="card">
                <div className="flex items-center justify-between mb-6">
                    <div>
                        <h2 className="font-display text-xl text-white">WhatsApp Agent</h2>
                        <p className="text-gray-500 text-sm mt-1">Connect your number to activate</p>
                    </div>
                    <div className={`p-3 rounded-xl ${isConnected ? 'bg-green-500/10'
                        : (isPairing || isRegenerating) ? 'bg-board-accent/10'
                            : 'bg-board-700'
                        }`}>
                        <Smartphone size={24} className={
                            isConnected ? 'text-board-green'
                                : (isPairing || isRegenerating) ? 'text-board-accent'
                                    : 'text-gray-500'
                        } />
                    </div>
                </div>

                {/* Status pill */}
                <div className="flex items-center gap-3 p-4 rounded-xl bg-board-700 mb-4">
                    <div className={`status-dot ${isRegenerating ? 'pairing' : waStatus}`} />
                    <div>
                        <p className="text-white font-medium">
                            {isConnected ? 'Agent Active'
                                : isPairing ? 'Fetching Pairing Code'
                                    : isRegenerating ? 'Regenerating Code…'
                                        : 'Agent Offline'}
                        </p>
                        <p className="text-gray-500 text-xs mt-0.5">
                            {isConnected
                                ? 'Auto-responding to allowed contacts'
                                : (isPairing || isRegenerating)
                                    ? 'Open WhatsApp → Linked Devices → Link with phone number'
                                    : 'Click Start Agent below to begin'}
                        </p>
                    </div>
                </div>

                {/* Live contact sync counter — visible during pairing and when connected */}
                {(isPairing || isConnected || isRegenerating) && (
                    <div className="mb-4">
                        <ContactSyncBadge
                            isVisible={true}
                            wsContactCount={wsContactCount}
                            userId={userId}
                        />
                    </div>
                )}

                {/* Actions */}
                <div className="flex flex-col gap-4">
                    {!isConnected && !isPairing && !isRegenerating && (
                        <>
                            <div className="flex flex-col gap-2">
                                <label className="text-sm text-gray-300 font-medium">
                                    Link with Phone Number
                                </label>
                                <input
                                    type="text"
                                    placeholder="e.g. +917310885365"
                                    value={phoneNumber}
                                    onChange={handlePhoneChange}
                                    className="input bg-board-800 border-board-600 focus:border-board-accent text-sm pb-2 pt-2"
                                />
                            </div>
                            <div className="flex gap-3 flex-wrap mt-2">
                                <button
                                    onClick={handleStart}
                                    disabled={isLocked}
                                    className="btn-accent flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    {isLocked
                                        ? <Loader size={16} className="animate-spin" />
                                        : <Play size={16} />}
                                    Start Agent
                                </button>
                            </div>
                        </>
                    )}
                    {(isConnected || isPairing || isRegenerating) && (
                        <div className="flex gap-3 flex-wrap">
                            <button
                                onClick={handleStop}
                                disabled={isLocked}
                                className="btn-ghost flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {isLocked
                                    ? <Loader size={16} className="animate-spin" />
                                    : <Square size={16} />}
                                Stop Agent
                            </button>
                        </div>
                    )}
                </div>

                {message && <p className="mt-3 text-sm text-board-accent/80">{message}</p>}
            </div>

            {/* Right panel */}
            <div className="card flex flex-col items-center justify-center min-h-72">
                {isConnected && (
                    <div className="text-center">
                        <div className="w-20 h-20 rounded-full bg-green-500/10 border-2 border-green-500/30 flex items-center justify-center mx-auto mb-4">
                            <CheckCircle size={40} className="text-board-green" />
                        </div>
                        <h3 className="font-display text-xl text-white mb-2">Connected!</h3>
                        <p className="text-gray-400 text-sm">WhatsApp linked. Agent is live.</p>
                        <div className="mt-3 flex items-center justify-center gap-2 text-xs text-gray-500">
                            <div className="status-dot connected" />
                            Monitoring allowed contacts
                        </div>
                        {wsContactCount > 0 && (
                            <div className="mt-4 flex justify-center">
                                <ContactSyncBadge isVisible={true} wsContactCount={wsContactCount} userId={userId} />
                            </div>
                        )}
                    </div>
                )}

                {isPairing && activePairingCode && (
                    <div className="text-center w-full">
                        <p className="text-gray-400 text-sm mb-4">
                            Open WhatsApp → ⋮ Menu → Linked Devices → <b>Link with phone number</b>
                        </p>
                        <div className="mx-auto mb-5 mt-4 p-5 md:p-8 border-2 border-dashed border-board-accent/50 rounded-xl bg-board-800/50 inline-block">
                            <h2 className="text-3xl md:text-5xl tracking-[0.2em] font-mono font-bold text-board-accent">
                                {activePairingCode}
                            </h2>
                        </div>

                        {/* Contact sync progress during pairing */}
                        {wsContactCount > 0 && (
                            <div className="mb-4 flex justify-center">
                                <div className="flex items-center gap-2 text-xs text-gray-400 bg-board-700/60 rounded-lg px-3 py-2">
                                    <Users size={11} className="text-board-accent" />
                                    Syncing contacts…
                                    <span className="text-white font-semibold tabular-nums">
                                        {wsContactCount.toLocaleString()}
                                    </span>
                                    found so far
                                    <span className="w-1.5 h-1.5 rounded-full bg-board-accent animate-pulse" />
                                </div>
                            </div>
                        )}

                        <div className="flex items-center justify-center gap-2 text-xs text-gray-500 animate-pulse">
                            <Loader size={12} className="animate-spin" />
                            Waiting for you to enter code…
                        </div>
                        <div className="mt-5 flex justify-center border-t border-board-600/50 pt-4">
                            <button
                                onClick={handleRegenerate}
                                disabled={isLocked}
                                className="text-xs text-board-accent hover:underline flex items-center gap-1 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {isLocked
                                    ? <Loader size={12} className="animate-spin" />
                                    : <RefreshCw size={12} />}
                                Code expired or failed? Generate a new one
                            </button>
                        </div>
                    </div>
                )}

                {isRegenerating && (
                    <div className="text-center text-gray-400">
                        <RefreshCw size={40} className="animate-spin mx-auto mb-4 text-board-accent" />
                        <p className="text-sm font-medium">Generating fresh code…</p>
                        <p className="text-xs text-gray-600 mt-1">
                            Stopping old session and requesting a new pairing code
                        </p>
                    </div>
                )}

                {isPairing && !activePairingCode && !isRegenerating && (
                    <div className="text-center text-gray-400">
                        <Loader size={40} className="animate-spin mx-auto mb-4 text-board-accent" />
                        <p className="text-sm font-medium">Initializing WhatsApp session…</p>
                        <p className="text-xs text-gray-600 mt-1">
                            Pairing code will appear in a few seconds
                        </p>
                        <p className="text-xs text-gray-700 mt-1">
                            Syncing your contacts in the background…
                        </p>
                    </div>
                )}

                {isDisconnected && (
                    <div className="text-center text-gray-500">
                        <AlertCircle size={40} className="mx-auto mb-4 text-gray-600" />
                        <p className="text-sm">Agent offline</p>
                        <p className="text-xs mt-1 text-gray-600">Click Start Agent to begin pairing</p>
                    </div>
                )}
            </div>

            {/* How it works */}
            <div className="card lg:col-span-2">
                <h3 className="font-medium text-white mb-4">How it works</h3>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                    {[
                        { n: '1', t: 'Start Agent', d: 'Initializes your isolated WhatsApp session on the server' },
                        { n: '2', t: 'Authenticate', d: 'Use the 8-character pairing code to link WhatsApp' },
                        { n: '3', t: 'Pick Contacts', d: 'Go to Contacts → select who the agent can talk to. All your chat history contacts are synced automatically.' },
                        { n: '4', t: 'Stay Human', d: "Agent replies in your style, adapts to each contact's energy" },
                    ].map(item => (
                        <div key={item.n} className="flex gap-3">
                            <div className="w-7 h-7 rounded-full bg-board-accent/20 border border-board-accent/30 flex items-center justify-center flex-shrink-0 mt-0.5">
                                <span className="text-board-accent text-xs font-bold">{item.n}</span>
                            </div>
                            <div>
                                <p className="text-white text-sm font-medium">{item.t}</p>
                                <p className="text-gray-500 text-xs mt-0.5 leading-relaxed">{item.d}</p>
                            </div>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    )
}