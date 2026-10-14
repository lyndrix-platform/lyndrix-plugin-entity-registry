import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

export default defineConfig({
  plugins: [react({ jsxRuntime: 'classic' })],
  build: {
    lib: {
      entry: resolve(__dirname, 'app/ui/react/index.tsx'),
      name: '__lyndrix_plugin_lyndrix_plugin_entity_registry',
      formats: ['iife'],
      fileName: () => 'ui_bundle.js',
    },
    outDir: 'app/ui/static',
    emptyOutDir: true,
    rollupOptions: {
      // Shared from the host shell via window globals — never bundled.
      external: ['react', 'react-dom', 'react-dom/client', 'react-i18next', 'i18next', '@lyndrix/ui'],
      output: {
        globals: {
          react: '__lyndrix_react',
          'react-dom': '__lyndrix_react',
          'react-dom/client': '__lyndrix_react_dom_client',
          'react-i18next': '__lyndrix_react_i18next',
          i18next: '__lyndrix_i18n',
          '@lyndrix/ui': '__lyndrix_ui',
        },
      },
    },
  },
})
