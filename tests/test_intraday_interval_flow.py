import unittest

import pandas as pd

from data.intraday_flow import aggregate_groups, analyze_interval, analyze_many


class IntradayIntervalFlowTest(unittest.TestCase):
    def test_analyzes_cumulative_tencent_amount_and_follow_through(self):
        frame = pd.DataFrame(
            [
                {"time": "1346", "price": 9.80, "volume": 900, "amount": 900_000},
                {"time": "1347", "price": 10.00, "volume": 1000, "amount": 1_000_000},
                {"time": "1400", "price": 10.50, "volume": 1300, "amount": 1_300_000},
                {"time": "1415", "price": 11.00, "volume": 1600, "amount": 1_600_000},
                {"time": "1500", "price": 10.50, "volume": 2200, "amount": 2_200_000},
            ]
        )

        frame.attrs.update({"trading_date": "20260717", "source": "tencent_ifzq"})

        result = analyze_interval(
            frame,
            "13:47",
            "14:15",
            follow_end="15:00",
            amount_mode="cumulative",
        )

        self.assertEqual(result["trading_date"], "20260717")
        self.assertEqual(result["source"], "tencent_ifzq")
        self.assertEqual(result["start_time"], "1347")
        self.assertEqual(result["end_time"], "1415")
        self.assertEqual(result["follow_end_time"], "1500")
        self.assertEqual(result["amount_mode"], "cumulative")
        self.assertAlmostEqual(result["price_change_pct"], 10.0)
        self.assertAlmostEqual(result["turnover_amount"], 600_000)
        self.assertAlmostEqual(result["observed_turnover_amount"], 2_200_000)
        self.assertAlmostEqual(result["turnover_observed_pct"], 600_000 / 2_200_000 * 100)
        self.assertAlmostEqual(result["follow_change_pct"], -4.5454545)
        self.assertAlmostEqual(result["net_change_pct"], 5.0)
        self.assertAlmostEqual(result["retention_ratio"], 0.5)

    def test_sums_incremental_amount_inside_interval(self):
        frame = pd.DataFrame(
            [
                {"time": "1347", "price": 10.00, "volume": 10, "amount": 100_000},
                {"time": "1400", "price": 10.20, "volume": 20, "amount": 200_000},
                {"time": "1415", "price": 10.40, "volume": 30, "amount": 300_000},
                {"time": "1500", "price": 10.30, "volume": 40, "amount": 400_000},
            ]
        )

        result = analyze_interval(
            frame,
            "1347",
            "1415",
            follow_end="1500",
            amount_mode="incremental",
        )

        self.assertEqual(result["amount_mode"], "incremental")
        self.assertEqual(result["turnover_amount"], 500_000)
        self.assertEqual(result["observed_turnover_amount"], 1_000_000)

    def test_uses_first_point_after_start_and_last_point_before_end(self):
        frame = pd.DataFrame(
            [
                {"time": "1348", "price": 10.00, "amount": 1_000_000},
                {"time": "1414", "price": 10.50, "amount": 1_400_000},
                {"time": "1459", "price": 10.40, "amount": 1_900_000},
            ]
        )

        result = analyze_interval(
            frame,
            "1347",
            "1415",
            follow_end="1500",
            amount_mode="cumulative",
        )

        self.assertEqual(result["start_time"], "1348")
        self.assertEqual(result["end_time"], "1414")
        self.assertEqual(result["follow_end_time"], "1459")

    def test_rejects_window_without_points(self):
        frame = pd.DataFrame([{"time": "0930", "price": 10.0, "amount": 1000}])

        with self.assertRaisesRegex(ValueError, "区间内没有有效分钟点"):
            analyze_interval(frame, "1347", "1415", amount_mode="cumulative")

    def test_rejects_decreasing_cumulative_amount_counter(self):
        frame = pd.DataFrame(
            [
                {"time": "1347", "price": 10.0, "amount": 1_000_000},
                {"time": "1400", "price": 10.2, "amount": 900_000},
                {"time": "1415", "price": 10.4, "amount": 1_100_000},
            ]
        )

        with self.assertRaisesRegex(ValueError, "累计成交额出现下降"):
            analyze_interval(frame, "1347", "1415", amount_mode="cumulative")

    def test_analyze_many_ranks_by_turnover_and_reports_failures(self):
        frames = {
            "000001": pd.DataFrame(
                [
                    {"time": "1347", "price": 10.0, "amount": 1_000_000},
                    {"time": "1415", "price": 10.5, "amount": 1_800_000},
                ]
            ),
            "000002": pd.DataFrame(
                [
                    {"time": "1347", "price": 20.0, "amount": 2_000_000},
                    {"time": "1415", "price": 20.4, "amount": 2_300_000},
                ]
            ),
            "000003": pd.DataFrame(),
        }

        records, errors = analyze_many(
            [
                {"code": "000002", "name": "乙"},
                {"code": "000001", "name": "甲", "group": "科技"},
                {"code": "000003", "name": "丙"},
            ],
            frames.__getitem__,
            "1347",
            "1415",
            amount_mode="cumulative",
            max_workers=2,
        )

        self.assertEqual([row["code"] for row in records], ["000001", "000002"])
        self.assertEqual(records[0]["name"], "甲")
        self.assertEqual(records[0]["group"], "科技")
        self.assertEqual(errors[0]["code"], "000003")

    def test_analyze_many_retries_transient_fetch_failure(self):
        frame = pd.DataFrame(
            [
                {"time": "1347", "price": 10.0, "amount": 1_000_000},
                {"time": "1415", "price": 10.5, "amount": 1_800_000},
            ]
        )
        attempts = 0

        def flaky_fetch(_code):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("temporary")
            return frame

        records, errors = analyze_many(
            [{"code": "000001", "name": "甲"}],
            flaky_fetch,
            "1347",
            "1415",
            amount_mode="cumulative",
            retries=2,
        )

        self.assertEqual(attempts, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(errors, [])

    def test_analyze_many_does_not_retry_deterministic_frame_error(self):
        attempts = 0

        def invalid_fetch(_code):
            nonlocal attempts
            attempts += 1
            return pd.DataFrame([{"time": "1347", "price": 10.0}])

        records, errors = analyze_many(
            [{"code": "000001", "name": "甲"}],
            invalid_fetch,
            "1347",
            "1415",
            amount_mode="cumulative",
            retries=3,
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(records, [])
        self.assertIn("缺少字段", errors[0]["error"])

    def test_aggregates_groups_using_interval_turnover_weights(self):
        records = [
            {
                "code": "000001",
                "turnover_amount": 100.0,
                "price_change_pct": 10.0,
                "follow_change_pct": -4.0,
                "net_change_pct": 5.6,
                "retention_ratio": 0.56,
            },
            {
                "code": "000002",
                "turnover_amount": 300.0,
                "price_change_pct": 2.0,
                "follow_change_pct": -1.0,
                "net_change_pct": 0.98,
                "retention_ratio": 0.49,
            },
        ]

        result = aggregate_groups(records, {"科技": ["000001", "000002"]})[0]

        self.assertEqual(result["group"], "科技")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["turnover_amount"], 400.0)
        self.assertAlmostEqual(result["weighted_price_change_pct"], 4.0)
        self.assertAlmostEqual(result["weighted_follow_change_pct"], -1.75)
        self.assertAlmostEqual(result["weighted_retention_ratio"], 0.5075)


if __name__ == "__main__":
    unittest.main()
