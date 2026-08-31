// 動態偵測 API_BASE：
// - 可用 localStorage.API_BASE 或 ?api_base= 覆蓋（例如 http://127.0.0.1:8000/api）
// - 在 Vite 開發模式 (port === 5173) 預設指向 http(s)://<當前主機>:8000/api（不限定 localhost）
// - 其他情況走同源 '/api'
const __url = new URL(window.location.href);
const __override = __url.searchParams.get('api_base') || localStorage.getItem('API_BASE');
const __devDefault = '/api';
const __devDirectBackend = `http://${window.location.hostname}:8000/api`;
const API_BASE = (__override
  || (window.location.port === '5173' ? __devDefault : '/api'));
try { console.info('[dyagent] API_BASE =', API_BASE); } catch {}

const CAPABILITIES_CACHE_KEY = 'dyagent_capabilities_cache';
const CAPABILITIES_CACHE_TTL_MS = 15000;
const MY_COST_USAGE_CACHE_KEY = 'dyagent_my_cost_usage_cache';
const MY_COST_USAGE_CACHE_TTL_MS = 60000;


function getLegacyPermissionsByRole(role) {
  const rolePermissions = {
    admin: [
      'create_agent',
      'read_agent',
      'update_agent',
      'delete_agent',
      'create_user',
      'read_user',
      'update_user',
      'delete_user',
      'chat',
    ],
    agent_admin: [
      'read_agent',
      'update_agent',
      'chat',
    ],
    user: ['chat'],
  };
  return Array.isArray(rolePermissions[role]) ? rolePermissions[role] : [];
}


function normalizeCapabilities(payload, fallbackUser) {
  const fallbackRole = String((fallbackUser && fallbackUser.role) || 'user');
  const fallbackPermissions = getLegacyPermissionsByRole(fallbackRole);
  const permissions = Array.isArray(payload && payload.permissions)
    ? payload.permissions.map(item => String(item || '').trim()).filter(Boolean)
    : fallbackPermissions;
  const roles = Array.isArray(payload && payload.roles)
    ? payload.roles.map(item => String(item || '').trim()).filter(Boolean)
    : [fallbackRole];
  const groups = Array.isArray(payload && payload.groups)
    ? payload.groups.map(item => String(item || '').trim()).filter(Boolean)
    : [];
  return {
    roles: Array.from(new Set(roles)),
    groups: Array.from(new Set(groups)),
    permissions: Array.from(new Set(permissions)),
  };
}


function readCachedCapabilities() {
  try {
    const rawCache = sessionStorage.getItem(CAPABILITIES_CACHE_KEY);
    if (!rawCache) {
      return null;
    }
    const cachePayload = JSON.parse(rawCache);
    const cachedAt = Number(cachePayload && cachePayload.cached_at);
    if (!cachedAt || Date.now() - cachedAt > CAPABILITIES_CACHE_TTL_MS) {
      return null;
    }
    return cachePayload.capabilities || null;
  } catch {
    return null;
  }
}


function writeCapabilitiesCache(capabilities) {
  try {
    sessionStorage.setItem(
      CAPABILITIES_CACHE_KEY,
      JSON.stringify({
        cached_at: Date.now(),
        capabilities,
      }),
    );
  } catch {}
}


function clearCapabilitiesCache() {
  try {
    sessionStorage.removeItem(CAPABILITIES_CACHE_KEY);
  } catch {}
}


function readCachedMyCostUsage() {
  try {
    const rawCache = sessionStorage.getItem(MY_COST_USAGE_CACHE_KEY);
    if (!rawCache) {
      return null;
    }
    const cachePayload = JSON.parse(rawCache);
    const cachedAt = Number(cachePayload && cachePayload.cached_at);
    if (!cachedAt || Date.now() - cachedAt > MY_COST_USAGE_CACHE_TTL_MS) {
      return null;
    }
    return cachePayload.usage || null;
  } catch {
    return null;
  }
}


