const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:3000';

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    },
    ...options
  });

  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    throw new Error(errorBody.message || `Request failed with status ${response.status}`);
  }

  return response.json();
}

export const api = {
  login: ({ email, password }) =>
    request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password })
    }),

  getConnections: (token) =>
    request('/integrations', {
      headers: { Authorization: `Bearer ${token}` }
    }),

  connectMeta: (token) =>
    request('/integrations/meta/connect', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` }
    }),

  connectShopify: (token) =>
    request('/integrations/shopify/connect', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` }
    }),

  disconnectIntegration: (token, provider) =>
    request(`/integrations/${provider}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` }
    })
};
