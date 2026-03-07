import os
import re
import hashlib
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from psycopg2.extras import RealDictCursor
from psycopg2 import pool, IntegrityError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-123')

@app.template_filter('fmtts')
def format_ts(dt):
    if not dt: return ''
    return dt.strftime('%-I:%M %p')

DEFAULT_CHANNEL = 'general'
HARDCODED_ADMIN = 'ashrafulishti'   # only this user can access /admin

# ── Connection pool ──
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


# ── MIGRATION ──
def migrate_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS channel VARCHAR(50) DEFAULT 'general'")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id         SERIAL PRIMARY KEY,
                    name       VARCHAR(50) UNIQUE NOT NULL,
                    password   VARCHAR(64),
                    created_by VARCHAR(100),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_admins (
                    id         SERIAL PRIMARY KEY,
                    channel    VARCHAR(50) NOT NULL,
                    username   VARCHAR(100) NOT NULL,
                    UNIQUE(channel, username)
                )
            """)
            # Seed channels: general and fun are open, secret is locked
            for ch, pw in [('general', None), ('fun', None), ('secret', hash_password('changeme'))]:
                cur.execute("""
                    INSERT INTO channels (name, password, created_by)
                    VALUES (%s, %s, 'system')
                    ON CONFLICT (name) DO NOTHING
                """, (ch, pw))
            # Seed ashrafulishti as admin of secret channel
            cur.execute("""
                INSERT INTO channel_admins (channel, username)
                VALUES ('secret', %s)
                ON CONFLICT DO NOTHING
            """, (HARDCODED_ADMIN,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Migration: {e}")
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
    """Returns True if current user can access this channel."""
    if not ch['password']:
        return True
    # hardcoded admin always has access
    if session.get('username') == HARDCODED_ADMIN:
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


# ── HOME ──
@app.route('/')
def home():
    return redirect(url_for('channel', channel_name=DEFAULT_CHANNEL))


# ── CHANNEL ──
@app.route('/c/<channel_name>')
def channel(channel_name):
    if 'username' not in session:
        return redirect(url_for('login'))
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
                'SELECT id, username, content, created_at FROM posts WHERE channel=%s '
                'ORDER BY created_at DESC LIMIT %s OFFSET %s',
                (channel_name, limit, offset)
            )
            posts = cur.fetchall()
    finally:
        release_db(conn)

    return render_template('home.html',
        posts=posts[::-1], page=page,
        channel=channel_name, all_channels=get_all_channels(),
        is_admin=(session.get('username') == HARDCODED_ADMIN))


# ── CHANNEL AUTH (password prompt for locked channels) ──
@app.route('/c/<channel_name>/auth', methods=['GET', 'POST'])
def channel_auth(channel_name):
    if 'username' not in session:
        return redirect(url_for('login'))
    ch = get_channel(channel_name)
    if not ch or not ch['password']:
        return redirect(url_for('channel', channel_name=channel_name))

    error = None
    if request.method == 'POST':
        if hash_password(request.form.get('password', '')) == ch['password']:
            acc = session.get('channel_access', {})
            acc[channel_name] = True
            session['channel_access'] = acc
            return redirect(url_for('channel', channel_name=channel_name))
        error = 'Wrong password.'

    return render_template('channel_auth.html',
        channel_name=channel_name, error=error,
        channels=get_all_channels())


# ── ADMIN PANEL (ashrafulishti only) ──
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if session.get('username') != HARDCODED_ADMIN:
        return redirect(url_for('home'))

    msg = None

    if request.method == 'POST':
        action = request.form.get('action')
        target_ch = request.form.get('channel', 'secret')

        if action == 'add_admin':
            new_user = request.form.get('username', '').strip()
            if new_user:
                conn = get_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute('INSERT INTO channel_admins (channel, username) VALUES (%s,%s) ON CONFLICT DO NOTHING', (target_ch, new_user))
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
                        cur.execute('DELETE FROM channel_admins WHERE channel=%s AND username=%s', (target_ch, rem_user))
                    conn.commit()
                    msg = f'Removed {rem_user} from #{target_ch}'
                except Exception as e:
                    conn.rollback(); msg = str(e)
                finally:
                    release_db(conn)

        elif action == 'change_password':
            new_pw = request.form.get('new_password', '').strip()
            if new_pw:
                conn = get_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute('UPDATE channels SET password=%s WHERE name=%s', (hash_password(new_pw), target_ch))
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

    # Gather data for all locked channels
    all_channels = get_all_channels()
    locked = [c for c in all_channels if c['locked']]
    admins_by_channel = {c['name']: get_channel_admins(c['name']) for c in locked}

    return render_template('admin.html',
        all_channels=all_channels,
        locked=locked,
        admins_by_channel=admins_by_channel,
        msg=msg)


# ── POLL ──
@app.route('/poll')
def poll():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    channel_name = request.args.get('channel', DEFAULT_CHANNEL)
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
                'SELECT id, username, content, created_at FROM posts WHERE id>%s AND channel=%s ORDER BY id ASC LIMIT 30',
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
def add_post():
    if 'username' not in session:
        return redirect(url_for('login'))
    content = request.form.get('content', '').strip()
    channel_name = request.form.get('channel', DEFAULT_CHANNEL)
    ch = get_channel(channel_name)
    if not ch or not has_channel_access(channel_name, ch):
        return redirect(url_for('channel_auth', channel_name=channel_name))
    if not content or len(content) > 500:
        return redirect(url_for('channel', channel_name=channel_name))
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO posts (username, content, channel) VALUES (%s,%s,%s)',
                        (session['username'], content, channel_name))
        conn.commit()
    except Exception:
        conn.rollback(); raise
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
                cur.execute('SELECT username FROM users WHERE username=%s AND password=%s',
                            (user, hash_password(pwd)))
                row = cur.fetchone()
        finally:
            release_db(conn)
        if row:
            session['username'] = user
            return redirect(url_for('home'))
        return render_template('login.html', error='Invalid credentials.')
    return render_template('login.html', error=None)


# ── REGISTER ──
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user = request.form.get('username', '').strip()
        pwd  = request.form.get('password', '')
        if not user or not pwd:
            return render_template('register.html', error='Fill in all fields.')
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO users (username, password) VALUES (%s,%s)',
                            (user, hash_password(pwd)))
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


if __name__ == '__main__':
    migrate_db()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
