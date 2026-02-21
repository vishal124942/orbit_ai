import React, { useState, useEffect } from 'react'
import { Save, Loader, AlertCircle, Check, RefreshCw } from 'lucide-react'
import { getSettings, updateSettings } from '../lib/api'

const MODELS = ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo']
const DEFAULT_INTENTS = ['money', 'emergency']

export default function SettingsPanel() {
    const [settings, setSettings] = useState(null)
    const [loading, setLoading] = useState(true)
    const [saving, setSaving] = useState(false)
    const [message, setMessage] = useState({ text: '', type: '' })

    // Form state
    const [model, setModel] = useState('gpt-4o')
    const [temperature, setTemperature] = useState(0.75)
    const [debounce, setDebounce] = useState(8)
    const [autoRespond, setAutoRespond] = useState(true)
    const [ttsEnabled, setTtsEnabled] = useState(true)
    const [soulOverride, setSoulOverride] = useState('')
    const [handoffIntents, setHandoffIntents] = useState(DEFAULT_INTENTS)
    const [intentInput, setIntentInput] = useState('')

    useEffect(() => {
        loadSettings()
    }, [])

    const loadSettings = async () => {
        setLoading(true)
        try {
            const data = await getSettings()
            const s = data.settings || {}
            setSettings(s)
            setModel(s.model || 'gpt-4o')
            setTemperature(s.temperature ?? 0.75)
            setDebounce(s.debounce_seconds ?? 8)
            setAutoRespond(s.auto_respond !== 0)
            setTtsEnabled(s.tts_enabled !== 0)
            setSoulOverride(s.soul_override || '')
            setHandoffIntents(s.handoff_intents || DEFAULT_INTENTS)
        } catch (e) {
            showMessage('Failed to load settings', 'error')
        } finally {
            setLoading(false)
        }
    }

    const handleSave = async () => {
        setSaving(true)
        try {
            await updateSettings({
                model,
                temperature,
                debounce_seconds: debounce,
                auto_respond: autoRespond,
                tts_enabled: ttsEnabled,
                soul_override: soulOverride || null,
                handoff_intents: handoffIntents,
            })
            showMessage('Settings saved successfully!', 'success')
        } catch (e) {
            showMessage(e.response?.data?.detail || 'Failed to save', 'error')
        } finally {
            setSaving(false)
        }
    }

    const showMessage = (text, type) => {
        setMessage({ text, type })
        setTimeout(() => setMessage({ text: '', type: '' }), 4000)
    }

    const addIntent = () => {
        const v = intentInput.trim().toLowerCase()
        if (v && !handoffIntents.includes(v)) {
            setHandoffIntents(prev => [...prev, v])
            setIntentInput('')
        }
    }

    if (loading) {
        return (
            <div className="flex items-center justify-center py-20">
                <Loader size={32} className="animate-spin text-board-accent" />
            </div>
        )
    }

    return (
        <div className="max-w-2xl space-y-6">
            <div className="card">
                <div className="flex items-center justify-between mb-6">
                    <div>
                        <h2 className="font-display text-xl text-white">Agent Settings</h2>
                        <p className="text-gray-500 text-sm mt-1">Configure how your agent behaves globally</p>
                    </div>
                    <button onClick={loadSettings} className="p-2 text-gray-500 hover:text-board-accent rounded-lg hover:bg-board-700 transition-all">
                        <RefreshCw size={16} />
                    </button>
                </div>

                {message.text && (
                    <div className={`mb-5 p-3 rounded-xl text-sm flex items-center gap-2 ${message.type === 'success'
                            ? 'bg-green-500/10 border border-green-500/20 text-green-400'
                            : 'bg-red-500/10 border border-red-500/20 text-red-400'
                        }`}>
                        {message.type === 'success' ? <Check size={14} /> : <AlertCircle size={14} />}
                        {message.text}
                    </div>
                )}

                <div className="space-y-6">
                    {/* Model */}
                    <div>
                        <label className="block text-sm font-medium text-gray-300 mb-2">AI Model</label>
                        <select
                            value={model}
                            onChange={e => setModel(e.target.value)}
                            className="input"
                        >
                            {MODELS.map(m => <option key={m} value={m}>{m}</option>)}
                        </select>
                    </div>

                    {/* Temperature */}
                    <div>
                        <div className="flex justify-between mb-2">
                            <label className="text-sm font-medium text-gray-300">Temperature</label>
                            <span className="text-sm text-board-accent font-mono">{temperature}</span>
                        </div>
                        <input
                            type="range" min="0" max="1" step="0.05"
                            value={temperature}
                            onChange={e => setTemperature(parseFloat(e.target.value))}
                            className="w-full accent-board-accent"
                        />
                        <div className="flex justify-between text-xs text-gray-600 mt-1">
                            <span>Focused</span>
                            <span>Creative</span>
                        </div>
                    </div>

                    {/* Debounce */}
                    <div>
                        <div className="flex justify-between mb-2">
                            <label className="text-sm font-medium text-gray-300">Response Delay (seconds)</label>
                            <span className="text-sm text-board-accent font-mono">{debounce}s</span>
                        </div>
                        <input
                            type="range" min="2" max="30" step="1"
                            value={debounce}
                            onChange={e => setDebounce(parseInt(e.target.value))}
                            className="w-full accent-board-accent"
                        />
                        <p className="text-xs text-gray-600 mt-1">
                            Wait time after last message before responding. Batches multiple messages.
                        </p>
                    </div>

                    {/* Toggles */}
                    <div className="space-y-3">
                        <ToggleRow
                            label="Auto Respond"
                            description="Agent automatically replies to allowed contacts"
                            value={autoRespond}
                            onChange={setAutoRespond}
                        />
                        <ToggleRow
                            label="Voice Notes (TTS)"
                            description="Agent can send voice note replies when appropriate"
                            value={ttsEnabled}
                            onChange={setTtsEnabled}
                        />
                    </div>

                    {/* Handoff intents */}
                    <div>
                        <label className="block text-sm font-medium text-gray-300 mb-2">
                            Handoff Intents
                        </label>
                        <p className="text-xs text-gray-500 mb-3">
                            When detected, agent stops and alerts the operator. Non-negotiable safety net.
                        </p>
                        <div className="flex flex-wrap gap-2 mb-3">
                            {handoffIntents.map(intent => (
                                <span
                                    key={intent}
                                    className="tag bg-red-500/10 text-red-400 border border-red-500/20 cursor-pointer"
                                    onClick={() => setHandoffIntents(prev => prev.filter(i => i !== intent))}
                                >
                                    {intent} <span className="text-red-600">×</span>
                                </span>
                            ))}
                        </div>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                value={intentInput}
                                onChange={e => setIntentInput(e.target.value)}
                                onKeyDown={e => e.key === 'Enter' && addIntent()}
                                placeholder="Add intent (e.g., legal, medical)"
                                className="input flex-1"
                            />
                            <button onClick={addIntent} className="btn-ghost text-sm px-4">Add</button>
                        </div>
                    </div>

                    {/* Soul override */}
                    <div>
                        <label className="block text-sm font-medium text-gray-300 mb-2">
                            Global Personality Override
                        </label>
                        <p className="text-xs text-gray-500 mb-3">
                            Replace the default soul.md for all contacts. Leave empty to use default.
                            Per-contact profiles (set in Contacts tab) override this.
                        </p>
                        <textarea
                            value={soulOverride}
                            onChange={e => setSoulOverride(e.target.value)}
                            placeholder="## My Agent Soul&#10;&#10;You are a professional assistant...&#10;(Leave empty to use soul.md)"
                            rows={8}
                            className="input font-mono text-xs leading-relaxed resize-y"
                        />
                    </div>
                </div>

                {/* Save */}
                <div className="mt-6 pt-5 border-t border-board-600 flex justify-end">
                    <button onClick={handleSave} disabled={saving} className="btn-accent flex items-center gap-2">
                        {saving ? <Loader size={16} className="animate-spin" /> : <Save size={16} />}
                        Save Settings
                    </button>
                </div>
            </div>

            {/* Info card */}
            <div className="card border-board-accent/20">
                <h3 className="text-sm font-medium text-board-accent mb-3">🧠 Self-Learning AI Profiles</h3>
                <p className="text-sm text-gray-400 leading-relaxed">
                    Use the <strong className="text-white">"AI Auto-Profile"</strong> button in the Contacts tab to let the agent analyze
                    your conversation history with any contact and automatically generate a custom personality profile.
                    This makes the agent adapt its tone, language mix, and humor style to match how you talk with each person.
                </p>
            </div>
        </div>
    )
}

function ToggleRow({ label, description, value, onChange }) {
    return (
        <div className="flex items-center justify-between p-3 rounded-xl bg-board-700">
            <div>
                <p className="text-sm text-white font-medium">{label}</p>
                <p className="text-xs text-gray-500">{description}</p>
            </div>
            <button
                onClick={() => onChange(!value)}
                className={`relative w-11 h-6 rounded-full transition-all duration-200 ${value ? 'bg-board-accent' : 'bg-board-500'
                    }`}
            >
                <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-all duration-200 ${value ? 'left-5.5' : 'left-0.5'
                    }`} style={{ left: value ? '22px' : '2px' }} />
            </button>
        </div>
    )
}