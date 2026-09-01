import json

from data.selection_report import latest_selection_report
from data.store.sqlite_store import StockStore


def test_latest_selection_report_prefers_durable_agent_result(tmp_path):
    store = StockStore(str(tmp_path / "report.db"))
    report = {
        "profile": "screening_report",
        "title": "夜间预选股 2026-09-01",
        "run": {"run_date": "2026-09-01", "count": 1},
        "candidates": [{"code": "002541", "name": "鸿路钢构"}],
    }
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT INTO agent_decision_submissions
               (submission_key,task,mode,as_of,provider,model,status,decision,result,created_at)
               VALUES ('selection:test','selection','','snapshot','codex','test','ready','{}',?,
                       '2026-09-01 00:20:04')""",
            (json.dumps(report, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()

    loaded = latest_selection_report(store)
    assert loaded == report


def test_latest_selection_report_returns_none_when_empty(tmp_path):
    assert latest_selection_report(StockStore(str(tmp_path / "empty.db"))) is None
