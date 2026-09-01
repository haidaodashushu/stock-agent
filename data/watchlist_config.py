"""自选监控配置读写。

配置文件：默认 config/watchlist.local.json，可由 STOCK_WATCHLIST_CONFIG 覆盖。
- items: 自选股票及监控策略/时间窗/间隔
- strategies: 可选监控策略定义

运行状态：data/runtime/watchlist_state.json
- last_run_at: 每只股票上次监控执行时间
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from config.runtime_paths import configurable_path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = configurable_path("STOCK_WATCHLIST_CONFIG", "config/watchlist.local.json")
STATE_PATH = configurable_path("STOCK_WATCHLIST_STATE", "data/runtime/watchlist_state.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "items": [],
    "strategies": {
        "washout_start": {"name": "洗盘/启动监控", "description": "判断洗盘是否结束，等待放量启动确认。"},
        "top_distribution": {"name": "顶部/出货监控", "description": "监控高位出货风险。", "enabled": False},
        "breakout_retest": {"name": "突破回踩监控", "description": "监控突破后的缩量回踩确认。", "enabled": False},
    },
}


def normalize_code(code: str) -> str:
    return str(code or "").strip().zfill(6)


def _default_config() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=path.suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_raw_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return _default_config()
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("items", [])
    cfg.setdefault("strategies", DEFAULT_CONFIG["strategies"])
    return cfg


def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"items": {}}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("items", {})
    return state


def _state_for_code(state: Dict[str, Any], code: str) -> Dict[str, Any]:
    return state.setdefault("items", {}).setdefault(normalize_code(code), {})


def _strip_runtime(cfg: Dict[str, Any]) -> Dict[str, Any]:
    clean = json.loads(json.dumps(cfg, ensure_ascii=False))
    clean.pop("updated_at", None)
    for item in clean.get("items", []):
        item.pop("last_run_at", None)
    return clean


def _merge_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    state = _load_state()
    for item in cfg.get("items", []):
        code = normalize_code(item.get("code"))
        runtime = state.get("items", {}).get(code, {})
        last_run_at = runtime.get("last_run_at") or item.get("last_run_at")
        if last_run_at:
            item["last_run_at"] = last_run_at
    if state.get("updated_at"):
        cfg["state_updated_at"] = state["updated_at"]
    return cfg


def load_config(include_state: bool = True) -> Dict[str, Any]:
    cfg = _load_raw_config()
    return _merge_state(cfg) if include_state else _strip_runtime(cfg)


def save_config(cfg: Dict[str, Any]) -> None:
    _atomic_write(CONFIG_PATH, _strip_runtime(cfg))


def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _atomic_write(STATE_PATH, state)


def list_items(enabled_only: bool = False) -> List[Dict[str, Any]]:
    items = load_config().get("items", [])
    if enabled_only:
        return [x for x in items if x.get("enabled", True)]
    return items


def upsert_item(item: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    code = normalize_code(item.get("code"))
    if not code or code == "000000":
        raise ValueError("股票代码不能为空")
    strategies = item.get("strategies") or ["washout_start"]
    if isinstance(strategies, str):
        strategies = [s.strip() for s in strategies.split(",") if s.strip()]
    time_windows = item.get("time_windows") or ["09:30-11:30", "13:00-15:05"]
    if isinstance(time_windows, str):
        time_windows = [s.strip() for s in time_windows.split(",") if s.strip()]
    normalized = {
        "code": code,
        "name": str(item.get("name") or "").strip(),
        "enabled": bool(item.get("enabled", True)),
        "strategies": strategies,
        "time_windows": time_windows,
        "interval_minutes": int(item.get("interval_minutes") or 60),
    }
    items = cfg.setdefault("items", [])
    for i, old in enumerate(items):
        if normalize_code(old.get("code")) == code:
            items[i] = normalized
            save_config(cfg)
            return normalized
    items.append(normalized)
    save_config(cfg)
    return normalized


def delete_item(code: str) -> bool:
    cfg = load_config()
    code = normalize_code(code)
    old_len = len(cfg.get("items", []))
    cfg["items"] = [x for x in cfg.get("items", []) if normalize_code(x.get("code")) != code]
    changed = len(cfg["items"]) != old_len
    if changed:
        save_config(cfg)
        state = _load_state()
        state.get("items", {}).pop(code, None)
        save_state(state)
    return changed


def mark_run(code: str, run_at: datetime | None = None) -> None:
    code = normalize_code(code)
    ts = (run_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    state = _load_state()
    _state_for_code(state, code)["last_run_at"] = ts
    save_state(state)
