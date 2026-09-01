"""板块轮动服务。

用问财板块涨幅 + 资金流数据构建轮动评分，并把板块强弱映射回股票候选池。
定位：给选股/盯盘提供“主线 + 补涨 + 过热”辅助因子，不替代量价策略。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from data.adapters.iwencai_client import IwenCaiClient
from data.ai_compute_pool import AI_COMPUTE_STOCKS
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.services.stock_sector_membership_service import load_stock_memberships
from data.store.sqlite_store import StockStore

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "data" / "cache" / "sector_rotation_snapshot.json"

NON_SECTOR_LABELS = {
    "融资融券", "沪股通", "深股通", "转融券标的", "标普道琼斯A股",
    "MSCI概念", "富时罗素概念", "同花顺漂亮100", "同花顺出海50",
    "同花顺新质50", "同花顺中特估100", "同花顺果指数",
    "中国AI 50", "高股息精选", "证金持股",
}


@dataclass
class SectorRotationSignal:
    name: str
    code: str = ""
    pct_6m: float = 0.0
    pct_3m: float = 0.0
    pct_1m: float = 0.0
    pct_5d: float = 0.0
    fund_inflow: float = 0.0
    score: float = 0.0
    stage: str = "neutral"  # leader / accelerating / laggard / overheat / neutral
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class SectorRotationService:
    """板块轮动评分服务。"""

    def __init__(
        self,
        client: IwenCaiClient | None = None,
        cache_minutes: int = 60,
        store: StockStore | None = None,
    ):
        self.client = client or IwenCaiClient(min_interval=0.6, timeout=40)
        self.cache_minutes = cache_minutes
        self.store = store or StockStore()

    def get_snapshot(self, refresh: bool = False, limit: int = 30) -> Dict[str, Any]:
        """返回轮动快照，默认读取短期缓存。"""
        if not refresh:
            cached = self._read_cache()
            if cached:
                return cached

        raw = self._fetch_raw(limit=limit)
        signals = self._build_signals(raw)
        errors = {
            key: str(value.get("error"))
            for key, value in raw.items()
            if isinstance(value, dict) and value.get("error")
        }
        if signals and errors:
            status = "partial"
        elif signals:
            status = "available"
        else:
            status = "unavailable"
        snapshot = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "iwencai",
            "status": status,
            "signals": [s.to_dict() for s in signals],
        }
        if errors:
            snapshot["errors"] = errors
        if status == "unavailable":
            snapshot["error"] = "; ".join(errors.values()) or "no sector signals returned"
        else:
            self._write_cache(snapshot)
        return snapshot

    def get_signals(self, refresh: bool = False, limit: int = 30) -> List[SectorRotationSignal]:
        snap = self.get_snapshot(refresh=refresh, limit=limit)
        return [SectorRotationSignal(**x) for x in snap.get("signals", [])]

    def get_stock_boosts(self, codes: List[str], refresh: bool = False) -> Dict[str, Tuple[float, List[str]]]:
        """把板块轮动映射到股票。返回 {code: (boost, tags)}。"""
        snapshot = self.get_snapshot(refresh=refresh)
        contexts = self.get_stock_contexts(codes, snapshot=snapshot)
        return {
            code: (
                float(context.get("rotation_score") or 0),
                [str(value) for value in context.get("tags") or []],
            )
            for code, context in contexts.items()
            if context.get("matches")
        }

    def get_stock_contexts(
        self,
        codes: List[str],
        *,
        snapshot: Dict[str, Any] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Map current memberships to one immutable rotation snapshot."""
        normalized = list(dict.fromkeys(str(code).zfill(6) for code in codes))
        if not normalized:
            return {}
        snapshot = snapshot or self.get_snapshot(refresh=False)
        signals: list[SectorRotationSignal] = []
        for row in snapshot.get("signals") or []:
            if not isinstance(row, dict):
                continue
            try:
                signal = SectorRotationSignal(**row)
            except TypeError:
                continue
            if signal.name and self._is_meaningful_sector(signal.name):
                signals.append(signal)
        sector_score = {signal.name: signal for signal in signals}
        memberships = load_stock_memberships(self.store, normalized)
        result: Dict[str, Dict[str, Any]] = {}
        for code in normalized:
            facts = [
                row for row in memberships.get(code, [])
                if self._is_meaningful_sector(str(row.get("sector_name") or ""))
            ]
            matched: dict[str, dict[str, Any]] = {}
            for fact in facts:
                hit = self._match_sector_detail(
                    str(fact.get("sector_name") or ""), sector_score,
                )
                if not hit:
                    continue
                signal, match_type = hit
                item = {
                    "name": signal.name,
                    "stage": signal.stage,
                    "score": signal.score,
                    "stock_boost": round(self._sector_to_stock_boost(signal), 2),
                    "matched_membership": fact.get("sector_name"),
                    "membership_type": fact.get("sector_type"),
                    "membership_source": fact.get("source"),
                    "match_type": match_type,
                }
                previous = matched.get(signal.name)
                if previous is None or float(item["stock_boost"]) > float(previous["stock_boost"]):
                    matched[signal.name] = item
            matches = sorted(
                matched.values(),
                key=lambda row: (float(row.get("score") or 0), float(row.get("stock_boost") or 0)),
                reverse=True,
            )
            scoring_matches = matches[:2]
            rotation_score = max(-2.0, min(3.0, round(
                sum(float(row.get("stock_boost") or 0) for row in scoring_matches), 2,
            )))
            industries = [
                str(row.get("sector_name")) for row in facts
                if row.get("sector_type") == "industry"
            ]
            concepts = [
                str(row.get("sector_name")) for row in facts
                if row.get("sector_type") == "concept"
            ]
            primary = next((
                str(row.get("sector_name")) for row in facts
                if row.get("sector_type") == "industry" and row.get("source") == "stock_metadata"
            ), industries[0] if industries else "")
            stages = {str(row.get("stage") or "neutral") for row in matches}
            if not facts:
                alignment = "unknown"
            elif stages.intersection({"leader", "accelerating"}) and rotation_score > 0:
                alignment = "positive"
            elif "overheat" in stages:
                alignment = "overheat"
            elif "laggard" in stages and rotation_score > 0:
                alignment = "watch"
            elif rotation_score < 0:
                alignment = "negative"
            else:
                alignment = "neutral"
            observed_values = sorted(
                str(row.get("observed_at") or "") for row in facts if row.get("observed_at")
            )
            result[code] = {
                "membership_status": "available" if facts else "missing",
                "membership_as_of": observed_values[-1] if observed_values else None,
                "primary_industry": primary or None,
                "industries": list(dict.fromkeys(industries))[:4],
                "concepts": list(dict.fromkeys(concepts))[:12],
                "rotation_status": snapshot.get("status") or "unavailable",
                "rotation_as_of": snapshot.get("created_at"),
                "rotation_source": snapshot.get("source"),
                "matches": matches[:5],
                "rotation_score": rotation_score,
                "alignment": alignment,
                "tags": [
                    f"轮动:{row['name']}:{row['stage']}" for row in scoring_matches
                ],
            }
        return result

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def _fetch_raw(self, limit: int = 30) -> Dict[str, Any]:
        queries = {
            "six_month": "近6个月涨幅排名前30的概念板块",
            "three_month": "近3个月涨幅排名前30的概念板块",
            "one_month": "近1个月涨幅排名前30的概念板块",
            "five_day": "近5日涨幅排名前30的概念板块",
            "fund": "主力资金净流入排名前30的概念板块 近3个月涨跌幅 近1个月涨跌幅",
            "low_recent": "近3个月涨幅小于10%且近5日涨幅大于5%的概念板块",
        }
        raw: Dict[str, Any] = {}
        for key, query in queries.items():
            try:
                raw[key] = self.client.query2data(
                    query,
                    skill_id="hithink-sector-selector",
                    limit=limit,
                )
            except Exception as e:
                logger.warning(f"板块轮动查询失败 {key}: {e}")
                raw[key] = {"datas": [], "error": str(e)}
        return raw

    # ------------------------------------------------------------------
    # 评分
    # ------------------------------------------------------------------
    def _build_signals(self, raw: Dict[str, Any]) -> List[SectorRotationSignal]:
        by_name: Dict[str, SectorRotationSignal] = {}

        def ensure(row: dict) -> SectorRotationSignal | None:
            name = self._name(row)
            if not name or not self._is_meaningful_sector(name):
                return None
            sig = by_name.get(name)
            if not sig:
                sig = SectorRotationSignal(name=name, code=str(row.get("指数代码", "")))
                by_name[name] = sig
            return sig

        for key, attr in (
            ("six_month", "pct_6m"),
            ("three_month", "pct_3m"),
            ("one_month", "pct_1m"),
            ("five_day", "pct_5d"),
        ):
            for row in raw.get(key, {}).get("datas", []):
                sig = ensure(row)
                if sig is None:
                    continue
                setattr(sig, attr, self._pct(row))

        for row in raw.get("fund", {}).get("datas", []):
            sig = ensure(row)
            if sig is None:
                continue
            sig.fund_inflow = self._fund(row)
            # fund query 同时带 1m/3m，补全缺失项
            pcts = self._all_pcts(row)
            if pcts:
                # 问财通常按 key 名日期返回，短周期/长周期顺序可能不固定：较小者更可能1m但保守只在缺失时填
                vals = sorted(pcts, key=lambda x: abs(x))
                if not sig.pct_1m and len(vals) >= 1:
                    sig.pct_1m = vals[0]
                if not sig.pct_3m and len(vals) >= 2:
                    sig.pct_3m = vals[-1]

        for row in raw.get("low_recent", {}).get("datas", []):
            sig = ensure(row)
            if sig is None:
                continue
            pcts = self._all_pcts(row)
            if len(pcts) >= 2:
                sig.pct_3m = pcts[0]
                sig.pct_5d = pcts[1]
            if "低位启动" not in sig.tags:
                sig.tags.append("低位启动")

        max_fund = max((abs(x.fund_inflow) for x in by_name.values()), default=1.0) or 1.0
        for sig in by_name.values():
            sig.score, sig.stage, sig.tags = self._score(sig, max_fund)

        return sorted(by_name.values(), key=lambda x: x.score, reverse=True)

    def _score(self, s: SectorRotationSignal, max_fund: float) -> Tuple[float, str, List[str]]:
        tags = list(dict.fromkeys(s.tags))
        fund_score = (s.fund_inflow / max_fund) * 4.0 if s.fund_inflow else 0.0
        momentum = min(4.0, max(-2.0, s.pct_5d / 4.0))
        month = min(3.0, max(-2.0, s.pct_1m / 8.0))
        trend = min(3.0, max(-1.0, s.pct_3m / 20.0))
        score = fund_score + momentum + month * 0.6 + trend * 0.8

        stage = "neutral"
        if s.pct_1m > 20 and s.pct_5d > 12:
            score -= 2.0
            stage = "overheat"
            tags.append("短线过热")
        elif s.pct_3m > 35 and s.pct_5d > 8:
            stage = "leader"
            tags.append("强主线")
        elif s.fund_inflow > 0 and s.pct_1m < 0 and s.pct_5d >= 0:
            score += 1.5
            stage = "laggard"
            tags.append("资金潜伏")
        elif s.pct_3m < 10 and s.pct_5d > 5:
            score += 1.2
            stage = "accelerating"
            tags.append("补涨启动")
        elif s.fund_inflow > 0 and 0 <= s.pct_1m <= 10:
            stage = "laggard"
            tags.append("低涨幅吸筹")

        return round(score, 2), stage, tags

    @staticmethod
    def _sector_to_stock_boost(s: SectorRotationSignal) -> float:
        if s.stage == "overheat":
            return min(0.5, max(-1.0, s.score / 10.0))
        if s.stage in ("laggard", "accelerating"):
            return min(1.8, max(0.0, s.score / 3.0))
        if s.stage == "leader":
            return min(1.2, max(0.0, s.score / 5.0))
        return min(0.8, max(-0.5, s.score / 6.0))

    # ------------------------------------------------------------------
    # 股票到板块映射
    # ------------------------------------------------------------------
    def _stock_sectors(self, code: str, db_sectors: List[str] | None = None) -> List[str]:
        sectors: List[str] = []
        info = FIFTEEN_FIVE_STOCKS.get(code) or {}
        sectors.extend(info.get("concepts", []))
        ai = AI_COMPUTE_STOCKS.get(code) or {}
        sectors.extend(ai.get("sectors", []))
        sectors.extend(db_sectors or [])
        return list(dict.fromkeys(
            x for x in sectors
            if x and self._is_meaningful_sector(x)
        ))

    def _db_sector_map(self, codes: List[str]) -> Dict[str, List[str]]:
        """Compatibility view backed by normalized memberships."""
        facts = load_stock_memberships(self.store, codes)
        return {
            code: list(dict.fromkeys(
                str(row.get("sector_name") or "") for row in rows if row.get("sector_name")
            ))
            for code, rows in facts.items()
        }

    @staticmethod
    def _is_meaningful_sector(name: str) -> bool:
        normalized = SectorRotationService._normalize_sector(name)
        excluded = {
            SectorRotationService._normalize_sector(value)
            for value in NON_SECTOR_LABELS
        }
        return bool(normalized) and normalized not in excluded

    def _match_sector(self, sector: str, sector_score: Dict[str, SectorRotationSignal]) -> SectorRotationSignal | None:
        detail = self._match_sector_detail(sector, sector_score)
        return detail[0] if detail else None

    def _match_sector_detail(
        self, sector: str, sector_score: Dict[str, SectorRotationSignal],
    ) -> tuple[SectorRotationSignal, str] | None:
        target = self._normalize_sector(sector)
        for name, signal in sector_score.items():
            if target and target == self._normalize_sector(name):
                return signal, "exact"
        alias_values = {
            self._normalize_sector(value)
            for value in self._aliases(sector)
            if self._normalize_sector(value) and self._normalize_sector(value) != target
        }
        for name, signal in sector_score.items():
            if self._normalize_sector(name) in alias_values:
                return signal, "alias"
        return None

    @staticmethod
    def _normalize_sector(value: str) -> str:
        return (
            str(value or "").strip().replace(" ", "")
            .replace("概念板块", "").replace("概念", "").replace("板块", "")
        )

    @staticmethod
    def _aliases(sector: str) -> List[str]:
        base = sector.replace("概念", "").replace("板块", "")
        groups = {
            "CPO光模块": ["共封装光学", "CPO", "光模块"],
            "AIDC数据中心": ["数据中心", "AIDC", "东数西算", "算力"],
            "AI服务器": ["数据中心", "东数西算", "液冷服务器", "算力"],
            "液冷": ["液冷服务器", "液冷"],
            "PCB": ["PCB", "印制电路板"],
            "先进封装": ["先进封装"],
            "存储芯片": ["存储芯片"],
            "AI芯片": ["芯片", "中国AI 50", "半导体"],
            "人工智能": ["人工智能", "中国AI 50"],
            "AI应用": ["人工智能", "中国AI 50", "阿里巴巴概念"],
            "机器人": ["机器人", "人形机器人"],
            "人形机器人": ["人形机器人", "机器人"],
            "汽车电子": ["汽车电子", "汽车芯片", "特斯拉概念"],
            "无人驾驶": ["汽车电子", "汽车芯片", "特斯拉概念", "新能源汽车"],
            "卫星导航": ["卫星导航", "无人机", "大飞机"],
            "低空经济": ["无人机", "大飞机", "卫星导航"],
            "信息安全": ["网络安全", "安防"],
            "网络安全": ["网络安全", "安防"],
            "物联网": ["物联网"],
            "高端装备": ["高端装备", "通用设备", "自动化设备"],
        }
        normalized = SectorRotationService._normalize_sector(sector)
        aliases = [sector, base]
        for canonical, values in groups.items():
            group = [canonical, *values]
            if normalized in {
                SectorRotationService._normalize_sector(value) for value in group
            }:
                aliases.extend(group)
        return list(dict.fromkeys(aliases))

    # ------------------------------------------------------------------
    # row helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _name(row: dict) -> str:
        return str(row.get("指数简称") or row.get("板块名称") or row.get("概念名称") or row.get("行业名称") or "")

    @staticmethod
    def _pct(row: dict) -> float:
        vals = SectorRotationService._all_pcts(row)
        return vals[0] if vals else 0.0

    @staticmethod
    def _all_pcts(row: dict) -> List[float]:
        vals = []
        for k, v in row.items():
            if str(k).startswith("涨跌幅["):
                try:
                    vals.append(float(v))
                except Exception:
                    pass
        return vals

    @staticmethod
    def _fund(row: dict) -> float:
        for k, v in row.items():
            if "主力" in str(k) and ("净买入" in str(k) or "资金流向" in str(k)):
                try:
                    return float(v)
                except Exception:
                    return 0.0
        return 0.0

    def _read_cache(self) -> Dict[str, Any] | None:
        try:
            if not CACHE_PATH.exists():
                return None
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            created_at = payload.get("created_at")
            try:
                observed_at = datetime.fromisoformat(str(created_at))
            except (TypeError, ValueError):
                observed_at = datetime.fromtimestamp(CACHE_PATH.stat().st_mtime)
            if datetime.now() - observed_at > timedelta(minutes=self.cache_minutes):
                return None
            payload.setdefault(
                "status",
                "available" if payload.get("signals") else "unavailable",
            )
            return payload
        except Exception:
            return None

    @staticmethod
    def _write_cache(snapshot: Dict[str, Any]):
        temporary = CACHE_PATH.with_name(f".{CACHE_PATH.name}.{os.getpid()}.tmp")
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(CACHE_PATH)
        except Exception as e:
            logger.warning(f"板块轮动缓存写入失败: {e}")
        finally:
            temporary.unlink(missing_ok=True)
