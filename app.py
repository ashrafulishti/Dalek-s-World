import os
import hashlib
from flask import Flask, render_template, request, session, redirect, url_for
from psycopg2.extras import RealDictCursor
from psycopg2 import pool, IntegrityError

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-123')

# ── Lazy connection pool: only created on first request ──
# This lets the app start and bind its port even if the DB is slow to wake up.
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
    """Borrow a connection from the pool."""
    return get_pool().getconn()

def release_db(conn):
    """Return connection to pool (does NOT close it)."""
    get_pool().putconn(conn)

def hash_password(password: str) -> str:
    """Simple SHA-256 hash. Use bcrypt in production for real security."""
    return hashlib.sha256(password.encode()).hexdigest()

# ── HOME ──
@app.route('/')
def home():
    if 'username' not in session:
        return redirect(url_for('login'))

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
                'SELECT username, content FROM posts '   # only fetch columns you use
                'ORDER BY created_at DESC '
                'LIMIT %s OFFSET %s',
                (limit, offset)
            )
            posts = cur.fetchall()
    finally:
        release_db(conn)   # always return to pool, even on error

    return render_template('home.html', posts=posts[::-1], page=page)

# ── POLL (lightweight JSON endpoint for live updates) ──
@app.route('/poll')
def poll():
    """Returns only new messages since a given post id. Tiny payload, no HTML rendering."""
    if 'username' not in session:
        return {'error': 'unauthorized'}, 401

    since_id = request.args.get('since', 0, type=int)

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, username, content FROM posts '
                'WHERE id > %s ORDER BY id ASC LIMIT 30',
                (since_id,)
            )
            rows = cur.fetchall()
    finally:
        release_db(conn)

    return {'messages': [dict(r) for r in rows]}

# ── POST ──
@app.route('/post', methods=['POST'])
def add_post():
    if 'username' not in session:
        return redirect(url_for('login'))

    content = request.form.get('content', '').strip()
    if not content or len(content) > 500:          # validate length server-side too
        return redirect(url_for('home'))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO posts (username, content) VALUES (%s, %s)',
                (session['username'], content)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db(conn)

    return redirect(url_for('home'))

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
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
