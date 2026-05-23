## To run the app

```bash
cd ~/binga/bingasys/bingasys-core
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

## Configuration

Optional environment variables live in `.env.example`:

```text
APP_NAME
DATABASE_PATH
```

Shopify and Meta credentials are saved through the API endpoints for this MVP:

```text
PUT /shopify/connection
PUT /meta/connection
```
