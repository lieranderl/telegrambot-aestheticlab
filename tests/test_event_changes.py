import unittest
from datetime import UTC, datetime

from src.services.event_changes import (
    event_end,
    event_start,
    group_by_series,
    has_details,
    is_past,
    series_id,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class SeriesIdTests(unittest.TestCase):
    def test_prefers_recurring_event_id(self) -> None:
        self.assertEqual(series_id({"id": "x", "recurringEventId": "series"}), "series")

    def test_falls_back_to_instance_id_suffix(self) -> None:
        self.assertEqual(series_id({"id": "abc_20261105T143000Z"}), "abc")
        self.assertEqual(series_id({"id": "abc_20261105"}), "abc")

    def test_returns_none_for_single_events(self) -> None:
        self.assertIsNone(series_id({"id": "plain"}))
        self.assertIsNone(series_id({"id": "abc_notastamp"}))


class EventTimingTests(unittest.TestCase):
    def test_end_uses_date_time(self) -> None:
        event = {"end": {"dateTime": "2026-11-01T14:00:00+01:00"}}

        self.assertEqual(event_end(event), datetime(2026, 11, 1, 13, 0, tzinfo=UTC))

    def test_naive_date_time_is_treated_as_utc(self) -> None:
        event = {"end": {"dateTime": "2026-11-01T14:00:00"}}

        self.assertEqual(event_end(event), datetime(2026, 11, 1, 14, 0, tzinfo=UTC))

    def test_all_day_end_is_exclusive(self) -> None:
        event = {"start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}}

        self.assertFalse(is_past(event, NOW))
        self.assertTrue(is_past(event, datetime(2026, 11, 2, tzinfo=UTC)))

    def test_falls_back_to_original_start_then_instance_id(self) -> None:
        self.assertEqual(
            event_end({"originalStartTime": {"dateTime": "2026-10-01T10:00:00Z"}}),
            datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        )
        self.assertEqual(
            event_start({"id": "s_20261005T090000Z"}),
            datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
        )
        self.assertEqual(
            event_start({"id": "s_20261005"}), datetime(2026, 10, 5, tzinfo=UTC)
        )

    def test_invalid_or_unknown_times_are_not_past(self) -> None:
        self.assertIsNone(event_end({"end": {"dateTime": "nope"}}))
        self.assertIsNone(event_start({"id": "s_20261399T999999Z"}))
        self.assertIsNone(event_end({"end": "2026-01-01"}))
        self.assertIsNone(event_end({"end": {}}))
        self.assertFalse(is_past({"id": "plain"}, NOW))

    def test_past_event_is_detected(self) -> None:
        self.assertTrue(is_past({"end": {"dateTime": "2026-10-31T10:00:00Z"}}, NOW))

    def test_has_details(self) -> None:
        self.assertFalse(has_details({"id": "x", "status": "cancelled"}))
        self.assertTrue(has_details({"summary": "Nails"}))
        self.assertTrue(has_details({"start": {"date": "2026-11-01"}}))


class GroupBySeriesTests(unittest.TestCase):
    def test_groups_instances_and_sorts_by_start(self) -> None:
        single = {"id": "single"}
        late = {"id": "s_20261110T090000Z"}
        early = {"id": "s_20261103T090000Z"}
        unknown = {"id": "u", "recurringEventId": "s"}

        groups = group_by_series([late, single, early, unknown])

        self.assertEqual(groups, [[early, late, unknown], [single]])

    def test_cancelled_occurrences_form_their_own_group(self) -> None:
        changed = {"id": "s_20261103T090000Z"}
        cancelled = {"id": "s_20261110T090000Z", "status": "cancelled"}
        changed_later = {"id": "s_20261117T090000Z"}

        groups = group_by_series([changed, cancelled, changed_later])

        self.assertEqual(groups, [[changed, changed_later], [cancelled]])
