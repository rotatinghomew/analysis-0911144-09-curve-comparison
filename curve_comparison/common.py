import math


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value, name, minimum=None):
    require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), f'{name} 必须是有限数值')
    require(minimum is None or value >= minimum, f'{name} 小于允许范围')
    return value


def unique(rows, field='id'):
    keys = [row[field] for row in rows]
    require(all(isinstance(key, str) and key for key in keys), '标识不能为空')
    require(len(keys) == len(set(keys)), '标识重复')


def table(data, field):
    rows = data[field]
    require(isinstance(rows, list), f'{field} 必须是数组')
    require(all(isinstance(row, dict) for row in rows), f'{field} 的行必须是对象')
    return rows
