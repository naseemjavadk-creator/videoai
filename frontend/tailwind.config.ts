import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#07070B",
          900: "#0D0D14",
          850: "#12121B",
          800: "#181823",
          700: "#23232F",
          600: "#2F2F3D",
        },
        brand: {
          cyan: "#22D3EE",
          pink: "#F472B6",
          lime: "#A3E635",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "var(--font-sans)", "ui-sans-serif", "sans-serif"],
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(34,211,238,.25), 0 10px 40px -10px rgba(34,211,238,.35)",
      },
      keyframes: {
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      animation: {
        shimmer: "shimmer 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;
