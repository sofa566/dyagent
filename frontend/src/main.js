// 動態偵測 API_BASE：
// - 可用 localStorage.API_BASE 或 ?api_base= 覆蓋（例如 http://127.0.0.1:8000/api）
// - 在 Vite 開發模式 (port === 5173) 預設指向 http(s)://<當前主機>:8000/api（不限定 localhost）
// - 其他情況走同源 '/api'
const __url = new URL(window.location.href);
const __override = __url.searchParams.get('api_base') || localStorage.getItem('API_BASE');
const __devDefault = `${window.location.protocol}//${window.location.hostname}:8000/api`;
const API_BASE = (__override
  || (window.location.port === '5173' ? __devDefault : '/api'));
try { console.info('[dyagent] API_BASE =', API_BASE); } catch {}

function sanitizeRuntimeText(rawText) {
  let text = String(rawText || '');
  text = text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/gi, '');
  text = text.replace(/<\/?system-reminder>/gi, '');
  text = text.replace(
    /Your operational mode has changed from plan to build\.\s*You are no longer in read-only mode\.\s*You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed\./gi,
    '',
  );
  text = text.replace(/^\s*Your operational mode has changed from plan to build\.\s*$/gim, '');
  text = text.replace(/^\s*You are no longer in read-only mode\.\s*$/gim, '');
  text = text.replace(/^\s*You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed\.\s*$/gim, '');
  return text.trim();
}

const api = {
  async request(endpoint, options = {}) {
    const token = localStorage.getItem('auth_token');
    const headers = {
      'Content-Type': 'application/json',
      ...(token && { Authorization: `Bearer ${token}` }),
      ...options.headers,
    };

    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      // 401 表示 token 無效或過期，自動清除並重導向到登入頁
      if (response.status === 401) {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('user');
        // 避免在登入頁再次重導向
        if (!window.location.pathname.endsWith('/login.html')) {
          window.location.href = '/pages/login.html';
        }
        throw new Error('登入已過期，請重新登入');
      }
      const error = await response.json().catch(() => ({ error: 'Unknown error' }));
      const message =
        (error && error.error)
        || (error && error.detail && error.detail.error)
        || (error && error.detail && typeof error.detail === 'string' ? error.detail : '')
        || (error && error.message)
        || `HTTP ${response.status}`;
      throw new Error(sanitizeRuntimeText(message) || `HTTP ${response.status}`);
    }

    if (response.status === 204) {
      return null;
    }

    return response.json();
  },

  get(endpoint) {
    return this.request(endpoint);
  },

  post(endpoint, data) {
    return this.request(endpoint, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  put(endpoint, data) {
    return this.request(endpoint, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  },

  delete(endpoint) {
    return this.request(endpoint, {
      method: 'DELETE',
    });
  },

  async uploadFile(endpoint, formData) {
    const token = localStorage.getItem('auth_token');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};

    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'POST',
      headers,
      body: formData,
    });

    if (!response.ok) {
      // 401 表示 token 無效或過期，自動清除並重導向到登入頁
      if (response.status === 401) {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('user');
        if (!window.location.pathname.endsWith('/login.html')) {
          window.location.href = '/pages/login.html';
        }
        throw new Error('登入已過期，請重新登入');
      }
      const error = await response.json().catch(() => ({ error: 'Unknown error' }));
      throw new Error(error.error || `HTTP ${response.status}`);
    }

    return response.json();
  },
};

const auth = {
  async login(email, password) {
    // 後端路由為 /api/login（非 /api/auth/login）
    const response = await api.post('/login', { email, password });
    localStorage.setItem('auth_token', response.token);
    localStorage.setItem('user', JSON.stringify(response.user));
    // 非 admin 登入後直接導向聊天頁
    try {
      if (response.user && response.user.role !== 'admin') {
        window.location.href = '/pages/chat.html';
      }
    } catch {}
    return response.user;
  },

  logout() {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('user');
    window.location.href = '/pages/login.html';
  },

  getUser() {
    const userStr = localStorage.getItem('user');
    return userStr ? JSON.parse(userStr) : null;
  },

  isAuthenticated() {
    return !!localStorage.getItem('auth_token');
  },
};

function showError(message) {
  const cleanedMessage = sanitizeRuntimeText(message) || '發生未知錯誤';
  const errorDiv = document.getElementById('error-message');
  if (errorDiv) {
    errorDiv.textContent = cleanedMessage;
    errorDiv.style.display = 'block';
  }
  console.error(cleanedMessage);
}

