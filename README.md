# Bingasys Core

FastAPI backend for Bingasys auth, onboarding, Shopify OAuth, Meta integration settings, and webhooks.

## Prerequisites

- Python 3.9+
- Docker Desktop or another Docker runtime
- ngrok for local Shopify OAuth testing
- A Shopify app in the Shopify Dev Dashboard

## Run Locally

Start Postgres:

```bash
docker compose up -d postgres
```

Create the backend environment file:

```bash
cp .env.example .env
```

Fill in the required secrets in `.env`, especially:

```env
AUTH_SECRET_KEY=
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bingasys
SHOPIFY_CLIENT_ID=
SHOPIFY_CLIENT_SECRET=
SHOPIFY_SCOPES=read_products,read_orders,write_orders,read_inventory,write_inventory,read_locations
```

Create a virtual environment and start the API:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

Health check:

```text
http://127.0.0.1:8000/api/health
```

## Shopify OAuth Locally

Expose the backend with ngrok:

```bash
ngrok http 8000
```

If you have a reserved ngrok domain, use it so the Shopify app URL stays stable:

```bash
ngrok http 8000 --url https://your-ngrok-domain.ngrok-free.dev
```

Update `.env`:

```env
BACKEND_URL=https://your-ngrok-domain.ngrok-free.dev
SHOPIFY_REDIRECT_URI=https://your-ngrok-domain.ngrok-free.dev/api/integrations/shopify/oauth/callback
FRONTEND_URL=http://localhost:5173
```

In the Shopify Dev Dashboard, configure and release an app version with:

```text
App URL:
https://your-ngrok-domain.ngrok-free.dev

Redirect URL:
https://your-ngrok-domain.ngrok-free.dev/api/integrations/shopify/oauth/callback
```

Required Admin API scopes:

```text
read_products
read_orders
write_orders
read_inventory
write_inventory
read_locations
```

After changing scopes, release the Shopify app version and reconnect/reinstall the app on the test store.

## Useful Checks

Check database tables:

```bash
PGPASSWORD=postgres psql -h localhost -U postgres -d bingasys -c "\dt"
```

Check saved Shopify connection without exposing the full token:

```bash
PGPASSWORD=postgres psql -h localhost -U postgres -d bingasys -c "SELECT user_id, shopify_store_domain, shopify_access_token IS NOT NULL AS has_token, shopify_refresh_token IS NOT NULL AS has_refresh_token, updated_at FROM integration_settings;"
```

## Notes

- Do not commit `.env`; it contains secrets.
- Database tables are created automatically on backend startup.
- Shopify access tokens are stored per user/store in Postgres after OAuth completes.
- Newly created Shopify public apps require expiring offline tokens; the backend requests `expiring=1` during OAuth token exchange.
