// Shared Tailwind Play CDN configuration, loaded right after the CDN script
// on every page. Tunes the default scale toward larger radii; shadow
// utilities are neutralized project-wide — depth comes from flat color and
// 1px borders only, never drop shadows. The `indigo` scale is overridden
// project-wide to a burnt-copper accent (matches --accent-* in app.css) so
// every existing `indigo-*` utility class renders in the new brand color
// without touching individual templates.
tailwind.config = {
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        heading: ['Geoform', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        lg: '0.75rem',
        xl: '1rem',
      },
      boxShadow: {
        sm: 'none',
        DEFAULT: 'none',
        md: 'none',
        lg: 'none',
        xl: 'none',
        '2xl': 'none',
        inner: 'none',
      },
      colors: {
        indigo: {
          50: '#FDF4EC',
          100: '#FAE3CE',
          200: '#F3C79C',
          300: '#E9A468',
          400: '#DD8142',
          500: '#CC6620',
          600: '#B8530F',
          700: '#944110',
          800: '#743411',
          900: '#5C2B10',
        },
      },
    },
  },
};
