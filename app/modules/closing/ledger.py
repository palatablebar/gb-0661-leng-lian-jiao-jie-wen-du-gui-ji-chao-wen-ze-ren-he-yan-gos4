from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json

from app.config import LEDGER_TOLERANCE
from app.errors import RuleError
from app.modules.calculate.integral import excess, fmt
from app.modules.closing.calendar import LOCAL, business_day, day_window
from app.modules.model.intervals import interpolate

REASON_LABELS = {
    'handover_gap': '责任未覆盖',
    'late_handover': '交接迟到',
    'disputed': '责任争议',
    'missing_sample': '缺测未知',
}
POLICY_LABELS = {'verifiable': '可证实分钟发布', 'hold': '留旧账等补齐'}
POLICIES = tuple(POLICY_LABELS)
OWNER_FIELDS = (('coverage_minutes', 'coverage'), ('normal_minutes', 'normal'),
                ('excess_minutes', 'excess'), ('degree_minutes', 'degree'),
                ('unknown_minutes', 'unknown'))
DIFF_FIELDS = ('covered_minutes', 'normal_minutes', 'excess_minutes', 'degree_minutes',
               'unknown_minutes', 'uncovered_minutes')


def _dt(value):
    return datetime.fromisoformat(value)


def _minutes(start, end):
    return Decimal(str((end - start).total_seconds())) / Decimal(60)


def _clip(left, right, start, end):
    left = max(left, start)
    right = min(right, end)
    return (left, right) if right > left else None


def _local(value):
    return value.astimezone(LOCAL).isoformat()


def _stat():
    return {'coverage': Decimal(0), 'normal': Decimal(0), 'excess': Decimal(0),
            'degree': Decimal(0), 'unknown': Decimal(0)}


def references(bundle):
    refs = bundle.get('inputs')
    if not refs or 'handovers' not in refs or 'calibrations' not in refs:
        raise RuleError('NO_REFERENCES', '批次缺少交接与校准快照，请用当前版本重新运行后再关账', 409)
    return refs


def _handovers(bundle, cutoff):
    """拆成可采信与迟到交接。

    迟到与否按记录开始所在业务日的次日 10:00 判定（跨零点记录在同一条链上口径一致），
    迟到记录保留证据但不参与责任归属。
    """
    refs = references(bundle)
    accepted = defaultdict(list)
    late = defaultdict(list)
    for shipment, entries in refs['handovers'].items():
        for entry in entries:
            item = {'start': _dt(entry['start']), 'end': _dt(entry['end']),
                    'owner': entry['owner'], 'received_at': entry.get('received_at')}
            if item['received_at']:
                entry_cutoff = day_window(business_day(entry['start']))['cutoff'].astimezone(timezone.utc)
                item['cutoff'] = entry_cutoff
                bucket = late if _dt(item['received_at']) > entry_cutoff else accepted
            else:
                item['cutoff'] = cutoff
                bucket = accepted
            bucket[shipment].append(item)
    return accepted, late


def _pairs(rows, max_gap):
    rows = sorted(rows, key=lambda row: _dt(row['at']))
    return [{'left': left, 'right': right,
             'known': _minutes(_dt(left['at']), _dt(right['at'])) <= max_gap}
            for left, right in zip(rows, rows[1:])]


def _windows(entries, pairs, start, end):
    """本业务日计入账本的窗口。

    有交接时为交接区间包络（含包络内部的责任缺口）再并上采样覆盖；
    没有交接时退化为采样覆盖。交接之外的采样不会被丢弃，会以“责任未覆盖”单列。
    """
    handovers = [c for c in (_clip(e['start'], e['end'], start, end) for e in entries) if c]
    samples = [c for c in (_clip(_dt(p['left']['at']), _dt(p['right']['at']), start, end) for p in pairs) if c]
    intervals = list(samples)
    if handovers:
        intervals.append((min(c[0] for c in handovers), max(c[1] for c in handovers)))
    return _merge(intervals)


def _merge(intervals):
    merged = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _cuts(span, entries, pairs):
    cuts = {span[0], span[1]}
    for entry in entries:
        clipped = _clip(entry['start'], entry['end'], *span)
        if clipped:
            cuts.update(clipped)
    for pair in pairs:
        clipped = _clip(_dt(pair['left']['at']), _dt(pair['right']['at']), *span)
        if clipped:
            cuts.update(clipped)
    return sorted(cuts)