function showSuccess(message) {
  const successDiv = document.getElementById('success-message');
  if (successDiv) {
    successDiv.textContent = message;
    successDiv.style.display = 'block';
    setTimeout(() => {
      successDiv.style.display = 'none';
    }, 3000);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const user = auth.getUser();

  // 依規則重排導覽連結：
  // - 儀表板 最左
  // - （admin）使用者、代理者、MCP 管理、技能管理
  // - 其他項目（如 聊天）置於上述之後
  // - 登出（或登入）永遠在最右
  function arrangeNavMenu(currentUser) {
    try {
      const navMenu = document.querySelector('.nav-menu');
      if (!navMenu) return;

      const getLinks = () => Array.from(navMenu.querySelectorAll('a'));
      const findByHrefEnd = (end) => getLinks().find(a => {
        const href = a.getAttribute('href') || '';
        return href.endsWith(end);
      });
      const byId = (id) => navMenu.querySelector(`#${id}`);

      const logoutOrAuth = byId('logout-link') || byId('auth-link');

      const order = [];

      // 儀表板（僅 admin 顯示）
      const dashboard = findByHrefEnd('/pages/dashboard.html');
      if (currentUser && currentUser.role === 'admin') {
        if (dashboard) order.push(dashboard);
      } else if (dashboard && dashboard.parentElement === navMenu) {
        navMenu.removeChild(dashboard);
      }

      // 角色相關排序
      if (currentUser && currentUser.role === 'admin') {
        const users = byId('users-admin-link') || findByHrefEnd('/pages/users.html');
        if (users) order.push(users);

        const agents = findByHrefEnd('/pages/agent-list.html');
        if (agents) order.push(agents);

        const mcps = byId('mcps-admin-link') || findByHrefEnd('/pages/mcps.html');
        if (mcps) order.push(mcps);

        const skills = byId('skills-admin-link') || findByHrefEnd('/pages/skills.html');
        if (skills) order.push(skills);

        const functionsLink = byId('functions-admin-link') || findByHrefEnd('/pages/functions.html');
        if (functionsLink) order.push(functionsLink);

        const ragDatasetsLink = byId('rag-datasets-admin-link') || findByHrefEnd('/pages/rag-datasets.html');
        if (ragDatasetsLink) order.push(ragDatasetsLink);

        // 其他（如 聊天）
        const chat = findByHrefEnd('/pages/chat.html');
        if (chat) order.push(chat);
      } else {
        // 非 admin：依既有頁面存在與否排序
        const users = findByHrefEnd('/pages/users.html');
        if (users) order.push(users);

        const agents = findByHrefEnd('/pages/agent-list.html');
        if (agents) order.push(agents);

        const mcps = findByHrefEnd('/pages/mcps.html');
        if (mcps) order.push(mcps);

        const skills = findByHrefEnd('/pages/skills.html');
        if (skills) order.push(skills);

        const chat = findByHrefEnd('/pages/chat.html');
        if (chat) order.push(chat);
      }

      // 其餘未收錄的項目（排除登出/登入；非 admin 排除儀表板）
      getLinks().forEach(a => {
        const href = a.getAttribute('href') || '';
        if (a === logoutOrAuth || order.includes(a)) return;
        if ((!currentUser || currentUser.role !== 'admin') && href.endsWith('/pages/dashboard.html')) return;
        order.push(a);
      });

      // 依序附加，最後放登出/登入
      order.forEach(a => navMenu.appendChild(a));
      if (logoutOrAuth) navMenu.appendChild(logoutOrAuth);
    } catch {}
  }

  // 若存在 #auth-link，根據登入狀態切換為 登入/登出
  const authLink = document.getElementById('auth-link');
  if (authLink) {
    if (user) {
      authLink.textContent = '登出';
      authLink.href = '#logout';
      authLink.id = 'logout-link'; // 正規化 id，後續用同一套綁定
      // 動態注入『使用者管理』（僅 admin 顯示）
      try {
        const navMenu = document.querySelector('.nav-menu');
        if (navMenu && user.role === 'admin') {
          if (!navMenu.querySelector('#users-admin-link')) {
            const usersLink = document.createElement('a');
            usersLink.id = 'users-admin-link';
            usersLink.href = '/pages/users.html';
            usersLink.textContent = '使用者管理';
            navMenu.appendChild(usersLink);
          }
          if (!navMenu.querySelector('#mcps-admin-link')) {
            const link = document.createElement('a');
            link.id = 'mcps-admin-link';
            link.href = '/pages/mcps.html';
            link.textContent = 'MCP 管理';
            navMenu.appendChild(link);
          }
          if (!navMenu.querySelector('#skills-admin-link')) {
            const link = document.createElement('a');
            link.id = 'skills-admin-link';
            link.href = '/pages/skills.html';
            link.textContent = '技能管理';
            navMenu.appendChild(link);
          }
          if (!navMenu.querySelector('#functions-admin-link')) {
            const link = document.createElement('a');
            link.id = 'functions-admin-link';
            link.href = '/pages/functions.html';
            link.textContent = 'Functions';
            navMenu.appendChild(link);
          }
          if (!navMenu.querySelector('#rag-datasets-admin-link')) {
            const link = document.createElement('a');
            link.id = 'rag-datasets-admin-link';
            link.href = '/pages/rag-datasets.html';
            link.textContent = 'RAG 資料集';
            navMenu.appendChild(link);
          }
        }
      } catch {}
    } else {
      authLink.textContent = '登入';
      authLink.href = '/pages/login.html';
    }
  }

  // 確保導覽列有登出按鈕（已登入時）
  if (user) {
    // 針對聊天頁：若為一般使用者，僅顯示登出按鈕
    try {
      const isChatPage = window.location.pathname.endsWith('/pages/chat.html');
      if (isChatPage && user.role === 'user') {
        const navMenu = document.querySelector('.nav-menu');
        if (navMenu) {
          // 保留現有的 #auth-link / #logout-link，其餘移除
          Array.from(navMenu.querySelectorAll('a')).forEach((a) => {
            if (a.id !== 'logout-link' && a.id !== 'auth-link') {
              navMenu.removeChild(a);
            }
          });
        }
      }
    } catch {}

    let logoutLink = document.getElementById('logout-link');
    if (!logoutLink) {
      const navMenu = document.querySelector('.nav-menu');
      if (navMenu) {
        logoutLink = document.createElement('a');
        logoutLink.id = 'logout-link';
        logoutLink.href = '#logout';
        logoutLink.textContent = '登出';
        navMenu.appendChild(logoutLink);
      }
    }

    if (logoutLink) {
      logoutLink.addEventListener('click', (e) => {
        e.preventDefault();
        const ok = window.confirm('確定要登出嗎？');
        if (ok) auth.logout();
      });
    }
  }

  // 最後統一整理導覽列順序
  arrangeNavMenu(user);
});

export { api, auth, showError, showSuccess, API_BASE };
