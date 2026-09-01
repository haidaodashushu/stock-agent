"""新闻事件规则打分。

第一版先用可解释关键词规则，避免大模型编造；后续可叠加NLP/LLM摘要。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class NewsScore:
    sentiment: str
    score: float
    risk_level: str
    tags: List[str]


POSITIVE_RULES = [
    ("重大合同", 3.0, "重大合同"), ("签订", 1.5, "合同/合作"), ("中标", 2.5, "中标"),
    ("战略合作", 2.0, "合作"), ("合作", 1.0, "合作"), ("量产", 2.0, "量产"),
    ("突破", 1.5, "技术突破"), ("国产替代", 2.0, "国产替代"), ("回购", 2.0, "回购"),
    ("增持", 2.0, "增持"), ("业绩预增", 2.5, "业绩预增"), ("净利润增长", 2.0, "业绩增长"),
    ("机构净买入", 2.0, "龙虎榜机构买入"), ("龙虎榜", 0.8, "龙虎榜"),
    ("政策支持", 2.0, "政策利好"), ("十五五", 1.5, "十五五"),
    ("芯片", 0.8, "芯片"), ("机器人", 0.8, "机器人"), ("低空经济", 0.8, "低空经济"),
    ("人工智能", 0.8, "AI"), ("AI", 0.8, "AI"), ("氮化镓", 0.8, "半导体材料"),
    ("工业互联网", 1.8, "工业互联网"), ("工业5G", 1.5, "工业5G"),
    ("工业软件", 1.2, "工业软件"), ("数据要素", 1.0, "数据要素"),
    ("算力", 1.0, "算力"), ("行动方案", 1.0, "政策文件"),
    ("实施意见", 1.0, "政策文件"), ("八部门", 1.2, "多部门政策"),
    ("工信部", 0.8, "工信部"),
]

NEGATIVE_RULES = [
    ("减持", -4.0, "减持"), ("拟减持", -4.0, "减持"), ("监管函", -3.0, "监管函"),
    ("立案", -5.0, "立案调查"), ("调查", -2.5, "调查"), ("问询函", -3.0, "问询函"),
    ("亏损", -3.0, "亏损"), ("下滑", -2.0, "业绩下滑"), ("终止", -2.5, "终止事项"),
    ("风险提示", -2.5, "风险提示"), ("异常波动", -1.2, "异常波动"),
    ("不存在应披露而未披露", -2.0, "异动澄清"), ("澄清", -1.5, "澄清"),
    ("解禁", -2.0, "限售解禁"), ("诉讼", -3.0, "诉讼"), ("处罚", -4.0, "处罚"),
]


def analyze_news(title: str, content: str = "", category: str = "news", heat: float = 0) -> NewsScore:
    text = f"{title or ''} {content or ''}"
    score = 0.0
    tags: List[str] = []

    for kw, delta, tag in POSITIVE_RULES:
        if kw in text:
            score += delta
            tags.append(tag)
    for kw, delta, tag in NEGATIVE_RULES:
        if kw in text:
            score += delta
            tags.append(tag)

    if category == "hot_keyword":
        # 热词本身偏催化，但控制权重，防止纯热词压过重大利空。
        score += min(float(heat or 0) / 100.0, 1.0)
        tags.append("热词")

    score = max(-5.0, min(5.0, round(score, 2)))
    sentiment = "positive" if score >= 1 else ("negative" if score <= -1 else "neutral")
    risk_level = "high" if score <= -3 or score >= 3 else ("medium" if abs(score) >= 1.5 else "low")
    # 保持顺序去重
    tags = list(dict.fromkeys(tags))
    return NewsScore(sentiment=sentiment, score=score, risk_level=risk_level, tags=tags)
