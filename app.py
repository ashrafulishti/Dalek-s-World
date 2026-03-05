from flask import Flask, render_template, request, session, redirect, url_for
import sqlite3

app = Flask(__name__)
app.secret_key = 'super-secret-key-123'

def get_db():
    conn = sqlite3.connect('data.db')
    conn.row_factory = sqlite3.Row 
    return conn

@app.route('/')
def home():
    conn = get_db()
    # Fetch posts sorted by time (newest first)
    posts = conn.execute('SELECT * FROM posts ORDER BY created_at ASC').fetchall()
    conn.close()
    return render_template('home.html', posts=posts)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        conn = get_db()
        try:
            conn.execute('INSERT INTO users (username, password) VALUES (?, ?)', (user, pwd))
            conn.commit()
            return "Registered! <a href='/login'>Login here</a>"
        except sqlite3.IntegrityError:
            return "Username exists! <a href='/register'>Try again</a>"
        finally:
            conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        conn = get_db()
        user_data = conn.execute('SELECT * FROM users WHERE username=? AND password=?', (user, pwd)).fetchone()
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
        # Database automatically handles the 'created_at' timestamp
        conn.execute('INSERT INTO posts (username, content) VALUES (?, ?)', (session['username'], content))
        conn.commit()
        conn.close()
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('home'))

if __name__ == '__main__':
    # Setup tables with the new timestamp column
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS posts 
                  (id INTEGER PRIMARY KEY, username TEXT, content TEXT, 
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
    app.run(host='0.0.0.0', port=5000, debug=True)