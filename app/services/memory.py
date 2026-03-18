import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.services.business import utc_timestamp


ACTIVE_MESSAGES = 8
ARCHIVE_SEGMENT = 4
CONTEXT_BUDGET = 600


def safe_key(user_id: str, session_id: str = "") -> str:
    return hashlib.sha256(json.dumps([user_id, session_id], ensure_ascii=False).encode()).hexdigest()


class ChatMemoryRepository:
    """Redis Lists in full mode and a durable equivalent for local demo use."""

    def __init__(self, path: Path, redis=None):
        self.path = path
        self.redis = redis

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    def initialize(self):
        if self.redis is not None:
            return
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                   user_id TEXT, id TEXT, title TEXT, updated_at TEXT, PRIMARY KEY(user_id, id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                   seq INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, session_id TEXT,
                   id TEXT, payload TEXT, UNIQUE(user_id, session_id, id)
                );
                CREATE INDEX IF NOT EXISTS messages_by_session ON messages(user_id, session_id, seq);
                CREATE TABLE IF NOT EXISTS summaries (id TEXT PRIMARY KEY, text TEXT);
                CREATE TABLE IF NOT EXISTS facts (
                   user_id TEXT, session_id TEXT, payload TEXT, PRIMARY KEY(user_id, session_id)
                );
                """)

    def _keys(self, user_id, session_id):
        prefix = "ecommerce:memory:" + safe_key(user_id, session_id)
        return [prefix + ":recent", prefix + ":archive", prefix + ":seen", prefix + ":facts",
                "ecommerce:sessions:" + safe_key(user_id)]

    @staticmethod
    def _merge_facts(previous, updates):
        if updates.get("order_id"):
            previous = previous if previous.get("order_id") == updates["order_id"] else {}
            return {key: value for key, value in {**previous, **updates}.items() if key != "pending"}
        if "pending" in updates and updates["pending"] is None:
            return {key: value for key, value in previous.items() if key != "pending"}
        return {**previous, **updates}

    async def append(self, user_id: str, session_id: str, item: dict) -> None:
        item = {"created_at": utc_timestamp(), **item}
        payload = json.dumps(item, ensure_ascii=False, default=str)
        if self.redis is not None:
            script = """
                if redis.call('SADD', KEYS[3], ARGV[1]) == 0 then return 0 end
                redis.call('RPUSH', KEYS[1], ARGV[2])
                if redis.call('LLEN', KEYS[1]) > tonumber(ARGV[5]) then
                    redis.call('RPUSH', KEYS[2], redis.call('LPOP', KEYS[1]))
                end
                local entry = redis.call('HGET', KEYS[5], ARGV[3])
                local session = entry and cjson.decode(entry) or {id = ARGV[3], title = '新会话'}
                session.updated_at = ARGV[4]
                redis.call('HSET', KEYS[5], ARGV[3], cjson.encode(session))
                local data = cjson.decode(ARGV[2])
                if data.facts and next(data.facts) then
                    local previous = redis.call('GET', KEYS[4])
                    local facts = previous and cjson.decode(previous) or {}
                    if data.facts.order_id and data.facts.order_id ~= facts.order_id then facts = {} end
                    for key, value in pairs(data.facts) do facts[key] = value end
                    if data.facts.order_id or data.facts.pending == cjson.null then facts.pending = nil end
                    redis.call('SET', KEYS[4], cjson.encode(facts))
                end
                return 1
            """
            await self.redis.eval(script, 5, *self._keys(user_id, session_id), item["id"], payload,
                                  session_id, item["created_at"], ACTIVE_MESSAGES)
            return

        def store():
            with self.connect() as conn, conn:
                cursor = conn.execute("INSERT OR IGNORE INTO messages (user_id,session_id,id,payload) VALUES (?,?,?,?)",
                                      (user_id, session_id, item["id"], payload))
                if cursor.rowcount:
                    conn.execute("""INSERT INTO sessions VALUES (?,?,'新会话',?)
                        ON CONFLICT(user_id,id) DO UPDATE SET updated_at=excluded.updated_at""",
                                 (user_id, session_id, item["created_at"]))
                    if updates := item.get("facts"):
                        row = conn.execute("SELECT payload FROM facts WHERE user_id=? AND session_id=?",
                                           (user_id, session_id)).fetchone()
                        saved = self._merge_facts(json.loads(row[0]) if row else {}, updates)
                        conn.execute("""INSERT INTO facts VALUES (?,?,?)
                            ON CONFLICT(user_id,session_id) DO UPDATE SET payload=excluded.payload""",
                                     (user_id, session_id, json.dumps(saved, ensure_ascii=False)))

        await asyncio.to_thread(store)

    async def history(self, user_id, session_id, offset=0, limit=50) -> tuple[list[dict], int]:
        if self.redis is not None:
            script = """
                local offset, limit = tonumber(ARGV[1]), tonumber(ARGV[2])
                local archived = redis.call('LLEN', KEYS[2])
                local total = archived + redis.call('LLEN', KEYS[1])
                local result = {total}
                if offset < archived then
                    local archived_page = redis.call('LRANGE', KEYS[2], offset, math.min(archived - 1, offset + limit - 1))
                    for _, item in ipairs(archived_page) do table.insert(result, item) end
                end
                local remaining = limit - (#result - 1)
                if remaining > 0 then
                    local first = math.max(0, offset - archived)
                    local recent_page = redis.call('LRANGE', KEYS[1], first, first + remaining - 1)
                    for _, item in ipairs(recent_page) do table.insert(result, item) end
                end
                return result
            """
            recent, archive, *_ = self._keys(user_id, session_id)
            total, *rows = await self.redis.eval(script, 2, recent, archive, offset, limit)
            return [json.loads(row) for row in rows], int(total)

        def select():
            with self.connect() as conn:
                total = conn.execute("SELECT COUNT(*) FROM messages WHERE user_id=? AND session_id=?",
                                     (user_id, session_id)).fetchone()[0]
                rows = conn.execute("""SELECT payload FROM messages WHERE user_id=? AND session_id=?
                    ORDER BY seq LIMIT ? OFFSET ?""", (user_id, session_id, limit, offset)).fetchall()
                return [json.loads(row[0]) for row in rows], total

        return await asyncio.to_thread(select)

    async def sessions(self, user_id: str) -> list[dict]:
        if self.redis is not None:
            rows = await self.redis.hvals(self._keys(user_id, "")[-1])
            return sorted((json.loads(row) for row in rows), key=lambda row: row["updated_at"], reverse=True)

        def select():
            with self.connect() as conn:
                rows = conn.execute("SELECT id,title,updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC,id",
                                    (user_id,)).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(select)

    async def facts(self, user_id, session_id):
        if self.redis is not None:
            entry = await self.redis.get(self._keys(user_id, session_id)[3])
            return json.loads(entry) if entry else {}

        def select():
            with self.connect() as conn:
                row = conn.execute("SELECT payload FROM facts WHERE user_id=? AND session_id=?",
                                   (user_id, session_id)).fetchone()
                return json.loads(row[0]) if row else {}

        return await asyncio.to_thread(select)

    async def title(self, user_id, session_id, value):
        if self.redis is not None:
            script = """
                local entry = redis.call('HGET', KEYS[1], ARGV[1])
                if entry then
                    local session = cjson.decode(entry)
                    if session.title == '新会话' then
                        session.title = ARGV[2]
                        redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(session))
                    end
                end
            """
            await self.redis.eval(script, 1, self._keys(user_id, session_id)[-1], session_id, value)
            return

        def update():
            with self.connect() as conn, conn:
                conn.execute("UPDATE sessions SET title=? WHERE user_id=? AND id=? AND title='新会话'",
                             (value, user_id, session_id))

        await asyncio.to_thread(update)

    async def summary(self, identifier, text=None):
        if self.redis is not None:
            key = f"ecommerce:summary:{identifier}"
            if text is None:
                return await self.redis.get(key)
            await self.redis.set(key, text, nx=True)
            return text

        def read_write():
            with self.connect() as conn, conn:
                if text is not None:
                    conn.execute("INSERT OR IGNORE INTO summaries VALUES (?,?)", (identifier, text))
                row = conn.execute("SELECT text FROM summaries WHERE id=?", (identifier,)).fetchone()
                return row[0] if row else None

        return await asyncio.to_thread(read_write)


class ConversationMemory:
    def __init__(self, repository, decisions, variant="layered"):
        self.repository = repository
        self.decisions = decisions
        self.variant = variant

    @staticmethod
    def _summary_id(user, session, group):
        return safe_key(user, session + ":" + ":".join(item["id"] for item in group))

    async def _segments(self, user, session, cutoff):
        if not cutoff:
            return []
        # Read a bounded window of complete, stable archive segments.
        archived_count = (cutoff // ARCHIVE_SEGMENT) * ARCHIVE_SEGMENT
        start = max(0, archived_count - 4 * ARCHIVE_SEGMENT)
        older, _ = await self.repository.history(user, session, start, cutoff - start)
        return [older[i:i + ARCHIVE_SEGMENT] for i in range(0, len(older), ARCHIVE_SEGMENT)]

    async def build(self, user, session, current_id) -> dict:
        _, total = await self.repository.history(user, session, 0, 1)
        cutoff = max(0, total - ACTIVE_MESSAGES)
        recent, _ = await self.repository.history(user, session, cutoff, ACTIVE_MESSAGES)
        recent = [item for item in recent if item["id"] != current_id]
        if self.variant == "recent":
            fitted = self._fit(recent, CONTEXT_BUDGET - 80)
            visible = {item["id"] for item in fitted}
            return {"recent": self._public(fitted), "summaries": [],
                    "facts": self._facts(item for item in recent if item["id"] in visible)}

        summaries, archived_facts = [], {}
        groups = await self._segments(user, session, cutoff)
        for group in groups:
            summary = await self.repository.summary(self._summary_id(user, session, group))
            summaries.append(summary or "；".join(item["content"][:26] for item in group))
            archived_facts.update(self._facts(group))
        facts = await self.repository.facts(user, session)
        if not facts:
            facts = {**archived_facts, **self._facts(recent)}
        # A byte-based budget provides a conservative cap without requiring online tokenizer assets.
        summary_text = "\n".join(summaries).encode()[-160:].decode(errors="ignore")
        fact_bytes = len(json.dumps(facts, ensure_ascii=False).encode())
        remaining = max(0, CONTEXT_BUDGET - len(summary_text.encode()) - fact_bytes)
        return {"recent": self._public(self._fit(recent, remaining)), "summaries": [summary_text] if summary_text else [], "facts": facts}

    @staticmethod
    def _facts(items):
        facts = {}
        for item in items:
            if item.get("facts"):
                facts = ChatMemoryRepository._merge_facts(facts, item["facts"])
        return facts

    @staticmethod
    def _public(items):
        return [{"role": item["role"], "content": item["content"]} for item in items]

    @staticmethod
    def _fit(items, budget):
        fitted = []
        for item in reversed(items):
            payload = item["content"].encode()[:max(0, budget - 32)].decode(errors="ignore")
            if not payload:
                break
            fitted.append({"id": item["id"], "role": item["role"], "content": payload})
            budget -= 32 + len(payload.encode())
        return list(reversed(fitted))

    async def process(self, user, session, first_message):
        first, total = await self.repository.history(user, session, 0, 1)
        if first and first[0]["content"] == first_message:
            title = first_message.strip()[:22]
            if self.decisions.chat_model is not None:
                title = (await self.decisions.completion("title", "用不超过十二个汉字概括售后会话标题。", {"message": first_message})).strip()[:22]
            await self.repository.title(user, session, title or "新会话")
        if self.variant != "layered":
            return
        for group in await self._segments(user, session, max(0, total - ACTIVE_MESSAGES)):
            if len(group) != ARCHIVE_SEGMENT:
                continue
            identifier = self._summary_id(user, session, group)
            if await self.repository.summary(identifier) is not None:
                continue
            summary = "；".join(item["content"][:28] for item in group)
            if self.decisions.chat_model is not None:
                summary = await self.decisions.completion("summary", "简短归纳这些历史消息的订单号、状态、金额、时间和承诺。",
                                                           {"messages": [{"role": item["role"], "content": item["content"]} for item in group]})
            await self.repository.summary(identifier, summary[:180])
