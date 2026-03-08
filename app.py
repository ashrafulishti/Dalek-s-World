import os
import re
import hashlib
import hmac
import time
from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, abort
from psycopg2.extras import RealDictCursor
from psycopg2 import pool, IntegrityError

app = Flask(__name__)

# ── Secret key ──
_secret = os.environ.get('SECRET_KEY', '')
if not _secret or _secret == 'default-secret-key-123':
    raise RuntimeError("SECRET_KEY environment variable must be set to a strong random value.")
app.secret_key = _secret

# ── Session hardening ──
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_ENV') != 'development',
    PERMANENT_SESSION_LIFETIME=3600,
)

@app.template_filter('fmtts')
def format_ts(dt):
    if not dt: return ''
    return dt.strftime('%-I:%M %p')

DEFAULT_CHANNEL = 'general'

# ── Rate limiting (in-memory, per-IP) ──
_rate_store: dict[str, list[float]] = {}

def _get_ip() -> str:
    return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()

def rate_limited(max_calls: int = 10, window: int = 60):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method == 'POST':
                ip  = _get_ip()
                now = time.time()
                hits = [t for t in _rate_store.get(ip, []) if now - t < window]
                if len(hits) >= max_calls:
                    return render_template(
                        request.endpoint + '.html',
                        error='Too many attempts. Please wait a minute.'
                    ), 429
                hits.append(now)
                _rate_store[ip] = hits
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# ── Validation ──
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,32}$')

def validate_username(u: str) -> str | None:
    if not USERNAME_RE.match(u):
        return 'Username must be 3-32 chars: letters, numbers, underscores only.'
    return None

def validate_password(p: str) -> str | None:
    if len(p) < 8:
        return 'Password must be at least 8 characters.'
    if len(p) > 128:
        return 'Password too long.'
    return None

# ── DB pool ──
_db_pool = None

def get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pool.ThreadedConnectionPool(
            minconn=2, maxconn=10,
            dsn=os.environ.get('DATABASE_URL')
        )
    return _db_pool

def get_db():
    return get_pool().getconn()

