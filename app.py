import os
import hashlib
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from psycopg2.extras import RealDictCursor
from psycopg2 import pool, IntegrityError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-123')

DEFAULT_CHANNEL = 'general'

# ── Lazy connection pool ──
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

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ── MIGRATION: runs once on boot, safe to re-run ──
def migrate_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Add channel column to posts if missing
            cur.execute("""
                ALTER TABLE posts
                ADD COLUMN IF NOT EXISTS channel VARCHAR(50) DEFAULT 'general'
            """)
            # Create channels table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id         SERIAL PRIMARY KEY,
                    name       VARCHAR(50) UNIQUE NOT NULL,
                    password   VARCHAR(64),          -- NULL means no password
                    created_by VARCHAR(100),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # Seed default channels (no password)
            for ch in ['general', 'transmissions', 'off-topic']:
                cur.execute("""
                    INSERT INTO channels (name, password, created_by)
                    VALUES (%s, NULL, 'system')
                    ON CONFLICT (name) DO NOTHING
                """, (ch,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Migration note: {e}")
    finally:
        release_db(conn)


def get_all_channels():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT name, password IS NOT NULL as locked FROM channels ORDER BY id')
            return cur.fetchall()
    finally:
        release_db(conn)


def channel_exists(name):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT name, password FROM channels WHERE name = %s', (name,))
            return cur.fetchone()
    finally:
        release_db(conn)


# ── HOME ──
@app.route('/')
def home():
    return redirect(url_for('channel', channel_name=DEFAULT_CHANNEL))


# ── CHANNEL PAGE ──
@app.route('/c/<channel_name>')
def channel(channel_name):
    if 'username' not in session:
        return redirect(url_for('login'))

    ch = channel_exists(channel_name)
    if not ch:
        return redirect(url_for('home'))

    # If channel is locked, check session for access
    if ch['password']:
        granted = session.get('channel_access', {})
        if channel_name not in granted:
            # Redirect to password prompt
            return redirect(url_for('channel_auth', channel_name=channel_name))

    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    limit  = 15
    offset = (page - 1) * limit

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, username, content FROM posts '
                'WHERE channel = %s ORDER BY created_at DESC LIMIT %s OFFSET %s',
                (channel_name, limit, offset)
            )
            posts = cur.fetchall()
    finally:
        release_db(conn)

    channels = get_all_channels()

    return render_template(
        'home.html',
        posts=posts[::-1],
        page=page,
        channel=channel_name,
        channels=channels
    )


# ── CHANNEL PASSWORD AUTH ──
@app.route('/c/<channel_name>/auth', methods=['GET', 'POST'])
def channel_auth(channel_name):
    if 'username' not in session:
        return redirect(url_for('login'))

    ch = channel_exists(channel_name)
    if not ch or not ch['password']:
        return redirect(url_for('channel', channel_name=channel_name))

    error = None
    if request.method == 'POST':
        entered = request.form.get('password', '')
        if hash_password(entered) == ch['password']:
            # Store access in session
            access = session.get('channel_access', {})
            access[channel_name] = True
            session['channel_access'] = access
            return redirect(url_for('channel', channel_name=channel_name))
        else:
            error = 'Wrong password. Try again.'

    channels = get_all_channels()
    return render_template(
        'channel_auth.html',
        channel_name=channel_name,
        error=error,
        channels=channels
    )


# ── CREATE CHANNEL ──
@app.route('/create-channel', methods=['POST'])
def create_channel():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401

    name     = request.form.get('name', '').strip().lower()
    password = request.form.get('password', '').strip()

    # Validate name: letters, numbers, hyphens only, max 30 chars
    import re
    if not name or not re.match(r'^[a-z0-9\-]{1,30}$', name):
        return jsonify({'error': 'Invalid channel name. Use lowercase letters, numbers, hyphens only.'}), 400

    hashed_pw = hash_password(password) if password else None

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO channels (name, password, created_by) VALUES (%s, %s, %s)',
                (name, hashed_pw, session['username'])
            )
        conn.commit()
        # Grant creator instant access
        if hashed_pw:
            access = session.get('channel_access', {})
            access[name] = True
            session['channel_access'] = access
        return jsonify({'ok': True, 'name': name})
    except IntegrityError:
        conn.rollback()
        return jsonify({'error': 'Channel already exists.'}), 409
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db(conn)


# ── POLL ──
@app.route('/poll')
def poll():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401

    channel_name = request.args.get('channel', DEFAULT_CHANNEL)
    ch = channel_exists(channel_name)
    if not ch:
        return jsonify({'messages': []})

    # Enforce access for locked channels
    if ch['password']:
        granted = session.get('channel_access', {})
        if channel_name not in granted:
            return jsonify({'error': 'forbidden'}), 403

    since_id = request.args.get('since', 0, type=int)

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, username, content FROM posts '
                'WHERE id > %s AND channel = %s ORDER BY id ASC LIMIT 30',
                (since_id, channel_name)
            )
            rows = cur.fetchall()
    finally:
        release_db(conn)

    return jsonify({'messages': [dict(r) for r in rows]})


# ── POST ──
@app.route('/post', methods=['POST'])
def add_post():
    if 'username' not in session:
        return redirect(url_for('login'))

    content      = request.form.get('content', '').strip()
    channel_name = request.form.get('channel', DEFAULT_CHANNEL)

    ch = channel_exists(channel_name)
    if not ch:
        return redirect(url_for('home'))

    # Enforce access
    if ch['password']:
        granted = session.get('channel_access', {})
        if channel_name not in granted:
            return redirect(url_for('channel_auth', channel_name=channel_name))

    if not content or len(content) > 500:
        return redirect(url_for('channel', channel_name=channel_name))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO posts (username, content, channel) VALUES (%s, %s, %s)',
                (session['username'], content, channel_name)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db(conn)

    return redirect(url_for('channel', channel_name=channel_name))


# ── LOGIN ──
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form.get('username', '').strip()
        pwd  = request.form.get('password', '')

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    'SELECT username FROM users WHERE username=%s AND password=%s',
                    (user, hash_password(pwd))
                )
                user_data = cur.fetchone()
        finally:
            release_db(conn)

        if user_data:
            session['username'] = user
            return redirect(url_for('home'))
        return "Invalid login! <a href='/login'>Try again</a>"

    return render_template('login.html')


# ── REGISTER ──
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user = request.form.get('username', '').strip()
        pwd  = request.form.get('password', '')

        if not user or not pwd:
            return "Username and password required. <a href='/register'>Try again</a>"

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO users (username, password) VALUES (%s, %s)',
                    (user, hash_password(pwd))
                )
            conn.commit()
            return redirect(url_for('login'))
        except IntegrityError:
            conn.rollback()
            return "Username already exists! <a href='/register'>Try again</a>"
        except Exception:
            conn.rollback()
            raise
        finally:
            release_db(conn)

    return render_template('register.html')


# ── LOGOUT ──
@app.route('/logout')
def logout():
    session.pop('username', None)
    session.pop('channel_access', None)
    return redirect(url_for('login'))


if __name__ == '__main__':
    migrate_db()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
