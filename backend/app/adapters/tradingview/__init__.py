"""TradingView integration - inbound alert webhooks only.

Receives alerts and normalises them into the domain's vendor-neutral
:class:`~app.domain.alerts.ExternalAlert`. It places no orders, supplies no
market data and generates no quantity, price, stop or target.
"""
