/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    // Django templates
    '../../templates/**/*.html',
    '../../**/templates/**/*.html',
    '../../../templates/**/*.html',
    // JS or React/Vue files (if any)
    './src/**/*.js',
  ],
  theme: {
    extend: {},
  },
  plugins: [
    require('daisyui'),
  ],
  daisyui: {
    themes: ["light", "dark"],
  },
}
