"""
config.py — 台指期契約規格與回測參數設定

所有數值皆可由使用者依實際狀況覆寫；此處提供合理預設值。
"""
import sys
from dataclasses import dataclass
from pathlib import Path


def app_base_dir(caller_file: str) -> Path:
    """專案根目錄（.env / data / frontend 的基準路徑）。

    PyInstaller 凍結後 __file__ 會解析到打包暫存目錄，因此改用 exe 所在
    目錄，讓 .env 等檔案照慣例放在 exe 旁邊即可被讀到。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(caller_file).resolve().parent.parent


@dataclass
class ContractSpec:
    """台指期契約規格"""
    name: str = "TX"              # TX=大台, MTX=小台, TMF=微台
    point_value: float = 200.0    # 每點台幣價值 (大台200 / 小台50 / 微台10)
    tick_size: float = 1.0        # 最小跳動點數


@dataclass
class CostConfig:
    """交易成本設定"""
    tax_rate: float = 0.00002     # 期貨交易稅：每次成交金額的萬分之0.2 (雙邊各收一次)
    commission_per_side: float = 25.0  # 每口單邊手續費（台幣），依券商而定，可調整
    slippage_points: float = 0.0  # 預設滑價點數，可依商品流動性調整


# 群益期貨 2024/2025 公告手續費區間（依商品別議價區間取「最大值」，採保守/worst-case估計）：
#   大台指(TX)：30~60 元/口 → 採 60 元
#   小台指(MTX)：20~35 元/口 → 採 35 元
#   微台指(TMF)：無群益官方公告區間，暫以市場常見報價上緣 15 元/口 保守估計（非群益官方數字，僅供參考）
COMMISSION_PER_SIDE_BY_CONTRACT: dict[str, float] = {
    "TX": 60.0,
    "MTX": 35.0,
    "TMF": 15.0,
}


def cost_config_for_contract(contract_name: str, *, tax_rate: float | None = None, slippage_points: float = 0.0) -> "CostConfig":
    """回傳採用群益期貨公告費率區間「最大值」（保守估計）的 CostConfig。"""
    commission = COMMISSION_PER_SIDE_BY_CONTRACT.get(contract_name, CostConfig().commission_per_side)
    return CostConfig(
        tax_rate=CostConfig().tax_rate if tax_rate is None else float(tax_rate),
        commission_per_side=commission,
        slippage_points=float(slippage_points),
    )


@dataclass
class StrategyConfig:
    """策略參數"""
    ma_fast: int = 5
    ma_mid: int = 20
    ma_slow: int = 60

    # 突破/跌破系統
    breakout_stop_loss_points: float = 150.0
    breakout_take_profit_points: float = 500.0

    # 回測/回彈系統
    pullback_stop_loss_points: float = 150.0
    pullback_take_profit_points: float = 300.0


@dataclass
class SessionConfig:
    """台指期交易時段（供未來擴充：判斷跳空、分時段報表等用）"""
    day_session_start: str = "08:45"
    day_session_end: str = "13:45"
    night_session_start: str = "15:00"
    night_session_end: str = "05:00"  # 次日


DEFAULT_CONTRACT = ContractSpec()
DEFAULT_COST = CostConfig()
DEFAULT_STRATEGY = StrategyConfig()
DEFAULT_SESSION = SessionConfig()