def build_day(bundle, day, policy='verifiable'):
    """按业务日切分证据：可证实分钟才进入责任账，未知与未覆盖单列且不折算给任何责任方。"""
    if policy not in POLICIES:
        raise RuleError('POLICY', '关账策略只能是 %s' % '/'.join(POLICIES))
    window = day_window(day)
    start = window['start'].astimezone(timezone.utc)
    end = window['end'].astimezone(timezone.utc)
    cutoff = window['cutoff'].astimezone(timezone.utc)
    max_gap = int(bundle['manifest']['parameters']['max_gap'])
    threshold = str(bundle['manifest']['parameters']['threshold'])

    accepted, late = _handovers(bundle, cutoff)
    samples = defaultdict(list)
    for row in bundle['model']['rows']:
        samples[row['shipment']].append(row)

    shipments = sorted(set(samples) | set(accepted) | set(late))
    batches, owner_rows = [], []
    uncovered_segments, unknown_segments, late_handovers = [], [], []
    totals = _stat()
    totals.update(span=Decimal(0), uncovered=Decimal(0))
    owner_excess_total = Decimal(0)

    for shipment in shipments:
        ship_samples = samples.get(shipment, [])
        ship_pairs = _pairs(ship_samples, max_gap)
        ship_accepted = accepted.get(shipment, [])
        ship_late = late.get(shipment, [])
        entries = ship_accepted + ship_late

        windows = _windows(entries, ship_pairs, start, end)
        for entry in ship_late:
            clipped = _clip(entry['start'], entry['end'], start, end)
            if not clipped:
                continue
            late_handovers.append({
                'shipment': shipment, 'owner': entry['owner'],
                'start': _local(clipped[0]), 'end': _local(clipped[1]),
                'minutes': fmt(_minutes(*clipped)),
                'received_at': _local(_dt(entry['received_at'])),
                'cutoff': entry['cutoff'].astimezone(LOCAL).isoformat(),
                'used_for_attribution': False,
            })
        if not windows:
            continue

        owner_stats = defaultdict(_stat)
        reasons = defaultdict(Decimal)
        batch = _stat()
        batch_unknown = Decimal(0)
        batch_uncovered = Decimal(0)
        for span in windows:
            cuts = _cuts(span, entries, ship_pairs)
            for lo, hi in zip(cuts, cuts[1:]):
                minutes = _minutes(lo, hi)
                if minutes <= 0:
                    continue
                totals['span'] += minutes
                mid = lo + (hi - lo) / 2
                holders = [entry for entry in ship_accepted if entry['start'] <= mid < entry['end']]
                late_holder = next((entry for entry in ship_late if entry['start'] <= mid < entry['end']), None)
                pair = next((p for p in ship_pairs
                             if _dt(p['left']['at']) <= lo and hi <= _dt(p['right']['at'])), None)

                if len(holders) > 1:
                    reason = 'disputed'
                elif not holders:
                    reason = 'late_handover' if late_holder else 'handover_gap'
                else:
                    reason = None

                if reason:
                    batch_uncovered += minutes
                    reasons[reason] += minutes
                    uncovered_segments.append({
                        'shipment': shipment, 'start': _local(lo), 'end': _local(hi),
                        'minutes': fmt(minutes), 'reason': reason,
                        'reason_label': REASON_LABELS[reason], 'owner': None,
                        'late_owner': late_holder['owner'] if late_holder else None,
                        'sources': [],
                    })
                    continue

                owner = holders[0]['owner']
                stat = owner_stats[owner]
                stat['coverage'] += minutes
                if pair and pair['known']:
                    left_value = interpolate(pair['left'], pair['right'], lo)
                    right_value = interpolate(pair['left'], pair['right'], hi)
                    over, area = excess(left_value, right_value, minutes, threshold)
                    stat['excess'] += over
                    stat['degree'] += area
                    stat['normal'] += minutes - over
                    batch['excess'] += over
                    batch['degree'] += area
                    batch['normal'] += minutes - over
                else:
                    stat['unknown'] += minutes
                    batch_unknown += minutes
                    unknown_segments.append({
                        'shipment': shipment, 'owner': owner,
                        'start': _local(lo), 'end': _local(hi),
                        'minutes': fmt(minutes), 'reason': 'missing_sample',
                        'reason_label': REASON_LABELS['missing_sample'], 'sources': [],
                    })

        batch_rows = []
        for owner, stat in sorted(owner_stats.items()):
            row = {'shipment': shipment, 'owner': owner}
            row.update({field: fmt(stat[key]) for field, key in OWNER_FIELDS})
            owner_rows.append(row)
            batch_rows.append({'owner': owner, **{field: row[field] for field in
                                                  (f for f, _ in OWNER_FIELDS)}})
            owner_excess_total += stat['excess']
            totals['coverage'] += stat['coverage']
            totals['normal'] += stat['normal']
            totals['excess'] += stat['excess']
            totals['degree'] += stat['degree']
            totals['unknown'] += stat['unknown']

        covered = batch['normal'] + batch['excess']
        batches.append({
            'shipment': shipment,
            'span_minutes': fmt(batch['normal'] + batch['excess'] + batch_unknown + batch_uncovered),
            'covered_minutes': fmt(covered),
            'normal_minutes': fmt(batch['normal']),
            'excess_minutes': fmt(batch['excess']),
            'degree_minutes': fmt(batch['degree']),
            'unknown_minutes': fmt(batch_unknown),
            'uncovered_minutes': fmt(batch_uncovered),
            'unprotected_minutes': fmt(batch_unknown + batch_uncovered),
            'uncovered_reasons': {key: fmt(value) for key, value in sorted(reasons.items()) if value},
            'owners': batch_rows,
            'samples': sum(1 for row in ship_samples if start <= _dt(row['at']) < end),
            'late_handovers': len(ship_late),
            'state': '待补测' if batch_unknown + batch_uncovered else ('超温' if batch['excess'] else '正常'),
        })
        totals['uncovered'] += batch_uncovered

    covered = totals['normal'] + totals['excess']
    unverified = totals['unknown'] + totals['uncovered']
    minute_delta = totals['span'] - (covered + unverified)
    owner_delta = totals['excess'] - owner_excess_total
    tolerance = Decimal(LEDGER_TOLERANCE)
    late_pending = sum((Decimal(s['minutes']) for s in uncovered_segments
                        if s['reason'] == 'late_handover'), Decimal(0))

    return {
        'day': window['day'],
        'window': {'start': window['start'].isoformat(), 'end': window['end'].isoformat(),
                   'cutoff': window['cutoff'].isoformat()},
        'policy': policy,
        'policy_label': POLICY_LABELS[policy],
        'rule_version': bundle['manifest']['rule_version'],
        'run_id': bundle['manifest']['run_id'],
        'input_digest': bundle['manifest']['sha256'],
        'parameters': bundle['manifest']['parameters'],
        'batches': batches,
        'owners': owner_rows,
        'totals': {
            'span_minutes': fmt(totals['span']),
            'covered_minutes': fmt(covered),
            'normal_minutes': fmt(totals['normal']),
            'excess_minutes': fmt(totals['excess']),
            'degree_minutes': fmt(totals['degree']),
            'unknown_minutes': fmt(totals['unknown']),
            'uncovered_minutes': fmt(totals['uncovered']),
            'unprotected_minutes': fmt(unverified),
            'batches': len(batches),
        },
        'uncovered_segments': uncovered_segments,
        'unknown_segments': unknown_segments,
        'late_handovers': late_handovers,
        'pending_minutes': fmt(late_pending),
        'state': 'EMPTY' if not batches else ('待补测' if unverified else '可归档'),
        'checks': {
            'minute_delta': fmt(minute_delta),
            'owner_excess_delta': fmt(owner_delta),
            'balanced': abs(minute_delta) <= tolerance and abs(owner_delta) <= tolerance,
            'compliant_minutes': fmt(totals['normal']),
            'unverified_minutes': fmt(unverified),
            'unknown_counts_as_compliant': False,
            'basis': '仅已知且已归属的分钟进入责任账；未知、未覆盖、迟到交接均单列，不折算给任何责任方',
        },
    }


