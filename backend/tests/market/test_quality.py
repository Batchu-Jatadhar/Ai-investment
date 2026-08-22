"""Tick data quality: nothing is discarded silently."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.domain.market.models import MarketTick, TickMode
from app.domain.market.quality import DataQualityIssue, TickValidator
from tests.market.conftest import (
    BASE_TIME,
    RELIANCE_TOKEN,
    UNKNOWN_TOKEN,
    make_tick,
)


def validator(resolver) -> TickValidator:  # noqa: ANN001
    return TickValidator(resolver, provider="test", stale_after=timedelta(seconds=30))


class TestAcceptance:
    def test_a_good_tick_is_accepted(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(make_tick(), now=BASE_TIME)
        assert result.accepted
        assert result.issues == ()

    def test_counters_track_acceptance(self, resolver) -> None:  # noqa: ANN001
        v = validator(resolver)
        v.validate(make_tick(), now=BASE_TIME)
        v.validate(make_tick(token=UNKNOWN_TOKEN), now=BASE_TIME)
        assert v.counters.received == 2
        assert v.counters.accepted == 1
        assert v.counters.rejected == 1


class TestUnknownInstrument:
    def test_unknown_token_is_rejected_and_recorded(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(
            make_tick(token=UNKNOWN_TOKEN, symbol=None), now=BASE_TIME
        )
        assert not result.accepted
        assert DataQualityIssue.UNKNOWN_INSTRUMENT in result.issue_kinds
        assert result.issues[0].detail["token"] == UNKNOWN_TOKEN


class TestInvalidValues:
    def test_zero_price_is_rejected(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(make_tick(price="0"), now=BASE_TIME)
        assert not result.accepted
        assert DataQualityIssue.INVALID_PRICE in result.issue_kinds

    def test_negative_price_is_rejected(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(make_tick(price="-5"), now=BASE_TIME)
        assert not result.accepted
        assert DataQualityIssue.INVALID_PRICE in result.issue_kinds

    def test_absurd_price_is_rejected(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(make_tick(price="999999999999"), now=BASE_TIME)
        assert not result.accepted
        assert DataQualityIssue.INVALID_PRICE in result.issue_kinds

    def test_negative_quantity_is_rejected(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(make_tick(volume=-1), now=BASE_TIME)
        assert not result.accepted
        assert DataQualityIssue.INVALID_QUANTITY in result.issue_kinds
        assert result.issues[0].detail["field"] == "volume"


class TestTimestamps:
    def test_missing_exchange_timestamp_in_full_mode_is_flagged_but_accepted(
        self, resolver
    ) -> None:  # noqa: ANN001
        tick = MarketTick(
            instrument_token=RELIANCE_TOKEN,
            last_price=Decimal("1400"),
            mode=TickMode.FULL,
            received_at=BASE_TIME,
            exchange_timestamp=None,
        )
        result = validator(resolver).validate(tick, now=BASE_TIME)
        assert result.accepted
        assert DataQualityIssue.MISSING_TIMESTAMP in result.issue_kinds
        assert result.tick is not None
        assert result.tick.event_time == BASE_TIME

    def test_ltp_mode_without_a_timestamp_is_not_an_anomaly(self, resolver) -> None:  # noqa: ANN001
        tick = MarketTick(
            instrument_token=RELIANCE_TOKEN,
            last_price=Decimal("1400"),
            mode=TickMode.LTP,
            received_at=BASE_TIME,
        )
        result = validator(resolver).validate(tick, now=BASE_TIME)
        assert result.accepted
        assert DataQualityIssue.MISSING_TIMESTAMP not in result.issue_kinds


class TestDuplicates:
    def test_identical_repeated_tick_is_rejected(self, resolver) -> None:  # noqa: ANN001
        v = validator(resolver)
        tick = make_tick()
        assert v.validate(tick, now=BASE_TIME).accepted
        second = v.validate(tick, now=BASE_TIME)
        assert not second.accepted
        assert DataQualityIssue.DUPLICATE_TICK in second.issue_kinds

    def test_same_timestamp_but_different_price_is_not_a_duplicate(self, resolver) -> None:  # noqa: ANN001
        v = validator(resolver)
        v.validate(make_tick(price="1400"), now=BASE_TIME)
        assert v.validate(make_tick(price="1401"), now=BASE_TIME).accepted


class TestOrdering:
    def test_out_of_order_tick_is_rejected(self, resolver) -> None:  # noqa: ANN001
        v = validator(resolver)
        later = BASE_TIME + timedelta(seconds=10)
        v.validate(make_tick(at=later), now=later)
        result = v.validate(make_tick(at=BASE_TIME, price="1399"), now=later)
        assert not result.accepted
        assert DataQualityIssue.OUT_OF_ORDER in result.issue_kinds

    def test_ordering_is_tracked_per_instrument(self, resolver, instruments) -> None:  # noqa: ANN001
        v = validator(resolver)
        later = BASE_TIME + timedelta(seconds=10)
        v.validate(make_tick(at=later), now=later)
        other = instruments[1]
        result = v.validate(
            make_tick(token=other.instrument_token, at=BASE_TIME, symbol="INFY"),
            now=later,
        )
        assert result.accepted

    def test_reset_clears_ordering_history(self, resolver) -> None:  # noqa: ANN001
        v = validator(resolver)
        later = BASE_TIME + timedelta(seconds=10)
        v.validate(make_tick(at=later), now=later)
        v.reset()
        assert v.validate(make_tick(at=BASE_TIME), now=later).accepted


class TestStaleness:
    def test_old_exchange_timestamp_is_flagged_but_kept(self, resolver) -> None:  # noqa: ANN001
        now = BASE_TIME + timedelta(minutes=5)
        result = validator(resolver).validate(make_tick(at=BASE_TIME), now=now)
        assert result.accepted
        assert DataQualityIssue.STALE_TICK in result.issue_kinds
        assert result.issues[0].detail["age_seconds"] == 300.0

    def test_a_fresh_tick_is_not_stale(self, resolver) -> None:  # noqa: ANN001
        result = validator(resolver).validate(
            make_tick(at=BASE_TIME), now=BASE_TIME + timedelta(seconds=2)
        )
        assert DataQualityIssue.STALE_TICK not in result.issue_kinds


class TestVolumeRegression:
    def test_falling_cumulative_volume_is_flagged(self, resolver) -> None:  # noqa: ANN001
        v = validator(resolver)
        v.validate(make_tick(at=BASE_TIME, volume=5000), now=BASE_TIME)
        later = BASE_TIME + timedelta(seconds=1)
        result = v.validate(make_tick(at=later, volume=4000), now=later)
        assert result.accepted  # flag only - the price is still usable
        assert DataQualityIssue.VOLUME_REGRESSION in result.issue_kinds


class TestIssueClassification:
    def test_rejecting_issues_are_marked_as_such(self) -> None:
        assert DataQualityIssue.DUPLICATE_TICK.rejects_tick is True
        assert DataQualityIssue.OUT_OF_ORDER.rejects_tick is True
        assert DataQualityIssue.INVALID_PRICE.rejects_tick is True
        assert DataQualityIssue.STALE_TICK.rejects_tick is False
        assert DataQualityIssue.MISSING_TIMESTAMP.rejects_tick is False
