import { useState, useEffect, useRef } from 'react'
import { Play, Square, Smartphone, CheckCircle, AlertCircle, Loader } from 'lucide-react'
import { startWaAgent, stopWaAgent, useAuthStore } from '../lib/api'
import axios from 'axios'

/**
 * Pairing Code Poller — REST polling as the primary delivery mechanism.
 * Works even if WebSocket is flaky. Polls /api/whatsapp/pairing-code every 2.5s.
 */
function usePairingCodePoller(enabled, onPairingCode, onConnected) {
    const timerRef = useRef(null)

    useEffect(() => {
        if (!enabled) {
            if (timerRef.current) clearInterval(timerRef.current)
            return
        }

        const poll = async () => {
            try {
                const token = useAuthStore.getState().token
                const res = await axios.get('/api/whatsapp/pairing-code', {
                    headers: { Authorization: `Bearer ${token}` },
                    timeout: 5000,
                })
                if (res.data.has_pairing_code) onPairingCode(res.data.pairing_code)
                if (res.data.status === 'connected') {
                    onConnected()
                    clearInterval(timerRef.current)
                }
            } catch (_) { }
        }

        poll()
        timerRef.current = setInterval(poll, 2500)
        return () => clearInterval(timerRef.current)
    }, [enabled])
}

