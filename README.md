# DALEKS — Real-Time Chat Web App

> A Discord-style group chat application built with Python (Flask) and PostgreSQL.  
> Features channels, locked rooms, live polling, an admin panel, and session-based auth.  
> Built by **ashrafulishti**.

---

## Table of Contents

1. [What Is This?](#what-is-this)
2. [Feature Overview](#feature-overview)
3. [Project File Structure](#project-file-structure)
4. [How It Works — Technical Deep Dive](#how-it-works--technical-deep-dive)
5. [Environment Variables](#environment-variables)
6. [Running Locally](#running-locally)
7. [Hosting on Render](#hosting-on-render)
8. [Database Setup (Neon)](#database-setup-neon)
9. [First-Time Setup After Deploy](#first-time-setup-after-deploy)
10. [Accessing the Admin Panel](#accessing-the-admin-panel)
11. [Resetting the Database](#resetting-the-database)
12. [Security Features](#security-features)
13. [Troubleshooting](#troubleshooting)

---

## What Is This?

DALEKS is a lightweight real-time group chat app. It looks and feels similar to Discord — with a server rail, channel sidebar, message feed, and a composer. It runs entirely on Python (Flask) on the backend and plain HTML/CSS/JavaScript on the frontend. There is no WebSocket — messages are delivered via polling every 2.5 seconds, which keeps the stack simple and hosting costs at zero.

It is designed to be hosted for free using:
- **Render** — for the Python web server
- **Neon** — for the PostgreSQL database

---

## Feature Overview

### Channels
- Three default channels: `#general`, `#fun`, `#secret`
- `#general` and `#fun` are open to all registered users
- `#secret` is password-locked — users must enter a channel password to gain access
- Channel access is stored in the session, so you only need to enter the password once per login
- Locked channels show a 🔒 icon in the sidebar

### Messaging
- Messages are paginated — 15 per page, newest first
- New messages appear automatically every 2.5 seconds via background polling (no page refresh needed)
- Messages are grouped by user — consecutive messages from the same person are collapsed visually
- Each user gets a consistent avatar color and username color derived from their username hash
- Messages are capped at 500 characters

### Authentication
- Register with a username (3–32 chars, letters/numbers/underscores) and password (min 8 chars)
- Passwords are hashed with SHA-256 before storage — never stored in plain text
- Sessions expire after 1 hour of inactivity
- Login is rate-limited to 10 attempts per minute per IP
- Registration is rate-limited to 5 attempts per minute per IP

### Admin Panel
- Accessible at `/admin` — only visible to users with `is_admin = TRUE` in the database
- From the admin panel you can:
  - See all channels and their lock status
  - Add or remove channel-level admins (users who can access a locked channel)
  - Change the password of a locked channel
  - Remove the lock from a channel (make it public)
  - Grant site admin access to any registered user
  - Revoke site admin access from other admins (cannot revoke your own)

### Security
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` (HTTPS only in production)
- Constant-time password comparison (`hmac.compare_digest`) prevents timing attacks
- Security headers on every response: `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`, `Referrer-Policy`
- Channel name inputs are validated against a strict regex to prevent injection
- `SECRET_KEY` is required at startup — the app refuses to run without it

---

## Project File Structure

```
your-project/
│
├── app.py                  # Main application — all routes, logic, DB, security
├── requirements.txt        # Python dependencies
├── render.yaml             # Render deployment config (optional)
│
└── templates/
    ├── home.html           # Main chat interface (Discord-style layout)
    ├── admin.html          # Admin panel
    ├── login.html          # Login page
    ├── register.html       # Registration page
    └── channel_auth.html   # Password prompt for locked channels
```

> Flask looks for templates in a folder literally named `templates/` next to `app.py`.  
> If your HTML files are not inside that folder, they will not be found.

---

## How It Works — Technical Deep Dive

### Startup & Database Migration

When the app starts (whether via `python app.py` or `gunicorn`), `migrate_db()` runs immediately at module level:

```python
migrate_db()  # bottom of app.py, outside any if block

if __name__ == '__main__':
    app.run(...)
```

`migrate_db()` uses `CREATE TABLE IF NOT EXISTS` for every table, so it is completely safe to run on every startup. If the tables already exist, nothing changes. If they don't exist (fresh database), it creates them and seeds the default channels.

**Table creation order matters:**
1. `users` — created first, no dependencies
2. `posts` — depends on nothing but references users by username string
3. `channels` — independent
4. `channel_admins` — depends on channels and users conceptually

After creating tables, it runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` to backfill any columns that might be missing on older installs (like `is_admin` or `channel`).

### Database Connection Pooling

Rather than opening a new database connection on every request, the app uses `psycopg2`'s `ThreadedConnectionPool`:

```python
_db_pool = pool.ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DATABASE_URL)
```

- `get_db()` — borrows a connection from the pool
- `release_db(conn)` — returns it when done
- Always called in a `try/finally` block so connections are never leaked

### Session & Authentication Flow

1. User submits login form → `POST /login`
2. App queries `users` table for that username
3. Submitted password is hashed with SHA-256 and compared using `hmac.compare_digest` (constant-time, prevents timing attacks)
4. If valid: `session.clear()` (prevents session fixation), then `session['username']` and `session['is_admin']` are set
5. Session is signed with `SECRET_KEY` — Flask uses this to generate a tamper-proof cookie signature
6. On every protected route, `@login_required` decorator checks `session['username']` exists

### Real-Time Polling

The frontend JavaScript polls `/poll?since=<last_id>&channel=<name>` every 2.5 seconds:

```javascript
setInterval(() => { if (polling) doPoll(); }, 2500);
```

The server returns only messages with `id > since_id`, so responses are tiny after the initial load. When the browser tab is hidden, polling pauses automatically (`visibilitychange` event) to save resources.

### Channel Access Control

```python
def has_channel_access(channel_name, ch):
    if not ch['password']:       # open channel
        return True
    if session.get('is_admin'):  # site admin bypasses all locks
        return True
    granted = session.get('channel_access', {})
    return channel_name in granted
```

When a user successfully enters a channel password, the channel name is added to `session['channel_access']` — a dict stored in their signed cookie. No extra DB queries needed on subsequent visits.

---

## Environment Variables

You must set these before running the app.

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ Yes | Random string used to sign session cookies. Must be long and secret. |
| `DATABASE_URL` | ✅ Yes | PostgreSQL connection string. Format: `postgresql://user:pass@host/dbname` |
| `FLASK_ENV` | Optional | Set to `development` to disable HTTPS-only cookies when running locally |

### Generating a SECRET_KEY

Run this once in your terminal:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

This outputs a 64-character random hex string like:
```
3f7a2c1e9b4d8f6a0e5c2b7d1a9f3e8c4b6d2a7f1e5c9b3d8a4f6e2c0b7d1a9f
```

Copy that and use it as your `SECRET_KEY`. Never commit it to Git.

---

## Running Locally

### Prerequisites
- Python 3.10 or higher
- A PostgreSQL database (local install, or use Neon's free tier)

### Steps

**1. Clone or download the project**
```bash
git clone https://github.com/yourname/daleks.git
cd daleks
```

**2. Create a virtual environment**
```bash
python -m venv venv
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Set environment variables**

On Mac/Linux:
```bash
export SECRET_KEY="your_random_secret_key_here"
export DATABASE_URL="postgresql://user:password@localhost/daleks"
export FLASK_ENV="development"
```

On Windows (Command Prompt):
```cmd
set SECRET_KEY=your_random_secret_key_here
set DATABASE_URL=postgresql://user:password@localhost/daleks
set FLASK_ENV=development
```

Or create a `.env` file and use `python-dotenv` — but make sure to add `.env` to `.gitignore`.

**5. Run the app**
```bash
python app.py
```

The app will:
- Connect to your database
- Run `migrate_db()` — creates all tables automatically
- Start on `http://localhost:10000`

**6. Open in browser**
```
http://localhost:10000
```

Register an account, then grant yourself admin via SQL (see [First-Time Setup](#first-time-setup-after-deploy)).

---

## Hosting on Render

Render is a free cloud hosting platform. The app is pre-configured for it via `render.yaml`.

### Steps

**1. Push your code to GitHub**

Make sure your repo contains:
- `app.py`
- `requirements.txt`
- `render.yaml`
- `templates/` folder with all HTML files

Do **not** commit `.env` files or `data.db`.

**2. Create a Render account**

Go to [render.com](https://render.com) and sign up.

**3. Create a new Web Service**

- Click **New → Web Service**
- Connect your GitHub repo
- Render will auto-detect `render.yaml` and fill in the settings

**4. Set environment variables on Render**

Go to your service → **Environment** tab → add:

| Key | Value |
|---|---|
| `SECRET_KEY` | Your generated random string |
| `DATABASE_URL` | Your Neon connection string |

**5. Deploy**

Click **Deploy**. Render will:
1. Run `pip install -r requirements.txt`
2. Start the app with `gunicorn app:app`
3. `migrate_db()` runs at startup — tables are created automatically

Your app will be live at `https://your-service-name.onrender.com`.

> **Note:** On Render's free tier, the service sleeps after 15 minutes of inactivity and takes ~30 seconds to wake up on the next request. This is normal.

---

## Database Setup (Neon)

Neon is a free serverless PostgreSQL provider. No setup SQL is needed — `migrate_db()` handles everything.

### Getting your DATABASE_URL

1. Go to [neon.tech](https://neon.tech) and create a free account
2. Create a new project
3. Go to **Dashboard → Connection Details**
4. Copy the connection string — it looks like:
   ```
   postgresql://username:password@ep-something.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
5. Paste this as your `DATABASE_URL` environment variable

That's it. The app creates all tables on first startup.

---

## First-Time Setup After Deploy

After deploying and creating your account:

**1. Register your account** at `/register`

**2. Grant yourself admin access** — go to Neon's SQL Editor and run:

```sql
UPDATE users SET is_admin = TRUE WHERE username = 'your_username';
```

**3. Log out and log back in** — the session needs to be refreshed to pick up the new `is_admin = TRUE` value.

**4. The ⚙ icon** will now appear in the bottom-left of the sidebar. Click it or go to `/admin`.

**5. Change the `#secret` channel password** — in the admin panel, find `#secret` and click **UPDATE** with a new password. The default is `changeme` and should be changed immediately.

---

## Accessing the Admin Panel

The admin panel is at:
```
https://your-app.onrender.com/admin
```

You can also access it by:
- Clicking the ⚙ icon in the bottom-left of the chat sidebar (only visible if you are an admin)
- Typing `/admin` directly in the browser URL bar

If you are not logged in as an admin, the URL returns a 403 and redirects to the login page. There is no visual link to `/admin` for non-admin users.

### What You Can Do in the Admin Panel

**Site Admins section:**
- See all users who have `is_admin = TRUE`
- Grant admin to any registered username
- Revoke admin from other admins (you cannot revoke yourself)

**Channel sections (one per locked channel):**
- See which users have been granted channel-admin access (bypass the password)
- Add or remove channel admins
- Change the channel password
- Remove the lock entirely (make the channel public)

---

## Resetting the Database

This wipes **everything** — all users, messages, channels, admins. Use when you want a completely fresh start.

### Step 1 — Run this in Neon's SQL Editor:

```sql
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
```

This nukes all tables without needing to know their names.

### Step 2 — Restart your Render service

Go to Render → your service → **Manual Deploy** or just wait for the next deploy. On startup, `migrate_db()` recreates all tables and seeds the default channels fresh.

### Step 3 — Re-grant yourself admin

After resetting, re-register your account and run the SQL again:

```sql
UPDATE users SET is_admin = TRUE WHERE username = 'your_username';
```

Then log out and back in.

> **Important:** Dropping the schema does NOT affect your Neon project or connection string. Your `DATABASE_URL` stays valid. Only the data and table structure inside the database is removed.

---

## Security Features

| Feature | Details |
|---|---|
| Password hashing | SHA-256 via `hashlib` — passwords are never stored in plain text |
| Timing attack prevention | `hmac.compare_digest` used for all password comparisons |
| Session fixation prevention | `session.clear()` called before setting new session on login |
| Session signing | Flask signs cookies using `SECRET_KEY` — tampering breaks the signature |
| Cookie hardening | `HttpOnly`, `SameSite=Lax`, `Secure` (HTTPS only in production) |
| Session expiry | Sessions expire after 1 hour (`PERMANENT_SESSION_LIFETIME=3600`) |
| Rate limiting | Login: 10 attempts/min per IP. Register: 5 attempts/min per IP |
| Input validation | Usernames: regex `^[A-Za-z0-9_]{3,32}$`. Passwords: 8–128 chars |
| Channel name validation | All channel name params validated against `^[A-Za-z0-9_-]{1,50}$` |
| Security headers | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `CSP`, `Referrer-Policy` |
| Forced SECRET_KEY | App refuses to start if `SECRET_KEY` is missing or set to the default value |
| Admin access | `is_admin` stored in DB — no hardcoded usernames anywhere in the code |

---

## Troubleshooting

### "SECRET_KEY environment variable must be set"
The app will not start without a proper secret key. Set it in your environment or on Render's Environment tab. Generate one with:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### "relation users does not exist" on register/login
The database migration failed silently. Check Render logs for a `Migration error:` line. Most likely causes:
- `DATABASE_URL` is not set or is wrong
- The Neon database is paused (free tier pauses after inactivity — just visit the Neon dashboard to wake it)

### Can register but can't access admin
You need to run the SQL grant command and then **log out and log in again**. The session caches `is_admin` at login time — a database update does not affect an existing session.

### Messages not appearing in real time
The poll runs every 2.5 seconds. If it stops working, check the browser console for 401 (session expired — log in again) or 403 (lost channel access — re-enter channel password).

### App on Render takes 30 seconds to load
This is expected on the free tier. Render spins down inactive services. The first request after a period of inactivity wakes it up. Paid tiers keep the service always-on.

### Can't log in after database reset
After `DROP SCHEMA public CASCADE`, all user accounts are gone. You need to re-register and re-grant admin via SQL.

---

## Requirements

```
Flask==3.1.3
gunicorn==25.1.0
psycopg2-binary
Werkzeug==3.1.6
```

Full list in `requirements.txt`.

---

*DALEKS — built by ashrafulishti*
