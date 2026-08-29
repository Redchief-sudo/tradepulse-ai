import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // During `npm run dev`, proxy API calls to the FastAPI backend
    // (`tradepulse dashboard`, bound to 127.0.0.1). Production serves the
    // built `dist/` directly from that same backend, so no proxy is
    // needed there -- see tradepulse/web/app.py's StaticFiles mount.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})
