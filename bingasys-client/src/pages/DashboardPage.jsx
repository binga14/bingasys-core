import IntegrationCard from '../components/IntegrationCard';

export default function DashboardPage({
  user,
  connections,
  loadingProvider,
  onConnectMeta,
  onConnectShopify,
  onDisconnect,
  onRefresh,
  onLogout,
  message,
  error
}) {
  return (
    <main className="container">
      <header className="topbar">
        <div>
          <h1>Integration Dashboard</h1>
          <p>Welcome, {user.email}</p>
        </div>
        <div className="topbar-actions">
          <button className="ghost" onClick={onRefresh}>Refresh</button>
          <button className="ghost" onClick={onLogout}>Logout</button>
        </div>
      </header>

      {message ? <p className="success">{message}</p> : null}
      {error ? <p className="error">{error}</p> : null}

      <section className="grid">
        <IntegrationCard
          title="Meta"
          description="Connect Facebook & Instagram messaging APIs using Meta OAuth."
          connected={Boolean(connections.meta?.connected)}
          accountLabel={connections.meta?.accountName}
          onConnect={onConnectMeta}
          onDisconnect={() => onDisconnect('meta')}
          loading={loadingProvider === 'meta'}
        />
        <IntegrationCard
          title="Shopify"
          description="Connect a Shopify store for products, inventory, and order creation workflows."
          connected={Boolean(connections.shopify?.connected)}
          accountLabel={connections.shopify?.shopDomain}
          onConnect={onConnectShopify}
          onDisconnect={() => onDisconnect('shopify')}
          loading={loadingProvider === 'shopify'}
        />
      </section>

      <section className="card notes">
        <h2>Backend expectations</h2>
        <ul>
          <li>Meta connection should grant permissions for Facebook and Instagram messaging.</li>
          <li>Shopify connection should return shop domain, access token, and optional MCP/MCB endpoint availability.</li>
          <li>Backend must store tenant-scoped credentials securely and rotate tokens as needed.</li>
        </ul>
      </section>
    </main>
  );
}
