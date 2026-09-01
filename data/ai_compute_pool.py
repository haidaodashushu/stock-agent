"""AI算力/科技细分赛道池。

用途：
- 给选股评分提供赛道权重；
- 给 Web/自选监控提供标签；
- 后续按赛道做强弱轮动、监控卡片和复盘。

注意：这是“候选池/主题池”，不是买入建议。最终仍需叠加量价、风控、交易规则。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# 赛道权重：越贴近 AI 算力核心瓶颈，权重越高。
AI_COMPUTE_SECTORS: Dict[str, dict] = {
    "CPO光模块": {"tier": 1, "score_bonus": 2.0, "keywords": ["CPO", "光模块", "光通信", "高速光器件"]},
    "AI服务器": {"tier": 1, "score_bonus": 2.0, "keywords": ["AI服务器", "服务器", "算力", "云计算"]},
    "高速连接": {"tier": 1, "score_bonus": 1.8, "keywords": ["高速连接", "连接器", "交换机"]},
    "PCB": {"tier": 1, "score_bonus": 1.8, "keywords": ["PCB", "高频高速板", "HDI"]},
    "液冷": {"tier": 1, "score_bonus": 1.6, "keywords": ["液冷", "散热", "温控"]},
    "电源": {"tier": 1, "score_bonus": 1.5, "keywords": ["电源", "UPS", "数据中心电源"]},
    "AIDC数据中心": {"tier": 1, "score_bonus": 1.6, "keywords": ["AIDC", "数据中心", "IDC", "算力租赁"]},
    "先进封装": {"tier": 1, "score_bonus": 1.8, "keywords": ["先进封装", "封测", "Chiplet"]},
    "存储芯片": {"tier": 1, "score_bonus": 1.7, "keywords": ["存储芯片", "HBM", "DRAM", "Flash"]},
    "AI芯片": {"tier": 2, "score_bonus": 1.5, "keywords": ["AI芯片", "GPU", "CPU", "智能计算"]},
    "AI PC": {"tier": 2, "score_bonus": 1.2, "keywords": ["AI PC", "端侧AI"]},
    "MLCC": {"tier": 2, "score_bonus": 1.1, "keywords": ["MLCC", "被动元件"]},
    "电子材料": {"tier": 2, "score_bonus": 1.1, "keywords": ["电子布", "树脂", "铜箔", "覆铜板"]},
    "算力租赁": {"tier": 3, "score_bonus": 0.6, "keywords": ["算力租赁", "云算力"]},
}

# 先放已知核心/高相关 A 股。后续可继续扩充、用 akshare/概念板块自动补充。
AI_COMPUTE_STOCKS: Dict[str, dict] = {
    # AI服务器 / AIDC
    "000977": {"name": "浪潮信息", "sectors": ["AI服务器", "AIDC数据中心"], "priority": "high"},
    "000938": {"name": "紫光股份", "sectors": ["AI服务器", "高速连接", "AIDC数据中心"], "priority": "high"},
    "601138": {"name": "工业富联", "sectors": ["AI服务器", "AIDC数据中心"], "priority": "high"},
    "603019": {"name": "中科曙光", "sectors": ["AI服务器", "AIDC数据中心"], "priority": "high"},

    # CPO / 光模块 / 光通信
    "300308": {"name": "中际旭创", "sectors": ["CPO光模块"], "priority": "high"},
    "300502": {"name": "新易盛", "sectors": ["CPO光模块"], "priority": "high"},
    "300394": {"name": "天孚通信", "sectors": ["CPO光模块", "高速连接"], "priority": "high"},
    "002281": {"name": "光迅科技", "sectors": ["CPO光模块"], "priority": "medium"},
    "300548": {"name": "博创科技", "sectors": ["CPO光模块"], "priority": "medium"},
    "300570": {"name": "太辰光", "sectors": ["CPO光模块"], "priority": "medium"},
    "300620": {"name": "光库科技", "sectors": ["CPO光模块"], "priority": "medium"},
    "000063": {"name": "中兴通讯", "sectors": ["高速连接", "AIDC数据中心"], "priority": "medium"},
    "301013": {"name": "利和兴", "sectors": ["高速连接", "AI服务器"], "priority": "high"},

    # PCB / 覆铜板 / 电子材料
    "002463": {"name": "沪电股份", "sectors": ["PCB"], "priority": "high"},
    "002916": {"name": "深南电路", "sectors": ["PCB"], "priority": "high"},
    "600183": {"name": "生益科技", "sectors": ["PCB", "电子材料"], "priority": "high"},
    "603228": {"name": "景旺电子", "sectors": ["PCB"], "priority": "medium"},
    "002938": {"name": "鹏鼎控股", "sectors": ["PCB", "AI PC"], "priority": "medium"},
    "301150": {"name": "中一科技", "sectors": ["电子材料"], "priority": "medium"},
    "300476": {"name": "胜宏科技", "sectors": ["PCB"], "priority": "high"},
    "301208": {"name": "中亦科技", "sectors": ["AIDC数据中心"], "priority": "medium"},

    # 液冷 / 温控 / 电源
    "300442": {"name": "润泽科技", "sectors": ["AIDC数据中心", "液冷"], "priority": "medium"},
    "300990": {"name": "同飞股份", "sectors": ["液冷"], "priority": "medium"},
    "002837": {"name": "英维克", "sectors": ["液冷", "AIDC数据中心"], "priority": "high"},
    "300499": {"name": "高澜股份", "sectors": ["液冷"], "priority": "medium"},
    "300274": {"name": "阳光电源", "sectors": ["电源", "AIDC数据中心"], "priority": "medium"},
    "002335": {"name": "科华数据", "sectors": ["电源", "AIDC数据中心"], "priority": "medium"},

    # 芯片 / 先进封装 / 存储
    "688041": {"name": "海光信息", "sectors": ["AI芯片"], "priority": "high"},
    "688256": {"name": "寒武纪", "sectors": ["AI芯片"], "priority": "high"},
    "300474": {"name": "景嘉微", "sectors": ["AI芯片"], "priority": "medium"},
    "600584": {"name": "长电科技", "sectors": ["先进封装"], "priority": "high"},
    "002156": {"name": "通富微电", "sectors": ["先进封装"], "priority": "high"},
    "002185": {"name": "华天科技", "sectors": ["先进封装"], "priority": "medium"},
    "688008": {"name": "澜起科技", "sectors": ["存储芯片", "AI芯片"], "priority": "high"},
    "603986": {"name": "兆易创新", "sectors": ["存储芯片"], "priority": "medium"},
    "688525": {"name": "佰维存储", "sectors": ["存储芯片", "AI PC"], "priority": "medium"},

    # AI PC / 端侧
    "688111": {"name": "金山办公", "sectors": ["AI PC"], "priority": "medium"},
    "300033": {"name": "同花顺", "sectors": ["AI PC"], "priority": "medium"},

    # 金融科技高弹性观察（用户自选）
    "300803": {"name": "指南针", "sectors": ["AI PC"], "priority": "medium"},
}


def get_stock_tags(code: str) -> List[str]:
    info = AI_COMPUTE_STOCKS.get(str(code).zfill(6), {})
    return list(info.get("sectors", []))


def get_stock_bonus(code: str) -> float:
    info = AI_COMPUTE_STOCKS.get(str(code).zfill(6))
    if not info:
        return 0.0
    bonus = sum(AI_COMPUTE_SECTORS.get(s, {}).get("score_bonus", 0.0) for s in info.get("sectors", []))
    if info.get("priority") == "high":
        bonus += 0.5
    return round(min(bonus, 4.0), 1)


def get_stocks_by_sector(sector: str) -> Dict[str, dict]:
    return {code: info for code, info in AI_COMPUTE_STOCKS.items() if sector in info.get("sectors", [])}


def all_sectors() -> List[Tuple[str, dict]]:
    return sorted(AI_COMPUTE_SECTORS.items(), key=lambda x: (x[1].get("tier", 99), x[0]))


if __name__ == "__main__":
    print(f"AI算力科技赛道池: {len(AI_COMPUTE_STOCKS)} 只")
    for sector, meta in all_sectors():
        members = get_stocks_by_sector(sector)
        print(f"{sector} T{meta['tier']} bonus={meta['score_bonus']}: {len(members)}只")