def _batch_index(ledger):
    return {batch['shipment']: batch for batch in (ledger or {}).get('batches', [])}


def _number(value):
    return Decimal(str(value))


TRANSIENT_KEYS = ('version', 'created', 'idempotent', 'previous_version', 'restored_from',
                  'changes', 'publishable', 'blockers', 'closing_key', 'active')


def content(ledger):
    """用于判断两次关账是否产生同一份责任账；忽略版本号与生成时间。"""
    if ledger is None:
        return None
    body = {key: value for key, value in ledger.items() if key not in TRANSIENT_KEYS}
    return json.dumps(body, sort_keys=True, ensure_ascii=False)


def same_content(left, right):
    return content(left) == content(right)


def compare_ledgers(previous, current):
    """同一口径下的版本差异：每批责任分钟、热暴露、未知与未覆盖如何变化。"""
    before = _batch_index(previous)
    after = _batch_index(current)
    rows = []
    for shipment in sorted(set(before) | set(after)):
        old, new = before.get(shipment), after.get(shipment)
        delta = {field: fmt(_number(new[field] if new else 0) - _number(old[field] if old else 0))
                 for field in DIFF_FIELDS}
        if old is None:
            status = 'new'
        elif new is None:
            status = 'removed'
        elif any(_number(delta[field]) != 0 for field in DIFF_FIELDS):
            status = 'changed'
        else:
            status = 'same'
        rows.append({'shipment': shipment, 'status': status, 'delta': delta,
                     'before': {field: old[field] for field in DIFF_FIELDS} if old else None,
                     'after': {field: new[field] for field in DIFF_FIELDS} if new else None})
    old_totals = (previous or {}).get('totals', {})
    new_totals = (current or {}).get('totals', {})
    totals = {field: fmt(_number(new_totals.get(field, '0')) - _number(old_totals.get(field, '0')))
              for field in ('normal_minutes', 'excess_minutes', 'degree_minutes',
                            'unknown_minutes', 'uncovered_minutes')}
    totals['batches'] = int(new_totals.get('batches', 0)) - int(old_totals.get('batches', 0))
    return {
        'from_version': (previous or {}).get('version'),
        'to_version': (current or {}).get('version'),
        'basis': 'official_ledger',
        'batches': rows,
        'totals': totals,
        'changed': any(row['status'] in ('new', 'removed', 'changed') for row in rows),
    }
