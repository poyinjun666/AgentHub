"""
持久化层 - SQLite 存储
- tasks 表：任务主表（id、状态、提交方、artifact、验收结果、重试次数）
- audit_log 表：每次状态变更 / 消息流转都落盘，支撑审计和回放
零依赖（Python 自带 sqlite3），可换 PostgreSQL/MySQL
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Optional

import config
from .state import TaskState, assert_transition, IllegalTransition


class Store:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def _cursor(self):
        cur = self._conn().cursor()
        try:
            yield cur
            self._conn().commit()
        except Exception:
            self._conn().rollback()
            raise

    def _init_schema(self):
        with self._cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id        TEXT PRIMARY KEY,
                    trace_id       TEXT NOT NULL,
                    title          TEXT,
                    spec           TEXT,          -- 任务规格 JSON
                    state          TEXT NOT NULL,
                    assignee       TEXT,          -- 负责的 agent_id
                    artifact_ref   TEXT,          -- 产出引用
                    self_report    TEXT,          -- agent 自报告
                    review_result  TEXT,          -- 中枢验收结果 JSON
                    retry_count    INTEGER DEFAULT 0,
                    created_at     REAL,
                    updated_at     REAL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL NOT NULL,
                    task_id    TEXT,
                    trace_id   TEXT,
                    actor      TEXT,             -- 谁触发的（agent_id / hub / system）
                    action     TEXT,             -- create / submit / transition / review / message
                    from_state TEXT,
                    to_state   TEXT,
                    detail     TEXT              -- JSON 详情
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id);")

    # ----------------------------------------------------------
    # 审计
    # ----------------------------------------------------------
    def audit(self, *, task_id: str = "", trace_id: str = "", actor: str = "",
              action: str, from_state: str = "", to_state: str = "", detail: dict = None):
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO audit_log (ts, task_id, trace_id, actor, action,
                                       from_state, to_state, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time.time(), task_id, trace_id, actor, action,
                from_state, to_state, json.dumps(detail or {}, ensure_ascii=False),
            ))

    # ----------------------------------------------------------
    # 任务 CRUD
    # ----------------------------------------------------------
    def create_task(self, *, title: str, spec: dict, assignee: str,
                    trace_id: str = "", actor: str = "system") -> dict:
        task_id = uuid.uuid4().hex[:12]
        trace_id = trace_id or task_id
        now = time.time()
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO tasks (task_id, trace_id, title, spec, state,
                                   assignee, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (task_id, trace_id, title, json.dumps(spec, ensure_ascii=False),
                  TaskState.CREATED, assignee, now, now))
        self.audit(task_id=task_id, trace_id=trace_id, actor=actor,
                   action="create", to_state=TaskState.CREATED,
                   detail={"title": title, "assignee": assignee})
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            for k in ("spec", "review_result"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except json.JSONDecodeError:
                        pass
            return d

    def list_tasks(self, state: str = None) -> list[dict]:
        with self._cursor() as cur:
            if state:
                rows = cur.execute("SELECT * FROM tasks WHERE state=? ORDER BY updated_at DESC",
                                   (state,)).fetchall()
            else:
                rows = cur.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def transition(self, task_id: str, to_state: str, actor: str = "",
                   detail: dict = None) -> dict:
        """状态机转换，非法跳转抛 IllegalTransition"""
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"task not found: {task_id}")
        from_state = task["state"]
        if from_state == to_state:
            return task
        try:
            assert_transition(from_state, to_state, task_id)
        except IllegalTransition:
            self.audit(task_id=task_id, trace_id=task["trace_id"], actor=actor,
                       action="illegal_transition_attempt",
                       from_state=from_state, to_state=to_state, detail=detail)
            raise

        with self._cursor() as cur:
            cur.execute("""
                UPDATE tasks SET state=?, updated_at=? WHERE task_id=?
            """, (to_state, time.time(), task_id))
        self.audit(task_id=task_id, trace_id=task["trace_id"], actor=actor,
                   action="transition", from_state=from_state, to_state=to_state,
                   detail=detail)
        return self.get_task(task_id)

    def update_artifact(self, task_id: str, artifact_ref: str, self_report: str,
                        actor: str = ""):
        task = self.get_task(task_id)
        with self._cursor() as cur:
            cur.execute("""
                UPDATE tasks SET artifact_ref=?, self_report=?, updated_at=?
                WHERE task_id=?
            """, (artifact_ref, self_report, time.time(), task_id))
        self.audit(task_id=task_id, trace_id=task["trace_id"], actor=actor,
                   action="update_artifact",
                   detail={"artifact_ref": artifact_ref, "self_report": self_report})

    def save_review(self, task_id: str, review_result: dict, actor: str = "hub"):
        task = self.get_task(task_id)
        with self._cursor() as cur:
            cur.execute("""
                UPDATE tasks SET review_result=?, updated_at=?
                WHERE task_id=?
            """, (json.dumps(review_result, ensure_ascii=False), time.time(), task_id))
        self.audit(task_id=task_id, trace_id=task["trace_id"], actor=actor,
                   action="review", detail=review_result)

    def inc_retry(self, task_id: str) -> int:
        with self._cursor() as cur:
            cur.execute("UPDATE tasks SET retry_count = retry_count + 1 WHERE task_id=?",
                        (task_id,))
            row = cur.execute("SELECT retry_count FROM tasks WHERE task_id=?",
                              (task_id,)).fetchone()
        return row["retry_count"]