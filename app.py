import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "app.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-secret")
app.config["DATABASE"] = DATABASE


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            start_place TEXT NOT NULL,
            end_place TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        """
    )
    trip_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(trips)").fetchall()
    }
    if "title" not in trip_columns:
        db.execute("ALTER TABLE trips ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    db.commit()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = get_db().execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()


@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        error = None
        if not username:
            error = "ユーザー名を入力してください。"
        elif not password:
            error = "パスワードを入力してください。"
        elif len(password) < 8:
            error = "パスワードは8文字以上にしてください。"

        if error is None:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (
                        username,
                        generate_password_hash(password),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                error = "そのユーザー名はすでに使われています。"
            else:
                flash("登録が完了しました。ログインしてください。", "success")
                return redirect(url_for("login"))
        flash(error, "error")

    return render_template("register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("ユーザー名またはパスワードが正しくありません。", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    active_trip = db.execute(
        """
        SELECT * FROM trips
        WHERE user_id = ? AND ended_at IS NULL
        ORDER BY started_at DESC LIMIT 1
        """,
        (g.user["id"],),
    ).fetchone()
    trips = db.execute(
        """
        SELECT *, CAST((julianday(ended_at) - julianday(started_at)) * 86400 AS INTEGER)
            AS duration_seconds
        FROM trips
        WHERE user_id = ? AND ended_at IS NOT NULL
        ORDER BY started_at DESC
        """,
        (g.user["id"],),
    ).fetchall()
    return render_template("dashboard.html", active_trip=active_trip, trips=trips)


@app.post("/trips/start")
@login_required
def start_trip():
    title = request.form.get("title", "").strip()
    start_place = request.form.get("start_place", "").strip()
    if not title:
        flash("タイトルを入力してください。", "error")
        return redirect(url_for("dashboard"))
    if not start_place:
        flash("開始地点を入力してください。", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    active = db.execute(
        "SELECT id FROM trips WHERE user_id = ? AND ended_at IS NULL",
        (g.user["id"],),
    ).fetchone()
    if active:
        flash("現在計測中の移動があります。先に終了してください。", "error")
    else:
        db.execute(
            """
            INSERT INTO trips (user_id, title, start_place, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                g.user["id"],
                title,
                start_place,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
        flash("計測を開始しました。", "success")
    return redirect(url_for("dashboard"))


@app.post("/trips/<int:trip_id>/finish")
@login_required
def finish_trip(trip_id):
    end_place = request.form.get("end_place", "").strip()
    if not end_place:
        flash("終了地点を入力してください。", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    trip = db.execute(
        "SELECT id FROM trips WHERE id = ? AND user_id = ? AND ended_at IS NULL",
        (trip_id, g.user["id"]),
    ).fetchone()
    if trip is None:
        flash("計測中の移動が見つかりません。", "error")
    else:
        db.execute(
            "UPDATE trips SET end_place = ?, ended_at = ? WHERE id = ?",
            (end_place, datetime.now(timezone.utc).isoformat(), trip_id),
        )
        db.commit()
        flash("計測を終了しました。", "success")
    return redirect(url_for("dashboard"))


@app.post("/trips/<int:trip_id>/delete")
@login_required
def delete_trip(trip_id):
    db = get_db()
    deleted = db.execute(
        "DELETE FROM trips WHERE id = ? AND user_id = ?",
        (trip_id, g.user["id"]),
    )
    db.commit()
    if deleted.rowcount == 0:
        flash("削除対象の履歴が見つかりません。", "error")
    else:
        flash("履歴を削除しました。", "success")
    return redirect(url_for("dashboard"))


def format_duration(seconds):
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}時間 {minutes}分"
    return f"{minutes}分 {seconds}秒"


app.jinja_env.filters["duration"] = format_duration


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)
