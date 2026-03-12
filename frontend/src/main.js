const API_BASE = '/api';

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
      const error = await response.json().catch(() => ({ error: 'Unknown error' }));
      throw new Error(error.error || `HTTP ${response.status}`);
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
};

const auth = {
  async login(email, password) {
    // 後端路由為 /api/login（非 /api/auth/login）
    const response = await api.post('/login', { email, password });
    localStorage.setItem('auth_token', response.token);
    localStorage.setItem('user', JSON.stringify(response.user));
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
  const errorDiv = document.getElementById('error-message');
  if (errorDiv) {
    errorDiv.textContent = message;
    errorDiv.style.display = 'block';
  }
  console.error(message);
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
});

export { api, auth, showError, showSuccess };
