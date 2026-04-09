/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts}"],
  theme: {
    extend: {
      colors: {
        panel: "#111318",
        card: "#181b22",
        line: "#2a2f3a",
      },
    },
  },
  plugins: [],
};
