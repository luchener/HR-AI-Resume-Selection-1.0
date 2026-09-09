/** @type {import('tailwindcss').Config} */
module.exports = {
    content: [
        "./app/**/*.{js,ts,jsx,tsx,mdx}",
        "./components/**/*.{js,ts,jsx,tsx,mdx}",
    ],
    theme: {
        extend: {
            animation: {
                'gradient': 'gradient 8s linear infinite',
                'shake': 'shake 0.35s ease-in-out',
            },
            keyframes: {
                'gradient': {
                    to: { 'background-position': '200% center' },
                },
                'shake': {
                    '0%, 100%': { transform: 'translateX(0)' },
                    '20%': { transform: 'translateX(-6px)' },
                    '40%': { transform: 'translateX(6px)' },
                    '60%': { transform: 'translateX(-4px)' },
                    '80%': { transform: 'translateX(4px)' },
                },
            },
            fontFamily: {
                sans: ['"Geist Sans"', 'sans-serif'],
                mono: ['"Space Grotesk"', 'monospace'],
            },
        },
    },
    plugins: [],
}