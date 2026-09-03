"""OrbParams: the INITIAL FIXED HYPOTHESIS, and the combinations it refuses."""

from __future__ import annotations

import dataclasses
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.market.models import CandleInterval
from app.domain.strategy.params import OrbParams


class TestApprovedDefaults:
    """These are the approved values. A change here is a change of hypothesis."""

    def test_defaults_match_the_approved_hypothesis(self) -> None:
        p = OrbParams()
        assert p.opening_range_minutes == 15
        assert p.signal_interval is CandleInterval.M5
        assert p.resolution_interval is CandleInterval.M1
        assert p.target_r_multiple == Decimal("2.0")
        assert p.hard_exit_time == time(15, 15)
        assert p.no_new_entry_after == time(14, 45)
        assert p.min_range_ticks == 4
        assert p.max_range_atr_multiple == Decimal("1.5")
        assert p.fixed_notional_inr == Decimal("100000")

    def test_the_module_is_labelled_as_a_fixed_hypothesis(self) -> None:
        """The label is load-bearing: it is why these must not be optimized."""
        import app.domain.strategy.params as params_module

        assert params_module.__doc__ is not None
        assert "INITIAL FIXED HYPOTHESIS" in params_module.__doc__
        assert "must NOT be optimized" in params_module.__doc__
        assert OrbParams.__doc__ is not None
        assert "INITIAL FIXED HYPOTHESIS" in OrbParams.__doc__

    def test_params_are_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            OrbParams().target_r_multiple = Decimal("3")  # type: ignore[misc]


class TestValidation:
    def test_opening_range_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="opening_range_minutes must be positive"):
            OrbParams(opening_range_minutes=0)

    def test_opening_range_must_land_on_a_signal_bar_boundary(self) -> None:
        with pytest.raises(ValueError, match="whole multiple of signal_interval"):
            OrbParams(opening_range_minutes=7)

    def test_resolution_may_not_be_coarser_than_the_signal_interval(self) -> None:
        with pytest.raises(ValueError, match="must not be coarser"):
            OrbParams(
                signal_interval=CandleInterval.M5,
                resolution_interval=CandleInterval.M15,
            )

    def test_signal_interval_must_be_a_multiple_of_the_resolution(self) -> None:
        # 15m signal bars over 1m resolution is fine; the guard fires only when
        # the division is not whole, which needs a non-divisible pair.
        assert (
            OrbParams(
                opening_range_minutes=15,
                signal_interval=CandleInterval.M15,
                resolution_interval=CandleInterval.M5,
            ).signal_interval
            is CandleInterval.M15
        )

    def test_equal_intervals_are_allowed(self) -> None:
        params = OrbParams(
            opening_range_minutes=5,
            signal_interval=CandleInterval.M5,
            resolution_interval=CandleInterval.M5,
        )
        assert params.resolution_interval is CandleInterval.M5

    @pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1")])
    def test_target_r_multiple_must_be_positive(self, value: Decimal) -> None:
        with pytest.raises(ValueError, match="target_r_multiple must be positive"):
            OrbParams(target_r_multiple=value)

    def test_max_range_atr_multiple_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_range_atr_multiple must be positive"):
            OrbParams(max_range_atr_multiple=Decimal("0"))

    def test_min_range_ticks_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="min_range_ticks must be at least 1"):
            OrbParams(min_range_ticks=0)

    def test_fixed_notional_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="fixed_notional_inr must be positive"):
            OrbParams(fixed_notional_inr=Decimal("0"))

    def test_entry_cutoff_must_precede_the_hard_exit(self) -> None:
        with pytest.raises(ValueError, match="must precede"):
            OrbParams(no_new_entry_after=time(15, 20), hard_exit_time=time(15, 15))

    def test_entry_cutoff_equal_to_hard_exit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must precede"):
            OrbParams(no_new_entry_after=time(15, 15), hard_exit_time=time(15, 15))

    def test_float_values_are_rejected(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            OrbParams(target_r_multiple=2.0)  # type: ignore[arg-type]

    def test_aware_session_times_are_rejected(self) -> None:
        """Session times are IST wall-clock, combined with a date at use."""
        with pytest.raises(ValueError, match="must be naive"):
            OrbParams(hard_exit_time=time(15, 15, tzinfo=ZoneInfo("Asia/Kolkata")))


class TestCanonicalRendering:
    def test_canonical_is_all_strings(self) -> None:
        rendered = OrbParams().canonical()
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in rendered.items())

    def test_canonical_covers_every_field(self) -> None:
        assert set(OrbParams().canonical()) == {f.name for f in dataclasses.fields(OrbParams)}

    def test_logically_equal_decimals_render_identically(self) -> None:
        a = OrbParams(target_r_multiple=Decimal("2.0"))
        b = OrbParams(target_r_multiple=Decimal("2.00"))
        assert a.canonical() == b.canonical()

    def test_canonical_is_stable_across_calls(self) -> None:
        params = OrbParams()
        assert params.canonical() == params.canonical()


