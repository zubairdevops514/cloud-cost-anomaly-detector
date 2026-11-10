from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from detector import Cost, detect, load_costs, render_report


def series(values, service='EC2'):
    return [Cost(date(2026, 1, 1) + timedelta(days=i), service, Decimal(str(value)))
            for i, value in enumerate(values)]


class DetectorTests(unittest.TestCase):
    def test_spike_after_constant_baseline(self):
        result = detect(series([40] * 7 + [125]))
        self.assertEqual(result[-1]['status'], 'anomaly')
        self.assertEqual(result[-1]['baseline_usd'], '40')
        self.assertIsNone(result[-1]['robust_z'])

    def test_small_change_and_decrease_not_alerted(self):
        for value in (41, 0):
            self.assertEqual(detect(series([40] * 7 + [value]))[-1]['status'], 'normal')

    def test_warmup_and_missing_days(self):
        rows = series([40] * 7)
        rows.append(Cost(date(2026, 2, 1), 'EC2', Decimal('125')))
        self.assertTrue(all(row['status'] == 'insufficient_history' for row in detect(rows)))

    def test_no_future_leakage(self):
        first = detect(series([39, 40, 41, 40, 39, 41, 40, 125]))
        extended = detect(series([39, 40, 41, 40, 39, 41, 40, 125, 9999]))
        self.assertEqual(first, extended[:len(first)])

    def test_services_are_independent(self):
        rows = series([40] * 7 + [125]) + series([5] * 8, 'S3')
        self.assertEqual(sum(row['status'] == 'anomaly' for row in detect(rows)), 1)

    def test_nonzero_mad_threshold(self):
        result = detect(series([39, 40, 41, 39, 40, 41, 40, 70]))[-1]
        self.assertEqual(result['status'], 'anomaly')
        self.assertGreater(result['robust_z'], 3.5)

    def test_csv_aggregates_duplicate_rows_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'x.csv')
            path.write_text('date,service,cost_usd\n2026-01-01,S3,0.1\n2026-01-01,S3,0.2\n')
            self.assertEqual(load_costs(path)[0].usd, Decimal('0.3'))

    def test_rejects_bad_costs(self):
        for value in ('NaN', 'Infinity', '-1', 'abc'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory, 'x.csv')
                path.write_text(f'date,service,cost_usd\n2026-01-01,S3,{value}\n')
                with self.assertRaises(ValueError):
                    load_costs(path)

    def test_report_escapes_input(self):
        report = render_report(detect(series([1], '<script>alert(1)</script>')))
        self.assertNotIn('<script>', report)
        self.assertIn('&lt;script&gt;', report)

    def test_invalid_configuration(self):
        for kwargs in ({'min_history': 8}, {'min_increase': 0}, {'z_threshold': float('nan')}):
            with self.assertRaises(ValueError):
                detect(series([1]), **kwargs)

    def test_real_sample_has_single_ec2_alert(self):
        rows = load_costs(Path(__file__).resolve().parents[1] / 'samples/costs.csv')
        alerts = [row for row in detect(rows) if row['status'] == 'anomaly']
        self.assertEqual([(row['date'], row['service']) for row in alerts], [('2026-10-08', 'Amazon EC2')])


if __name__ == '__main__':
    unittest.main()
