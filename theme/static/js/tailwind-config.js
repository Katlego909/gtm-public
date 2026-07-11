// Shared Tailwind Play CDN configuration, loaded right after the CDN script
// on every page. Tunes the default scale toward larger radii; shadow
// utilities are neutralized project-wide — depth comes from flat color and
// 1px borders only, never drop shadows.
tailwind.config = {
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
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
    },
  },
};
