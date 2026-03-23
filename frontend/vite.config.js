import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base: './' makes all asset paths relative — required for GitHub Pages
// where the app is served from a subdirectory, not a domain root.
export default defineConfig({
  plugins: [react()],
  base: '/Observability/',
})
