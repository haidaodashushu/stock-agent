import argparse
import sqlite3

import scripts.live_trade_intent as live_trade_intent


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE live_trade_intents (
               intent_id TEXT PRIMARY KEY,
               status TEXT,
               suggested_price REAL,
               suggested_volume INTEGER,
               filled_price REAL DEFAULT 0,
               filled_volume INTEGER DEFAULT 0,
               filled_amount REAL DEFAULT 0,
               filled_at TEXT DEFAULT '',
               user_note TEXT DEFAULT '',
               expires_at TEXT DEFAULT ''
           )"""
    )
    conn.execute(
        """INSERT INTO live_trade_intents
           (intent_id, status, suggested_price, suggested_volume)
           VALUES ('L20260812110420-76991C', 'filled', 75.45, 100)"""
    )
    conn.commit()
    conn.close()


def test_fill_correction_preserves_reported_historical_execution_time(tmp_path, monkeypatch):
    db_path = tmp_path / "live-fill.db"
    _create_db(db_path)

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(live_trade_intent, "conn", connect)
    args = argparse.Namespace(
        intent_id="L20260812110420-76991C",
        price=76.20,
        volume=100,
        note="用户确认实际于2026-08-11卖出清仓",
        force=False,
        filled_at="2026-08-11 15:00:00",
    )

    assert live_trade_intent.fill(args) == 0

    conn = connect()
    row = conn.execute(
        "SELECT status, filled_price, filled_volume, filled_at, user_note "
        "FROM live_trade_intents WHERE intent_id=?",
        (args.intent_id,),
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "status": "filled",
        "filled_price": 76.2,
        "filled_volume": 100,
        "filled_at": "2026-08-11 15:00:00",
        "user_note": "用户确认实际于2026-08-11卖出清仓",
    }


def test_fill_rejects_invalid_historical_execution_time_before_database_write(monkeypatch):
    monkeypatch.setattr(
        live_trade_intent,
        "conn",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )
    args = argparse.Namespace(
        intent_id="L20260812110420-76991C",
        price=76.20,
        volume=100,
        note="",
        force=False,
        filled_at="2026-08-11",
    )

    assert live_trade_intent.fill(args) == 2
