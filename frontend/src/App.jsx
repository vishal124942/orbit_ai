import React, { useEffect } from 'react'
import { Routes, Route, Navigate, useNavigate } from 'react-router-dom'
import { useAuthStore, fetchMe } from './lib/api'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'

function AuthGuard({ children }) {
    const { token, isLoading } = useAuthStore()

    if (!token) return <Navigate to="/" replace />
    if (isLoading) return (
        <div className="min-h-screen flex items-center justify-center">
            <div className="text-center">
                <div className="w-12 h-12 border-2 border-board-accent border-t-transparent rounded-full animate-spin mx-auto mb-4" />
                <p className="text-gray-500">Loading...</p>
            </div>
        </div>
    )
    return children
}

export default function App() {
    const { token, setAuth, setLoading } = useAuthStore()
    const navigate = useNavigate()

    useEffect(() => {
        if (!token) {
            setLoading(false)
            return
        }
        // Validate token by fetching user
        fetchMe()
            .then(data => {
                setAuth(data.user, token)
                setLoading(false)
            })
            .catch(() => {
                useAuthStore.getState().logout()
                setLoading(false)
                navigate('/')
            })
    }, [])

    return (
        <Routes>
            <Route path="/" element={
                token ? <Navigate to="/dashboard" replace /> : <LoginPage />
            } />
            <Route path="/dashboard" element={
                <AuthGuard>
                    <DashboardPage />
                </AuthGuard>
            } />
            <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
    )
}