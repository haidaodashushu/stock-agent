"""
strategy/base.py - 策略基类定义
所有策略继承 BaseStrategy，实现 evaluate() 方法
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import json


class BaseStrategy(ABC):
    """策略基类"""

    def __init__(self, name: str = "", params: Optional[Dict] = None):
        self.name = name or self.__class__.__name__
        self.params = params or {}

    @abstractmethod
    def evaluate(self, context: Dict[str, Any]) -> Any:
        """
        执行策略评估
        Args:
            context: 上下文数据，由引擎传入
                    包含行情数据、持仓信息、账户信息等
        Returns:
            策略评估结果，格式由具体策略定义
        """
        ...

    def get_param(self, key: str, default: Any = None) -> Any:
        """获取策略参数"""
        return self.params.get(key, default)

    def set_param(self, key: str, value: Any):
        """更新策略参数"""
        self.params[key] = value

    def to_dict(self) -> Dict:
        """序列化"""
        return {
            "name": self.name,
            "type": self.__class__.__name__,
            "params": self.params,
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name='{self.name}', params={json.dumps(self.params, ensure_ascii=False)})"
