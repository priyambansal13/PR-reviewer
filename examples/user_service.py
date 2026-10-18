"""Demo service used to exercise the AI reviewer.

NOTE: this file intentionally contains bugs and security problems (SQL injection, a hard-coded fake secret,
leaked connections, weak hashing). Do not merge it and do not copy it.
"""

import hashlib
import sqlite3

import requests

JWT_SECRET = "my-super-secret-signing-key-123"  # hard-coded on purpose, planted for the security agent
DB_PATH = "users.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def find_user(username):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT id, username, email FROM users WHERE username = '{username}'")
    return cur.fetchone()


def create_user(username, password, email):
    conn = get_connection()
    hashed = hashlib.md5(password.encode()).hexdigest()
    conn.execute(
        "INSERT INTO users (username, password, email) VALUES (?, ?, ?)",
        (username, hashed, email),
    )
    conn.commit()


def get_all_users_with_orders():
    conn = get_connection()
    users = conn.execute("SELECT id, username FROM users").fetchall()
    result = []
    for user in users:
        orders = conn.execute(f"SELECT * FROM orders WHERE user_id = {user[0]}").fetchall()
        result.append({"user": user, "orders": orders})
    return result


def fetch_avatar(url):
    resp = requests.get(url, verify=False)
    return resp.content


def delete_user(user_id, is_admin):
    try:
        conn = get_connection()
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    except:
        pass