export default function WhatsAppPanel({ waStatus, pairingCode: wsPairingCode, onStatusChange, onAnalyticsRefresh }) {
    const [loading, setLoading] = useState(false)
    const [message, setMessage] = useState('')
    const [polledPairingCode, setPolledPairingCode] = useState(null)
    const [phoneNumber, setPhoneNumber] = useState('')

    // Primary source: REST polling. Secondary: WebSocket push.
    const activePairingCode = polledPairingCode || wsPairingCode

    usePairingCodePoller(
        waStatus === 'pairing',
        setPolledPairingCode,
        () => { onStatusChange('connected'); onAnalyticsRefresh?.() }
    )

    useEffect(() => {
        if (waStatus === 'connected') {
            setPolledPairingCode(null)
        }
    }, [waStatus])

    const isConnected = waStatus === 'connected'
    const isPairing = waStatus === 'pairing'
    const isDisconnected = waStatus === 'disconnected'

    // Format phone number to exactly +91-XXXXXXXXXX
    const handlePhoneChange = (e) => {
        let val = e.target.value;
        if (!val) {
            setPhoneNumber('');
            return;
        }
        // Extract digits only
        let digits = val.replace(/\D/g, '');

        // Auto-prefix with 91 if the user starts typing an Indian local number (e.g. 7, 8, 9)
        if (digits.length > 0 && digits.length <= 10 && !digits.startsWith('91')) {
            digits = '91' + digits;
        }

        // Format as +CC-XXXXXXXXXX
        if (digits.length > 2) {
            setPhoneNumber(`+${digits.substring(0, 2)}-${digits.substring(2, 12)}`);
        } else if (digits.length > 0) {
            setPhoneNumber(`+${digits}`);
        } else {
            setPhoneNumber('');
        }
    }

    const handleStart = async () => {
        setLoading(true)
        setMessage('')
        if (!phoneNumber.trim() || phoneNumber.replace(/\D/g, '').length < 10) {
            setMessage('Please enter a valid phone number with your country code (e.g. +917310885365).')
            setLoading(false)
            return
        }
        try {
            await startWaAgent(phoneNumber.trim())
            onStatusChange('pairing')
            setMessage('Starting... Pairing code will appear shortly.')
        } catch (e) {
            setMessage(e.response?.data?.detail || 'Failed to start agent')
        } finally {
            setLoading(false)
        }
    }

    const handleStop = async () => {
        setLoading(true)
        try {
            await stopWaAgent()
            onStatusChange('disconnected')
            setPolledPairingCode(null)
            setMessage('Agent stopped.')
            onAnalyticsRefresh?.()
        } catch (e) {
            setMessage(e.response?.data?.detail || 'Failed to stop')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Connection card */}
            <div className="card">
                <div className="flex items-center justify-between mb-6">
                    <div>
                        <h2 className="font-display text-xl text-white">WhatsApp Agent</h2>
                        <p className="text-gray-500 text-sm mt-1">Connect your number to activate</p>
                    </div>
                    <div className={`p-3 rounded-xl ${isConnected ? 'bg-green-500/10' : isPairing ? 'bg-board-accent/10' : 'bg-board-700'
                        }`}>
                        <Smartphone size={24} className={
                            isConnected ? 'text-board-green' : isPairing ? 'text-board-accent' : 'text-gray-500'
                        } />
                    </div>
                </div>

                {/* Status pill */}
                <div className="flex items-center gap-3 p-4 rounded-xl bg-board-700 mb-6">
                    <div className={`status-dot ${waStatus}`} />
                    <div>
                        <p className="text-white font-medium">
                            {isConnected ? 'Agent Active' : isPairing ? 'Fetching Pairing Code' : 'Agent Offline'}
                        </p>
                        <p className="text-gray-500 text-xs mt-0.5">
                            {isConnected ? 'Auto-responding to allowed contacts'
                                : isPairing ? 'Open WhatsApp → Linked Devices → Link with phone number'
                                    : 'Click Start Agent below to begin'}
                        </p>
                    </div>
                </div>

                {/* Actions */}
                <div className="flex flex-col gap-4">
                    {!isConnected && !isPairing && (
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
                                <button onClick={handleStart} disabled={loading} className="btn-accent flex items-center gap-2">
                                    {loading ? <Loader size={16} className="animate-spin" /> : <Play size={16} />}
                                    Start Agent
                                </button>
                            </div>
                        </>
                    )}
                    {(isConnected || isPairing) && (
                        <div className="flex gap-3 flex-wrap">
                            <button onClick={handleStop} disabled={loading} className="btn-ghost flex items-center gap-2">
                                {loading ? <Loader size={16} className="animate-spin" /> : <Square size={16} />}
                                Stop Agent
                            </button>
                        </div>
                    )}
                </div>

                {message && <p className="mt-3 text-sm text-board-accent/80">{message}</p>}
            </div>

            {/* QR / Connected display */}
            <div className="card flex flex-col items-center justify-center min-h-72">
                {isConnected && (
                    <div className="text-center">
                        <div className="w-20 h-20 rounded-full bg-green-500/10 border-2 border-green-500/30 flex items-center justify-center mx-auto mb-4">
                            <CheckCircle size={40} className="text-board-green" />
                        </div>
                        <h3 className="font-display text-xl text-white mb-2">Connected!</h3>
                        <p className="text-gray-400 text-sm">WhatsApp linked. Agent is live.</p>
                        <div className="mt-4 flex items-center justify-center gap-2 text-xs text-gray-500">
                            <div className="status-dot connected" />
                            Monitoring allowed contacts
                        </div>
                    </div>
                )}

                {isPairing && activePairingCode && (
                    <div className="text-center">
                        <p className="text-gray-400 text-sm mb-4">
                            Open WhatsApp → ⋮ Menu → Linked Devices → <b>Link with phone number</b>
                        </p>
                        <div className="mx-auto mb-6 mt-4 p-5 md:p-8 border-2 border-dashed border-board-accent/50 rounded-xl bg-board-800/50 inline-block">
                            <h2 className="text-3xl md:text-5xl tracking-[0.2em] font-mono font-bold text-board-accent">
                                {activePairingCode}
                            </h2>
                        </div>
                        <div className="flex items-center justify-center gap-2 text-xs text-gray-500 animate-pulse">
                            <Loader size={12} className="animate-spin" />
                            Waiting for you to enter code...
                        </div>
                    </div>
                )}

                {isPairing && !activePairingCode && (
                    <div className="text-center text-gray-400">
                        <Loader size={40} className="animate-spin mx-auto mb-4 text-board-accent" />
                        <p className="text-sm font-medium">Initializing WhatsApp session...</p>
                        <p className="text-xs text-gray-600 mt-1">
                            Pairing code will appear in a few seconds
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
                        { n: '3', t: 'Pick Contacts', d: 'Go to Contacts → select who the agent can talk to' },
                        { n: '4', t: 'Stay Human', d: 'Agent replies in your style, adapts to each contact\'s energy' },
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