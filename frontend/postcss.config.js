/**
 * PostCSS configuration for the AutoEdit frontend.
 *
 * This project uses:
 * - Next.js 14
 * - Tailwind CSS 3.4.x
 * - Autoprefixer
 *
 * The configuration is intentionally minimal and production-ready:
 * - `tailwindcss` processes the Tailwind directives used in
 *   `frontend/styles/globals.css` (`@tailwind base/components/utilities`);
 * - `autoprefixer` adds vendor prefixes for broader browser compatibility.
 *
 * The CommonJS export format is used here because:
 * - `postcss.config.js` is conventionally loaded by tooling in CJS form;
 * - it integrates cleanly with the current `next.config.js` file, which also
 *   uses CommonJS (`module.exports = nextConfig`).
 */

module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};