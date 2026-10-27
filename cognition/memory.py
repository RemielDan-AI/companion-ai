"""记忆库：SQLite 三层存储。

- episodic  情景记忆：每轮原文 + 摘要 + 情绪 + 重要度
- semantic  语义记忆：事实 / 偏好 / 关系（带置信度和证据数）
- feedback  反馈：点赞 / 点踩 / 纠正

阶段 2 新增（结构.txt 里 cognition/ 只列了数据文件，这里的 .py 是实现代码，
其中 memory.db / vectors/ 仍是结构里约定的数据位置）。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "cognition" / "memory.db"


class _LockedConnection:
    """sqlite3 连接的多线程包装。

    窗口程序（gui.py）在主线程建好 Companion，对话却跑在工作线程里，
    而 sqlite3 默认禁止跨线程使用连接（"SQLite objects created in a thread..."）。
    这里关掉那个检查，并用锁把访问串行化。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def close(self):
        with self._lock:
            return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    session      TEXT DEFAULT '',
    turn         INTEGER DEFAULT 0,
    user_text    TEXT NOT NULL,
    reply_text   TEXT NOT NULL,
    summary      TEXT DEFAULT '',
    emotion      TEXT DEFAULT '',
    importance   INTEGER DEFAULT 3
);
CREATE INDEX IF NOT EXISTS idx_episodic_created ON episodic(created_at);

CREATE TABLE IF NOT EXISTS semantic (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    content         TEXT NOT NULL,
    confidence      REAL DEFAULT 0.5,
    evidence_count  INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'active',
    source_episodic INTEGER,
    UNIQUE(kind, content)
);
CREATE INDEX IF NOT EXISTS idx_semantic_kind ON semantic(kind, status);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    episodic_id INTEGER,
    rating      TEXT NOT NULL,
    note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    content     TEXT NOT NULL,
    due         TEXT DEFAULT '',
    status      TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memory_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL,
    target_id  INTEGER,
    detail     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memory_log_created ON memory_log(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_log_action ON memory_log(action);

CREATE TABLE IF NOT EXISTS memory_link (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    kind       TEXT DEFAULT 'related',
    weight     REAL DEFAULT 0.5,
    note       TEXT DEFAULT '',
    UNIQUE(source, target, kind)
);
CREATE INDEX IF NOT EXISTS idx_memory_link_source ON memory_link(source);
CREATE INDEX IF NOT EXISTS idx_memory_link_target ON memory_link(target);

-- 压缩快照（借鉴 Cyrene-Agent）：压缩前把语义表整表存一份，出错或想反悔都能回滚
CREATE TABLE IF NOT EXISTS compression_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    threshold  REAL DEFAULT 0.6,
    payload    TEXT DEFAULT '[]',
    status     TEXT DEFAULT 'applied',
    note       TEXT DEFAULT ''
);

-- 别名表（2026-10-16）：把「小雨」和「林小雨」认成同一个人。
--
-- 为什么要**存表**而不是每次现推：
--   · 别名推错是**正确性 bug**——它会把两个人的事混到一起，比搜不到严重得多。
--     有表才能审计、才能人工否掉（`status='rejected'`）、才能追问"这条是谁学的"。
--   · 推导规则是启发式的，每次查询现推会**不稳定**（同一问题两次结果可能不同）；
--     存下来就固定了。
--   · 用户可以自己加：「小雨就是林小雨」这种只有本人知道的事，规则永远推不出来。
--
-- `source` 取值：called（显式命名）/ suffix（简称，如林小雨→小雨）/
--                relation（关系词，如妹妹）/ user（用户手填）/ import（导入）
-- `status` 取值：active / rejected（人工否掉，保留痕迹不删）
CREATE TABLE IF NOT EXISTS alias (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT DEFAULT '',
    alias      TEXT NOT NULL,
    canonical  TEXT NOT NULL,
    kind       TEXT DEFAULT '',
    source     TEXT DEFAULT 'suffix',
    confidence REAL DEFAULT 0.5,
    status     TEXT DEFAULT 'active',
    episodic_id INTEGER DEFAULT 0,
    note       TEXT DEFAULT '',
    UNIQUE(alias, canonical)
);
CREATE INDEX IF NOT EXISTS idx_alias_alias ON alias(alias);
CREATE INDEX IF NOT EXISTS idx_alias_canonical ON alias(canonical);

