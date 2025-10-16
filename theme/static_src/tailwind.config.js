/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    // Django templates
    './templates/**/*.html',
    './**/templates/**/*.html',
    // Optional: if you render strings from python
    './**/*.py',
  ],
  theme: {
    container: { center: true, padding: '1rem' },
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'Noto Sans', 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol'],
        heading: ['Inter', 'ui-sans-serif', 'system-ui'],
      },
      colors: {
        // Neutral SaaS palette (tweak to taste)
        brand: {
          50:  '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',   // primary
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
        },
        ink: {
          50:  '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1f2937',
          900: '#0f172a',   // body text
        },
      },
      borderRadius: {
        xl: '1rem',
        '2xl': '1.25rem',
      },
      boxShadow: {
        soft: '0 10px 24px rgba(2,6,23,.06)',
        lift: '0 12px 32px rgba(2,6,23,.12)',
      },
      transitionTimingFunction: {
        smooth: 'cubic-bezier(.22,1,.36,1)',
      },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography'),
  ],
};
