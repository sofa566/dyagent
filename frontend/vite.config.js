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
        register: resolve(__dirname, 'src/pages/register.html'),
        'agent-list': resolve(__dirname, 'src/pages/agent-list.html'),
        'create-agent': resolve(__dirname, 'src/pages/create-agent.html'),
        'edit-agent': resolve(__dirname, 'src/pages/edit-agent.html'),
        'agent-llm': resolve(__dirname, 'src/pages/agent-llm.html'),
        'agent-llm-config': resolve(__dirname, 'src/pages/agent-llm-config.html'),
        'agent-rag-config': resolve(__dirname, 'src/pages/agent-rag-config.html'),
        'line-console': resolve(__dirname, 'src/pages/line-console.html'),
        chat: resolve(__dirname, 'src/pages/chat.html'),
        dashboard: resolve(__dirname, 'src/pages/dashboard.html'),
        users: resolve(__dirname, 'src/pages/users.html'),
        'access-control': resolve(__dirname, 'src/pages/access-control.html'),
        'user-edit': resolve(__dirname, 'src/pages/user-edit.html'),
        mcps: resolve(__dirname, 'src/pages/mcps.html'),
        'mcp-edit': resolve(__dirname, 'src/pages/mcp-edit.html'),
        skills: resolve(__dirname, 'src/pages/skills.html'),
        'skill-edit': resolve(__dirname, 'src/pages/skill-edit.html'),
        functions: resolve(__dirname, 'src/pages/functions.html'),
        'rag-datasets': resolve(__dirname, 'src/pages/rag-datasets.html'),
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
