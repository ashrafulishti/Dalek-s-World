import os
import psycopg2
from flask import Flask, render_template, request, session, redirect, url_for
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-123')

def get_db():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

@app.route('/')
def home():
    # 1. Force Login Check
    if 'username' not in session:
        return redirect(url_for('login'))
        
    # 2. Fix Pagination: Always default to page 1
    try:
        page = int(request.args.get('page', 1))
        if page < 1: page = 1 # Safety check
    except ValueError:
        page = 1
        
    limit = 15
    offset = (page - 1) * limit
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Fetch newest posts
    cur.execute('''SELECT * FROM posts ORDER BY created_at DESC LIMIT %s OFFSET %s''', (limit, offset))
    posts = cur.fetchall()
    cur.close(); conn.close()
    
    # Reverse to show newest at bottom
    return render_template('home.html', posts=posts[::-1], page=page)

@app.route('/post', methods=['POST'])
def add_post():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    content = request.form.get('content')
    if content:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO posts (username, content) VALUES (%s, %s)', (session['username'], content))
        conn.commit()
        cur.close(); conn.close()
    return redirect(url_for('home'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user, pwd = request.form['username'], request.form['password']
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT * FROM users WHERE username=%s AND password=%s', (user, pwd))
        user_data = cur.fetchone()
        cur.close(); conn.close()
        if user_data:
            session['username'] = user
            return redirect(url_for('home'))
        return "Invalid login! <a href='/login'>Try again</a>"
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user, pwd = request.form['username'], request.form['password']
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO users (username, password) VALUES (%s, %s)', (user, pwd))
            conn.commit()
            return redirect(url_for('login'))
        except:
            return "Username exists! <a href='/register'>Try again</a>"
        finally:
            cur.close(); conn.close()
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
