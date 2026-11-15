"""面向工艺比较的曲线分析：把阶段错位（相位）与幅度差异分开。

设计约束（见 docs/analysis.md）：

* 采样点不是独立样本。同一试件的重复测量先在试件内平均，再按批次平均；
  工艺按批次施加，批次才是推断单位。
* 对齐只允许整条工艺臂共用一个、有上限的 x 方向平移，且始终并列无对齐
  结果，避免把真正的处理效应一起移走。
* 严格共同支撑之外不推断、不置零、不外推；early_stop 的尾段只登记不补值。
* 缺少共同观测阶段或分组条件不满足时显式停止，而不是制造平滑精度。
* 所有网格坐标都是原始 x，对齐映射 x_aligned = x_raw + shift 随结果返回，
  可回到 /records 的原始点和具体试件核对。
"""
import itertools
import math

import numpy as np
from scipy import stats

from .common import finite, require

STRATEGIES = ('none', 'cross_correlation', 'reference_shift')
SENSITIVITY_CAPS = (0.05, 0.10, 0.20)
DEFAULT_GRID_POINTS = 51
DEFAULT_CAP = 0.10
PERMUTATION_LIMIT = 1000
PERMUTATION_DRAWS = 2000
_RNG_SEED = 20261115


def _native(value):
    """把 numpy 类型转成原生 JSON 类型，NaN/Inf 一律转 None。"""
    if isinstance(value, dict):
        return {key: _native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _strict_support(rows):
    lower = max(row['points'][0][0] for row in rows)
    upper = min(row['points'][-1][0] for row in rows)
    if lower >= upper:
        return None
    return [lower, upper]


def _interpolate_rows(rows, grid):
    """每条曲线在原始坐标网格上线性插值；网格保证落在各曲线自身跨度内。"""
    matrix = np.empty((len(rows), len(grid)))
    spans = []
    for index, row in enumerate(rows):
        xs = np.asarray([point[0] for point in row['points']], dtype=float)
        ys = np.asarray([point[1] for point in row['points']], dtype=float)
        matrix[index] = np.interp(grid, xs, ys)
        spans.append((float(xs[0]), float(xs[-1]), len(xs)))
    return matrix, spans


def _lag_scan(reference, candidate, grid, cap_x):
    """候选曲线相对参照的相位滞后扫描，一次返回三种口径。

    约定 candidate(x + lag) ≈ reference(x)。lag 是把候选曲线“移回”参照所需
    的 x 平移，对齐时直接使用 arm_shifts = lag：x_aligned = x_raw + lag。

    * lag < 0：候选曲线的特征在原始坐标上更靠左，即相位领先（新工艺更早
      到达同一阶段），需要左移才能与参照重合；
    * lag > 0：候选曲线相位滞后，需要右移。

    三种口径：

    * standardized：整体 z 标准化后的 SSE，去掉幅度与截距差异，只比形状；
    * derivative：对 z 标准化曲线取斜率后再比，进一步去掉单调抬升，只比曲率；
    * raw：不做标准化，幅度差会泄漏进滞后，仅作对照。

    滞后可能小于网格间距，因此内部在同一段共同支撑上加密 4 倍插值后再扫描
    （仍是线性插值、不超出支撑），步长为加密网格间距的 1/4。
    """
    dense = np.linspace(grid[0], grid[-1], (len(grid) - 1) * 4 + 1)
    ref_dense = np.interp(dense, grid, reference)
    cand_dense = np.interp(dense, grid, candidate)
    ref_z = (ref_dense - ref_dense.mean()) / (ref_dense.std(ddof=0) + 1e-12)
    cand_z = (cand_dense - cand_dense.mean()) / (cand_dense.std(ddof=0) + 1e-12)
    variants = {
        'standardized': (ref_z, cand_z),
        'derivative': (np.gradient(ref_z), np.gradient(cand_z)),
        'raw': (ref_dense, cand_dense),
    }
    step = (dense[1] - dense[0]) / 4
    lags = np.arange(-cap_x, cap_x + step / 2, step)
    results = {}
    for name, (ref_use, cand_use) in variants.items():
        best_lag, best_sse, best_n = 0.0, math.inf, 0
        for lag in lags:
            shifted = np.interp(dense, dense + lag, cand_use, left=np.nan, right=np.nan)
            mask = np.isfinite(shifted)
            if mask.sum() < 8:
                continue
            sse = float(np.sum((shifted[mask] - ref_use[mask]) ** 2) / mask.sum())
            if sse < best_sse:
                best_sse, best_lag, best_n = sse, float(lag), int(mask.sum())
        # 浮点零点清理：扫描候选本身在 0 附近时去掉 1e-15 量级噪声，
        # 不改变任何可解析的非零滞后
        if abs(best_lag) < 1e-12:
            best_lag = 0.0
        results[name] = (best_lag, best_n)
    return results


def _welch_interval(values_a, values_b, confidence=0.95):
    """Welch 置信区间，支持向量（逐点）或标量（积分值）。返回下界、上界、自由度。"""
    n_a, n_b = len(values_a), len(values_b)
    mean_a = np.mean(values_a, axis=0)
    mean_b = np.mean(values_b, axis=0)
    var_a = np.var(values_a, axis=0, ddof=1) if n_a > 1 else np.zeros_like(mean_a)
    var_b = np.var(values_b, axis=0, ddof=1) if n_b > 1 else np.zeros_like(mean_b)
    se_a2 = var_a / n_a
    se_b2 = var_b / n_b
    se = np.sqrt(se_a2 + se_b2)
    with np.errstate(divide='ignore', invalid='ignore'):
        df = (se_a2 + se_b2) ** 2 / (
            np.where(se_a2 > 0, se_a2 ** 2 / (n_a - 1), 0.0)
            + np.where(se_b2 > 0, se_b2 ** 2 / (n_b - 1), 0.0)
        )
    df = np.where(np.isfinite(df) & (df > 0), df, np.inf)
    quantile = stats.t.ppf((1 + confidence) / 2, df)
    gap = mean_b - mean_a
    lower = gap - quantile * se
    upper = gap + quantile * se
    zero_se = se == 0
    lower = np.where(zero_se, gap, lower)
    upper = np.where(zero_se, gap, upper)
    return gap, lower, upper, df


def _credible_regions(grid, lower, upper, baseline, comparator):
    """连续可信区间：置信界整体位于 0 的同一侧的连续网格段。"""
    significant = (lower > 0) | (upper < 0)
    regions = []
    start = None
    for index, flag in enumerate(significant):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            regions.append((start, index - 1))
            start = None
    if start is not None:
        regions.append((start, len(grid) - 1))
    result = []
    for left, right in regions:
        result.append({
            'x_from': float(grid[left]),
            'x_to': float(grid[right]),
            'direction': f'{comparator}_above_{baseline}' if lower[left] > 0
            else f'{comparator}_below_{baseline}',
            'gap_min': float(np.minimum(lower[left:right + 1].min(), upper[left:right + 1].min())),
            'gap_max': float(np.maximum(lower[left:right + 1].max(), upper[left:right + 1].max())),
            'basis': '批次级逐点 Welch 95% 置信区间，未做多重性校正',
        })
    return result


def _integrated(values, grid):
    """每条曲线在网格上的梯形积分均值（y 原单位，与区间长度无关的整体水平）。"""
    return np.trapezoid(values, grid, axis=-1) / (grid[-1] - grid[0])


def _permutation_test(batch_values, labels):
    """以批次为单位的置换检验，比较两工艺臂积分均值的差。"""
    values = np.asarray(batch_values, dtype=float)
    labels = np.asarray(labels)
    arms = sorted(set(labels.tolist()))
    n_one = int(np.sum(labels == arms[1]))
    observed = float(values[labels == arms[1]].mean() - values[labels == arms[0]].mean())
    total = math.comb(len(values), n_one)
    if total <= PERMUTATION_LIMIT:
        statistics = []
        for chosen in itertools.combinations(range(len(values)), n_one):
            mask = np.zeros(len(values), dtype=bool)
            mask[list(chosen)] = True
            statistics.append(values[mask].mean() - values[~mask].mean())
        method, draws = 'exact', total
    else:
        rng = np.random.default_rng(_RNG_SEED)
        statistics = []
        for _ in range(PERMUTATION_DRAWS):
            mask = rng.permutation(len(values)) < n_one
            statistics.append(values[mask].mean() - values[~mask].mean())
        method, draws = 'monte_carlo', PERMUTATION_DRAWS
    statistics = np.asarray(statistics)
    p_value = float((1 + np.sum(np.abs(statistics) >= abs(observed) - 1e-12)) / (1 + len(statistics)))
    return observed, p_value, method, draws


def _shift_arm_matrix(matrix, grid, shift):
    """臂内每条曲线平移后的取值与有效掩码；x_aligned = x_raw + shift。"""
    aligned_x = grid + shift
    shifted = np.empty_like(matrix)
    for index in range(matrix.shape[0]):
        shifted[index] = np.interp(grid, aligned_x, matrix[index], left=np.nan, right=np.nan)
    valid = np.all(np.isfinite(shifted), axis=0)
    return shifted, valid


def compare(data, baseline=None, grid_points=DEFAULT_GRID_POINTS,
            cap=DEFAULT_CAP, strategy='cross_correlation', reference_shift=None):
    """返回工艺比较分析结果。无法比较时 status 为 stopped 并给出 stop_reasons。"""
    rows = data['curves']
    finite(grid_points, 'grid_points')
    require(isinstance(grid_points, int) and 5 <= grid_points <= 201, 'grid_points 需为 5 到 201 的整数')
    finite(cap, 'cap')
    require(0.0 < cap <= 0.5, 'cap 需为 (0, 0.5] 之间的支撑宽度比例')
    require(strategy in STRATEGIES, f'strategy 只支持 {STRATEGIES}')

    treatments = sorted({row['treatment'] for row in rows})
    parameters = {
        'baseline': baseline, 'grid_points': grid_points, 'cap': cap,
        'strategy': strategy, 'reference_shift': reference_shift,
    }
    if len(treatments) != 2:
        return _native(_stopped(parameters, rows, ['工艺比较需要恰好两个 treatment 标签']))
    if baseline is None:
        baseline = 'old' if 'old' in treatments else treatments[0]
    require(baseline in treatments, 'baseline 必须是资料中的 treatment 标签')
    comparator = treatments[1] if treatments[0] == baseline else treatments[0]
    parameters['baseline'] = baseline

    batch_arm = {}
    for row in rows:
        arm = row['treatment']
        if row['batch'] in batch_arm and batch_arm[row['batch']] != arm:
            return _native(_stopped(
                parameters, rows,
                ['同一批次出现多个 treatment，批次不能作为独立实验单位，停止比较']))
        batch_arm[row['batch']] = arm

    support = _strict_support(rows)
    if support is None:
        return _native(_stopped(parameters, rows, ['各曲线没有共同观测阶段（共同支撑为空），停止比较']))

    if strategy == 'reference_shift':
        require(isinstance(reference_shift, dict), 'reference_shift 策略需要 shifts 映射')
        require(set(reference_shift) == set(treatments), 'reference_shift 的键必须覆盖两个 treatment')
        for arm in treatments:
            finite(reference_shift[arm], f'reference_shift[{arm}]')

    grid = np.linspace(support[0], support[1], grid_points)
    width = float(grid[-1] - grid[0])
    cap_x = cap * width
    curve_values, spans = _interpolate_rows(rows, grid)

    # 试件层：重复测量在试件内平均（等权）
    specimens = {}
    for index, row in enumerate(rows):
        specimens.setdefault(row['specimen'], []).append(index)
    specimen_records, specimen_values, specimen_arm = [], [], []
    for specimen, indices in sorted(specimens.items()):
        first = rows[indices[0]]
        specimen_records.append({
            'specimen': specimen, 'batch': first['batch'], 'treatment': first['treatment'],
            'curve_ids': [rows[i]['id'] for i in indices],
            'replicate_count': len(indices),
        })
        specimen_values.append(curve_values[indices].mean(axis=0))
        specimen_arm.append(first['treatment'])
    specimen_values = np.asarray(specimen_values)
    specimen_arm = np.asarray(specimen_arm)

    # 批次层：试件等权平均；批次是推断单位
    batches = sorted(batch_arm)
    batch_records, batch_values, batch_arm_labels = [], [], []
    for batch in batches:
        members = [i for i, record in enumerate(specimen_records) if record['batch'] == batch]
        batch_records.append({
            'batch': batch, 'treatment': batch_arm[batch],
            'specimen_ids': [specimen_records[i]['specimen'] for i in members],
            'curve_ids': [cid for i in members for cid in specimen_records[i]['curve_ids']],
            'n_specimens': len(members),
        })
        batch_values.append(specimen_values[members].mean(axis=0))
        batch_arm_labels.append(batch_arm[batch])
    batch_values = np.asarray(batch_values)
    batch_arm_labels = np.asarray(batch_arm_labels)

    arm_index = {
        arm: np.asarray([i for i, label in enumerate(batch_arm_labels) if label == arm])
        for arm in treatments
    }
    stop_reasons = []
    inference_ok = all(len(arm_index[arm]) >= 2 for arm in treatments)
    if not inference_ok:
        stop_reasons.append('至少一个工艺臂的批次数少于 2，无法估计批次间方差：仅给描述性结果，不做置信区间与置换检验')
    for arm in treatments:
        if len(arm_index[arm]) < 3:
            stop_reasons.append(f'工艺臂 {arm} 批次数为 {len(arm_index[arm])}，小样本结果需谨慎')

    arm_mean = {arm: batch_values[arm_index[arm]].mean(axis=0) for arm in treatments}

    # 各策略所需的臂位移估计。baseline 臂位移为 0，comparator 相对 baseline
    # 扫描；主口径 standardized（形状匹配），derivative/raw 同时返回供核对。
    def estimated_shifts(scan_cap_x):
        shifts = {baseline: {name: (0.0, 0) for name in ('standardized', 'derivative', 'raw')}}
        comparator_lags = _lag_scan(arm_mean[baseline], arm_mean[comparator], grid, scan_cap_x)
        shifts[comparator] = comparator_lags
        return shifts

    strategy_blocks = []
    strategy_blocks.append(_strategy_block(
        'none', None, {arm: 0.0 for arm in treatments},
        grid, batch_values, batch_arm_labels, arm_index, baseline, comparator,
        width, inference_ok, cap_x, reference_based=False, lag_conventions=None))

    for scan_cap in SENSITIVITY_CAPS:
        lag_variants = estimated_shifts(scan_cap * width)
        shifts = {arm: lag_variants[arm]['standardized'][0] for arm in treatments}
        block = _strategy_block(
            'cross_correlation', scan_cap, shifts,
            grid, batch_values, batch_arm_labels, arm_index, baseline, comparator,
            width, inference_ok, scan_cap * width, reference_based=False,
            lag_conventions=lag_variants)
        strategy_blocks.append(block)

    if strategy == 'reference_shift':
        given = {arm: float(reference_shift[arm]) for arm in treatments}
        for arm in treatments:
            if abs(given[arm]) > cap_x + 1e-12:
                raise ValueError(f'reference_shift[{arm}] 超过 cap 允许的平移上限 {cap_x:.6g}')
        strategy_blocks.append(_strategy_block(
            'reference_shift', cap, given,
            grid, batch_values, batch_arm_labels, arm_index, baseline, comparator,
            width, inference_ok, cap_x, reference_based=True))

    primary_key = 'none' if strategy == 'none' else (
        'reference_shift' if strategy == 'reference_shift'
        else f'cross_correlation:{cap if cap in SENSITIVITY_CAPS else DEFAULT_CAP}')
    primary = next(block for block in strategy_blocks if block['key'] == primary_key)
    if strategy == 'cross_correlation' and cap not in SENSITIVITY_CAPS:
        primary['cap_note'] = (
            f'请求 cap={cap} 不在敏感性网格 {list(SENSITIVITY_CAPS)} 内；主块展示默认 '
            f'{DEFAULT_CAP}，各敏感性 cap 结果见 strategies')

    # 无对齐整体差异（原始口径），用于相位/幅度分解
    raw_block = strategy_blocks[0]

    tails = []
    global_end = max(span[1] for span in spans)
    for index, row in enumerate(rows):
        tails.append({
            'curve_id': row['id'], 'specimen': row['specimen'], 'batch': row['batch'],
            'treatment': row['treatment'], 'end_reason': row['end_reason'],
            'last_x': spans[index][1], 'last_y': float(row['points'][-1][1]),
            'within_strict_support': bool(spans[index][1] >= support[1] - 1e-12),
        })
    short_tails = [item for item in tails if item['last_x'] < global_end - 1e-12]

    trace_curves = []
    for index, row in enumerate(rows):
        trace_curves.append({
            'curve_id': row['id'], 'specimen': row['specimen'], 'batch': row['batch'],
            'treatment': row['treatment'], 'end_reason': row['end_reason'],
            'first_x': spans[index][0], 'last_x': spans[index][1],
            'n_raw_points': spans[index][2],
            'grid_x': [float(x) for x in grid],
            'interpolated_y': [float(y) for y in curve_values[index]],
        })

    result = {
        'status': 'ok' if inference_ok else 'inference_stopped',
        'stop_reasons': stop_reasons,
        'parameters': parameters,
        'treatments': {'baseline': baseline, 'comparator': comparator},
        'support': {
            'strict_common_support': [float(support[0]), float(support[1])],
            'width': width,
            'grid_points': grid_points,
            'rule': '所有曲线原始跨度的交集；之外不推断、不置零、不外推',
        },
        'counts': {
            'curves': len(rows),
            'specimens': {arm: int(np.sum(specimen_arm == arm)) for arm in treatments},
            'batches': {arm: int(len(arm_index[arm])) for arm in treatments},
            'replicate_curves_per_specimen': sorted({
                record['replicate_count'] for record in specimen_records}),
        },
        'phase_amplitude_split': {
            'raw_integrated_gap': None if raw_block['integrated'] is None
            else raw_block['integrated']['mean_gap'],
            'aligned_integrated_gap': None if primary['integrated'] is None
            else primary['integrated']['mean_gap'],
            'phase_attributable_change': (
                None if raw_block['integrated'] is None or primary['integrated'] is None
                else raw_block['integrated']['mean_gap'] - primary['integrated']['mean_gap']),
            'between_arm_lag': primary['phase']['between_arm_lag'],
            'note': 'raw 为无对齐整体差；aligned 为去除相位后的幅度残差；两者之差是对齐所吸收的部分。推断停用时积分差为空',
        },
        'strategies': strategy_blocks,
        'primary_strategy': primary_key,
        'tails': {
            'curves_ending_before_global_max_end': short_tails,
            'all_curve_endpoints': tails,
            'global_max_end_x': float(global_end),
            'rule': '严格共同支撑之外的尾段只登记原始端点，不补零、不外推、不参与差异推断；early_stop 不视为 y=0',
        },
        'trace': {
            'coordinate_note': 'grid_x 为原始 x 坐标；对齐后坐标为 grid_x + 该工艺臂 shift',
            'curves': trace_curves,
            'specimens': specimen_records,
            'batches': batch_records,
        },
        'interpretation': {
            'inference_basis': '批次为独立实验单位的总体关联比较；treatment 是工艺标签而非随机分配承诺，不单独声称因果处理效应',
            'multiplicity': '逐点可信区间未做多重性校正，作用区间应与整体置换检验一起解读',
            'alignment_guard': '位移按工艺臂整体估计且有上限；无对齐结果始终保留，若结论随 cap 翻转应视为对齐敏感',
            'phase_identifiability': '标准化与导数口径滞后接近而 raw 口径偏大时，差异主要来自幅度而非相位；单调曲线上相位与幅度天然部分不可区分，应同时查看三种口径',
            'small_sample': '批次数量有限时置信区间与 p 值不稳定，应回到原始坐标与试件核对',
        },
    }
    return _native(result)


def _strategy_block(key_strategy, cap_value, shifts, grid, batch_values, batch_arm_labels,
                    arm_index, baseline, comparator, width, inference_ok, cap_x,
                    reference_based, lag_conventions=None):
    treatments = list(arm_index)
    aligned_grids, valid_masks, shifted_batches = {}, {}, {}
    for arm in treatments:
        shifted, valid = _shift_arm_matrix(batch_values[arm_index[arm]], grid, shifts[arm])
        shifted_batches[arm] = shifted
        valid_masks[arm] = valid
        aligned_grids[arm] = [float(x + shifts[arm]) for x in grid]
    common_valid = valid_masks[baseline] & valid_masks[comparator]
    inference_grid = grid[common_valid]

    conventions = None
    if lag_conventions is not None:
        conventions = {
            name: {
                'arm_lags': {arm: float(lag_conventions[arm][name][0]) for arm in treatments},
                'between_arm_lag': float(
                    lag_conventions[comparator][name][0]
                    - lag_conventions[baseline][name][0]),
            }
            for name in ('standardized', 'derivative', 'raw')
        }
    block = {
        'key': key_strategy if key_strategy == 'none' or reference_based
        else f'{key_strategy}:{cap_value}',
        'strategy': key_strategy,
        'cap_fraction': None if cap_value is None else float(cap_value),
        'phase': {
            'arm_shifts': {arm: float(shifts[arm]) for arm in treatments},
            'between_arm_lag': float(shifts[comparator] - shifts[baseline]),
            'lag_percent_of_support_width': float(
                abs(shifts[comparator] - shifts[baseline]) / width * 100),
            'sign_convention': 'lag 为对齐所用 x 平移（x_aligned=x_raw+lag）；lag<0 表示 comparator 相位领先需左移，lag>0 表示相位滞后需右移',
            'binding_cap': {arm: bool(abs(shifts[arm]) >= cap_x - 1e-12) for arm in treatments}
            if key_strategy != 'none' else {arm: False for arm in treatments},
            'lag_conventions': conventions,
            'reference_based': reference_based,
        },
        'aligned_support': [float(inference_grid[0]), float(inference_grid[-1])]
        if len(inference_grid) else None,
        'aligned_grid_points': int(len(inference_grid)),
        'arm_aligned_grids': aligned_grids,
        'feasible': bool(len(inference_grid) >= 3),
    }

    if len(inference_grid) < 3:
        block.update({'integrated': None, 'pointwise': None,
                      'stop_reason': '平移后共同网格不足 3 点，该策略不可用'})
        return block

    base_shifted = shifted_batches[baseline][:, common_valid]
    comp_shifted = shifted_batches[comparator][:, common_valid]
    pointwise = {
        'grid_x': [float(x) for x in inference_grid],
        f'{baseline}_mean': [float(x) for x in base_shifted.mean(axis=0)],
        f'{comparator}_mean': [float(x) for x in comp_shifted.mean(axis=0)],
    }
    integrated = None
    if inference_ok:
        gap, lower, upper, df = _welch_interval(base_shifted, comp_shifted)
        pointwise.update({
            f'gap_{comparator}_minus_{baseline}': [float(x) for x in gap],
            'ci95_lower': [float(x) for x in lower],
            'ci95_upper': [float(x) for x in upper],
            'welch_df': [None if not math.isfinite(d) else float(d) for d in df],
            'credible_regions': _credible_regions(
                inference_grid, lower, upper, baseline, comparator),
        })
        base_integrated = _integrated(base_shifted, inference_grid)
        comp_integrated = _integrated(comp_shifted, inference_grid)
        _, p_value, method, draws = _permutation_test(
            np.concatenate([base_integrated, comp_integrated]),
            np.concatenate([
                np.full(len(base_integrated), baseline),
                np.full(len(comp_integrated), comparator),
            ]))
        igap, ilower, iupper, _ = _welch_interval(
            base_integrated.reshape(-1, 1), comp_integrated.reshape(-1, 1))
        baseline_level = float(np.mean(base_integrated))
        integrated = {
            'mean_gap': float(igap[0]),
            'ci95_lower': float(ilower[0]),
            'ci95_upper': float(iupper[0]),
            'permutation_p': p_value,
            'permutation_method': method,
            'permutation_draws': draws,
            'relative_gap_percent': float(igap[0] / baseline_level * 100)
            if baseline_level != 0 else None,
            'statistic': '网格梯形积分均值之差（comparator - baseline）',
        }
    else:
        pointwise.update({
            f'gap_{comparator}_minus_{baseline}': [
                float(x) for x in comp_shifted.mean(axis=0) - base_shifted.mean(axis=0)],
            'ci95_lower': None, 'ci95_upper': None, 'credible_regions': [],
        })
    block.update({'integrated': integrated, 'pointwise': pointwise})
    return block


def _stopped(parameters, rows, reasons):
    return {
        'status': 'stopped',
        'stop_reasons': reasons,
        'parameters': parameters,
        'curve_spans': [
            {'curve_id': row['id'], 'treatment': row['treatment'],
             'batch': row['batch'], 'specimen': row['specimen'],
             'first_x': row['points'][0][0], 'last_x': row['points'][-1][0],
             'end_reason': row['end_reason']}
            for row in rows
        ],
    }
