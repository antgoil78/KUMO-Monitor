import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'build',
    emptyOutDir: true
  },
  server: {
    proxy: {
      '/api': process.env.KUMO_API_PROXY || 'http://localhost:5000'
    }
  }
})
