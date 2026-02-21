/** @type {import('tailwindcss').Config} */
export default {
    content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
    theme: {
        extend: {
            colors: {
                board: {
                    900: '#0C0E12',
                    800: '#13161C',
                    700: '#1A1E27',
                    600: '#232733',
                    500: '#2D3344',
                    accent: '#F6C453',
                    'accent-dim': '#C49A2F',
                    green: '#22C55E',
                    red: '#EF4444',
                    blue: '#3B82F6',
                }
            },
            fontFamily: {
                display: ['"DM Serif Display"', 'Georgia', 'serif'],
                body: ['"DM Sans"', 'system-ui', 'sans-serif'],
                mono: ['"JetBrains Mono"', 'monospace'],
            },
            animation: {
                'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                'slide-up': 'slideUp 0.3s ease-out',
                'fade-in': 'fadeIn 0.4s ease-out',
            },
            keyframes: {
                slideUp: {
                    '0%': { transform: 'translateY(10px)', opacity: '0' },
                    '100%': { transform: 'translateY(0)', opacity: '1' },
                },
                fadeIn: {
                    '0%': { opacity: '0' },
                    '100%': { opacity: '1' },
                }
            }
        }
    },
    plugins: []
}