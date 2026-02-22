import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
    LogOut, RefreshCw, Bot, Zap, Users,
    ChevronRight, Activity
} from 'lucide-react'
import { useAuthStore, fetchMe, getAnalytics, createWebSocket, startWaAgent } from '../lib/api'
import WhatsAppPanel from '../components/WhatsAppPanel'
import ContactsPanel from '../components/ContactsPanel'

const TAB_CONFIG = [
    { id: 'whatsapp', label: 'WhatsApp', icon: <Bot size={16} /> },
    { id: 'contacts', label: 'Contacts', icon: <Users size={16} /> },
]

export default function DashboardPage() {
    const navigate = useNavigate()
    const { user, logout, token } = useAuthStore()
    const [activeTab, setActiveTab] = useState('whatsapp')
    const [waStatus, setWaStatus] = useState('disconnected')
    const [qrCode, setQrCode] = useState(null)
    const [pairingCode, setPairingCode] = useState(null)
    const [analytics, setAnalytics] = useState(null)
    const [ws, setWs] = useState(null)

    useEffect(() => {
        if (!token) { navigate('/'); return }
        if (!user) {
            fetchMe().then(data => {
                useAuthStore.setState({ user: data.user })
                setWaStatus(data.whatsapp.status || 'disconnected')
            }).catch(() => { logout(); navigate('/') })
        }
        loadAnalytics()
    }, [token])

    // WebSocket connection
    useEffect(() => {
        if (!user?.id) return
        const socket = createWebSocket(user.id, handleWsMessage)
        setWs(socket)
        return () => socket.close()
    }, [user?.id])

    const handleWsMessage = useCallback((msg) => {
        if (msg.type === 'qr') {
            setQrCode(msg.data)
            setPairingCode(null)
            setWaStatus('pairing')
        } else if (msg.type === 'pairing_code') {
            setPairingCode(msg.data)
            setQrCode(null)
            setWaStatus('pairing')
        } else if (msg.type === 'status') {
            setWaStatus(msg.status)
            if (msg.status === 'connected') {
                setQrCode(null)
                setPairingCode(null)
                loadAnalytics()
            }
        } else if (msg.type === 'contacts_synced') {
            loadAnalytics()
        }
    }, [])

    const loadAnalytics = async () => {
        try {
            const data = await getAnalytics()
            setAnalytics(data)
        } catch (e) { }
    }

    const handleLogout = () => {
        if (ws) ws.close()
        logout()
        navigate('/')
    }

    const statusColor = {
        connected: 'text-board-green',
        pairing: 'text-board-accent',
        disconnected: 'text-gray-500',
    }[waStatus] || 'text-gray-500'

    const statusLabel = {
        connected: 'Connected',
        pairing: 'Pairing...',
        disconnected: 'Offline',
    }[waStatus] || 'Offline'

    return (
        <div className="min-h-screen flex flex-col">
            {/* Top navbar */}
            <header className="glass border-b border-board-600 sticky top-0 z-50">
                <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
                    {/* Logo */}
                    <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-lg bg-board-700 border border-board-accent/30 flex items-center justify-center">
                            <span className="text-lg">🤖</span>
                        </div>
                        <span className="font-display text-lg text-white">Orbit AI</span>
                    </div>

                    {/* Status pill */}
                    <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-board-700 border border-board-600">
                        <div className={`status-dot ${waStatus}`} />
                        <span className={`text-xs font-medium ${statusColor}`}>{statusLabel}</span>
                    </div>

                    {/* User + logout */}
                    <div className="flex items-center gap-3">
                        {user?.avatar_url && (
                            <img src={user.avatar_url} alt={user.name} className="w-8 h-8 rounded-full ring-2 ring-board-accent/20" />
                        )}
                        <span className="text-sm text-gray-300 hidden sm:block">{user?.name}</span>
                        <button onClick={handleLogout} className="p-2 text-gray-500 hover:text-board-accent rounded-lg hover:bg-board-700 transition-all">
                            <LogOut size={16} />
                        </button>
                    </div>
                </div>
            </header>

            {/* Stats bar */}
            {analytics && (
                <div className="bg-board-800 border-b border-board-600">
                    <div className="max-w-7xl mx-auto px-6 py-3 flex items-center gap-8 overflow-x-auto">
                        <StatChip icon={<Activity size={14} />} label="Messages" value={analytics.total_messages} />
                        <StatChip icon={<Users size={14} />} label="Contacts" value={analytics.total_contacts} />
                        <StatChip icon={<Zap size={14} />} label="Agent Active" value={analytics.allowed_contacts} suffix="contacts" />
                    </div>
                </div>
            )}

            {/* Main content */}
            <div className="flex-1 max-w-7xl mx-auto w-full px-6 py-8">
                {/* Tab nav */}
                <div className="flex gap-1 mb-8 bg-board-800 border border-board-600 p-1 rounded-2xl w-fit">
                    {TAB_CONFIG.map(tab => (
                        <button
                            key={tab.id}
                            onClick={() => setActiveTab(tab.id)}
                            className={`flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 ${activeTab === tab.id
                                ? 'bg-board-accent text-board-900'
                                : 'text-gray-400 hover:text-gray-200 hover:bg-board-700'
                                }`}
                        >
                            {tab.icon}
                            {tab.label}
                        </button>
                    ))}
                </div>

                {/* Tab content */}
                <div className="animate-slide-up">
                    {activeTab === 'whatsapp' && (
                        <WhatsAppPanel
                            waStatus={waStatus}
                            pairingCode={pairingCode}
                            onStatusChange={setWaStatus}
                            onClearPairingCode={() => setPairingCode(null)}
                            onAnalyticsRefresh={loadAnalytics}
                        />
                    )}
                    {activeTab === 'contacts' && (
                        <ContactsPanel waStatus={waStatus} />
                    )}
                </div>
            </div>
        </div>
    )
}

function StatChip({ icon, label, value, suffix = '' }) {
    return (
        <div className="flex items-center gap-2 text-sm whitespace-nowrap">
            <span className="text-board-accent/70">{icon}</span>
            <span className="text-gray-500">{label}:</span>
            <span className="text-white font-medium">{value} {suffix}</span>
        </div>
    )
}