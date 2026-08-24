from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.models import ClosedPosition, Side


@dataclass(frozen=True)
class Statistics:
    trades: int
    wins: int
    losses: int
    win_rate: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    profit_factor: Decimal | None
    max_drawdown: Decimal
    average_win: Decimal
    average_loss: Decimal
    long_pnl: Decimal
    short_pnl: Decimal


def calculate_statistics(closed: list[ClosedPosition], period: str = "all") -> Statistics:
    start = _period_start(period)
    trades = [item for item in closed if item.closed_at >= start]
    pnls = [item.realized_pnl for item in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins, Decimal())
    gross_loss = sum(losses, Decimal())
    max_drawdown = _max_drawdown(pnls)
    return Statistics(
        trades=len(trades), wins=len(wins), losses=len(losses),
        win_rate=(Decimal(len(wins)) / Decimal(len(trades)) * 100 if trades else Decimal()),
        gross_profit=gross_profit, gross_loss=gross_loss, net_pnl=sum(pnls, Decimal()),
        profit_factor=(gross_profit / abs(gross_loss) if gross_loss else None),
        max_drawdown=max_drawdown,
        average_win=(gross_profit / len(wins) if wins else Decimal()),
        average_loss=(gross_loss / len(losses) if losses else Decimal()),
        long_pnl=sum((item.realized_pnl for item in trades if item.position.side is Side.LONG), Decimal()),
        short_pnl=sum((item.realized_pnl for item in trades if item.position.side is Side.SHORT), Decimal()),
    )


def _period_start(period: str) -> datetime:
    now = datetime.now(UTC)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    return datetime.min.replace(tzinfo=UTC)


def _max_drawdown(pnls: list[Decimal]) -> Decimal:
    equity = peak = Decimal()
    max_drawdown = Decimal()
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown
