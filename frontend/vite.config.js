import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  root: 'src',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'src/index.html'),
        login: resolve(__dirname, 'src/pages/login.html'),
        'agent-list': resolve(__dirname, 'src/pages/agent-list.html'),
        'create-agent': resolve(__dirname, 'src/pages/create-agent.html'),
        'edit-agent': resolve(__dirname, 'src/pages/edit-agent.html'),
        chat: resolve(__dirname, 'src/pages/chat.html'),
        dashboard: resolve(__dirname, 'src/pages/dashboard.html'),
        admin: resolve(__dirname, 'src/pages/admin/users.html'),
        logs: resolve(__dirname, 'src/pages/admin/logs.html'),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
});
