import math

import pytest

from siting.score.ramps import WeightedMean, ramp, weighted_mean_available


class TestRamp:
    def test_ascending_endpoints_and_midpoint(self):
        assert ramp(-5, 0, 10) == 0.0
        assert ramp(0, 0, 10) == 0.0
        assert ramp(5, 0, 10) == 50.0
        assert ramp(10, 0, 10) == 100.0
        assert ramp(99, 0, 10) == 100.0

    def test_descending_by_y(self):
        # Grid proximity default: 100 at <= 1 km, 0 at >= 30 km (Req 6.4).
        assert ramp(0.5, 1, 30, 100, 0) == 100.0
        assert ramp(1, 1, 30, 100, 0) == 100.0
        assert ramp(15.5, 1, 30, 100, 0) == pytest.approx(50.0)
        assert ramp(30, 1, 30, 100, 0) == 0.0
        assert ramp(45, 1, 30, 100, 0) == 0.0

    def test_wave_and_port_defaults(self):
        # Req 9.3: 100 at <= 1 m, 0 at >= 3 m.
        assert ramp(2.0, 1, 3, 100, 0) == pytest.approx(50.0)
        # Req 9.4: 100 at <= 10 km, 0 at >= 50 km.
        assert ramp(20, 10, 50, 100, 0) == pytest.approx(75.0)

    def test_reversed_x_is_same_line(self):
        assert ramp(7.5, 10, 0, 100, 0) == pytest.approx(ramp(7.5, 0, 10, 0, 100))
        assert ramp(20, 10, 0, 100, 0) == 100.0
        assert ramp(-1, 10, 0, 100, 0) == 0.0

    def test_degenerate_interval_is_step(self):
        assert ramp(4.9, 5, 5, 100, 0) == 100.0
        assert ramp(5, 5, 5, 100, 0) == 100.0  # x0 endpoint inclusive
        assert ramp(5.1, 5, 5, 100, 0) == 0.0

    def test_output_bounded(self):
        for x in [-1e9, 0.1, 0.3, 0.7, 1e9]:
            v = ramp(x, 0.1, 0.7, 20, 100)
            assert 20 <= v <= 100

    def test_nan_rejected(self):
        with pytest.raises(ValueError):
            ramp(math.nan, 0, 1)


class TestWeightedMeanAvailable:
    def test_all_available(self):
        r = weighted_mean_available({"a": 100, "b": 0}, {"a": 0.5, "b": 0.5})
        assert r == WeightedMean(50.0, [])
        value, omitted = r  # unpacks as tuple
        assert value == 50.0 and omitted == []

    def test_unequal_weights(self):
        r = weighted_mean_available({"a": 100, "b": 0}, {"a": 3, "b": 1})
        assert r.value == pytest.approx(75.0)

    def test_renormalizes_and_reports_omitted(self):
        # Req 6.6: remaining sub-score alone.
        r = weighted_mean_available(
            {"transmission": 80.0, "substation": None},
            {"transmission": 0.5, "substation": 0.5},
        )
        assert r.value == pytest.approx(80.0)
        assert r.omitted == ["substation"]

    def test_missing_and_nan_treated_as_unavailable(self):
        r = weighted_mean_available(
            {"depth": 60.0, "wave": math.nan},
            {"depth": 1, "slope": 1, "wave": 1, "port": 1},
        )
        assert r.value == pytest.approx(60.0)
        assert r.omitted == ["slope", "wave", "port"]

    def test_all_null_is_none(self):
        r = weighted_mean_available({"a": None, "b": None}, {"a": 1, "b": 1})
        assert r.value is None
        assert r.omitted == ["a", "b"]

    def test_zero_weight_sum_is_none(self):
        r = weighted_mean_available({"a": 50.0, "b": None}, {"a": 0, "b": 1})
        assert r.value is None
        assert r.omitted == ["b"]

    def test_extra_scores_ignored(self):
        r = weighted_mean_available({"a": 40.0, "zzz": 100.0}, {"a": 1})
        assert r == WeightedMean(40.0, [])

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError):
            weighted_mean_available({"a": 1.0}, {"a": -1})