-- **待确认的问题**（2026-10-18）：把"不确定的记忆"以提问方式抛给用户，
-- 用户回答后**把回答和这个问题关联起来**，据此把记忆升为确定 / 改对 / 否掉。
--
-- 为什么要落库而不是只存在内存里：一次提问可能隔几轮才被回答，
-- 而"用户回的这句话是对哪条记忆的回应"必须留痕，否则**张冠李戴**——
-- 把 A 的确认当成 B 的确认，会把错记忆固化成事实。
CREATE TABLE IF NOT EXISTS pending_question (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    session      TEXT DEFAULT '',
    ask_turn     INTEGER DEFAULT 0,
    semantic_ids TEXT DEFAULT '[]',   -- 问的是哪几条记忆（JSON 列表）
    topic        TEXT DEFAULT '',     -- 问的是什么（"住处"/"忌口"…）
    asked        TEXT DEFAULT '',     -- 实际问出去的那句话（审计用）
    status       TEXT DEFAULT 'open', -- open / yes / no / corrected / expired
    answer       TEXT DEFAULT '',     -- 用户原话
    resolved_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pq_status ON pending_question(status);
CREATE INDEX IF NOT EXISTS idx_pq_session ON pending_question(session);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def normalize(text: str) -> str:
    """语义记忆去重用的归一化：去空白、去句末标点。"""
    return " ".join(str(text).split()).rstrip("。．.！!？?；;，,")


def parse_time(text: str) -> dt.datetime | None:
    """宽松解析时间戳，解析不了返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


class Memory:
    def __init__(self, path: pathlib.Path | str = DB_PATH):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(self.path), check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn = _LockedConnection(raw, self._lock)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列。

        - 阶段 3：情景记忆记下由哪个技能处理；
        - 阶段 6：记档位；
        - 本次（项目改进v1）：访问追踪（last_access_at／access_count）、
          用户保留标记（retention）、冷存储（archived）、初始最高值（peak）。
        """
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(episodic)")}
        if "skill" not in columns:
            self.conn.execute("ALTER TABLE episodic ADD COLUMN skill TEXT DEFAULT ''")
        if "level" not in columns:
            self.conn.execute("ALTER TABLE episodic ADD COLUMN level TEXT DEFAULT ''")
        for table in ("episodic", "semantic"):
            cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if "importance" not in cols:
                # 语义记忆以前只有 confidence，没有重要度：按置信度折算一个初始值
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN importance REAL DEFAULT 3")
                backfill = ("MAX(1.0, MIN(5.0, confidence * 5.0))"
                            if table == "semantic" else "3")
                self.conn.execute(f"UPDATE {table} SET importance = {backfill}")
            if "last_access_at" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN last_access_at TEXT DEFAULT ''")
            if "access_count" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN access_count INTEGER DEFAULT 0")
            if "retention" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN retention TEXT DEFAULT 'normal'")
            if "archived" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN archived INTEGER DEFAULT 0")
            if "peak" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN peak REAL DEFAULT 0")
            # 情绪标签（改进 21）：语义记忆也带情绪，可按情绪过滤
            if "emotion" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN emotion TEXT DEFAULT ''")
            # 去重合并（改进 17）：被合并掉的记忆留个指向
            if table == "semantic" and "merged_into" not in cols:
                self.conn.execute(
                    "ALTER TABLE semantic ADD COLUMN merged_into INTEGER DEFAULT 0")
            # 冲突处理（改进 18）：和谁冲突、被谁取代
            if table == "semantic":
                if "conflict_with" not in cols:
                    self.conn.execute(
                        "ALTER TABLE semantic ADD COLUMN conflict_with INTEGER DEFAULT 0")
                if "conflict_score" not in cols:
                    self.conn.execute(
                        "ALTER TABLE semantic ADD COLUMN conflict_score INTEGER DEFAULT 0")
                if "resolver_priority" not in cols:
                    self.conn.execute(
                        "ALTER TABLE semantic ADD COLUMN resolver_priority TEXT DEFAULT 'none'")
                # **把历史上被藏起来的冲突条目救回来**（2026-10-15）。
                # 以前冲突的新说法写 `status='pending'`，而检索只取 `active` →
                # 用户没回答那条询问，这条记忆就**永久丢失**。
                # 冲突条目的 `conflict_with` 就是"它是被藏的那条"的标记，
                # 所以能安全地只翻这一批，不动别的状态。
                self.conn.execute(
                    "UPDATE semantic SET status='active'"
                    " WHERE status='pending' AND conflict_with>0")
                self.conn.commit()
                if "core_fact" not in cols:
                    self.conn.execute(
                        "ALTER TABLE semantic ADD COLUMN core_fact INTEGER DEFAULT 0")
                if "superseded_by" not in cols:
                    self.conn.execute(
                        "ALTER TABLE semantic ADD COLUMN superseded_by INTEGER DEFAULT 0")
            # 老数据补上峰值＝当前重要度
            self.conn.execute(
                f"UPDATE {table} SET peak = importance WHERE peak IS NULL OR peak <= 0")

    # ---------------- 情景记忆 ----------------
    def add_episode(self, user_text: str, reply_text: str, summary: str = "",
                    emotion: str = "", importance: int = 3, session: str = "",
                    turn: int = 0, skill: str = "", level: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO episodic (created_at, session, turn, user_text, reply_text,"
            " summary, emotion, importance, skill, level, peak, last_access_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), session, turn, user_text, reply_text, summary, emotion,
             int(importance), skill, level, float(importance), ""),
        )
        self.conn.commit()
        episode_id = int(cur.lastrowid)
        # 自动关联（改进 20）：同一会话里与上一轮建立时间关联
        try:
            previous = self.conn.execute(
                "SELECT id FROM episodic WHERE session=? AND id<? ORDER BY id DESC LIMIT 1",
                (session, episode_id)).fetchone()
            if previous:
                self.add_link(f"episodic#{episode_id}", f"episodic#{previous['id']}",
                              "temporal", 0.4, "同一会话的相邻两轮")
        except Exception:
            pass
        return episode_id

    # ---------------- 记忆关联（改进 20） ----------------
    # 核心事实（身份类）：用户最常问、也最该记住的那十来条。
    # 它们数量少、价值高，所以回忆类问题里**无论排序如何都优先保证进注入块**。
    CORE_FACT_MARKS = ("叫", "住在", "老家", "城市", "过敏", "不吃", "不喝", "喜欢喝",
                       "乐器", "吉他", "钢琴", "点睡", "作息", "睡觉", "起床",
                       "生日", "年龄", "工作", "职业", "上班", "上学", "养的", "宠物",
                       "猫", "狗", "咖啡", "茶", "口味", "妹妹", "弟弟", "哥哥", "姐姐")

    @classmethod
    def is_core_fact(cls, content: str) -> bool:
        """这条算不算"核心事实"（身份类、会被反复问到的）。"""
        text = str(content or "")
        return any(mark in text for mark in cls.CORE_FACT_MARKS)

    @staticmethod
    def parse_key(key: str) -> tuple[str, int]:
        """把 "episodic#12" 拆成 ("episodic", 12)。"""
        raw = str(key or "").strip()
        if "#" not in raw:
            raise ValueError(f"记忆编号要写成 episodic#1 或 semantic#2，收到：{raw}")
        table, _, number = raw.partition("#")
        table = table.strip()
        if table in ("episodic", "episodic记忆", "情景"):
            table = "episodic"
        elif table in ("semantic", "语义"):
            table = "semantic"
        elif table in ("value", "价值观"):
            table = "value"          # 价值观条目不在记忆库里，只作为关联的一端
        elif table in ("skill", "技能"):
            table = "skill"          # 技能调用日志同理
        else:
            raise ValueError(f"未知的记忆类型：{table}")
        if table in ("value", "skill"):
            return table, int(number) if str(number).isdigit() else number
        return table, int(number)

    def entry_by_key(self, key: str) -> dict | None:
        table, number = self.parse_key(key)
        if table not in ("episodic", "semantic"):
            return None          # 价值观/技能这类端点不在记忆库里，只有关联关系
        return self.entry(table, number)

    def add_link(self, source: str, target: str, kind: str = "related",
                 weight: float = 0.5, note: str = "") -> int:
        """建立关联（kind：causal 因果／temporal 时间／person 人物／related 相关）。"""
        self.parse_key(source)
        self.parse_key(target)
        if source == target:
            raise ValueError("不能把记忆关联到自己")
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_link (created_at, source, target, kind,"
            " weight, note) VALUES (?,?,?,?,?,?)",
            (now(), source, target, kind, float(weight), note))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM memory_link WHERE source=? AND target=? AND kind=?",
            (source, target, kind)).fetchone()
        return int(row["id"]) if row else 0

    def links(self, key: str | None = None, kind: str | None = None,
              limit: int = 100) -> list[dict]:
        """看关联：给定 key 时看它的所有关联（双向），不给就列全部。"""
        sql = "SELECT * FROM memory_link"
        args: list = []
        where = []
        if key:
            self.parse_key(key)
            where.append("(source=? OR target=?)")
            args.extend([key, key])
        if kind:
            where.append("kind=?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY weight DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def linked_entries(self, key: str, limit: int = 2) -> list[dict]:
        """按关联取回**对方那条记忆**（检索时连带召回用）。"""
        out: list[dict] = []
        for link in self.links(key, limit=limit * 3):
            other = link["target"] if link["source"] == key else link["source"]
            if other == key:
                continue
            entry = self.entry_by_key(other)
            if not entry:
                continue
            entry = dict(entry)
            entry["key"] = other
            entry["link_kind"] = link["kind"]
            entry["link_weight"] = link["weight"]
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def delete_link(self, link_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memory_link WHERE id=?", (int(link_id),))
        self.conn.commit()
        if cur.rowcount:
            self.log("unlink", "memory_link", int(link_id), "")
            return True
        return False

    def link_by_entity(self, key: str, texts: list[str], kind: str = "person") -> int:
        """按共同人物/实体自动关联。

        实体名来源两边合起来：本次文本里「叫XX」新介绍的，以及库里已有记忆里出现过的。
        这样第二次提到「团子」（哪怕没再写「叫」字）也能连上第一次介绍它的那条。
        """
        from cognition.terms import find_called_names
        joined = " ".join(str(text or "") for text in texts)
        if not joined.strip():
            return 0
        existing = self.search_texts(limit=500, include_archived=False)
        # ⚠ 名字口径**必须与 `_enrich_relation` 一致**：原来这里是 `{2,3}`，
        #    「公司叫工业软件公司」会抓出**假实体「工业软」**并拿它去建链。
        names: set[str] = set(find_called_names(joined))
        for item in existing:
            content = str(item.get("summary") or item.get("content") or "")
            names.update(find_called_names(content))
        targets = {name for name in names if name in joined}
        if not targets:
            return 0
        made = 0
        for item in existing:
            other_key = f"{item['target']}#{item['id']}"
            if other_key == key:
                continue
            content = str(item.get("summary") or item.get("content") or "")
            if any(name in content for name in targets):
                self.add_link(key, other_key, kind, 0.5,
                              "共同提到：" + "、".join(sorted(targets)))
                made += 1
                if made >= 3:            # 别一次连太多，网络会糊
                    break
        return made

    # ---------------- 访问、回升与冷存储 ----------------
    def _table(self, target: str) -> str:
        if target not in ("episodic", "semantic"):
            raise ValueError(f"未知的记忆表：{target}")
        return target

    def entry(self, target: str, target_id: int) -> dict | None:
        row = self.conn.execute(
            f"SELECT * FROM {self._table(target)} WHERE id=?", (target_id,)).fetchone()
        return dict(row) if row else None

    # ---- 待确认的问题：把不确定的记忆变成一次提问，再把回答关联回来 ----
    # 设计意图（2026-10-18）：不再试图"把答案说死"（实测两次都失败），
    # 而是**以不确定的口吻问出去**，让用户来确认/纠正。
    # ⚠ 必须留痕：一次提问可能隔几轮才被回答，
    #   "用户回的是哪条记忆"要能对上，否则**张冠李戴**——把 A 的确认当成 B 的。

    def ask_question(self, semantic_ids: list[int], topic: str, asked: str,
                     session: str = "", turn: int = 0) -> int:
        import json as _json
        cur = self.conn.execute(
            "INSERT INTO pending_question (created_at, session, ask_turn,"
            " semantic_ids, topic, asked, status) VALUES (?,?,?,?,?,?,'open')",
            (now(), session, int(turn), _json.dumps(list(semantic_ids)),
             str(topic)[:40], str(asked)[:200]))
        self.conn.commit()
        self.log("ask", "pending_question", cur.lastrowid,
                 f"{topic}：{asked[:60]}")
        return int(cur.lastrowid)

    def open_question(self, session: str = "", max_age: int = 6,
                      before_turn: int | None = None) -> dict | None:
        """最近一个**还没回答、也没过期**的问题。

        ⚠ `before_turn` 是必须的：提问发生在 `assemble()`（生成回复之前），
          而解决发生在 `remember_turn()`（生成之后）——**同一轮里**。
          不排除本轮的话，会把"触发提问的那句话"当成"用户对这个提问的回答"，
          于是每条待确认记忆都会被立刻误判。只认**更早轮次**问出去的。
        """
        sql = ("SELECT * FROM pending_question WHERE status='open'"
               " AND session=?")
        args: list = [session]
        if before_turn is not None:
            sql += " AND ask_turn < ?"
            args.append(int(before_turn))
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def confirm_semantic(self, semantic_id: int, detail: str = "") -> None:
        """用户确认了 → 证据 +1、置信度上调（把"可能"变成"确定"）。"""
        row = self.conn.execute("SELECT * FROM semantic WHERE id=?",
                                (int(semantic_id),)).fetchone()
        if not row:
            return
        old = float(row["confidence"] or 0.5)
        new = min(0.98, old + 0.18)
        self.conn.execute(
            "UPDATE semantic SET confidence=?, evidence_count=COALESCE(evidence_count,1)+1,"
            " updated_at=? WHERE id=?", (new, now(), int(semantic_id)))
        self.conn.commit()
        self.log("confirm", "semantic", semantic_id,
                 f"用户确认，置信 {old:.2f}→{new:.2f}　{detail}")

    def reject_semantic(self, semantic_id: int, detail: str = "") -> None:
        """用户否掉了 → **不再注入**（保留行以备审计，不删）。

        ⚠ 用 `status='rejected'` 而不是删除：万一判定错了还能查回来，
          而删除是不可逆的。
        """
        self.conn.execute(
            "UPDATE semantic SET status='rejected', updated_at=? WHERE id=?",
            (now(), int(semantic_id)))
        self.conn.commit()
        self.log("reject", "semantic", semantic_id, f"用户否认　{detail}")

    def resolve_question(self, qid: int, status: str, answer: str = "") -> None:
        self.conn.execute(
            "UPDATE pending_question SET status=?, answer=?, resolved_at=?"
            " WHERE id=?", (status, str(answer)[:200], now(), int(qid)))
        self.conn.commit()
        self.log("resolve", "pending_question", qid, f"{status}：{answer[:60]}")

    def expire_questions(self, keep_open: int = 6, session: str = "") -> int:
        """把太老的 open 问题标成 expired（防止迟到的一句"嗯"被误认）。"""
        rows = self.conn.execute(
            "SELECT id, ask_turn FROM pending_question WHERE status='open'"
            " AND session=? ORDER BY id DESC", (session,)).fetchall()
        n = 0
        for i, row in enumerate(rows):
            if i < keep_open:
                continue
            self.conn.execute(
                "UPDATE pending_question SET status='expired', resolved_at=?"
                " WHERE id=?", (now(), row["id"]))
            n += 1
        if n:
            self.conn.commit()
        return n

    def all_questions(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM pending_question ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()]

    def log(self, action: str, target: str, target_id: int | None = None,
            detail: str = "") -> int:
        """审计日志（改进 22）：记下每次写入、检索、修改、删除、归档、唤醒。"""
        cur = self.conn.execute(
            "INSERT INTO memory_log (created_at, action, target, target_id, detail)"
            " VALUES (?,?,?,?,?)",
            (now(), action, target, target_id, str(detail)[:400]),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_logs(self, limit: int = 30, action: str = "") -> list[dict]:
        if action:
            rows = self.conn.execute(
                "SELECT * FROM memory_log WHERE action=? ORDER BY id DESC LIMIT ?",
                (action, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memory_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def log_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM memory_log").fetchone()[0])

    def touch(self, target: str, target_id: int, action: str = "retrieved",
              detail: str = "") -> dict | None:
        """一次访问：次数 +1、刷新最后访问时间、重要度回升、冷存储自动唤醒。

        action: retrieved（被检索命中）／mentioned（被用户主动提起）／
                self_mentioned（**AI 自己复述**——不给净加分，只按衰减走，防自激循环）。
        """
        from cognition import decay as decay_engine
        rules = decay_engine.load_rules()
        entry = self.entry(target, target_id)
        if not entry:
            return None
        amount = float((rules.get("boost") or {}).get(action, 0.0))
        current = decay_engine.decayed_importance(entry, rules)
        if action == "self_mentioned":
            new_base = decay_engine.clamp_self_mention(entry, current, rules)
        else:
            new_base = decay_engine.boost_importance(entry, amount, rules, current=current)
        woke = int(entry.get("archived") or 0) == 1
        self.conn.execute(
            f"UPDATE {self._table(target)} SET last_access_at=?, access_count=?,"
            " importance=?, archived=0 WHERE id=?",
            (now(), int(entry.get("access_count") or 0) + 1, new_base, target_id),
        )
        self.conn.commit()
        self.log("wake" if woke else action, target, target_id,
                 detail or f"重要度 {current:.2f} → {new_base:.2f}")
        updated = self.entry(target, target_id)
        if updated:
            updated["woke"] = woke
        return updated

    def set_retention(self, target: str, target_id: int, retention: str) -> bool:
        """用户标记保留策略：normal／permanent／forgettable／temporary。"""
        if retention not in ("normal", "permanent", "forgettable", "temporary"):
            raise ValueError(f"未知的保留策略：{retention}")
        self.conn.execute(
            f"UPDATE {self._table(target)} SET retention=? WHERE id=?",
            (retention, target_id))
        self.conn.commit()
        self.log("retention", target, target_id, retention)
        return True

    def set_archived(self, target: str, target_id: int, archived: bool = True,
                     reason: str = "手动") -> bool:
        self.conn.execute(
            f"UPDATE {self._table(target)} SET archived=? WHERE id=?",
            (1 if archived else 0, target_id))
        self.conn.commit()
        self.log("archive" if archived else "wake", target, target_id, reason)
        return True

    def archive_cold(self, rules: dict | None = None) -> list[dict]:
        """把长期低重要度的记忆转入冷存储（不是删除，检索仍可命中）。"""
        from cognition import decay as decay_engine
        rules = rules or decay_engine.load_rules()
        moved: list[dict] = []
        for table in ("episodic", "semantic"):
            rows = self.conn.execute(
                f"SELECT * FROM {table} WHERE archived=0").fetchall()
            for row in rows:
                entry = dict(row)
                if decay_engine.should_archive(entry, rules):
                    self.conn.execute(f"UPDATE {table} SET archived=1 WHERE id=?",
                                      (entry["id"],))
                    self.log("archive", table, entry["id"], "长期低重要度")
                    moved.append({"target": table, "id": entry["id"],
                                  "summary": entry.get("summary") or entry.get("content", "")})
        if moved:
            self.conn.commit()
        return moved

    def update_entry(self, target: str, target_id: int, **fields) -> bool:
        """用户手动修改记忆（改进 23）。只允许改白名单字段。"""
        allowed = {"summary", "content", "emotion", "importance", "confidence",
                   "retention", "kind", "status", "archived"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not changes:
            return False
        assignments = ", ".join(f"{k}=?" for k in changes)
        values = list(changes.values())
        if "importance" in changes:      # 手动改重要度时同步抬高峰值，否则回升会被压住
            assignments += ", peak=MAX(peak, ?)"
            values.append(float(changes["importance"]))
        if target == "semantic":
            assignments += ", updated_at=?"
            values.append(now())
        values.append(target_id)
        self.conn.execute(
            f"UPDATE {self._table(target)} SET {assignments} WHERE id=?", values)
        self.conn.commit()
        self.log("update", target, target_id,
                 "、".join(f"{k}={v}" for k, v in changes.items()))
        return True

    def delete_entry(self, target: str, target_id: int) -> bool:
        """删除一条记忆（改进 23）。删除前留一条审计日志。"""
        entry = self.entry(target, target_id)
        if not entry:
            return False
        self.log("delete", target, target_id,
                 str(entry.get("summary") or entry.get("content") or "")[:80])
        self.conn.execute(f"DELETE FROM {self._table(target)} WHERE id=?", (target_id,))
        self.conn.commit()
        return True

    def search_texts(self, keyword: str = "", limit: int = 50,
                     include_archived: bool = True) -> list[dict]:
        """按关键字列出记忆（改进 23 的查看/搜索）。"""
        out: list[dict] = []
        for table in ("episodic", "semantic"):
            where = []
            params: list = []
            if keyword:
                field = "summary" if table == "episodic" else "content"
                where.append(f"({field} LIKE ? OR {'user_text' if table == 'episodic' else 'content'} LIKE ?)")
                params.extend([f"%{keyword}%", f"%{keyword}%"])
            if not include_archived:
                where.append("archived=0")
            sql = f"SELECT * FROM {table}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            for row in self.conn.execute(sql, params).fetchall():
                item = dict(row)
                item["target"] = table
                out.append(item)
        out.sort(key=lambda r: (r.get("created_at") or "", r.get("id") or 0), reverse=True)
        return out[:limit]


    # ---------------- 多会话（基础改进 1） ----------------
    def sessions(self) -> list[dict]:
        """列出所有会话：轮数、起止时间、是否已归档。"""
        rows = self.conn.execute(
            "SELECT session, COUNT(*) AS turns, MIN(created_at) AS first_at,"
            " MAX(created_at) AS last_at FROM episodic"
            " GROUP BY session ORDER BY last_at DESC").fetchall()
        archived: set[str] = set()
        for row in self.conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'session_archived:%'"):
            if str(row["value"]) == "1":
                archived.add(str(row["key"]).split(":", 1)[1])
        out = []
        for row in rows:
            name = row["session"] or "(默认)"
            out.append({"session": row["session"], "label": name,
                        "turns": int(row["turns"]), "first_at": row["first_at"],
                        "last_at": row["last_at"],
                        "archived": (row["session"] or "") in archived})
        return out

    def session_episodes(self, session: str, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM episodic WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_session_archived(self, session: str, archived: bool = True) -> None:
        key = f"session_archived:{session or ''}"
        self.set_meta(key, "1" if archived else "0")
        self.log("archive" if archived else "wake", "session", None,
                 f"会话 {session or '(默认)'}")

    def delete_session(self, session: str) -> dict:
        """删掉一个会话的对话历史（情景记忆），语义记忆与画像保留。"""
        count = int(self.conn.execute(
            "SELECT COUNT(*) FROM episodic WHERE session=?", (session,)).fetchone()[0])
        self.conn.execute("DELETE FROM episodic WHERE session=?", (session,))
        self.conn.commit()
        self.log("delete", "session", None, f"删除会话 {session or '(默认)'} 的 {count} 轮")
        return {"deleted": count}

    # ---------------- 整库导出 / 导入（改进 27 迁移备份） ----------------
    TABLES = ("episodic", "semantic", "feedback", "reminders", "meta", "memory_link")

    def export_bundle(self, path) -> dict:
        """把记忆库导成一个 JSON 文件（换设备搬过去即可，换模型不用动）。"""
        path = pathlib.Path(path)
        payload = {"exported_at": now(), "version": 1, "tables": {}}
        for table in self.TABLES:
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            payload["tables"][table] = [dict(r) for r in rows]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        counts = {k: len(v) for k, v in payload["tables"].items()}
        self.log("export", "bundle", None, f"{path.name} {counts}")
        return {"path": str(path), "counts": counts}

    def import_bundle(self, path, merge: bool = True) -> dict:
        """从 JSON 备份恢复。merge=True 时跳过已存在的行，不覆盖现有记忆。"""
        path = pathlib.Path(path)
        if not path.exists():
            raise FileNotFoundError(f"备份文件不存在：{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        tables = payload.get("tables") or {}
        report: dict = {}
        for table in self.TABLES:
            rows = tables.get(table) or []
            added = 0
            for row in rows:
                # 保留 id 一起导入：这样重复导入能按 id 合并，引用关系也不会错位
                columns = list(row)
                if not columns:
                    continue
                if merge and "id" in row:
                    exists = self.conn.execute(
                        f"SELECT 1 FROM {table} WHERE id=?", (row["id"],)).fetchone()
                    if exists:
                        continue
                placeholders = ",".join("?" for _ in columns)
                try:
                    self.conn.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                        [row[c] for c in columns])
                    added += 1
                except Exception:
                    continue
            report[table] = added
        self.conn.commit()
        self.log("import", "bundle", None, f"{path.name} {report}")
        return report

    def export_memories(self, fmt: str = "json", keyword: str = "", days: int = 0,
                        include_archived: bool = True, redact: bool = False,
                        path: pathlib.Path | str | None = None) -> dict:
        """导出记忆（改进 24）：JSON / Markdown，可按时间与关键字筛选，可选脱敏。

        返回 {"path": ..., "count": ...}；path 为 None 时只返回内容不落盘。
        """
        import json
        import re

        items = self.search_texts(keyword=keyword, limit=100000,
                                  include_archived=include_archived)
        if days > 0:
            cutoff = dt.datetime.now() - dt.timedelta(days=days)
            items = [i for i in items
                     if (parse_time(i.get("created_at")) or cutoff) >= cutoff]

        def clean(text: str) -> str:
            text = str(text or "")
            if not redact:
                return text
            text = re.sub(r"1[3-9]\d{9}", "[手机号]", text)
            text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[邮箱]", text)
            text = re.sub(r"\d{6,}", "[数字]", text)
            return text

        rows = []
        for item in items:
            entry = {
                "target": item.get("target"),
                "id": item.get("id"),
                "created_at": item.get("created_at"),
                "content": clean(item.get("summary") or item.get("content") or ""),
                "emotion": item.get("emotion") or "",
                "kind": item.get("kind") or "",
                "importance": item.get("importance"),
                "peak": item.get("peak"),
                "access_count": item.get("access_count"),
                "last_access_at": item.get("last_access_at") or "",
                "retention": item.get("retention") or "normal",
                "archived": int(item.get("archived") or 0),
            }
            rows.append(entry)

        content = ""
        if fmt == "md":
            lines = ["# 记忆导出", "",
                     f"- 导出时间：{now()}",
                     f"- 条数：{len(rows)}"
                     + (f"（关键字：{keyword}）" if keyword else "")
                     + (f"（最近 {days} 天）" if days else "")
                     + ("（已脱敏）" if redact else ""), ""]
            for entry in rows:
                tag = "冷存储" if entry["archived"] else "活跃"
                lines.append(f"- [{entry['target']}#{entry['id']}] {entry['content']}"
                             f"　（{entry['created_at']}｜重要度 {entry['importance']}"
                             f"｜{entry['retention']}｜{tag}）")
            content = "\n".join(lines) + "\n"
        else:
            content = json.dumps({"exported_at": now(), "count": len(rows),
                                  "items": rows}, ensure_ascii=False, indent=2)

        result = {"count": len(rows), "content": content, "path": None}
        if path is not None:
            path = pathlib.Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.log("export", "all", None,
                     f"{fmt} {len(rows)} 条" + ("（脱敏）" if redact else ""))
            result["path"] = str(path)
        return result

    def decay_state(self, limit: int = 0) -> list[dict]:
        """每条记忆当前的有效重要度与衰减状态（改进 16：可查看衰减状态）。"""
        from cognition import decay as decay_engine
        rules = decay_engine.load_rules()
        out = []
        for item in self.search_texts(limit=limit or 100000):
            out.append({
                "target": item.get("target"),
                "id": item.get("id"),
                "content": item.get("summary") or item.get("content") or "",
                "base": float(item.get("importance") or 0),
                "now": decay_engine.decayed_importance(item, rules),
                "retention": item.get("retention") or "normal",
                "archived": int(item.get("archived") or 0),
                "access_count": int(item.get("access_count") or 0),
                "last_access_at": item.get("last_access_at") or item.get("created_at") or "",
            })
        out.sort(key=lambda r: r["now"])
        return out

    def recent_episodes(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM episodic ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def episode(self, episode_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM episodic WHERE id = ?", (episode_id,)
        ).fetchone()
        return dict(row) if row else None

    def episode_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0])

    # ---------------- 语义记忆 ----------------
    def upsert_semantic(self, kind: str, content: str, confidence: float = 0.5,
                        episodic_id: int | None = None,
                        importance: float | None = None,
                        emotion: str = "") -> tuple[int, bool]:
        """写入或加权重。返回 (id, 是否新建)。importance 不传就按置信度折算。"""
        content = normalize(content)
        if not content:
            raise ValueError("语义记忆内容为空")
        row = self.conn.execute(
            "SELECT id, evidence_count, confidence, peak FROM semantic WHERE kind=? AND content=?",
            (kind, content),
        ).fetchone()
        stamp = now()
        if row:
            # 重复出现 → 升权（证据数 +1，置信度每多一条证据 +0.08，上限 0.95）
            evidence = int(row["evidence_count"]) + 1
            conf = min(0.95, float(row["confidence"]) + 0.08)
            # 重复出现的信息：峰值往上抬一点（改进 13：重复出现 → 初始高）
            peak = min(5.0, (float(row["peak"] or 0) or 3.0) + 0.3)
            self.conn.execute(
                "UPDATE semantic SET evidence_count=?, confidence=?, updated_at=?,"
                " peak=MAX(peak, ?), emotion=CASE WHEN ?='' THEN emotion ELSE ? END,"
                " source_episodic=COALESCE(?, source_episodic) WHERE id=?",
                (evidence, conf, stamp, peak, emotion, emotion, episodic_id, row["id"]),
            )
            self.conn.commit()
            return int(row["id"]), False
        initial = float(importance) if importance else float(confidence) * 5.0
        initial = max(1.0, min(5.0, initial))
        # 冲突检测（改进 18）：只对**事实**做——事实是唯一值的（住哪、叫什么），
        # 偏好可以并存（喜欢咖啡也可以喜欢跑步），不该被判成矛盾。
        conflict_with = 0
        conflict_info: dict = {}
        if kind == "fact":
            for existing in self.all_semantic(kind=kind):
                is_conflict, detail = self.conflict_pair(content, existing["content"])
                if is_conflict:
                    conflict_with = int(existing["id"])
                    # 打分排队（借鉴 Cyrene-Agent）：只有值得问的才冒泡给用户。
                    # 这里能给到的信号：证据只有新说法一方（one_side）、
                    # 目标是否还 active、影响面按重要度折算。
                    from cognition.conflict_score import score_conflict
                    impact = "high" if initial >= 4 else "medium" if initial >= 2.5 else "low"
                    conflict_info = score_conflict(
                        evidence="one_side",
                        active_target=(existing.get("status") == "active"),
                        impact_scope=impact,
                        local_contradiction=True,
                    )
                    break
        # ⚠⚠ **冲突不再让新说法隐身**（2026-10-15 改）。
        # 原来这里写 `status = "pending"`，而 `pending` 被
        # `all_semantic(status="active")` 挡在检索之外 →
        # **用户没回答那条询问，新说法就永久丢失**（实测：住南京→搬到成都，
        # 新的那条从没进过提示）。这是"永久丢失"的根源，比抽得不全更严重。
        # 现在：**新说法照常 active、照常可检索**，只用 `conflict_with` 记下
        # "它取代了哪条"，留给解析器（模型合并或用户拍板）去收尾。
        status = "active"
        cur = self.conn.execute(
            "INSERT INTO semantic (created_at, updated_at, kind, content, confidence,"
            " evidence_count, source_episodic, importance, peak, last_access_at, emotion,"
            " status, conflict_with, conflict_score, resolver_priority, core_fact)"
            " VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
            (stamp, stamp, kind, content, float(confidence), episodic_id,
             initial, initial, "", emotion or "", status, conflict_with,
             int(conflict_info.get("score", 0)),
             str(conflict_info.get("priority", "none")),
             1 if self.is_core_fact(content) else 0),
        )
        if conflict_with:
            priority = conflict_info.get("priority", "none")
            self.log("conflict", "semantic", int(cur.lastrowid),
                     f"新说法「{content[:30]}」与 #{conflict_with}"
                     f"「{next((i['content'] for i in self.all_semantic(kind=kind, status='active') if i['id'] == conflict_with), '')[:30]}」"
                     f"冲突（分数 {conflict_info.get('score', 0)}／{priority}）")
        self.conn.commit()
        return int(cur.lastrowid), True

    def all_semantic(self, kind: str | None = None, status: str = "active",
                     emotion: str | None = None) -> list[dict]:
        sql = "SELECT * FROM semantic WHERE status=?"
        args: list = [status]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if emotion:
            sql += " AND emotion=?"
            args.append(emotion)
        sql += " ORDER BY confidence DESC, evidence_count DESC, id DESC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ---------------- 情绪标签与情绪分布（改进 21） ----------------
    def emotion_stats(self, table: str = "both") -> dict:
        """按情绪统计记忆条数：能看出"什么话题伴随什么情绪"。"""
        out: dict[str, int] = {}
        tables = ("episodic", "semantic") if table == "both" else (self._table(table),)
        for name in tables:
            for row in self.conn.execute(
                    f"SELECT emotion, COUNT(*) AS n FROM {name}"
                    " WHERE COALESCE(emotion,'')<>'' GROUP BY emotion").fetchall():
                out[row["emotion"]] = out.get(row["emotion"], 0) + int(row["n"])
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def by_emotion(self, emotion: str, limit: int = 20) -> list[dict]:
        out: list[dict] = []
        for name in ("episodic", "semantic"):
            rows = self.conn.execute(
                f"SELECT * FROM {name} WHERE emotion=? ORDER BY id DESC LIMIT ?",
                (emotion, limit)).fetchall()
            for row in rows:
                item = dict(row)
                item["target"] = name
                out.append(item)
        return out

    def set_emotion(self, target: str, target_id: int, emotion: str) -> bool:
        """手动纠正情绪标签（改进 21：标签可纠正）。"""
        self.conn.execute(
            f"UPDATE {self._table(target)} SET emotion=? WHERE id=?",
            (emotion, target_id))
        self.conn.commit()
        self.log("update", target, target_id, f"情绪标签改为 {emotion}")
        return True

    # ---------------- 语义记忆去重合并（改进 17） ----------------
    @staticmethod
    def _bigrams(text: str) -> set:
        clean = "".join(ch for ch in str(text) if ch.isalnum())
        return {clean[i:i + 2] for i in range(len(clean) - 1)} or {clean}

    @classmethod
    def similarity(cls, a: str, b: str) -> float:
        ga, gb = cls._bigrams(a), cls._bigrams(b)
        if not ga or not gb:
            return 0.0
        return len(ga & gb) / len(ga | gb)

    # ---------------- 冲突处理（改进 18） ----------------
    # ---- 冲突检测的第二档：**栏位变了值**（2026-10-15 加）----
    # 第一档（共同前缀 + 不同尾巴）精度高但覆盖窄：它认得出
    # 「对方住在南京 ↔ 对方住在上海」，却认不出「对方住在南京 ↔ 对方搬到成都」
    # （共同前缀只有「对方」两个字，过不了 0.6 的门槛）。
    # 而后面这种**状态变更**恰恰是最该处理的一类。
    #
    # 第二档的形状：**同一个栏位 + 有变更词 + 值不同**。
    #   ✅ 对方住在南京 ↔ 对方搬到成都      （栏位=住所，变更词=搬到）
    #   ✅ 对方妹妹在读大学 ↔ 对方妹妹转学了  （栏位=学校，变更词=转学）
    #   ❌ 对方对芒果过敏 ↔ 对方对花生过敏    （没栏位词 → 不判，两条都对）
    #   ❌ 对方在工业软件公司上班 ↔ 对方在医疗软件公司上班
    #      （同为「单位」栏位但没有变更词 → **不判**。这两条可能真是两家公司，
    #        误判的代价比漏判大：会去动用户的记忆）
    _SLOT_WORDS: dict[str, tuple[str, ...]] = {
        "住所": ("住在", "搬到", "搬去", "搬家", "住所", "住址", "老家", "定居"),
        "单位": ("公司", "单位", "上班", "工作", "入职", "离职", "跳槽"),
        "学校": ("大学", "学校", "读书", "上学", "转学", "毕业", "专业"),
        "感情": ("男朋友", "女朋友", "对象", "结婚", "分手", "订婚"),
    }
    _CHANGE_WORDS = ("搬到", "搬去", "搬家", "改成", "改为", "换了", "换成",
                     "现在", "已经", "不再", "转学", "离职", "跳槽",
                     "分手", "毕业", "结了", "换成", "改叫")

    # **多值槽位**：这些谓词天然可以有多条并存，**永远不算冲突**（2026-10-18 补）。
    #
    # ⚠ 为什么必须单独列：原判据**第一档是"共同前缀占 60% + 两个不同的尾巴"**，
    #   而注释里认为「对方对芒果过敏 ↔ 对方对花生过敏」不会被判（前缀只有「对方对」3 字，
    #   占不到 7 字的 60%）。
    #   **但主语一长就破了**：`对方林小雨对花生过敏` ↔ `对方林小雨对海鲜过敏`
    #   共同前缀「对方林小雨对」= 6 字，正好 ≥ 0.6×10 → **误判成冲突**。
    #   实测后果：注入块里出现「同一件事有两种说法…以新的为准」，
    #   等于告诉模型"花生过期了"；下游的合并机制还可能**真的把两条忌口合成一条**（丢记忆）。
    _MULTI_VALUE = ("过敏", "不耐受", "不吃", "不能吃", "不敢吃", "忌口",
                    "喜欢", "爱喝", "爱吃", "讨厌", "怕")

    @classmethod
    def _slots(cls, text: str) -> set[str]:
        return {name for name, words in cls._SLOT_WORDS.items()
                if any(w in text for w in words)}

    @classmethod
    def _slot_value(cls, text: str) -> str:
        """把栏位词抠掉，剩下的当作「值」，用来比是不是同一个值。"""
        out = text
        for words in cls._SLOT_WORDS.values():
            for w in words:
                out = out.replace(w, "")
        return out.strip("，。、 的了对在")

    @classmethod
    def conflict_pair(cls, a: str, b: str) -> tuple[bool, str]:
        """判断两条语义记忆是不是「同一件事说了不同的值」。

        两档判据，命中任一即算冲突：

        **第一档（原有，高精度）**：共同前缀占了较短那条的大部分，
        剩下两个**都非空且不同**的小尾巴
        （「对方住在杭州」vs「对方住在上海」）。
        单纯补充信息（「喜欢喝美式」vs「喜欢喝美式，不加糖」）不算冲突——
        共同前缀吃掉整条短句、尾巴为空，交给去重合并处理。

        **第二档（新增，覆盖状态变更）**：同一个栏位（住所/单位/学校/感情）
        **且**有一方带变更词（搬到/改成/现在/不再/转学…）**且**值不同。
        「对方住在南京」vs「对方搬到成都」这种共同前缀很短的，靠这一档兜住。
        ⚠ 只有同栏位而没有变更词的**不判**（可能真是两家公司、两颗牙），
          误判代价比漏判大——会去动用户的记忆。
        """
        a, b = str(a or ""), str(b or "")
        if not a or not b or a == b:
            return False, ""

        # ---- 多值槽位：先排除，两条并存是对的 ----
        # ⚠ 必须在**第一档之前**判（见 `_MULTI_VALUE` 的注释：主语一长，
        #   第一档就会把两条忌口误判成冲突）。
        # ⚠ 只在**同一个谓词**上排除（2026-10-18 收紧）：
        #   若写成"两边各有一个多值词就不判"，会把「喜欢拿铁 ↔ 讨厌拿铁」
        #   这种**真矛盾**也放过。要同一个谓词（过敏↔过敏、不吃↔不吃）才算并存。
        shared_multi = {w for w in cls._MULTI_VALUE if w in a and w in b}
        if shared_multi:
            return False, ""

        # ---- 第一档：共同前缀 ----
        limit = min(len(a), len(b))
        prefix = 0
        while prefix < limit and a[prefix] == b[prefix]:
            prefix += 1
        if prefix >= 2 and prefix >= 0.6 * limit:
            tail_a, tail_b = a[prefix:], b[prefix:]
            if (tail_a and tail_b
                    and not tail_a.startswith(tail_b)
                    and not tail_b.startswith(tail_a)
                    and cls.similarity(a, b) >= 0.3):
                return True, f"{a} ↔ {b}"

        # ---- 第二档：同栏位 + 变更词 + 值不同 ----
        slots = cls._slots(a) & cls._slots(b)
        if slots:
            has_change = any(w in a or w in b for w in cls._CHANGE_WORDS)
            if has_change:
                va, vb = cls._slot_value(a), cls._slot_value(b)
                if va and vb and va != vb \
                        and va not in vb and vb not in va:
                    return True, f"{a} ↔ {b}（{'/'.join(sorted(slots))}变更）"
        return False, ""

    def core_facts(self, limit: int = 6) -> list[dict]:
        """身份类核心事实（名字/城市/宠物/过敏/作息…），按重要度取前几条。

        用途：回忆类问题里**兜底注入**——它们数量少但最常被问到，
        不该因为检索排序把它们挤掉（实测"我对什么过敏"就因此答不出来还编）。
        """
        rows = self.conn.execute(
            "SELECT * FROM semantic WHERE core_fact=1 AND status='active'"
            " ORDER BY importance DESC, id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["key"] = f"semantic#{item['id']}"
            item["score"] = max(0.35, float(item.get("importance") or 0) / 5.0)
            out.append(item)
        return out

    def pending_conflicts(self, limit: int = 20, include_none: bool = False) -> list[dict]:
        """列出待确认的冲突（新说法与旧说法矛盾，等用户拍板）。

        冒泡门槛：**只隐藏 none**（纯词法误判、目标已归档、完全没有证据）。
        high/normal/idle 都会列出，idle 排在后面。
        为什么不按原设计只留 high/normal：我们的提取管道拿不到"检索相关度""刚注入过"
        这类信号，分数天然偏低，若按 55 分卡，几乎所有真实冲突都会被判 none 而不问用户。

        include_none=True 时连 none 一起列出（调试/审计用）。
        """
        wanted = ("high", "normal", "idle", "none") if include_none else ("high", "normal", "idle")
        marks = ",".join("?" for _ in wanted)
        # ⚠ 查的是「**未决**冲突」而不是「status=pending」（2026-10-15 改）。
        # 冲突条目现在保持 `active`（不再隐身），所以判定条件变成
        # `conflict_with>0 且还没被解析掉`。
        rows = self.conn.execute(
            "SELECT * FROM semantic WHERE status='active' AND conflict_with>0"
            f" AND resolver_priority IN ({marks})"
            " ORDER BY conflict_score DESC, id DESC LIMIT ?",
            (*wanted, limit)).fetchall()
        out = []
        for row in rows:
            new = dict(row)
            old = self.entry("semantic", int(row["conflict_with"])) or {}
            out.append({"new_id": new["id"], "new": new["content"],
                        "score": int(new.get("conflict_score") or 0),
                        "priority": str(new.get("resolver_priority") or "none"),
                        "old_id": old.get("id"), "old": old.get("content", ""),
                        "created_at": new["created_at"]})
        return out

    def resolve_conflict(self, new_id: int, choice: str) -> dict:
        """用户拍板：choice = new（用新的）/ old（保留旧的）/ both（两个都对）。

        无论选哪个，**两条记录都留在库里**（旧的记 superseded_by，新的记状态），
        所以事后能追溯"以前是这么说的"（改进 18：不自动覆盖、旧版本可追溯）。
        """
        new = self.entry("semantic", new_id)
        if not new or int(new.get("conflict_with") or 0) <= 0:
            raise ValueError(f"#{new_id} 不是待确认的冲突条目")
        old_id = int(new["conflict_with"])
        stamp = now()
        if choice == "new":
            self.conn.execute(
                "UPDATE semantic SET status='superseded', superseded_by=?, updated_at=?"
                " WHERE id=?", (new_id, stamp, old_id))
            self.conn.execute(
                "UPDATE semantic SET status='active', conflict_with=0,"
                " confidence=MIN(0.95, confidence+0.05), updated_at=? WHERE id=?",
                (stamp, new_id))
        elif choice == "old":
            self.conn.execute(
                "UPDATE semantic SET status='rejected', updated_at=? WHERE id=?",
                (stamp, new_id))
        elif choice == "both":
            self.conn.execute(
                "UPDATE semantic SET status='active', conflict_with=0, updated_at=?"
                " WHERE id=?", (stamp, new_id))
        else:
            raise ValueError("choice 只能是 new / old / both")
        self.conn.commit()
        self.log("resolve_conflict", "semantic", new_id,
                 f"选了 {choice}（旧条目 #{old_id}「{str(new.get('content'))[:20]}」）")
        return {"ok": True, "choice": choice, "new_id": new_id, "old_id": old_id}

    def compress_transaction(self, threshold: float = 0.6,
                             dry_run: bool = False) -> dict:
        """带快照的压缩：先存快照 → 再合并 → 失败自动回滚，成功也可事后回滚。

        原实现（dedupe_semantic）是就地 UPDATE，合错了没法退。这里按借鉴来的
        顺序做：**先落快照，再动数据**；回滚时按原 status 原样写回（不是统统变 active）。
        """
        import json as _json
        before = [dict(row) for row in
                  self.conn.execute("SELECT * FROM semantic").fetchall()]
        if dry_run:
            preview = self.dedupe_semantic(threshold=threshold, dry_run=True)
            return {"merged": len(preview), "preview": True, "log_id": 0,
                    "total_before": len(before)}
        cur = self.conn.execute(
            "INSERT INTO compression_log (created_at, threshold, payload, status, note)"
            " VALUES (?,?,?,'applied','')",
            (now(), float(threshold), _json.dumps(before, ensure_ascii=False)))
        log_id = int(cur.lastrowid)
        self.conn.commit()
        try:
            merged = self.dedupe_semantic(threshold=threshold)
        except Exception as exc:
            self.conn.execute("UPDATE compression_log SET status='failed', note=?"
                              " WHERE id=?", (str(exc)[:120], log_id))
            self.conn.commit()
            self.rollback_compression(log_id)
            raise
        return {"merged": len(merged), "log_id": log_id,
                "total_before": len(before), "preview": False}

    def rollback_compression(self, log_id: int) -> dict:
        """把某次压缩回滚：按快照原样写回（保留 id 与 status），删掉压缩后新增的行。"""
        import json as _json
        row = self.conn.execute("SELECT * FROM compression_log WHERE id=?",
                                (int(log_id),)).fetchone()
        if not row:
            raise ValueError(f"没有这次压缩记录 #{log_id}")
        try:
            snapshot = _json.loads(row["payload"] or "[]")
        except Exception as exc:
            raise ValueError(f"快照损坏，无法回滚：{exc}") from exc
        if not snapshot:
            raise ValueError(f"#{log_id} 的快照是空的，拒绝回滚（怕删错数据）")
        columns = [item[1] for item in
                   self.conn.execute("PRAGMA table_info(semantic)").fetchall()]
        self.conn.execute("DELETE FROM semantic")
        for item in snapshot:
            keys = [key for key in columns if key in item]
            placeholders = ",".join("?" for _ in keys)
            self.conn.execute(
                f"INSERT INTO semantic ({','.join(keys)}) VALUES ({placeholders})",
                tuple(item[key] for key in keys))
        self.conn.execute("UPDATE compression_log SET status='rolled_back' WHERE id=?",
                          (int(log_id),))
        self.conn.commit()
        self.log("rollback_compression", "semantic", None,
                 f"回滚压缩 #{log_id}，恢复 {len(snapshot)} 条")
        return {"restored": len(snapshot), "log_id": int(log_id)}

    def compression_logs(self, limit: int = 10) -> list[dict]:
        """看压缩记录（能不能回滚、回滚过哪些）。"""
        rows = self.conn.execute(
            "SELECT id, created_at, threshold, status, note,"
            " length(payload) AS payload_size FROM compression_log"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def dedupe_semantic(self, threshold: float = 0.6, dry_run: bool = False) -> list[dict]:
        """合并相似语义记忆（同类型内）：保留更完整的一条，证据数累加，时间范围保留最早。

        被合并的那条标记 status='merged' 并记下 merged_into，合并动作写审计日志，
        所以"合并了谁、合并到哪"都能查（改进 17）。
        """
        merged: list[dict] = []
        for kind in ("fact", "preference", "relation"):
            items = self.all_semantic(kind=kind)
            used: set[int] = set()
            for i, base in enumerate(items):
                if base["id"] in used:
                    continue
                for other in items[i + 1:]:
                    if other["id"] in used:
                        continue
                    if self.similarity(base["content"], other["content"]) < threshold:
                        continue
                    # 谁更完整留谁；一样长就留证据更多的
                    keep, drop = (base, other)
                    if (len(other["content"]), other["evidence_count"]) > \
                            (len(base["content"]), base["evidence_count"]):
                        keep, drop = other, base
                    merged.append({"keep": keep["id"], "drop": drop["id"],
                                   "content": keep["content"],
                                   "dropped": drop["content"],
                                   "score": round(self.similarity(
                                       base["content"], other["content"]), 3)})
                    used.add(drop["id"])
                    if not dry_run:
                        self.conn.execute(
                            "UPDATE semantic SET status='merged', merged_into=?,"
                            " updated_at=? WHERE id=?",
                            (keep["id"], now(), drop["id"]))
                        self.conn.execute(
                            "UPDATE semantic SET evidence_count=evidence_count+?,"
                            " confidence=MAX(confidence, ?), created_at=MIN(created_at, ?),"
                            " updated_at=? WHERE id=?",
                            (int(drop["evidence_count"] or 1), float(drop["confidence"] or 0),
                             drop["created_at"], now(), keep["id"]))
                        self.log("merge", "semantic", keep["id"],
                                 f"合并 #{drop['id']}「{str(drop['content'])[:30]}」")
        if not dry_run and merged:
            self.conn.commit()
        return merged

    def semantic_count(self, status: str = "active") -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM semantic WHERE status=?", (status,)
        ).fetchone()[0])

    def set_semantic_status(self, semantic_id: int, status: str) -> None:
        self.conn.execute("UPDATE semantic SET status=?, updated_at=? WHERE id=?",
                          (status, now(), semantic_id))
        self.conn.commit()

    # ---------------- 反馈 ----------------
    def add_feedback(self, rating: str, note: str = "",
                     episodic_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO feedback (created_at, episodic_id, rating, note) VALUES (?,?,?,?)",
            (now(), episodic_id, rating, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def feedback_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT rating, COUNT(*) AS n FROM feedback GROUP BY rating"
        ).fetchall()
        return {r["rating"]: int(r["n"]) for r in rows}

    def recent_feedback(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 提醒 ----------------
    def add_reminder(self, content: str, due: str = "") -> int:
        content = normalize(content)
        if not content:
            raise ValueError("提醒内容为空")
        cur = self.conn.execute(
            "INSERT INTO reminders (created_at, content, due) VALUES (?,?,?)",
            (now(), content, due),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_reminders(self, limit: int = 10, status: str = "open") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close_reminder(self, reminder_id: int) -> None:
        self.conn.execute("UPDATE reminders SET status='done' WHERE id=?", (reminder_id,))
        self.conn.commit()

    # ---------------- 元信息 ----------------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def stats(self) -> dict:
        archived = {}
        for table in ("episodic", "semantic"):
            try:
                archived[table] = int(self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE archived=1").fetchone()[0])
            except Exception:
                archived[table] = 0
        return {
            "episodes": self.episode_count(),
            "semantic": self.semantic_count(),
            "feedback": self.feedback_stats(),
            "reminders": int(self.conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE status='open'").fetchone()[0]),
            "archived_episodes": archived["episodic"],
            "archived_semantic": archived["semantic"],
            "logs": self.log_count(),
        }

    def clear(self) -> dict[str, int]:
        """清空所有记忆表（调试用）。返回各表清掉了多少行。"""
        counts: dict[str, int] = {}
        for table in ("episodic", "semantic", "feedback", "reminders", "meta",
                      "memory_log", "memory_link"):
            try:
                counts[table] = int(self.conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                self.conn.execute(f"DELETE FROM {table}")
            except Exception:
                counts[table] = 0
        self.conn.commit()
        # 重置自增序列，便于调试时从 #1 重新计数
        try:
            self.conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('episodic','semantic','feedback','reminders','memory_log')")
            self.conn.commit()
        except Exception:
            pass
        return counts

    def close(self) -> None:
        self.conn.close()
