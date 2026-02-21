import  { useState, useEffect, useRef } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { Play, Square, Smartphone, CheckCircle, AlertCircle, Loader} from 'lucide-react'
import { startWaAgent, stopWaAgent, useAuthStore } from '../lib/api'
import axios from 'axios'

/**
 * QR Poller — REST polling as the primary QR delivery mechanism.
 * Works even if WebSocket is flaky. Polls /api/whatsapp/qr every 2.5s.
 */
function useQRPoller(enabled, onQR, onConnected) {
    const timerRef = useRef(null)

    useEffect(() => {
        if (!enabled) {
            if (timerRef.current) clearInterval(timerRef.current)
            return
        }

        const poll = async () => {
            try {
                const token = useAuthStore.getState().token
                const res = await axios.get('/api/whatsapp/qr', {
                    headers: { Authorization: `Bearer ${token}` },
                    timeout: 5000,
                })
                if (res.data.has_qr) onQR(res.data.qr_code)
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

export default function WhatsAppPanel({ waStatus, qrCode: wsQrCode, onStatusChange, onAnalyticsRefresh }) {
    const [loading, setLoading] = useState(false)
    const [message, setMessage] = useState('')
    const [polledQR, setPolledQR] = useState(null)

    // Primary QR source: REST polling. Secondary: WebSocket push.
    const activeQR = polledQR || wsQrCode

    useQRPoller(
        waStatus === 'pairing',
        setPolledQR,
        () => { onStatusChange('connected'); onAnalyticsRefresh?.() }
    )

    useEffect(() => {
        if (waStatus === 'connected') setPolledQR(null)
    }, [waStatus])

    const isConnected = waStatus === 'connected'
    const isPairing = waStatus === 'pairing'
    const isDisconnected = waStatus === 'disconnected'

    const handleStart = async () => {
        setLoading(true)
        setMessage('')
        try {
            await startWaAgent()
            onStatusChange('pairing')
            setMessage('Starting... QR code will appear shortly.')
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
            setPolledQR(null)
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
                            {isConnected ? 'Agent Active' : isPairing ? 'Scanning QR Code' : 'Agent Offline'}
                        </p>
                        <p className="text-gray-500 text-xs mt-0.5">
                            {isConnected ? 'Auto-responding to allowed contacts'
                                : isPairing ? 'Open WhatsApp → Linked Devices → Link a Device'
                                    : 'Click Start Agent below to begin'}
                        </p>
                    </div>
                </div>

                {/* Actions */}
                <div className="flex gap-3 flex-wrap">
                    {!isConnected && !isPairing && (
                        <button onClick={handleStart} disabled={loading} className="btn-accent flex items-center gap-2">
                            {loading ? <Loader size={16} className="animate-spin" /> : <Play size={16} />}
                            Start Agent
                        </button>
                    )}
                    {(isConnected || isPairing) && (
                        <button onClick={handleStop} disabled={loading} className="btn-ghost flex items-center gap-2">
                            {loading ? <Loader size={16} className="animate-spin" /> : <Square size={16} />}
                            Stop Agent
                        </button>
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

                {isPairing && activeQR && (
                    <div className="text-center">
                        <p className="text-gray-400 text-sm mb-4">
                            Open WhatsApp → ⋮ Menu → Linked Devices → Link a Device
                        </p>
                        <div className="qr-frame mx-auto mb-4">
                            <QRCodeSVG value={activeQR} size={220} level="M" />
                        </div>
                        <div className="flex items-center justify-center gap-2 text-xs text-gray-500 animate-pulse">
                            <Loader size={12} className="animate-spin" />
                            Waiting for scan...
                        </div>
                    </div>
                )}

                {isPairing && !activeQR && (
                    <div className="text-center text-gray-400">
                        <Loader size={40} className="animate-spin mx-auto mb-4 text-board-accent" />
                        <p className="text-sm font-medium">Initializing WhatsApp session...</p>
                        <p className="text-xs text-gray-600 mt-1">QR code will appear in a few seconds</p>
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
                        { n: '2', t: 'Scan QR Code', d: 'Link WhatsApp from your phone — just like WhatsApp Web' },
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