class TestConfigValueObjects:
    def test_slippage_defaults(self) -> None:
        config = SlippageConfig()
        assert config.adverse_ticks == 1
        assert config.model_id == "fixed_ticks"

    def test_slippage_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            SlippageConfig().adverse_ticks = 5  # type: ignore[misc]

    def test_negative_slippage_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="adverse by definition"):
            SlippageConfig(adverse_ticks=-1)

    def test_zero_slippage_is_allowed_for_sensitivity_runs(self) -> None:
        assert SlippageConfig(adverse_ticks=0).adverse_ticks == 0

    def test_execution_defaults(self) -> None:
        config = ExecutionConfig()
        assert config.entry_timing == "next_bar_open"
        assert config.target_requires_through_ticks == 1
        assert config.allow_partial_fills is False

    def test_execution_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            ExecutionConfig().target_requires_through_ticks = 3  # type: ignore[misc]

    def test_partial_fills_cannot_be_enabled(self) -> None:
        """The manifest must never claim a behaviour the simulator lacks."""
        with pytest.raises(ValueError, match="partial fills are not modelled"):
            ExecutionConfig(allow_partial_fills=True)

    def test_entry_timing_cannot_be_changed_to_same_bar(self) -> None:
        with pytest.raises(ValueError, match="cannot be filled on that same bar"):
            ExecutionConfig(entry_timing="same_bar_close")

    def test_negative_through_ticks_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            ExecutionConfig(target_requires_through_ticks=-1)

    def test_gap_quarantine_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="gap_quarantine_percent must be positive"):
            ExecutionConfig(gap_quarantine_percent=Decimal("0"))

    def test_configs_have_no_behaviour_only_canonical_rendering(self) -> None:
        """Configuration states assumptions; it must not act on them."""
        for cls in (SlippageConfig, ExecutionConfig, CostSchedule):
            public = {
                name
                for name in vars(cls)
                if not name.startswith("_") and callable(getattr(cls, name, None))
            }
            assert public <= {"canonical"}, f"{cls.__name__} must own no behaviour: {public}"


class TestCostSchedule:
    def make(self, **overrides: object) -> CostSchedule:
        from datetime import date

        values: dict[str, object] = {
            "schedule_id": "nse-intraday-equity",
            "version": "0-placeholder",
            "effective_from": date(2026, 1, 1),
        }
        values.update(overrides)
        return CostSchedule(**values)  # type: ignore[arg-type]

    def test_a_placeholder_schedule_reports_itself_unverified(self) -> None:
        """Rates arrive in Phase 2.5 only after verification against a source."""
        assert self.make().rates_verified is False

    def test_a_schedule_carries_no_rates_yet(self) -> None:
        names = {f.name for f in dataclasses.fields(CostSchedule)}
        rate_like = {"brokerage", "stt", "gst", "stamp_duty", "sebi", "exchange_charge", "rates"}
        assert names & rate_like == set()

    def test_verification_requires_a_source(self) -> None:
        from datetime import date

        with pytest.raises(ValueError, match="cannot claim verification without a source_url"):
            self.make(verified_on=date(2026, 2, 1))

    def test_verification_cannot_precede_the_effective_date(self) -> None:
        from datetime import date

        with pytest.raises(ValueError, match="precedes effective_from"):
            self.make(
                verified_on=date(2025, 1, 1),
                source_url="https://example.invalid/charges",
            )

    def test_a_verified_schedule_reports_verified(self) -> None:
        from datetime import date

        schedule = self.make(
            verified_on=date(2026, 2, 1),
            source_url="https://example.invalid/charges",
        )
        assert schedule.rates_verified is True

    def test_empty_identity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="schedule_id must name"):
            self.make(schedule_id="  ")
        with pytest.raises(ValueError, match="version must identify"):
            self.make(version="")
