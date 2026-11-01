// Tailwind v3 build config, compiled with the standalone CLI (no Node needed)
// into theme/static/css/tailwind.css, which every base template links. This
// replaces the Play CDN, which compiled CSS in the browser on every page load.
//
// Rebuild after adding/changing utility classes (the Docker build also
// rebuilds it, so a stale committed copy can't reach production):
//   tailwindcss -c theme/tailwind.config.js -i theme/tailwind.input.css \
//     -o theme/static/css/tailwind.css --minify      (add --watch while developing)
// Standalone CLI v3.4.17: https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.17
//
// Tunes the default scale toward larger radii; shadow utilities are
// neutralized project-wide -- depth comes from flat color and 1px borders
// only, never drop shadows. The `indigo` scale is overridden project-wide to a
// burnt-copper accent (matches --accent-* in app.css) so every existing
// `indigo-*` utility class renders in the brand color.
//
// Class names must appear literally in a scanned file: build them from whole
// strings (e.g. 'bg-red-100 text-red-500'), never 'bg-' + color.
module.exports = {
  content: {
    relative: true,
    files: [
      '../templates/**/*.html',
      '../gtm/**/*.{html,py}',
      '../dashboard/**/*.{html,py}',
      './templates/**/*.html',
      './static/js/**/*.js',
    ],
  },
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
  plugins: [require('@tailwindcss/typography')],
};
