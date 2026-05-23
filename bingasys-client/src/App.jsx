import { useEffect, useState } from 'react';
import LoginForm from './components/LoginForm';
import DashboardPage from './pages/DashboardPage';
import { api } from './services/api';

const TOKEN_KEY = 'bingasys_client_token';
const USER_KEY = 'bingasys_client_user';

export default function App() {
  const [token, setToken] = useState(localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState(() => {
    const stored = localStorage.getItem(USER_KEY);
    return stored ? JSON.parse(stored) : null;
  });
  const [connections, setConnections] = useState({ meta: {}, shopify: {} });
  const [loadingLogin, setLoadingLogin] = useState(false);
  const [loadingProvider, setLoadingProvider] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (token) {
      refreshConnections(token);
    }
  }, [token]);

  async function refreshConnections(activeToken = token) {
    if (!activeToken) return;
    setError('');
    try {
      const data = await api.getConnections(activeToken);
      setConnections(data);
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleLogin(credentials) {
    setLoadingLogin(true);
    setError('');
    setMessage('');
    try {
      const data = await api.login(credentials);
      localStorage.setItem(TOKEN_KEY, data.token);
      localStorage.setItem(USER_KEY, JSON.stringify(data.user));
      setToken(data.token);
      setUser(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingLogin(false);
    }
  }

  async function handleConnect(provider) {
    setLoadingProvider(provider);
    setError('');
    setMessage('');
    try {
      const data =
        provider === 'meta' ? await api.connectMeta(token) : await api.connectShopify(token);

      if (data.redirectUrl) {
        window.location.href = data.redirectUrl;
        return;
      }

      setMessage(`${provider} connected successfully.`);
      await refreshConnections();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingProvider('');
    }
  }

  async function handleDisconnect(provider) {
    setLoadingProvider(provider);
    setError('');
    setMessage('');
    try {
      await api.disconnectIntegration(token, provider);
      setMessage(`${provider} disconnected.`);
      await refreshConnections();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingProvider('');
    }
  }

  function handleLogout() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    setToken(null);
    setUser(null);
    setConnections({ meta: {}, shopify: {} });
  }

  if (!token || !user) {
    return (
      <div className="screen-center">
        <LoginForm onLogin={handleLogin} loading={loadingLogin} error={error} />
      </div>
    );
  }

  return (
    <DashboardPage
      user={user}
      connections={connections}
      loadingProvider={loadingProvider}
      onConnectMeta={() => handleConnect('meta')}
      onConnectShopify={() => handleConnect('shopify')}
      onDisconnect={handleDisconnect}
      onRefresh={refreshConnections}
      onLogout={handleLogout}
      message={message}
      error={error}
    />
  );
}
