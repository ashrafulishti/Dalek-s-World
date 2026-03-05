from flask import Flask, render_template, request, session, redirect, url_for
import os
import psycopg2

app = Flask(__name__)
app.secret_key = 'super-secret-key-123'

def get_db():
    # This connects to the Cloud Locker using the link you saved in Render
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn

@app.route('/')
def home():
    conn = get_db()
    cur = conn.cursor()
    # Fetch posts
    cur.execute('SELECT * FROM posts ORDER BY created_at ASC')
    posts = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('home.html', posts=posts)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO users (username, password) VALUES (%s, %s)', (user, pwd))
            conn.commit()
            return "Registered! <a href='/login'>Login here</a>"
        except:
            return "Username exists! <a href='/register'>Try again</a>"
        finally:
            cur.close()
            conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE username=%s AND password=%s', (user, pwd))
        user_data = cur.fetchone()
        cur.close()
        conn.close()
        if user_data:
            session['username'] = user
            return redirect(url_for('home'))
        return "Invalid login! <a href='/login'>Try again</a>"
    return render_template('login.html')

@app.route('/post', methods=['POST'])
def add_post():
    if 'username' in session:
        content = request.form['content']
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO posts (username, content) VALUES (%s, %s)', (session['username'], content))
        conn.commit()
        cur.close()
        conn.close()
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('home'))

if __name__ == '__main__':
    conn = get_db()
    cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT)')
    cur.execute('''CREATE TABLE IF NOT EXISTS posts 
                  (id SERIAL PRIMARY KEY, username TEXT, content TEXT, 
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    cur.close()
    conn.close()
    app.run(host='0.0.0.0', port=5000)
