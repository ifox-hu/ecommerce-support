import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


DEMO_ORDERS = [
    ("ORD-1001", "alice", "云感运动鞋", "已支付", "运输中", "YT731001", "已到达杭州转运中心", "", "", 32900),
    ("ORD-1002", "alice", "恒温随行杯", "已完成", "已签收", "SF731002", "已由本人签收", "REF-1002", "退款处理中", 15900),
    ("ORD-2001", "bob", "降噪耳机", "已完成", "已签收", "JD732001", "已送达指定地址", "", "", 49900),
]


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class BusinessRepository:
    """Store seeded commerce facts and enforce ownership at the database boundary."""

    def __init__(self, sqlite_path: Path, mysql_url: str = ""):
        self.sqlite_path = sqlite_path
        self.mysql_url = urlsplit(mysql_url) if mysql_url else None

    @contextmanager
    def connect(self):
        if self.mysql_url:
            import pymysql

            conn = pymysql.connect(host=self.mysql_url.hostname or "localhost", port=self.mysql_url.port or 3306,
                                   user=unquote(self.mysql_url.username or ""),
                                   password=unquote(self.mysql_url.password or ""),
                                   database=unquote(self.mysql_url.path.lstrip("/")), charset="utf8mb4",
                                   cursorclass=pymysql.cursors.DictCursor, connect_timeout=5, read_timeout=5,
                                   write_timeout=5)
        else:
            conn = sqlite3.connect(self.sqlite_path, timeout=10)
            conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def execute(self, cursor, sql: str, params: tuple = ()) -> None:
        cursor.execute(sql.replace("?", "%s") if self.mysql_url else sql, params)

    def initialize(self) -> None:
        if self.mysql_url:
            return
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as cursor:
            cursor.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, product TEXT NOT NULL,
                    status TEXT NOT NULL, logistics_status TEXT NOT NULL, tracking TEXT NOT NULL,
                    logistics_detail TEXT NOT NULL, refund_id TEXT NOT NULL, refund_status TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL, order_id TEXT, status TEXT NOT NULL,
                    owner TEXT, created_at TEXT NOT NULL
                );
                """)
            cursor.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)", DEMO_ORDERS)

    def lookup_order(self, user_id: str, order_id: str) -> dict | None:
        with self.connect() as cursor:
            self.execute(cursor, "SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id))
            row = cursor.fetchone()
        return dict(row) if row else None

    def create_ticket(self, user_id: str, run_id: str, reason: str, order_id: str | None) -> dict:
        ticket_id = "T-" + hashlib.sha256(run_id.encode()).hexdigest()[:16].upper()
        with self.connect() as cursor:
            params = (ticket_id, user_id, run_id, reason, order_id, "pending", utc_timestamp())
            insert = "INSERT INTO tickets (id,user_id,run_id,reason,order_id,status,created_at) VALUES (?,?,?,?,?,?,?)"
            insert += " ON DUPLICATE KEY UPDATE id=id" if self.mysql_url else " ON CONFLICT(run_id) DO NOTHING"
            self.execute(cursor, insert, params)
            self.execute(cursor, "SELECT id,status,reason,order_id,owner,created_at FROM tickets WHERE run_id=? AND user_id=?", (run_id, user_id))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Ticket write did not resolve to the requesting user")
            return dict(row)

    def tickets(self, user_id: str | None) -> list[dict]:
        with self.connect() as cursor:
            select = "SELECT id,run_id,status,reason,order_id,owner,created_at FROM tickets"
            order = " ORDER BY created_at DESC, id DESC LIMIT 100"
            if user_id is None:
                self.execute(cursor, select + order)
            else:
                self.execute(cursor, select + " WHERE user_id=?" + order, (user_id,))
            return [dict(row) for row in cursor.fetchall()]

    def claim(self, ticket_id: str, owner: str) -> dict | None:
        with self.connect() as cursor:
            self.execute(cursor, "UPDATE tickets SET status='claimed',owner=? WHERE id=? AND status='pending'",
                         (owner, ticket_id))
            self.execute(cursor, "SELECT id,status,reason,order_id,owner,created_at FROM tickets WHERE id=?", (ticket_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
