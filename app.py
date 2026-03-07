import os
import hashlib
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from psycopg2.extras import RealDictCursor
from psycopg2 import pool, IntegrityError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-123')

# ── Channels — add or rename freely ──
CHANNELS = ['general', 'transmissions', 'off-topic']
DEFAULT_CHANNEL = 'general'

# ── Lazy connection pool ──
_db_pool = None

def get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=os.environ.get('DATABASE_URL')
        )
    return _db_pool

def get_db():
    return get_pool().getconn()

def release_db(conn):
    get_pool().putconn(conn)

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def valid_channel(ch):
    return ch if ch in CHANNELS else DEFAULT_CHANNEL

def migrate_db():
    """Adds channel column to posts if it does not exist. Safe to run every boot."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE posts
                ADD COLUMN IF NOT EXISTS channel VARCHAR(50) DEFAULT 'general'
            """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Migration note: {e}")
    finally:
        release_db(conn)

# ── HOME: redirect to default channel ──
@app.route('/')
def home():
    return redirect(url_for('channel', channel_name=DEFAULT_CHANNEL))

# ── CHANNEL PAGE ──
@app.route('/c/<channel_name>')
def channel(channel_name):
    if 'username' not in session:
        return redirect(url_for('login'))

    channel_name = valid_channel(channel_name)

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
                'WHERE channel = %s '
                'ORDER BY created_at DESC '
                'LIMIT %s OFFSET %s',
                (channel_name, limit, offset)
            )
            posts = cur.fetchall()
    finally:
        release_db(conn)

    return render_template(
        'home.html',
        posts=posts[::-1],
        page=page,
        channel=channel_name,
        channels=CHANNELS
    )

# ── POLL ──
@app.route('/poll')
def poll():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401

    since_id     = request.args.get('since', 0, type=int)
    channel_name = valid_channel(request.args.get('channel', DEFAULT_CHANNEL))

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, username, content FROM posts '
                'WHERE id > %s AND channel = %s '
                'ORDER BY id ASC LIMIT 30',
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
    channel_name = valid_channel(request.form.get('channel', DEFAULT_CHANNEL))

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
    return redirect(url_for('login'))

if __name__ == '__main__':
    migrate_db()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
