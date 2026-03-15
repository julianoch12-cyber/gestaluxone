#!/usr/bin/env python3
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
DB_PATH = ROOT / "scrutiny.db"

ROLE_LEVELS = {
    "viewer": 1,
    "operator": 2,
    "supervisor": 3,
    "admin": 4,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_bucket(value: float) -> str:
    if value < 30:
        return "baixo"
    if value < 70:
        return "normal"
    return "alto"


@dataclass
class SessionUser:
    user_id: int
    username: str
    role: str


class RealtimeHub:
    def __init__(self) -> None:
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = []
        cond = threading.Condition()
        subscriber = {"queue": q, "cond": cond, "active": True}
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber) -> None:
        with self._lock:
            subscriber["active"] = False
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
        with subscriber["cond"]:
            subscriber["cond"].notify_all()

    def broadcast(self, event: Dict) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            with sub["cond"]:
                sub["queue"].append(payload)
                sub["cond"].notify()


class AppState:
    def __init__(self) -> None:
        self.sessions: Dict[str, SessionUser] = {}
        self.sessions_lock = threading.Lock()
        self.hub = RealtimeHub()
        self.init_db()

    def init_db(self) -> None:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS data_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                region TEXT NOT NULL,
                area TEXT NOT NULL,
                value REAL NOT NULL,
                bucket TEXT NOT NULL,
                received_at TEXT NOT NULL,
                received_by INTEGER NOT NULL,
                FOREIGN KEY(received_by) REFERENCES users(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor INTEGER,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(actor) REFERENCES users(id)
            )
            """
        )
        conn.commit()

        seeds = [
            ("admin", "admin123", "admin"),
            ("supervisor", "super123", "supervisor"),
            ("operador", "oper123", "operator"),
            ("viewer", "view123", "viewer"),
        ]
        for username, password, role in seeds:
            cur.execute(
                "INSERT OR IGNORE INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)",
                (username, password, role, utc_now()),
            )
        conn.commit()
        conn.close()

    def db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def create_session(self, user: SessionUser) -> str:
        token = secrets.token_hex(24)
        with self.sessions_lock:
            self.sessions[token] = user
        return token

    def get_session(self, token: str) -> Optional[SessionUser]:
        with self.sessions_lock:
            return self.sessions.get(token)


STATE = AppState()


class Handler(BaseHTTPRequestHandler):
    server_version = "ScrutinyServer/0.1"

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            return self._serve_static(rel, self._mime_for(rel))
        if path == "/api/dashboard":
            return self._dashboard()
        if path == "/api/history":
            return self._history()
        if path == "/api/events":
            return self._events()
        return self._json({"error": "Rota não encontrada"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if path == "/api/data":
            return self._receive_data()
        return self._json({"error": "Rota não encontrada"}, status=HTTPStatus.NOT_FOUND)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def _require_auth(self, min_role: str) -> Optional[SessionUser]:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._json({"error": "Token em falta"}, status=HTTPStatus.UNAUTHORIZED)
            return None
        token = auth.replace("Bearer ", "", 1).strip()
        user = STATE.get_session(token)
        if not user:
            self._json({"error": "Sessão inválida"}, status=HTTPStatus.UNAUTHORIZED)
            return None
        if ROLE_LEVELS[user.role] < ROLE_LEVELS[min_role]:
            self._json({"error": "Sem permissão"}, status=HTTPStatus.FORBIDDEN)
            return None
        return user

    def _login(self):
        payload = self._read_json()
        if not payload:
            return self._json({"error": "JSON inválido"}, status=HTTPStatus.BAD_REQUEST)
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", "")).strip()
        conn = STATE.db()
        cur = conn.cursor()
        user_row = cur.execute(
            "SELECT id, username, role FROM users WHERE username = ? AND password = ?",
            (username, password),
        ).fetchone()
        if not user_row:
            conn.close()
            return self._json({"error": "Credenciais inválidas"}, status=HTTPStatus.UNAUTHORIZED)

        user = SessionUser(user_id=user_row["id"], username=user_row["username"], role=user_row["role"])
        token = STATE.create_session(user)
        self._audit(conn, user.user_id, "login", f"Utilizador {user.username} autenticado")
        conn.close()
        return self._json(
            {
                "token": token,
                "user": {"id": user.user_id, "username": user.username, "role": user.role},
            }
        )

    def _receive_data(self):
        user = self._require_auth("operator")
        if not user:
            return
        payload = self._read_json()
        if not payload:
            return self._json({"error": "JSON inválido"}, status=HTTPStatus.BAD_REQUEST)

        required = ["source", "region", "area", "value"]
        missing = [k for k in required if payload.get(k) in (None, "")]
        if missing:
            return self._json(
                {"error": f"Campos em falta: {', '.join(missing)}"},
                status=HTTPStatus.BAD_REQUEST,
            )
        try:
            value = float(payload["value"])
        except (TypeError, ValueError):
            return self._json({"error": "value deve ser numérico"}, status=HTTPStatus.BAD_REQUEST)

        source = str(payload["source"]).strip()
        region = str(payload["region"]).strip()
        area = str(payload["area"]).strip()
        if value < 0 or value > 100:
            return self._json({"error": "value deve estar entre 0 e 100"}, status=HTTPStatus.BAD_REQUEST)

        bucket = compute_bucket(value)
        now = utc_now()
        conn = STATE.db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO data_points (source, region, area, value, bucket, received_at, received_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source, region, area, value, bucket, now, user.user_id),
        )
        conn.commit()

        self._audit(
            conn,
            user.user_id,
            "submit_data",
            f"Fonte={source}; Região={region}; Área={area}; Valor={value}; Bucket={bucket}",
        )
        conn.close()

        dashboard = self._build_dashboard(region_filter=None)
        STATE.hub.broadcast({"type": "dashboard_update", "data": dashboard})
        return self._json({"ok": True, "bucket": bucket, "processed_at": now})

    def _dashboard(self):
        if not self._require_auth("viewer"):
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        region = query.get("region", [None])[0]
        return self._json(self._build_dashboard(region))

    def _build_dashboard(self, region_filter: Optional[str]):
        conn = STATE.db()
        cur = conn.cursor()

        params = []
        region_sql = ""
        if region_filter:
            region_sql = " WHERE region = ? "
            params.append(region_filter)

        totals = cur.execute(
            f"SELECT COUNT(*) as total, AVG(value) as avg_value FROM data_points{region_sql}", params
        ).fetchone()
        by_bucket = cur.execute(
            f"SELECT bucket, COUNT(*) as total FROM data_points{region_sql} GROUP BY bucket",
            params,
        ).fetchall()
        by_region = cur.execute(
            "SELECT region, COUNT(*) as total, AVG(value) as avg_value FROM data_points GROUP BY region ORDER BY total DESC"
        ).fetchall()
        recent = cur.execute(
            f"SELECT source, region, area, value, bucket, received_at FROM data_points{region_sql} ORDER BY id DESC LIMIT 10",
            params,
        ).fetchall()

        conn.close()

        return {
            "summary": {
                "total_records": totals["total"] or 0,
                "average_value": round(totals["avg_value"] or 0, 2),
            },
            "bucket_distribution": {row["bucket"]: row["total"] for row in by_bucket},
            "regions": [dict(row) for row in by_region],
            "recent_records": [dict(row) for row in recent],
            "generated_at": utc_now(),
        }

    def _history(self):
        if not self._require_auth("supervisor"):
            return
        conn = STATE.db()
        rows = conn.execute(
            """
            SELECT a.id, u.username as actor, u.role, a.action, a.details, a.created_at
            FROM audit_logs a
            LEFT JOIN users u ON u.id = a.actor
            ORDER BY a.id DESC
            LIMIT 200
            """
        ).fetchall()
        conn.close()
        return self._json({"entries": [dict(row) for row in rows]})

    def _events(self):
        if not self._require_auth("viewer"):
            return

        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        subscriber = STATE.hub.subscribe()
        try:
            self.wfile.write(b"event: ping\ndata: {\"status\":\"connected\"}\n\n")
            self.wfile.flush()
            while subscriber["active"]:
                with subscriber["cond"]:
                    subscriber["cond"].wait(timeout=20)
                    if not subscriber["active"]:
                        break
                    queue = list(subscriber["queue"])
                    subscriber["queue"].clear()
                if not queue:
                    self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                    self.wfile.flush()
                    continue
                for payload in queue:
                    msg = f"event: update\ndata: {payload}\n\n".encode("utf-8")
                    self.wfile.write(msg)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            STATE.hub.unsubscribe(subscriber)

    def _audit(self, conn: sqlite3.Connection, actor: Optional[int], action: str, details: str) -> None:
        conn.execute(
            "INSERT INTO audit_logs (actor, action, details, created_at) VALUES (?, ?, ?, ?)",
            (actor, action, details, utc_now()),
        )
        conn.commit()

    def _serve_static(self, rel: str, content_type: str):
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self._json({"error": "Path inválido"}, status=HTTPStatus.BAD_REQUEST)
        if not target.exists() or not target.is_file():
            return self._json({"error": "Ficheiro não encontrado"}, status=HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(target.read_bytes())

    def _mime_for(self, rel: str):
        if rel.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if rel.endswith(".css"):
            return "text/css; charset=utf-8"
        if rel.endswith(".html"):
            return "text/html; charset=utf-8"
        return "application/octet-stream"

    def _json(self, payload: Dict, status: int = HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def run(port: int = 8000):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Servidor disponível em http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