def release_db(conn):
    get_pool().putconn(conn)

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def safe_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ── Migration ──
def migrate_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:

            # users table — created first, everything depends on it
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id         SERIAL PRIMARY KEY,
                    username   VARCHAR(100) UNIQUE NOT NULL,
                    password   VARCHAR(64) NOT NULL,
                    is_admin   BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # posts table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id         SERIAL PRIMARY KEY,
                    username   VARCHAR(100) NOT NULL,
                    content    TEXT NOT NULL,
                    channel    VARCHAR(50) DEFAULT 'general',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # channels table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id         SERIAL PRIMARY KEY,
                    name       VARCHAR(50) UNIQUE NOT NULL,
                    password   VARCHAR(64),
                    created_by VARCHAR(100),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # channel_admins table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_admins (
                    id         SERIAL PRIMARY KEY,
                    channel    VARCHAR(50) NOT NULL,
                    username   VARCHAR(100) NOT NULL,
                    UNIQUE(channel, username)
                )
            """)

            # Safe ALTERs for existing deployments with old schema
            cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS channel VARCHAR(50) DEFAULT 'general'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE")

            # Seed default channels
            for ch, pw in [('general', None), ('fun', None), ('secret', hash_password('changeme'))]:
                cur.execute("""
                    INSERT INTO channels (name, password, created_by)
                    VALUES (%s, %s, 'system')
                    ON CONFLICT (name) DO NOTHING
                """, (ch, pw))

        conn.commit()
        print("Database ready.")
    except Exception as e:
        conn.rollback()
        print(f"Migration error: {e}")
    finally:
        release_db(conn)


# ── Helpers ──
def get_all_channels():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT name, password IS NOT NULL as locked FROM channels ORDER BY id')
            return cur.fetchall()
    finally:
        release_db(conn)

def get_channel(name):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT name, password FROM channels WHERE name=%s', (name,))
            return cur.fetchone()
    finally:
        release_db(conn)

def has_channel_access(channel_name, ch):
    if not ch['password']:
        return True
    if session.get('is_admin'):
        return True
    granted = session.get('channel_access', {})
    return channel_name in granted

def get_channel_admins(channel_name):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT username FROM channel_admins WHERE channel=%s ORDER BY id', (channel_name,))
            return [r['username'] for r in cur.fetchall()]
    finally:
        release_db(conn)

# ── Decorators ──
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# ── Security headers ──
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    return response


# ── HOME ──
@app.route('/')
def home():
    return redirect(url_for('channel', channel_name=DEFAULT_CHANNEL))


# ── CHANNEL ──
@app.route('/c/<channel_name>')
@login_required
def channel(channel_name):
    if not re.match(r'^[A-Za-z0-9_-]{1,50}$', channel_name):
        return redirect(url_for('home'))

    ch = get_channel(channel_name)
    if not ch:
        return redirect(url_for('home'))
    if not has_channel_access(channel_name, ch):
        return redirect(url_for('channel_auth', channel_name=channel_name))

    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    limit, offset = 15, (page - 1) * 15
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, username, content, created_at FROM posts '
                'WHERE channel=%s ORDER BY created_at DESC LIMIT %s OFFSET %s',
                (channel_name, limit, offset)
            )
            posts = cur.fetchall()
    finally:
        release_db(conn)

    return render_template('home.html',
        posts=posts[::-1], page=page,
        channel=channel_name,
        all_channels=get_all_channels(),
        is_admin=session.get('is_admin', False))


# ── CHANNEL AUTH ──
@app.route('/c/<channel_name>/auth', methods=['GET', 'POST'])
@login_required
def channel_auth(channel_name):
    if not re.match(r'^[A-Za-z0-9_-]{1,50}$', channel_name):
        return redirect(url_for('home'))

    ch = get_channel(channel_name)
    if not ch or not ch['password']:
        return redirect(url_for('channel', channel_name=channel_name))

    error = None
    if request.method == 'POST':
        submitted = hash_password(request.form.get('password', ''))
        if safe_compare(submitted, ch['password']):
            acc = session.get('channel_access', {})
            acc[channel_name] = True
            session['channel_access'] = acc
            return redirect(url_for('channel', channel_name=channel_name))
        error = 'Wrong password.'

    return render_template('channel_auth.html',
        channel_name=channel_name, error=error,
        channels=get_all_channels())


# ── ADMIN PANEL ──
@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin():
    msg = None

    if request.method == 'POST':
        action    = request.form.get('action')
        target_ch = request.form.get('channel', '').strip()

        if action in ('add_admin', 'remove_admin', 'change_password', 'remove_password'):
            if not get_channel(target_ch):
                msg = 'Channel not found.'
            elif action == 'add_admin':
                new_user = request.form.get('username', '').strip()
                err = validate_username(new_user) if new_user else 'No username provided.'
                if err:
                    msg = err
                else:
                    conn = get_db()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                'INSERT INTO channel_admins (channel, username) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                                (target_ch, new_user)
                            )
                        conn.commit()
                        msg = f'Added {new_user} to #{target_ch}'
                    except Exception as e:
                        conn.rollback(); msg = str(e)
                    finally:
                        release_db(conn)

            elif action == 'remove_admin':
                rem_user = request.form.get('username', '').strip()
                if rem_user:
                    conn = get_db()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                'DELETE FROM channel_admins WHERE channel=%s AND username=%s',
                                (target_ch, rem_user)
                            )
                        conn.commit()
                        msg = f'Removed {rem_user} from #{target_ch}'
                    except Exception as e:
                        conn.rollback(); msg = str(e)
                    finally:
                        release_db(conn)

            elif action == 'change_password':
                new_pw = request.form.get('new_password', '').strip()
                err = validate_password(new_pw) if new_pw else 'No password provided.'
                if err:
                    msg = err
                else:
                    conn = get_db()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                'UPDATE channels SET password=%s WHERE name=%s',
                                (hash_password(new_pw), target_ch)
                            )
                        conn.commit()
                        msg = f'Password updated for #{target_ch}'
                    except Exception as e:
                        conn.rollback(); msg = str(e)
                    finally:
                        release_db(conn)

            elif action == 'remove_password':
                conn = get_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute('UPDATE channels SET password=NULL WHERE name=%s', (target_ch,))
                    conn.commit()
                    msg = f'#{target_ch} is now public'
                except Exception as e:
                    conn.rollback(); msg = str(e)
                finally:
                    release_db(conn)

        elif action == 'grant_admin':
            target_user = request.form.get('username', '').strip()
            err = validate_username(target_user) if target_user else 'No username provided.'
            if err:
                msg = err
            else:
                conn = get_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute('UPDATE users SET is_admin=TRUE WHERE username=%s', (target_user,))
                        if cur.rowcount == 0:
                            msg = f'User "{target_user}" not found.'
                        else:
                            conn.commit()
                            msg = f'{target_user} granted site admin.'
                except Exception as e:
                    conn.rollback(); msg = str(e)
                finally:
                    release_db(conn)

        elif action == 'revoke_admin':
            target_user = request.form.get('username', '').strip()
            if target_user == session.get('username'):
                msg = 'You cannot revoke your own admin access.'
            elif target_user:
                conn = get_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute('UPDATE users SET is_admin=FALSE WHERE username=%s', (target_user,))
                    conn.commit()
                    msg = f'{target_user} admin access revoked.'
                except Exception as e:
                    conn.rollback(); msg = str(e)
                finally:
                    release_db(conn)

    all_channels = get_all_channels()
    locked = [c for c in all_channels if c['locked']]
    admins_by_channel = {c['name']: get_channel_admins(c['name']) for c in locked}

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT username FROM users WHERE is_admin=TRUE ORDER BY username')
            site_admins = [r['username'] for r in cur.fetchall()]
    finally:
        release_db(conn)

    return render_template('admin.html',
        all_channels=all_channels,
        locked=locked,
        admins_by_channel=admins_by_channel,
        site_admins=site_admins,
        current_user=session.get('username'),
        msg=msg)


# ── POLL ──
@app.route('/poll')
@login_required
def poll():
    channel_name = request.args.get('channel', DEFAULT_CHANNEL)
    if not re.match(r'^[A-Za-z0-9_-]{1,50}$', channel_name):
        return jsonify({'messages': []})

    ch = get_channel(channel_name)
    if not ch:
        return jsonify({'messages': []})
    if not has_channel_access(channel_name, ch):
        return jsonify({'error': 'forbidden'}), 403

    since_id = request.args.get('since', 0, type=int)
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, username, content, created_at FROM posts '
                'WHERE id>%s AND channel=%s ORDER BY id ASC LIMIT 30',
                (since_id, channel_name)
            )
            rows = cur.fetchall()
    finally:
        release_db(conn)

    return jsonify({'messages': [{
        'id': r['id'],
        'username': r['username'],
        'content': r['content'],
        'ts': r['created_at'].strftime('%-I:%M %p') if r.get('created_at') else ''
    } for r in rows]})


# ── POST ──
@app.route('/post', methods=['POST'])
@login_required
def add_post():
    content      = request.form.get('content', '').strip()
    channel_name = request.form.get('channel', DEFAULT_CHANNEL)

    if not re.match(r'^[A-Za-z0-9_-]{1,50}$', channel_name):
        return redirect(url_for('home'))

    ch = get_channel(channel_name)
    if not ch or not has_channel_access(channel_name, ch):
        return redirect(url_for('channel_auth', channel_name=channel_name))
    if not content or len(content) > 500:
        return redirect(url_for('channel', channel_name=channel_name))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO posts (username, content, channel) VALUES (%s,%s,%s)',
                (session['username'], content, channel_name)
            )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        release_db(conn)
    return redirect(url_for('channel', channel_name=channel_name))


# ── LOGIN ──
@app.route('/login', methods=['GET', 'POST'])
@rate_limited(max_calls=10, window=60)
def login():
    if request.method == 'POST':
        user = request.form.get('username', '').strip()
        pwd  = request.form.get('password', '')
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT username, password, is_admin FROM users WHERE username=%s', (user,))
                row = cur.fetchone()
        finally:
            release_db(conn)

        stored_hash = row['password'] if row else 'x' * 64
        if row and safe_compare(hash_password(pwd), stored_hash):
            session.clear()
            session['username'] = row['username']
            session['is_admin'] = bool(row['is_admin'])
            session.permanent = True
            return redirect(url_for('home'))

        return render_template('login.html', error='Invalid credentials.')
    return render_template('login.html', error=None)


# ── REGISTER ──
@app.route('/register', methods=['GET', 'POST'])
@rate_limited(max_calls=5, window=60)
def register():
    if request.method == 'POST':
        user = request.form.get('username', '').strip()
        pwd  = request.form.get('password', '')

        err = validate_username(user) or validate_password(pwd)
        if err:
            return render_template('register.html', error=err)

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO users (username, password) VALUES (%s,%s)',
                    (user, hash_password(pwd))
                )
            conn.commit()
            return redirect(url_for('login'))
        except IntegrityError:
            conn.rollback()
            return render_template('register.html', error='Username already exists.')
        except Exception:
            conn.rollback(); raise
        finally:
            release_db(conn)
    return render_template('register.html', error=None)


# ── LOGOUT ──
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Error handlers ──
@app.errorhandler(403)
def forbidden(e):
    return render_template('login.html', error='Access denied.'), 403


# ── Runs at startup under both gunicorn and direct python ──
migrate_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
