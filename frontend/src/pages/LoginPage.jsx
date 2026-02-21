import React, { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authGoogleLogin, fetchMe, useAuthStore } from '../lib/api'

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || ''

export default function LoginPage() {
    const navigate = useNavigate()
    const { setAuth } = useAuthStore()
    const btnRef = useRef(null)
    const [error, setError] = useState('')
    const [loading, setLoading] = useState(false)

    useEffect(() => {
        if (!window.google) return
        window.google.accounts.id.initialize({
            client_id: GOOGLE_CLIENT_ID,
            callback: handleGoogleCredential,
            auto_select: false,
        })
        window.google.accounts.id.renderButton(btnRef.current, {
            theme: 'filled_black',
            size: 'large',
            text: 'continue_with',
            shape: 'rectangular',
            width: 280,
        })
    }, [])

    const handleGoogleCredential = async ({ credential }) => {
        setLoading(true)
        setError('')
        try {
            const data = await authGoogleLogin(credential)
            setAuth(data.user, data.access_token)
            navigate('/dashboard')
        } catch (e) {
            setError(e.response?.data?.detail || 'Login failed. Please try again.')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="min-h-screen flex flex-col items-center justify-center relative overflow-hidden">
            {/* Background grid */}
            <div className="absolute inset-0 opacity-5"
                style={{
                    backgroundImage: `linear-gradient(#F6C453 1px, transparent 1px), linear-gradient(90deg, #F6C453 1px, transparent 1px)`,
                    backgroundSize: '60px 60px'
                }}
            />

            {/* Glow orbs */}
            <div className="absolute top-1/4 left-1/2 -translate-x-1/2 w-96 h-96 bg-board-accent/10 rounded-full blur-3xl pointer-events-none" />
            <div className="absolute bottom-1/4 left-1/3 w-64 h-64 bg-board-blue/5 rounded-full blur-3xl pointer-events-none" />

            <div className="relative z-10 w-full max-w-sm mx-auto px-6 animate-fade-in">
                {/* Logo */}
                <div className="text-center mb-12">
                    <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-board-700 border border-board-accent/30 mb-5 glow-accent">
                        <span className="text-3xl">🤖</span>
                    </div>
                    <h1 className="font-display text-4xl text-white mb-2">Orbit AI</h1>
                    <p className="text-gray-400 text-sm leading-relaxed">
                        Your intelligent WhatsApp companion.<br />
                        <span className="text-board-accent">Always on. Always you.</span>
                    </p>
                </div>

                {/* Login card */}
                <div className="card glow-accent">
                    <h2 className="font-display text-xl text-white mb-1">Welcome back</h2>
                    <p className="text-gray-500 text-sm mb-7">
                        Sign in with Google to manage your agent.
                    </p>

                    {error && (
                        <div className="mb-4 p-3 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
                            {error}
                        </div>
                    )}

                    <div className="flex justify-center">
                        <div ref={btnRef} />
                    </div>

                    {loading && (
                        <div className="mt-4 text-center text-gray-400 text-sm animate-pulse">
                            Authenticating...
                        </div>
                    )}
                </div>

                {/* Footer */}
                <p className="text-center text-gray-600 text-xs mt-8">
                    By signing in, you agree to our{' '}
                    <a href="#" className="text-board-accent/70 hover:text-board-accent underline">Terms</a>
                    {' & '}
                    <a href="#" className="text-board-accent/70 hover:text-board-accent underline">Privacy</a>
                </p>
            </div>
        </div>
    )
}