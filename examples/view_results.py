"""
AgentHub 任务结果查看工具
用法：
    cd D:/CodeProject/AgentHub
    python examples/view_results.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def list_tasks(limit: int = 10):
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT task_id, trace_id, title, state, assignee, artifact_ref, self_report, review_result, updated_at "
        "FROM tasks ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    print(f"\n[Tasks] 最近 {len(rows)} 个任务：\n")
    print(f"{'任务ID':<14} {'状态':<10} {'负责人':<10} {'更新时间':<20} {'标题'}")
    print("-" * 80)
    for r in rows:
        print(f"{r['task_id']:<14} {r['state']:<10} {r['assignee'] or '-':<10} "
              f"{format_time(r['updated_at']):<20} {r['title']}")

        rr = r["review_result"]
        if rr:
            try:
                if isinstance(rr, str):
                    rr = json.loads(rr)
                verdict = rr.get("verdict", "")
                issues = rr.get("issues", [])
                if verdict:
                    print(f"  +- 验收结果: {verdict}")
                    for issue in issues[:3]:
                        print(f"      - {issue}")
            except Exception:
                pass

        if r["artifact_ref"]:
            path = r["artifact_ref"].replace("file://", "")
            print(f"  +- 产物文件: {path}")


def list_audit_logs(limit: int = 10):
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, actor, action, from_state, to_state, task_id "
        "FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    print(f"\n[Audit] 最近 {len(rows)} 条审计日志：\n")
    print(f"{'时间':<20} {'执行者':<10} {'动作':<20} {'任务ID':<14} {'状态流转'}")
    print("-" * 80)
    for r in rows:
        transition = f"{r['from_state'] or '-'} -> {r['to_state'] or '-'}"
        print(f"{format_time(r['ts']):<20} {r['actor']:<10} {r['action']:<20} "
              f"{r['task_id']:<14} {transition}")


def main():
    print("=" * 80)
    print("AgentHub Result Viewer")
    print(f"数据库: {config.DB_PATH}")
    print(f"产物目录: {os.path.join(os.path.dirname(config.DB_PATH), 'artifacts')}")
    print("=" * 80)

    list_tasks(10)
    list_audit_logs(10)


if __name__ == "__main__":
    main()
