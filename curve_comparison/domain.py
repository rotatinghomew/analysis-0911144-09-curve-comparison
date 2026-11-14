from collections import Counter
from .common import finite, require, table, unique


def validate(data):
    rows = table(data, 'curves')
    unique(rows)
    specimen_batches = {}
    for row in rows:
        require(bool(row['specimen']) and bool(row['batch']) and bool(row['treatment']), '曲线分组不完整')
        require(row['end_reason'] in ('complete', 'early_stop'), '终止原因不支持')
        require(len(row['points']) >= 2, '曲线至少需要两个观测点')
        positions = []
        for position, value in row['points']:
            finite(position, 'x')
            finite(value, 'y')
            positions.append(position)
        require(positions == sorted(set(positions)), '原始坐标必须严格递增')
        key = row['batch'], row['treatment']
        require(row['specimen'] not in specimen_batches or specimen_batches[row['specimen']] == key, '同一试件的批次和工艺不一致')
        specimen_batches[row['specimen']] = key


def support(rows):
    if not rows:
        return None
    lower = max(row['points'][0][0] for row in rows)
    upper = min(row['points'][-1][0] for row in rows)
    return [lower, upper] if lower < upper else None


def records(data):
    return data['curves']


def summarize(data):
    rows = data['curves']
    return {'curves': len(rows), 'specimens': len({row['specimen'] for row in rows}), 'batches': len({row['batch'] for row in rows}), 'treatments': dict(Counter(row['treatment'] for row in rows)), 'common_raw_support': support(rows), 'meaning': '原坐标观测概览，不是对齐或差异检验'}
