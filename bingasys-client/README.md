# Bingasys Client

A basic React frontend dashboard for tenant login and integration setup.

## Features

- Email/password login (delegates auth to backend).
- Connect Meta account (Facebook + Instagram APIs via backend OAuth).
- Connect Shopify store (product, inventory, and order orchestration backend).
- Integration status display and disconnect actions.

## Expected Backend Endpoints

- `POST /auth/login` -> `{ token, user }`
- `GET /integrations` -> `{ meta: {...}, shopify: {...} }`
- `POST /integrations/meta/connect` -> `{ redirectUrl? }`
- `POST /integrations/shopify/connect` -> `{ redirectUrl? }`
- `DELETE /integrations/:provider`

## Setup

```bash
npm install
npm run dev
```

Optional env:

```bash
VITE_API_BASE_URL=http://localhost:3000
```
