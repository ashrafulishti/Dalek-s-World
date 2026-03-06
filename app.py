import os
import psycopg2
from flask import Flask, render_template, request, session, redirect, url_for
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
# Use an environment variable for security, default for local testing
app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-key-123')

def get_db():
    # Ensure DATABASE_URL is set in Render Environment Variables
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

@app.route('/')
def home():
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
        
    limit = 15
    offset = (page - 1) * limit
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT * FROM posts ORDER BY created_at DESC LIMIT %s OFFSET %s', (limit, offset))
    posts = cur.fetchall()
    cur.close(); conn.close()
    
    return render_template('home.html', posts=posts[::-1], page=page)

@app.route('/post', methods=['POST'])
def add_post():
    if 'username' in session:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO posts (username, content) VALUES (%s, %s)', 
                    (session['username'], request.form['content']))
        conn.commit()
        cur.close(); conn.close()
    return redirect(url_for('home'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO users (username, password) VALUES (%s, %s)', 
                        (request.form['username'], request.form['password']))
            conn.commit()
            return redirect(url_for('login'))
        except:
            return "Username exists!"
        finally:
            cur.close(); conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT * FROM users WHERE username=%s AND password=%s', 
                    (request.form['username'], request.form['password']))
        user = cur.fetchone()
        cur.close(); conn.close()
        if user:
            session['username'] = user['username']
            return redirect(url_for('home'))
        return "Invalid login!"
    return render_template('login.html')

if __name__ == '__main__':
    # Render overrides this, but this keeps local dev working
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
