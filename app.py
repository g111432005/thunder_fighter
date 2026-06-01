"""
app.py — 雷霆戰機 Flask 後端

路由：
  GET  /              首頁（排行榜）
  GET  /status        即時遊戲狀態
  GET  /records       歷史紀錄頁面
  POST /api/update    微控制器推送遊戲狀態
  GET  /api/records   取得歷史紀錄（JSON）
  GET  /api/status    取得即時狀態（JSON，供輪詢 / WebSocket fallback）

資料庫：SQLite，檔案 game.db（自動建立）
即時推送：Flask-SocketIO（namespace /game）
"""

import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "game.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = "thunder-fighter-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# 記憶體中的最新遊戲狀態（每次 POST /api/update 更新）
_current_state = {
    "score":  0,
    "level":  1,
    "lives":  3,
    "status": "waiting",
}


# ══════════════════════════════════════════════════════════
# 資料庫初始化
# ══════════════════════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_records (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                score    INTEGER NOT NULL,
                level    INTEGER NOT NULL,
                datetime TEXT    NOT NULL
            )
        """)
        conn.commit()


def save_record(score: int, level: int):
    dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO game_records (score, level, datetime) VALUES (?, ?, ?)",
            (score, level, dt)
        )
        conn.commit()


def get_records(limit: int = 50) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, score, level, datetime FROM game_records "
            "ORDER BY score DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"id": r[0], "score": r[1], "level": r[2], "datetime": r[3]}
            for r in rows]


def get_top_scores(limit: int = 10) -> list:
    return get_records(limit)


# ══════════════════════════════════════════════════════════
# 頁面路由
# ══════════════════════════════════════════════════════════

@app.route("/")
def index():
    top = get_top_scores(10)
    return render_template("index.html", top_scores=top)


@app.route("/status")
def status_page():
    return render_template("status.html")


@app.route("/records")
def records_page():
    records = get_records(50)
    return render_template("records.html", records=records)


# ══════════════════════════════════════════════════════════
# REST API
# ══════════════════════════════════════════════════════════

@app.route("/api/update", methods=["POST"])
def api_update():
    """微控制器 POST 遊戲狀態。"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid JSON"}), 400

    required = {"score", "level", "lives", "status"}
    if not required.issubset(data.keys()):
        return jsonify({"success": False, "message": "Missing fields"}), 400

    _current_state.update({
        "score":  int(data["score"]),
        "level":  int(data["level"]),
        "lives":  int(data["lives"]),
        "status": str(data["status"]),
    })

    # 遊戲結束時儲存紀錄
    if data["status"] == "gameover":
        save_record(data["score"], data["level"])

    # 透過 WebSocket 廣播給瀏覽器
    socketio.emit("game_state", _current_state, namespace="/game")

    return jsonify({"success": True, "message": "Game state updated."})


@app.route("/api/records", methods=["GET"])
def api_records():
    """取得歷史紀錄（JSON）。"""
    records = get_records(50)
    return jsonify({"records": records})


@app.route("/api/status", methods=["GET"])
def api_status():
    """取得目前遊戲狀態（JSON，供輪詢）。"""
    return jsonify(_current_state)


# ══════════════════════════════════════════════════════════
# WebSocket 事件
# ══════════════════════════════════════════════════════════

@socketio.on("connect", namespace="/game")
def on_connect():
    emit("game_state", _current_state)


# ══════════════════════════════════════════════════════════
# 啟動
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    print("Thunder Fighter Flask Server started on http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
