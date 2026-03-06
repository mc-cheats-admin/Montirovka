import type { Config } from "tailwindcss";

/**
 * Tailwind CSS configuration for the AutoEdit frontend.
 *
 * Key goals of this configuration:
 * - work cleanly with Next.js 14 App Router;
 * - scan all actual frontend source locations used in this project;
 * - expose semantic design tokens that match the product specification;
 * - stay synchronized with the CSS variables already defined in
 *   frontend/styles/globals.css;
 * - provide a small but useful extension layer for repeated UI patterns
 *   such as app surfaces, shadows and gradient backgrounds.
 *
 * Notes about integration with the current codebase:
 * - Existing components already use many arbitrary Tailwind values like
 *   bg-[#121A2B], border-white/10 and custom shadow strings.
 * - This config does not replace those utilities; instead, it adds
 *   semantic aliases so future files can use stable tokens such as
 *   bg-app, text-app-text, shadow-app and similar theme-based classes.
 * - The content globs intentionally include app, components, lib and styles
 *   directories because all of them are part of the current frontend tree.
 */
const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
    "./styles/**/*.{css,scss}",
  ],
  theme: {
    extend: {
      colors: {
        app: {
          DEFAULT: "#0B1020",
          bg: "#0B1020",
          surface: "#121A2B",
          "surface-secondary": "#182235",
          accent: "#7C5CFF",
          "accent-secondary": "#00C2FF",
          success: "#22C55E",
          error: "#EF4444",
          text: "#F3F4F6",
          muted: "#A5B4CC",
          border: "rgba(255,255,255,0.08)",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "Inter", "system-ui", "sans-serif"],
      },
      borderRadius: {
        card: "16px",
        button: "12px",
      },
      boxShadow: {
        app: "0 10px 30px rgba(0,0,0,0.25)",
        "app-md": "0 8px 24px rgba(0,0,0,0.2)",
        glow: "0 10px 30px rgba(124,92,255,0.25)",
      },
      backgroundImage: {
        "app-gradient":
          "linear-gradient(90deg, #7C5CFF 0%, #00C2FF 100%)",
        "app-bg":
          "radial-gradient(circle at top left, rgba(124, 92, 255, 0.12), transparent 28%), radial-gradient(circle at top right, rgba(0, 194, 255, 0.10), transparent 24%), linear-gradient(180deg, #0B1020 0%, #0D1324 100%)",
        "grid-soft":
          "linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px)",
      },
      backgroundSize: {
        "grid-soft": "24px 24px",
      },
      maxWidth: {
        "8xl": "90rem",
      },
      keyframes: {
        "pulse-soft": {
          "0%, 100%": {
            opacity: "0.7",
            transform: "scale(1)",
          },
          "50%": {
            opacity: "1",
            transform: "scale(1.02)",
          },
        },
        "fade-in-up": {
          "0%": {
            opacity: "0",
            transform: "translateY(8px)",
          },
          "100%": {
            opacity: "1",
            transform: "translateY(0)",
          },
        },
      },
      animation: {
        "pulse-soft": "pulse-soft 2.4s ease-in-out infinite",
        "fade-in-up": "fade-in-up 0.35s ease-out",
      },
    },
  },
  plugins: [],
};

export default config;