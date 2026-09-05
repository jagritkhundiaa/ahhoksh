# Railway Deploy — Telegram Bot

Ready-to-push folder for [Railway](https://railway.app).

## What's included

- `bot-6.py` — main bot
- `checker_bridge.py` — Shopify bridge
- `Procfile` — tells Railway to run the worker
- `requirements.txt` — Python dependencies
- `runtime.txt` — Python 3.12
- `.env.example` — copy this to `.env` locally; use Railway Variables in production

## Before you deploy

This bot imports these local modules:

- `auth.py`
- `helpers.py`
- `hit.py`

If you have them, copy them into this same folder before pushing. If they are already inside `bot-6.py`, you can skip this.

Also add any static files the bot needs:

- `sites.txt`
- `gifs/` folder (animated GIFs)
- `proxies.txt` (if you use a static proxy file)

## Deploy steps

1. **Create a Railway account** at https://railway.app
2. **Install the Railway CLI** (optional but useful):
   ```bash
   npm i -g @railway/cli
   railway login
   ```
3. **Create a new project**:
   ```bash
   cd railway-bot
   railway init
   ```
4. **Set environment variables**:
   ```bash
   railway variables set BOT_TOKEN=your_token_here
   ```
   Or use the Railway dashboard: **Project → Variables**.
5. **Deploy**:
   ```bash
   railway up
   ```
   Or push this folder to a GitHub repo and deploy from GitHub in Railway.

## Important notes

- Use a **Worker** service, not a web service. Telegram bots don't need an HTTP port.
- Railway restarts containers often. Local files created at runtime (`sites.txt`, logs, data files) will be lost unless you:
  - Store them in an external database (Supabase / PostgreSQL)
  - Or commit static files like `sites.txt` and `gifs/` to the repo
- For persistent proxy/user data, move JSON storage to a database or external file store.
