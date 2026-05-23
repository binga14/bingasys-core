export default function IntegrationCard({
  title,
  description,
  connected,
  accountLabel,
  onConnect,
  onDisconnect,
  loading
}) {
  return (
    <article className="integration-card">
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      <div className="integration-status">
        <span className={connected ? 'badge connected' : 'badge'}>
          {connected ? 'Connected' : 'Not Connected'}
        </span>
        {connected && accountLabel ? <small>{accountLabel}</small> : null}
      </div>
      <div className="actions">
        {!connected ? (
          <button onClick={onConnect} disabled={loading}>
            {loading ? 'Connecting...' : `Connect ${title}`}
          </button>
        ) : (
          <button className="danger" onClick={onDisconnect} disabled={loading}>
            {loading ? 'Disconnecting...' : 'Disconnect'}
          </button>
        )}
      </div>
    </article>
  );
}
