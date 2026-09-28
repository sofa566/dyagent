import { defineConfig } from 'vite';
import { resolve } from 'path';

const backendProxyTarget = process.env.VITE_BACKEND_PROXY_TARGET || 'http://localhost:8000';
const websocketProxyTarget = process.env.VITE_WS_PROXY_TARGET || 'ws://localhost:8000';

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
        'reminder-schedule': resolve(__dirname, 'src/pages/reminder-schedule.html'),
        'health-reminder': resolve(__dirname, 'src/pages/health-reminder.html'),
        'health-source-rules': resolve(__dirname, 'src/pages/health-source-rules.html'),
        'scheduler-management': resolve(__dirname, 'src/pages/scheduler-management.html'),
        renals: resolve(__dirname, 'src/pages/renals.html'),
        'renal-care-nurse': resolve(__dirname, 'src/pages/renal-care-nurse.html'),
        'renal-care-patient': resolve(__dirname, 'src/pages/renal-care-patient.html'),
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
        target: backendProxyTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: websocketProxyTarget,
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
