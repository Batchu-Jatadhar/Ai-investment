"""Domain layer.

One package per bounded context from architecture v0.3 section 3.3.  These
packages are intentionally empty in Phase 0: no trading behaviour exists yet.
Each package documents what it will own and which phase introduces it.

Dependency rule (enforced by review, and by an import check once modules
exist): strategy and exits must never import broker, orders or
ai -- that restriction is what keeps them pure and backtestable.
"""
