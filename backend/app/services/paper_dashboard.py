"""The PAPER trader dashboard read model.

Everything the dashboard shows is derived here, from the running
:class:`~app.services.paper_session.PaperSession`, so the frontend only renders.
A value that is not known is ``None`` - never a placeholder number - and every
reason a trade is not being taken is spelled out in ``blocked_reasons``.

Prices are the session's own completed bars. Nothing here reads the database, a
broker or the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.config.settings import TradingMode
from app.domain.backtest.execution import target_price
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.contract import Signal
from app.domain.strategy.orb import OrbReason
from app.services.paper_session import PaperSession

__all__ = ["PaperDashboard", "build_paper_dashboard"]

StrategyAction = Literal["BUY", "SELL", "WAIT", "HOLD", "EXIT"]
TradeStatus = Literal[
    "UNAVAILABLE", "NO_SIGNAL", "BLOCKED", "ORDER_WORKING", "IN_POSITION", "EXITED"
]

_STRATEGY_TEXT = {
    OrbReason.LONG_BREAKOUT: "Price closed above the opening range",
    OrbReason.SHORT_BREAKOUT: "Price closed below the opening range",
    OrbReason.OPENING_RANGE_INCOMPLETE: "Opening range is still forming",
    OrbReason.NO_BREAKOUT: "No close outside the opening range",
    OrbReason.RANGE_TOO_NARROW: "Opening range is too narrow to trade",
    OrbReason.RANGE_TOO_WIDE: "Opening range is too wide relative to ATR",
    OrbReason.ATR_UNAVAILABLE: "Not enough history for ATR; the strategy will not trade",
    OrbReason.ENTRY_CUTOFF_REACHED: "Past the last entry time for the session",
    OrbReason.DIRECTION_ALREADY_SIGNALLED: "This direction already signalled today",
}


_PAISE = Decimal("0.01")


def _money(value: Decimal | None) -> str | None:
    """Render at paise scale when that loses nothing; otherwise the exact value."""
    if value is None:
        return None
    scaled = value.quantize(_PAISE)
    return str(scaled if scaled == value else value)


class InstrumentView(BaseModel):
    symbol: str
    instrument_token: int


class SessionView(BaseModel):
    state: str
    is_trading_day: bool
    local_time: str


class DataView(BaseModel):
    status: Literal["fresh", "stale", "unavailable"]
    last_bar_start: str | None = None
    last_bar_end: str | None = None
    last_close: str | None = None
    age_seconds: float | None = None
    stale_after_seconds: float


class StrategyView(BaseModel):
    action: StrategyAction
    reason_code: str | None = None
    reason: str | None = None


class RiskReasonView(BaseModel):
    code: str
    detail: str


class RiskView(BaseModel):
    status: Literal["APPROVED", "REJECTED", "NOT_EVALUATED"]
    quantity: int | None = None
    limited_by: str | None = None
    reasons: list[RiskReasonView] = []


class AiView(BaseModel):
    status: Literal[
        "TAKE_TRADE", "WAIT", "REJECT", "INVALID_RESPONSE", "NOT_CONSULTED", "NOT_CONFIGURED"
    ]
    reason: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None


class PlanView(BaseModel):
    direction: Literal["LONG", "SHORT"] | None = None
    entry: str | None = None
    entry_basis: Literal["fill", "signal_bar_close"] | None = None
    stop_loss: str | None = None
    target: str | None = None
    reward_to_risk: str | None = None
    quantity: int | None = None


class PositionView(BaseModel):
    side: Literal["FLAT", "LONG", "SHORT"] | None = None
    quantity: int | None = None
    entry_price: str | None = None
    mark_price: str | None = None
    unrealized_pnl: str | None = None
    realized_pnl: str | None = None
    closed_trades: int | None = None


class PaperDashboard(BaseModel):
    trading_mode: str
    is_paper: bool
    generated_at: str
    session_running: bool
    instrument: InstrumentView | None = None
    session: SessionView
    data: DataView
    strategy: StrategyView
    risk: RiskView
    ai: AiView
    plan: PlanView
    position: PositionView
    trade_status: TradeStatus
    blocked_reasons: list[str]


def build_paper_dashboard(
    session: PaperSession | None,
    *,
    trading_mode: TradingMode,
    now: datetime,
    stale_after: timedelta,
    calendar: MarketSessionCalendar | None = None,
) -> PaperDashboard:
    calendar = (
        session.calendar
        if session is not None
        else (calendar or MarketSessionCalendar.nse_equity())
    )
    described = calendar.describe(now)
    session_view = SessionView(
        state=str(described["state"]),
        is_trading_day=bool(described["is_trading_day"]),
        local_time=str(described["local_time"]),
    )
    is_paper = trading_mode is TradingMode.PAPER
    blocked: list[str] = []
    if not is_paper:
        blocked.append(
            f"Trading mode is {trading_mode.value.upper()}; this dashboard serves PAPER mode only"
        )

    last = session.last if session is not None else None
    if session is None:
        blocked.append("No paper session is running in this process")
    elif last is None:
        blocked.append("The paper session has not received a completed bar yet")

    data = DataView(status="unavailable", stale_after_seconds=stale_after.total_seconds())
    if last is not None:
        age = (now - last.bar.end_at).total_seconds()
        stale = age > stale_after.total_seconds()
        data = DataView(
            status="stale" if stale else "fresh",
            last_bar_start=last.bar.start_at.isoformat(),
            last_bar_end=last.bar.end_at.isoformat(),
            last_close=str(last.bar.close),
            age_seconds=round(age, 1),
            stale_after_seconds=stale_after.total_seconds(),
        )
        if stale:
            blocked.append(
                f"Market data is stale: the last bar closed {round(age)}s ago; "
                "do not act on these values"
            )

    if session is None or last is None:
        return PaperDashboard(
            trading_mode=trading_mode.value,
            is_paper=is_paper,
            generated_at=now.isoformat(),
            session_running=session is not None,
            instrument=_instrument(session),
            session=session_view,
            data=data,
            strategy=StrategyView(action="WAIT"),
            risk=RiskView(status="NOT_EVALUATED"),
            ai=AiView(
                status="NOT_CONFIGURED"
                if session is None or session.analyst is None
                else "NOT_CONSULTED"
            ),
            plan=PlanView(),
            position=PositionView(),
            trade_status="UNAVAILABLE",
            blocked_reasons=blocked,
        )

    paper = session.paper
    position = paper.position
    working = paper.working_order
    exited = any(fill.reason.is_exit for fill in last.fills)

    # -- strategy action ---------------------------------------------------
    action: StrategyAction
    if exited:
        action = "EXIT"
    elif position is not None:
        action = "HOLD"
    elif last.signal is not None and last.skipped_because is None:
        action = "BUY" if last.signal.direction.is_long else "SELL"
    else:
        action = "WAIT"
    strategy = StrategyView(
        action=action,
        reason_code=last.strategy_reason.value,
        reason=_STRATEGY_TEXT[last.strategy_reason],
    )

    # -- risk ----------------------------------------------------------------
    risk = RiskView(status="NOT_EVALUATED")
    if last.risk is not None:
        risk = RiskView(
            status="APPROVED" if last.risk.approved else "REJECTED",
            quantity=last.risk.quantity if last.risk.approved else None,
            limited_by=last.risk.limited_by.value if last.risk.limited_by else None,
            reasons=[
                RiskReasonView(code=r.code.value, detail=r.detail) for r in last.risk.rejections
            ],
        )
        for r in last.risk.rejections:
            blocked.append(f"Risk rejected ({r.code.value}): {r.detail}")

    # -- AI ------------------------------------------------------------------
    ai = AiView(status="NOT_CONFIGURED" if session.analyst is None else "NOT_CONSULTED")
    entry = last.entry
    if session.analyst is not None and entry is not None and entry.decided_by != "risk":
        verdict = entry.verdict
        ai = AiView(
            status=verdict.decision.value if verdict is not None else "INVALID_RESPONSE",
            reason=entry.reason,
            model_id=session.analyst.model_id,
            prompt_version=session.analyst.prompt_version,
        )
        if not entry.take:
            blocked.append(f"AI filter did not approve: {entry.reason}")

    if last.skipped_because is not None:
        blocked.append("A new signal was ignored because a position or order is already open")
    if last.order is not None and last.order.reasons:
        blocked.append(f"Paper execution rejected the order: {', '.join(last.order.reasons)}")

    # -- plan --------------------------------------------------------------
    plan_signal: Signal | None
    entry_price: Decimal | None
    basis: Literal["fill", "signal_bar_close"] | None
    quantity: int | None
    if position is not None:
        plan_signal, entry_price, basis = paper.held_signal, position.entry.price, "fill"
        quantity = position.quantity
    elif working is not None:
        plan_signal, entry_price, basis = working.signal, last.bar.close, "signal_bar_close"
        quantity = working.quantity
    elif last.signal is not None and last.skipped_because is None:
        plan_signal, entry_price, basis = last.signal, last.bar.close, "signal_bar_close"
        quantity = risk.quantity
    else:
        plan_signal, entry_price, basis, quantity = None, None, None, None
    plan = PlanView()
    if plan_signal is not None and entry_price is not None:
        plan = PlanView(
            direction="LONG" if plan_signal.direction.is_long else "SHORT",
            entry=str(entry_price),
            entry_basis=basis,
            stop_loss=str(plan_signal.stop_price),
            target=_money(target_price(plan_signal, entry_price)),
            reward_to_risk=str(plan_signal.target_r_multiple),
            quantity=quantity,
        )

    # -- position ----------------------------------------------------------
    realized = sum((t.net_pnl for t in paper.trades), Decimal(0))
    mark = last.bar.close
    if position is None:
        position_view = PositionView(
            side="FLAT",
            realized_pnl=_money(realized),
            closed_trades=len(paper.trades),
            mark_price=str(mark),
        )
    else:
        move = (
            mark - position.entry.price
            if position.direction.is_long
            else position.entry.price - mark
        )
        position_view = PositionView(
            side="LONG" if position.direction.is_long else "SHORT",
            quantity=position.quantity,
            entry_price=str(position.entry.price),
            mark_price=str(mark),
            unrealized_pnl=_money(move * position.quantity),
            realized_pnl=_money(realized),
            closed_trades=len(paper.trades),
        )

    # -- trade status ------------------------------------------------------
    status: TradeStatus
    if position is not None:
        status = "IN_POSITION"
    elif exited:
        status = "EXITED"
    elif working is not None:
        status = "ORDER_WORKING"
    elif last.signal is not None:
        status = "BLOCKED"
    else:
        status = "NO_SIGNAL"

    return PaperDashboard(
        trading_mode=trading_mode.value,
        is_paper=is_paper,
        generated_at=now.isoformat(),
        session_running=True,
        instrument=_instrument(session),
        session=session_view,
        data=data,
        strategy=strategy,
        risk=risk,
        ai=ai,
        plan=plan,
        position=position_view,
        trade_status=status,
        blocked_reasons=blocked,
    )


def _instrument(session: PaperSession | None) -> InstrumentView | None:
    if session is None:
        return None
    inst = session.instrument
    return InstrumentView(symbol=inst.key, instrument_token=inst.instrument_token)
