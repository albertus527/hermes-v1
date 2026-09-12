import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Fixed Website Builder R1 frontend toolchain. Generated projects may extend
// this file inside their own copied workspace; the platform-owned template at
// templates/frontend-starter/ is never modified by a build.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // `host: true` so a sandbox/browser QA pass can reach the dev server from
    // outside the project's own network namespace. The project runner is
    // responsible for assigning a free port (`vite --port <n>`).
    host: true,
    port: 5173,
    strictPort: false
  },
  preview: {
    host: true,
    port: 4173,
    strictPort: false
  },
  build: {
    outDir: 'dist',
    target: 'es2023',
    sourcemap: false
  }
})
