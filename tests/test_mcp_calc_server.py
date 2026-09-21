"""Tests for the calc/stats MCP server tools (mcp_calc_server.py)."""

from __future__ import annotations

from sqwakvox.mcp_calc_server import (
    calculator,
    stats_2d,
    stats_mean,
    stats_median,
    stats_minmax,
    stats_percentile,
    stats_quartiles,
    stats_stddev,
    stats_summary,
    stats_variance,
)


class TestCalculator:
    def test_basic_arithmetic(self) -> None:
        assert calculator("2 + 3") == "5"
        assert calculator("10 - 4") == "6"
        assert calculator("6 * 7") == "42"
        assert calculator("20 / 4") == "5"

    def test_sqrt_and_pow(self) -> None:
        assert calculator("sqrt(16)") == "4"
        assert calculator("pow(2, 3)") == "8"

    def test_empty_expression(self) -> None:
        assert "Error" in calculator("")


class TestStatsSummary:
    def test_basic_summary(self) -> None:
        result = stats_summary("1, 2, 3, 4, 5")
        assert "Count: 5" in result
        assert "Mean: 3" in result
        assert "Median: 3" in result
        assert "Min: 1" in result
        assert "Max: 5" in result
        assert "Variance" in result
        assert "Std Dev" in result
        assert "Q1" in result
        assert "Q3" in result
        assert "IQR" in result

    def test_ddof_parameter(self) -> None:
        result_pop = stats_summary("1, 2, 3, 4, 5", ddof=0)
        result_sample = stats_summary("1, 2, 3, 4, 5", ddof=1)
        assert "Variance (ddof=0)" in result_pop
        assert "Variance (ddof=1)" in result_sample
        assert "Std Dev (ddof=0)" in result_pop
        assert "Std Dev (ddof=1)" in result_sample
        assert result_pop != result_sample

    def test_empty_input(self) -> None:
        assert "Error" in stats_summary("")

    def test_mode_detection(self) -> None:
        result = stats_summary("1, 2, 2, 3, 3, 3")
        assert "Mode(s): 3" in result


class TestStatsMean:
    def test_mean(self) -> None:
        assert stats_mean("10, 20, 30") == "20"

    def test_empty(self) -> None:
        assert "Error" in stats_mean("")


class TestStatsMedian:
    def test_median_odd(self) -> None:
        assert stats_median("1, 2, 3") == "2"

    def test_median_even(self) -> None:
        result = stats_median("1, 2, 3, 4")
        assert "2.5" in result

    def test_empty(self) -> None:
        assert "Error" in stats_median("")


class TestStatsStddev:
    def test_population_stddev(self) -> None:
        result = stats_stddev("2, 4, 4, 4, 5, 5, 7, 9")
        assert "2" in result

    def test_sample_stddev(self) -> None:
        result_ddof1 = stats_stddev("2, 4, 4, 4, 5, 5, 7, 9", ddof=1)
        result_ddof0 = stats_stddev("2, 4, 4, 4, 5, 5, 7, 9", ddof=0)
        assert float(result_ddof1) > float(result_ddof0)

    def test_empty(self) -> None:
        assert "Error" in stats_stddev("")


class TestStatsVariance:
    def test_population_variance(self) -> None:
        result = stats_variance("2, 4, 4, 4, 5, 5, 7, 9")
        assert "4" in result

    def test_sample_variance(self) -> None:
        result_ddof1 = stats_variance("1, 2, 3, 4, 5", ddof=1)
        result_ddof0 = stats_variance("1, 2, 3, 4, 5", ddof=0)
        assert float(result_ddof1) > float(result_ddof0)

    def test_empty(self) -> None:
        assert "Error" in stats_variance("")


class TestStatsMinmax:
    def test_minmax(self) -> None:
        result = stats_minmax("5, 1, 9, 3")
        assert "Min: 1" in result
        assert "Max: 9" in result

    def test_empty(self) -> None:
        assert "Error" in stats_minmax("")


class TestStatsPercentile:
    def test_median_percentile(self) -> None:
        result = stats_percentile("1, 2, 3, 4, 5", q=50)
        assert "3" in result

    def test_custom_percentile(self) -> None:
        result = stats_percentile("1, 2, 3, 4, 5", q=25)
        assert "2" in result or "2.5" in result

    def test_invalid_q(self) -> None:
        assert "Error" in stats_percentile("1, 2, 3", q=150)

    def test_empty(self) -> None:
        assert "Error" in stats_percentile("")


class TestStatsQuartiles:
    def test_quartiles(self) -> None:
        result = stats_quartiles("1, 2, 3, 4, 5, 6, 7, 8")
        assert "Q1" in result
        assert "Q2" in result
        assert "Q3" in result
        assert "IQR" in result

    def test_empty(self) -> None:
        assert "Error" in stats_quartiles("")


class TestStats2D:
    def test_column_axis(self) -> None:
        matrix = "1, 10\n2, 20\n3, 30"
        result = stats_2d(matrix, axis=0)
        assert "Column-wise" in result
        assert "1" in result
        assert "2" in result
        assert "3" in result

    def test_row_axis(self) -> None:
        matrix = "1, 10\n2, 20\n3, 30"
        result = stats_2d(matrix, axis=1)
        assert "Row-wise" in result

    def test_ddof_parameter(self) -> None:
        matrix = "1, 2\n3, 4"
        result = stats_2d(matrix, axis=0, ddof=1)
        assert "ddof=1" in result

    def test_invalid_axis(self) -> None:
        matrix = "1, 2\n3, 4"
        result = stats_2d(matrix, axis=2)
        assert "Error" in result

    def test_empty_matrix(self) -> None:
        assert "Error" in stats_2d("")


class TestIntegration:
    def test_summary_consistency(self) -> None:
        numbers = "10, 20, 30, 40, 50"
        summary = stats_summary(numbers)
        mean = stats_mean(numbers)
        median = stats_median(numbers)
        assert "Mean: 30" in summary
        assert mean == "30"
        assert "Median: 30" in summary
        assert median == "30"

    def test_percentile_within_range(self) -> None:
        result = stats_percentile("1, 50, 100", q=0)
        assert "1" in result
        result = stats_percentile("1, 50, 100", q=100)
        assert "100" in result