function writeMyCostUsageCache(usage) {
  try {
    sessionStorage.setItem(
      MY_COST_USAGE_CACHE_KEY,
      JSON.stringify({
        cached_at: Date.now(),
        usage,
      }),
    );
  } catch {}
}


function clearMyCostUsageCache() {
  try {
    sessionStorage.removeItem(MY_COST_USAGE_CACHE_KEY);
  } catch {}
}

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

    let response;
    try {
      response = await fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers,
      });
    } catch (networkError) {
      if (window.location.port === '5173' && !__override) {
        const hint = `無法連線 API，請確認後端已啟動（可改用 ?api_base=${__devDirectBackend} 測試）`;
        throw new Error(hint);
      }
      throw networkError;
    }

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
    clearCapabilitiesCache();
    clearMyCostUsageCache();
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
    clearCapabilitiesCache();
    clearMyCostUsageCache();
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


async function getCapabilities(options = {}) {
  const { forceRefresh = false } = options;
  const user = auth.getUser();
  if (!user || !auth.isAuthenticated()) {
    return normalizeCapabilities(null, user);
  }

  if (!forceRefresh) {
    const cachedCapabilities = readCachedCapabilities();
    if (cachedCapabilities) {
      return normalizeCapabilities(cachedCapabilities, user);
    }
  }

  try {
    const payload = await api.get('/me/capabilities');
    const normalized = normalizeCapabilities(payload, user);
    writeCapabilitiesCache(normalized);
    return normalized;
  } catch {
    const fallbackCapabilities = normalizeCapabilities(null, user);
    writeCapabilitiesCache(fallbackCapabilities);
    return fallbackCapabilities;
  }
}


async function getMyCostUsage(options = {}) {
  const { forceRefresh = false } = options;
  if (!auth.isAuthenticated()) {
    return null;
  }
  if (!forceRefresh) {
    const cachedUsage = readCachedMyCostUsage();
    if (cachedUsage) {
      return cachedUsage;
    }
  }

  try {
    const payload = await api.get('/me/cost/usage');
    const usage = payload && payload.usage ? payload.usage : null;
    if (usage) {
      writeMyCostUsageCache(usage);
    }
    return usage;
  } catch {
    return null;
  }
}


function formatNumberShort(rawValue) {
  const value = Number(rawValue || 0);
  if (!Number.isFinite(value)) {
    return '0';
  }
  return value.toLocaleString('zh-TW');
}


function formatCostUsd(rawValue) {
  const value = Number(rawValue || 0);
  if (!Number.isFinite(value)) {
    return '0.000000';
  }
  return value.toFixed(6);
}


