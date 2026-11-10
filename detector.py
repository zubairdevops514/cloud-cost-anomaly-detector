"""Explainable daily cloud-cost anomaly detection using a rolling median and MAD."""
import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import html
import json
import math
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class Cost:
    day: date
    service: str
    usd: Decimal


def load_costs(path):
    grouped = defaultdict(Decimal)
    with Path(path).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        required = {'date', 'service', 'cost_usd'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('CSV needs date,service,cost_usd columns')
        for row_number, row in enumerate(reader, 2):
            try:
                day = date.fromisoformat(row['date'])
                service = row['service'].strip()
                usd = Decimal(row['cost_usd'])
                if not service or not usd.is_finite() or usd < 0:
                    raise ValueError('service must be nonempty; cost must be finite and nonnegative')
            except (ValueError, InvalidOperation, TypeError, AttributeError) as exc:
                raise ValueError(f'Invalid CSV row {row_number}: {exc}') from exc
            grouped[(day, service)] += usd
    if not grouped:
        raise ValueError('CSV has no cost rows')
    return [Cost(day, service, usd) for (day, service), usd in sorted(grouped.items())]


def detect(costs, window=7, min_history=7, z_threshold=3.5, min_increase=Decimal('10')):
    if window < 1 or not 1 <= min_history <= window:
        raise ValueError('Require 1 <= min_history <= window')
    try:
        min_increase = Decimal(str(min_increase))
    except InvalidOperation as exc:
        raise ValueError('min-increase must be a valid decimal amount') from exc
    if (not math.isfinite(z_threshold) or z_threshold <= 0 or
            not min_increase.is_finite() or min_increase <= 0):
        raise ValueError('Thresholds must be positive and finite')
    services = defaultdict(list)
    seen = set()
    for cost in costs:
        identity = (cost.service, cost.day)
        if identity in seen:
            raise ValueError('Duplicate service/day; aggregate input first with load_costs')
        if not cost.usd.is_finite() or cost.usd < 0:
            raise ValueError('Costs must be nonnegative and finite')
        seen.add(identity)
        services[cost.service].append(cost)
    results = []
    for service, rows in sorted(services.items()):
        history = []
        for row in sorted(rows, key=lambda item: item.day):
            # Calendar window, not the last N observed records. Missing days are not zero.
            history = [old for old in history if old.day >= row.day - timedelta(days=window)]
            values = [old.usd for old in history]
            result = {'date': row.day.isoformat(), 'service': service,
                      'cost_usd': str(row.usd), 'history_days': len(values),
                      'status': 'insufficient_history', 'baseline_usd': None,
                      'increase_usd': None, 'robust_z': None}
            if len(values) >= min_history:
                baseline = median(values)
                increase = row.usd - baseline
                mad = median([abs(value - baseline) for value in values])
                z = Decimal('0.6745') * increase / mad if mad else None
                anomaly = increase >= min_increase and (mad == 0 or z >= Decimal(str(z_threshold)))
                result.update(status='anomaly' if anomaly else 'normal',
                              baseline_usd=str(baseline), increase_usd=str(increase),
                              robust_z=round(float(z), 3) if z is not None else None)
            results.append(result)
            # All observed values enter later baselines, including alerts; document contamination risk.
            history.append(row)
    return sorted(results, key=lambda row: (row['date'], row['service']))


def render_report(results):
    counts = {key: sum(row['status'] == key for row in results)
              for key in ('anomaly', 'normal', 'insufficient_history')}
    cells = []
    for row in results:
        values = (row['date'], row['service'], row['cost_usd'], row['baseline_usd'],
                  row['increase_usd'], row['history_days'], row['status'])
        cells.append('<tr class="' + row['status'] + '">' + ''.join(
            '<td>' + html.escape(str(value) if value is not None else '—') + '</td>'
            for value in values) + '</tr>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cloud Cost Review</title><style>
body{font:16px system-ui,sans-serif;color:#182b42;background:#f3f6fa;margin:0;padding:40px}
main{max-width:1100px;margin:auto}h1{font-size:36px;margin-bottom:8px}p{line-height:1.6}
.cards{display:flex;gap:16px;flex-wrap:wrap;margin:28px 0}.card{background:white;padding:20px;border-radius:12px;min-width:150px}
.card strong{display:block;font-size:30px}table{width:100%;border-collapse:collapse;background:white}
th,td{padding:12px;text-align:left;border-bottom:1px solid #e1e7ef}th{background:#182b42;color:white}
.anomaly{background:#fff0e5}.insufficient_history{color:#53647a}.scroll{overflow:auto}
</style><main><p>FINOPS / EXPLAINABLE DETECTION</p><h1>Cloud Cost Review</h1>
<p>Rolling median baseline with median absolute deviation. Alerts require a minimum dollar increase.
Missing dates are not treated as zero spend. Review alerts before taking action.</p><div class="cards">''' + ''.join(
        f'<div class="card"><strong>{value}</strong>{html.escape(key.replace("_", " "))}</div>'
        for key, value in counts.items()) + '''</div><div class="scroll"><table>
<thead><tr><th>Date</th><th>Service</th><th>USD</th><th>Baseline</th><th>Increase</th><th>History days</th><th>Status</th></tr></thead>
<tbody>''' + ''.join(cells) + '</tbody></table></div></main></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--window', type=int, default=7)
    parser.add_argument('--min-history', type=int, default=7)
    parser.add_argument('--min-increase', default='10')
    parser.add_argument('--z-threshold', type=float, default=3.5)
    parser.add_argument('--json', type=Path)
    parser.add_argument('--html', type=Path)
    parser.add_argument('--fail-on-anomaly', action='store_true')
    args = parser.parse_args()
    try:
        results = detect(load_costs(args.csv), args.window, args.min_history,
                         args.z_threshold, args.min_increase)
        payload = {'method': 'rolling-median-mad', 'currency': 'USD',
                   'config': {'window_days': args.window, 'min_history': args.min_history,
                              'min_increase_usd': str(args.min_increase), 'z_threshold': args.z_threshold},
                   'results': results}
        text = json.dumps(payload, indent=2, allow_nan=False)
        if args.json:
            args.json.write_text(text + '\n', encoding='utf-8')
        if args.html:
            args.html.write_text(render_report(results), encoding='utf-8')
        print(text)
        if args.fail_on_anomaly and any(row['status'] == 'anomaly' for row in results):
            raise SystemExit(1)
    except (ValueError, OSError, InvalidOperation) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
