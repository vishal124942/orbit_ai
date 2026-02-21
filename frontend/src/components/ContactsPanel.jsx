import { useState, useEffect, useMemo } from 'react'
import {
    Search, CheckSquare, Square, ChevronDown, ChevronUp,
    RefreshCw, Save, X, Sparkles, Loader,
    Check, AlertCircle, Phone, Sliders, Star, MessageSquare
} from 'lucide-react'
import {
    getContacts, getTopContacts, syncContacts, getSettings, updateAllowlist,
    updateContactTone, generateContactSoul
} from '../lib/api'
import clsx from 'clsx'

function jidToGradient(jid) {
    let hash = 0
    for (let i = 0; i < jid.length; i++) {
        hash = jid.charCodeAt(i) + ((hash << 5) - hash)
    }
    const h1 = Math.abs(hash % 360)
    const h2 = (h1 + 40) % 360
    return `linear-gradient(135deg, hsl(${h1}, 60%, 45%), hsl(${h2}, 70%, 35%))`
}

export default function ContactsPanel({ waStatus }) {
    const [contacts, setContacts] = useState([])
    const [topContacts, setTopContacts] = useState([])
    const [loading, setLoading] = useState(false)
    const [syncing, setSyncing] = useState(false)
    const [saving, setSaving] = useState(false)
    const [search, setSearch] = useState('')
    const [viewMode, setViewMode] = useState('smart') // 'smart' | 'all'
    const [selectedJids, setSelectedJids] = useState(new Set())
    const [message, setMessage] = useState({ text: '', type: '' })
    const [expandedContact, setExpandedContact] = useState(null)
    const [generatingFor, setGeneratingFor] = useState(null)
    const [bulkGenerating, setBulkGenerating] = useState(false)
    const [bulkProgress, setBulkProgress] = useState({ done: 0, total: 0 })

    const isAgentRunning = waStatus === 'connected'

    useEffect(() => {
        loadAll()
    }, [])

    useEffect(() => {
        if (isAgentRunning) loadTopContacts()
    }, [isAgentRunning])

    const loadAll = async () => {
        setLoading(true)
        try {
            await Promise.all([loadContacts(), loadCurrentAllowlist(), loadTopContacts()])
        } finally {
            setLoading(false)
        }
    }

    const loadContacts = async () => {
        try {
            const data = await getContacts()
            setContacts(data.contacts || [])
        } catch (e) { }
    }

    const loadTopContacts = async () => {
        try {
            const data = await getTopContacts(10)
            setTopContacts(data.contacts || [])
        } catch (e) { }
    }

    const loadCurrentAllowlist = async () => {
        try {
            const data = await getSettings()
            setSelectedJids(new Set(data.allowed_jids || []))
        } catch (e) { }
    }

    const handleSync = async () => {
        if (!isAgentRunning) { showMessage('Connect WhatsApp first', 'error'); return }
        setSyncing(true)
        try {
            await syncContacts()
            showMessage('Syncing contacts...', 'success')
            setTimeout(() => { loadContacts(); loadTopContacts() }, 3000)
            setTimeout(() => { loadContacts(); loadTopContacts() }, 7000)
        } catch (e) {
            showMessage(e.response?.data?.detail || 'Sync failed', 'error')
        } finally {
            setSyncing(false)
        }
    }

    const handleSave = async () => {
        setSaving(true)
        try {
            await updateAllowlist(Array.from(selectedJids))
            showMessage(
                selectedJids.size === 0
                    ? 'Agent will respond to all inbound messages.'
                    : `Saved! Agent will auto-profile ${selectedJids.size} contact(s).`,
                'success'
            )
            await loadCurrentAllowlist()
        } catch (e) {
            showMessage('Failed to save', 'error')
        } finally {
            setSaving(false)
        }
    }

    const toggleContact = (jid) => {
        setSelectedJids(prev => {
            const next = new Set(prev)
            if (next.has(jid)) next.delete(jid)
            else next.add(jid)
            return next
        })
    }

    const selectAll = () => {
        // Select all currently displayed contacts
        setSelectedJids(prev => {
            const next = new Set(prev)
            displayedContacts.forEach(c => next.add(c.jid))
            return next
        })
    }

    const clearAll = () => {
        // Clear ALL selections (not just visible)
        setSelectedJids(new Set())
    }

    const handleBulkAutoProfile = async () => {
        // Only run for contacts that actually have conversation history
        const candidates = displayedContacts.filter(c =>
            selectedJids.has(c.jid) && !c.is_group && c.has_history
        )
        if (candidates.length === 0) {
            showMessage('No selected contacts have enough chat history yet. Chat with them first!', 'error')
            return
        }
        setBulkGenerating(true)
        setBulkProgress({ done: 0, total: candidates.length })
        let successCount = 0
        for (let i = 0; i < candidates.length; i++) {
            setBulkProgress({ done: i, total: candidates.length })
            try {
                await generateContactSoul(candidates[i].jid)
                successCount++
            } catch (e) { }
        }
        setBulkGenerating(false)
        setBulkProgress({ done: 0, total: 0 })
        showMessage(`Auto-Profile done for ${successCount}/${candidates.length} contacts.`, 'success')
    }

    const handleGenerateSoul = async (contact) => {
        setGeneratingFor(contact.jid)
        try {
            await generateContactSoul(contact.jid)
            showMessage(`AI soul generated for ${contact.name}!`, 'success')
        } catch (e) {
            showMessage(e.response?.data?.detail || 'Need at least 5 messages with this contact first.', 'error')
        } finally {
            setGeneratingFor(null)
        }
    }

    const showMessage = (text, type = 'info') => {
        setMessage({ text, type })
        setTimeout(() => setMessage({ text: '', type: '' }), 5000)
    }

    // Smart mode: top 10 active contacts; All mode: full list
    const baseContacts = viewMode === 'smart' ? topContacts : contacts

    const displayedContacts = useMemo(() => {
        let list = baseContacts
        if (search) {
            const q = search.toLowerCase()
            list = list.filter(c =>
                c.name?.toLowerCase().includes(q) ||
                c.number?.includes(q) ||
                c.jid?.includes(q)
            )
        }
        return list
    }, [baseContacts, search])

    const allVisibleSelected = displayedContacts.length > 0 && displayedContacts.every(c => selectedJids.has(c.jid))
    const profileableCount = displayedContacts.filter(c => selectedJids.has(c.jid) && c.has_history).length

    return (
        <div className="space-y-4">
            {/* Header */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div>
                    <h2 className="font-display text-xl text-white">Contacts</h2>
                    <p className="text-gray-500 text-sm mt-0.5">
                        Agent responds to <span className="text-board-accent font-medium">all inbound messages</span>
                        {selectedJids.size > 0 && <> · <span className="text-board-accent font-medium">{selectedJids.size}</span> marked for auto-profiling</>}
                    </p>
                </div>
                <div className="flex gap-2 flex-wrap justify-end">
                    <button
                        onClick={handleSync}
                        disabled={syncing || !isAgentRunning}
                        className="btn-ghost flex items-center gap-2 text-sm"
                        title={!isAgentRunning ? 'Connect WhatsApp first' : 'Sync contacts'}
                    >
                        <RefreshCw size={14} className={syncing ? 'animate-spin' : ''} />
                        {syncing ? 'Syncing...' : 'Sync'}
                    </button>
                    {isAgentRunning && selectedJids.size > 0 && profileableCount > 0 && (
                        <button
                            onClick={handleBulkAutoProfile}
                            disabled={bulkGenerating}
                            className="btn-ghost flex items-center gap-2 text-sm text-board-accent border border-board-accent/30 hover:bg-board-accent/10"
                            title="Generate AI Auto-Profile for selected contacts with history"
                        >
                            {bulkGenerating
                                ? <><Loader size={14} className="animate-spin" /> Profiling {bulkProgress.done}/{bulkProgress.total}...</>
                                : <><Sparkles size={14} /> Auto-Profile ({profileableCount})</>
                            }
                        </button>
                    )}
                    <button onClick={handleSave} disabled={saving} className="btn-accent flex items-center gap-2 text-sm">
                        {saving ? <Loader size={14} className="animate-spin" /> : <Save size={14} />}
                        Save
                    </button>
                </div>
            </div>

            {/* Info banner */}
            <div className="flex items-start gap-2 p-3 rounded-xl bg-board-accent/5 border border-board-accent/15 text-sm text-board-accent/80">
                <MessageSquare size={15} className="mt-0.5 flex-shrink-0" />
                <span>
                    <strong>Agent responds to everyone</strong> — the selection below is for <strong>AI Auto-Profiling</strong> only (personalised personality per contact).
                    Start with <strong>Smart mode</strong> to pick your top 10 most-active contacts.
                </span>
            </div>

            {/* Toast */}
            {message.text && (
                <div className={clsx(
                    'p-3 rounded-xl text-sm flex items-center gap-2',
                    message.type === 'success' && 'bg-green-500/10 border border-green-500/20 text-green-400',
                    message.type === 'error' && 'bg-red-500/10 border border-red-500/20 text-red-400',
                    message.type === 'info' && 'bg-board-accent/10 border border-board-accent/20 text-board-accent',
                )}>
                    {message.type === 'success' && <Check size={14} />}
                    {message.type === 'error' && <AlertCircle size={14} />}
                    {message.text}
                </div>
            )}

            {/* View mode + search */}
            <div className="flex flex-col sm:flex-row gap-3">
                <div className="flex gap-1 bg-board-800 border border-board-600 p-1 rounded-xl">
                    <button
                        onClick={() => setViewMode('smart')}
                        className={clsx(
                            'px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-all',
                            viewMode === 'smart' ? 'bg-board-accent text-board-900' : 'text-gray-400 hover:text-gray-200'
                        )}
                    >
                        <Star size={11} />
                        Smart Top 10
                    </button>
                    <button
                        onClick={() => setViewMode('all')}
                        className={clsx(
                            'px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
                            viewMode === 'all' ? 'bg-board-accent text-board-900' : 'text-gray-400 hover:text-gray-200'
                        )}
                    >
                        All ({contacts.length})
                    </button>
                </div>

                <div className="relative flex-1">
                    <Search size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-gray-500" />
                    <input
                        type="text"
                        className="input pl-10"
                        placeholder="Search contacts..."
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                    />
                    {search && (
                        <button onClick={() => setSearch('')} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300">
                            <X size={14} />
                        </button>
                    )}
                </div>
            </div>

            {/* Select all bar */}
            <div className="flex items-center justify-between px-3 py-2 bg-board-800/60 border border-board-600/50 rounded-xl">
                <div className="flex items-center gap-3">
                    <button
                        onClick={selectAll}
                        disabled={displayedContacts.length === 0}
                        className="flex items-center gap-2 text-sm text-gray-400 hover:text-white transition-colors disabled:opacity-40"
                    >
                        <CheckSquare size={15} />
                        Select all
                    </button>
                    {selectedJids.size > 0 && (
                        <>
                            <span className="text-gray-700">·</span>
                            <button
                                onClick={clearAll}
                                className="flex items-center gap-2 text-sm text-red-400/70 hover:text-red-400 transition-colors"
                            >
                                <X size={13} />
                                Clear all ({selectedJids.size})
                            </button>
                        </>
                    )}
                </div>
                <span className="text-xs text-gray-600">
                    {viewMode === 'smart'
                        ? `Top ${displayedContacts.length} most active`
                        : `${displayedContacts.length} of ${contacts.length}`
                    }
                </span>
            </div>

            {/* Contact list */}
            <div className="card p-2">
                {loading && (
                    <div className="py-16 text-center">
                        <Loader size={28} className="animate-spin mx-auto mb-3 text-board-accent" />
                        <p className="text-gray-500 text-sm">Loading contacts...</p>
                    </div>
                )}

                {!loading && displayedContacts.length === 0 && (
                    <div className="py-16 text-center">
                        <div className="w-16 h-16 rounded-2xl bg-board-700 border border-board-600 flex items-center justify-center mx-auto mb-4">
                            {isAgentRunning ? <Star size={28} className="text-gray-600" /> : <Phone size={28} className="text-gray-600" />}
                        </div>
                        <p className="text-gray-400 font-medium">
                            {viewMode === 'smart'
                                ? (isAgentRunning ? 'No active contacts yet' : 'Agent offline')
                                : 'No contacts found'
                            }
                        </p>
                        <p className="text-gray-600 text-sm mt-1 max-w-xs mx-auto">
                            {viewMode === 'smart' && isAgentRunning
                                ? 'Chat with some contacts first — they\'ll appear here ranked by activity.'
                                : isAgentRunning
                                    ? 'Click Sync to pull your WhatsApp contacts.'
                                    : 'Start the agent from the WhatsApp tab first.'}
                        </p>
                        {isAgentRunning && (
                            <button onClick={handleSync} disabled={syncing} className="btn-accent mt-4 text-sm inline-flex items-center gap-2">
                                <RefreshCw size={14} className={syncing ? 'animate-spin' : ''} />
                                Sync Now
                            </button>
                        )}
                    </div>
                )}

                <div className="space-y-0.5">
                    {displayedContacts.map((contact, idx) => (
                        <ContactRow
                            key={contact.jid}
                            contact={contact}
                            rank={viewMode === 'smart' ? idx + 1 : null}
                            isSelected={selectedJids.has(contact.jid)}
                            isExpanded={expandedContact === contact.jid}
                            isGenerating={generatingFor === contact.jid}
                            onToggle={() => toggleContact(contact.jid)}
                            onExpand={() => setExpandedContact(prev => prev === contact.jid ? null : contact.jid)}
                            onGenerateSoul={() => handleGenerateSoul(contact)}
                            isAgentRunning={isAgentRunning}
                        />
                    ))}
                </div>
            </div>
        </div>
    )
}

function ContactRow({ contact, rank, isSelected, isExpanded, isGenerating, onToggle, onExpand, onGenerateSoul, isAgentRunning }) {
    const [toneInput, setToneInput] = useState(contact.custom_tone || '')
    const [saving, setSaving] = useState(false)

    const initials = contact.name
        ? contact.name.replace(/^\+/, '').split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2)
        : '?'

    const handleSaveTone = async () => {
        setSaving(true)
        try {
            await updateContactTone({ contact_jid: contact.jid, custom_tone: toneInput })
        } catch (e) { }
        finally { setSaving(false) }
    }

    const msgCount = contact.msg_count || 0
    const hasHistory = contact.has_history || false
    const lastMsg = contact.last_msg
    const lastMsgLabel = lastMsg ? (() => {
        // Ensure UTC parsing — DB stores UTC but without 'Z' JS treats it as local time
        const rawStr = lastMsg.endsWith('Z') ? lastMsg : lastMsg + 'Z'
        const d = new Date(rawStr.replace(' ', 'T'))
        const now = new Date()
        const diffH = (now - d) / 3600000
        if (diffH < 1) return 'just now'
        if (diffH < 24) return `${Math.floor(diffH)}h ago`
        const diffD = Math.floor(diffH / 24)
        if (diffD < 7) return `${diffD}d ago`
        return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
    })() : null

    return (
        <div className={clsx(
            'rounded-xl transition-all duration-150',
            isSelected ? 'bg-board-accent/5 border border-board-accent/15' : 'border border-transparent hover:bg-board-700/50'
        )}>
            <div className="flex items-center gap-3 p-3">
                {/* Rank badge or checkbox */}
                <div className="flex-shrink-0 flex items-center gap-1.5">
                    {rank && (
                        <span className="text-[10px] text-gray-600 font-mono w-4 text-right">{rank}</span>
                    )}
                    <div
                        className="cursor-pointer"
                        onClick={e => { e.stopPropagation(); onToggle() }}
                    >
                        {isSelected
                            ? <CheckSquare size={18} className="text-board-accent" />
                            : <Square size={18} className="text-gray-600 hover:text-gray-400" />
                        }
                    </div>
                </div>

                {/* Avatar */}
                <div
                    className="w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-bold text-white shadow-sm"
                    style={{ background: jidToGradient(contact.jid) }}
                >
                    {initials}
                </div>

                {/* Info */}
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                        <p className="text-white text-sm font-medium truncate">{contact.name || contact.number}</p>
                        {lastMsgLabel && (
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-500/10 text-green-400 border border-green-500/20">
                                <MessageSquare size={8} />
                                {lastMsgLabel}
                            </span>
                        )}
                        {contact.custom_tone && (
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-board-accent/10 text-board-accent border border-board-accent/20">
                                <Sliders size={8} />
                                profiled
                            </span>
                        )}
                    </div>
                    <p className="text-gray-500 text-xs truncate">{contact.number}</p>
                </div>

                {/* Expand */}
                <button
                    onClick={e => { e.stopPropagation(); onExpand() }}
                    className="p-1.5 text-gray-600 hover:text-gray-300 rounded-lg hover:bg-board-600/50 transition-all"
                >
                    {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </button>
            </div>

            {/* Expanded panel */}
            {isExpanded && (
                <div className="px-4 pb-4 pt-1 space-y-3 border-t border-board-600/50">
                    <div>
                        <label className="text-xs text-gray-500 mb-1.5 block">Custom tone for this contact</label>
                        <input
                            type="text"
                            value={toneInput}
                            onChange={e => setToneInput(e.target.value)}
                            placeholder="e.g., 'formal', 'heavy banter', 'roast mode'"
                            className="input text-sm"
                        />
                    </div>
                    <div className="flex gap-2">
                        <button onClick={handleSaveTone} disabled={saving} className="btn-accent text-xs py-2 px-3 flex items-center gap-1.5">
                            {saving ? <Loader size={12} className="animate-spin" /> : <Save size={12} />}
                            Save Tone
                        </button>
                        {isAgentRunning && (
                            <button
                                onClick={onGenerateSoul}
                                disabled={isGenerating || !hasHistory}
                                title={!hasHistory ? 'Need at least 5 messages with this contact first' : 'Generate AI personality profile'}
                                className={clsx(
                                    'btn-ghost text-xs py-2 px-3 flex items-center gap-1.5',
                                    !hasHistory && 'opacity-40 cursor-not-allowed'
                                )}
                            >
                                {isGenerating ? <Loader size={12} className="animate-spin" /> : <Sparkles size={12} />}
                                {hasHistory ? 'AI Auto-Profile' : 'Need more chats'}
                            </button>
                        )}
                    </div>
                    {!hasHistory && (
                        <p className="text-xs text-gray-600">Chat with {contact.name} on WhatsApp to build history, then come back to generate their profile.</p>
                    )}
                </div>
            )}
        </div>
    )
}