function escapeHtml(rawText) {
  return String(rawText || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}


function hasPermission(capabilities, permission) {
  const permissionSet = new Set(Array.isArray(capabilities && capabilities.permissions) ? capabilities.permissions : []);
  return permissionSet.has(String(permission || '').trim());
}


function hasAnyPermission(capabilities, permissions) {
  const permissionSet = new Set(Array.isArray(capabilities && capabilities.permissions) ? capabilities.permissions : []);
  return (permissions || []).some(permission => permissionSet.has(String(permission || '').trim()));
}

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

document.addEventListener('DOMContentLoaded', async () => {
  const user = auth.getUser();
  const capabilities = user ? await getCapabilities() : normalizeCapabilities(null, null);

  function renderNavbarUserMeta(currentUser, currentCapabilities) {
    try {
      const navBar = document.querySelector('.navbar');
      if (!navBar) return;

      const navMenu = navBar.querySelector('.nav-menu');
      if (!navMenu) return;

      const existingMeta = navBar.querySelector('#nav-user-meta');
      if (!currentUser || !auth.isAuthenticated()) {
        if (existingMeta) {
          existingMeta.remove();
        }
        return;
      }

      const roles = Array.isArray(currentCapabilities && currentCapabilities.roles)
        ? currentCapabilities.roles.map(item => String(item || '').trim()).filter(Boolean)
        : [];
      const groups = Array.isArray(currentCapabilities && currentCapabilities.groups)
        ? currentCapabilities.groups.map(item => String(item || '').trim()).filter(Boolean)
        : [];
      const roleText = roles.length > 0 ? roles.join('、') : '無角色';
      const groupText = groups.length > 0 ? groups.join('、') : '未分組';
      const username = String((currentUser && currentUser.username) || '使用者').trim() || '使用者';
      const primaryLineText = `歡迎，${username}！ ${roleText}｜${groupText}`;
      const secondaryLineText = '總使用 Tokens 載入中...  |  Cost USD 載入中...';

      const metaElement = existingMeta || document.createElement('div');
      metaElement.id = 'nav-user-meta';
      metaElement.className = 'nav-user-meta';
      metaElement.innerHTML = `
        <span class="meta-line-primary">${escapeHtml(primaryLineText)}</span>
        <span class="meta-line-secondary">${escapeHtml(secondaryLineText)}</span>
      `;
      metaElement.title = `${primaryLineText}\n${secondaryLineText}`;

      if (!existingMeta) {
        navBar.insertBefore(metaElement, navMenu);
      }

      getMyCostUsage()
        .then((usage) => {
          if (!usage) {
            const fallbackPrimaryLineText = `歡迎，${username}！ ${roleText}｜${groupText}`;
            const fallbackSecondaryLineText = '總使用 Tokens -  |  Cost USD -';
            metaElement.innerHTML = `
              <span class="meta-line-primary">${escapeHtml(fallbackPrimaryLineText)}</span>
              <span class="meta-line-secondary">${escapeHtml(fallbackSecondaryLineText)}</span>
            `;
            metaElement.title = `${fallbackPrimaryLineText}\n${fallbackSecondaryLineText}`;
            return;
          }
          const totalTokens = formatNumberShort(usage.total_tokens || 0);
          const costUsd = formatCostUsd(usage.cost_usd || 0);
          const loadedPrimaryLineText = `歡迎，${username}！ ${roleText}｜${groupText}`;
          const loadedSecondaryLineText = `總使用 Tokens ${totalTokens}  |  Cost USD ${costUsd}`;
          metaElement.innerHTML = `
            <span class="meta-line-primary">${escapeHtml(loadedPrimaryLineText)}</span>
            <span class="meta-line-secondary">${escapeHtml(loadedSecondaryLineText)}</span>
          `;
          metaElement.title = `${loadedPrimaryLineText}\n${loadedSecondaryLineText}`;
        })
        .catch(() => {
          const fallbackPrimaryLineText = `歡迎，${username}！ ${roleText}｜${groupText}`;
          const fallbackSecondaryLineText = '總使用 Tokens -  |  Cost USD -';
          metaElement.innerHTML = `
            <span class="meta-line-primary">${escapeHtml(fallbackPrimaryLineText)}</span>
            <span class="meta-line-secondary">${escapeHtml(fallbackSecondaryLineText)}</span>
          `;
          metaElement.title = `${fallbackPrimaryLineText}\n${fallbackSecondaryLineText}`;
        });
    } catch {}
  }

  // 依規則重排導覽連結：
  // - 儀表板 最左
  // - （admin）使用者、代理者、MCP 管理、技能管理
  // - 其他項目（如 聊天）置於上述之後
  // - 登出（或登入）永遠在最右
  function arrangeNavMenu(currentUser, currentCapabilities) {
    try {
      const navMenu = document.querySelector('.nav-menu');
      if (!navMenu) return;

      const getLinks = () => Array.from(navMenu.querySelectorAll('a'));
      const findByHrefEnd = (end) => getLinks().find(a => {
        const href = a.getAttribute('href') || '';
        return href.endsWith(end);
      });
      const byId = (id) => navMenu.querySelector(`#${id}`);

      const permissionSet = new Set(Array.isArray(currentCapabilities && currentCapabilities.permissions) ? currentCapabilities.permissions : []);
      const canAccess = (requiredPermissions) => {
        if (!Array.isArray(requiredPermissions) || requiredPermissions.length === 0) {
          return true;
        }
        return requiredPermissions.some(permission => permissionSet.has(permission));
      };

      const navConfigs = [
        { id: 'dashboard-link', href: '/pages/dashboard.html', text: '儀表板', requiredPermissions: ['dashboard.read', 'logs.read'] },
        { id: 'users-admin-link', href: '/pages/users.html', text: '使用者管理', requiredPermissions: ['read_user', 'update_user'] },
        { id: 'access-control-link', href: '/pages/access-control.html', text: '權限管理', requiredPermissions: ['update_user', 'role.read', 'group.read', 'role.update', 'group.update'] },
        { id: 'agent-list-link', href: '/pages/agent-list.html', text: '代理者', requiredPermissions: ['read_agent', 'update_agent', 'create_agent'] },
        { id: 'mcps-admin-link', href: '/pages/mcps.html', text: 'MCP 管理', requiredPermissions: ['mcp.read'] },
        { id: 'skills-admin-link', href: '/pages/skills.html', text: '技能管理', requiredPermissions: ['skills.read'] },
        { id: 'functions-admin-link', href: '/pages/functions.html', text: 'Functions', requiredPermissions: ['functions.read'] },
        { id: 'rag-datasets-admin-link', href: '/pages/rag-datasets.html', text: 'RAG 資料集', requiredPermissions: ['rag.read'] },
        { id: 'chat-link', href: '/pages/chat.html', text: '聊天', requiredPermissions: ['chat'] },
      ];

      const findByHref = (href) => getLinks().find(a => (a.getAttribute('href') || '').endsWith(href));
      for (const navConfig of navConfigs) {
        const hasPermissionForLink = canAccess(navConfig.requiredPermissions);
        let navLink = byId(navConfig.id) || findByHref(navConfig.href);
        if (!hasPermissionForLink) {
          if (navLink && navLink.parentElement === navMenu) {
            navMenu.removeChild(navLink);
          }
          continue;
        }
        if (!navLink) {
          navLink = document.createElement('a');
          navLink.id = navConfig.id;
          navLink.href = navConfig.href;
          navLink.textContent = navConfig.text;
          navMenu.appendChild(navLink);
        } else {
          navLink.id = navConfig.id;
          navLink.href = navConfig.href;
          navLink.textContent = navConfig.text;
        }
      }

      const logoutOrAuth = byId('logout-link') || byId('auth-link');

      const order = [];

      const orderedPaths = [
        '/pages/dashboard.html',
        '/pages/users.html',
        '/pages/access-control.html',
        '/pages/agent-list.html',
        '/pages/mcps.html',
        '/pages/skills.html',
        '/pages/functions.html',
        '/pages/rag-datasets.html',
        '/pages/chat.html',
      ];
      for (const path of orderedPaths) {
        const link = findByHrefEnd(path);
        if (link) {
          order.push(link);
        }
      }

      // 其餘未收錄的項目（排除登出/登入；非 admin 排除儀表板）
      getLinks().forEach(a => {
        const href = a.getAttribute('href') || '';
        if (a === logoutOrAuth || order.includes(a)) return;
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
    } else {
      authLink.textContent = '登入';
      authLink.href = '/pages/login.html';
    }
  }

  // 確保導覽列有登出按鈕（已登入時）
  if (user) {
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
  arrangeNavMenu(user, capabilities);
  renderNavbarUserMeta(user, capabilities);
});

export { api, auth, showError, showSuccess, API_BASE, getCapabilities, hasPermission, hasAnyPermission, clearCapabilitiesCache };
