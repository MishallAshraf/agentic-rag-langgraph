import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the browser talks only to Vite (one origin), and Vite forwards
// /api/* to the Python API. No CORS is needed, and the API stays on localhost.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        // API_PORT is the same setting api.py reads, for when 8000 is taken.
        target: `http://127.0.0.1:${process.env.API_PORT ?? '8000'}`,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
