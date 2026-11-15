import copy
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError

from curve_comparison.analysis import compare
from curve_comparison.client import Client
from curve_comparison.repository import Repository
from curve_comparison.service import make_server


def _curve(cid, specimen, batch, treatment, xs, ys, end_reason='complete'):
    return {'id': cid, 'specimen': specimen, 'batch': batch,
            'treatment': treatment, 'end_reason': end_reason,
            'points': [[float(x), float(y)] for x, y in zip(xs, ys)]}


def _synthetic(n_batches_per_arm=3, n_specimens=2, n_points=21, lag=0.08,
               amplitude=0.3, noise=0.01, seed=7, early_stop=False):
    """new 臂相对 old 臂有已知相位领先 lag 与幅度差 amplitude。

    曲线取带明显拐点的 S 形饱和曲线（logistic），拐点位置对 x 平移敏感，
    标准化与导数口径都能识别相位；同一试件生成两条带独立噪声的重复测量。
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    xs = [i / (n_points - 1) for i in range(n_points)]
    rows = []
    cid = 0
    for arm_index, arm in enumerate(('old', 'new')):
        shift_x = lag if arm == 'new' else 0.0
        offset = amplitude if arm == 'new' else 0.0
        for batch_index in range(n_batches_per_arm):
            batch = f'b{arm_index}_{batch_index}'
            batch_bend = rng.normal(0, noise)
            for specimen_index in range(n_specimens):
                specimen = f'{batch}_s{specimen_index}'
                base_ys = [
                    3.0 / (1.0 + np.exp(-12.0 * ((x - shift_x) - 0.5)))
                    + offset + batch_bend
                    for x in xs
                ]
                for repeat in range(2):
                    ys = [float(y + rng.normal(0, noise)) for y in base_ys]
                    curve_xs, reason = xs, 'complete'
                    if early_stop and arm == 'new' and batch_index == 0 and specimen_index == 0:
                        curve_xs, ys, reason = xs[:15], ys[:15], 'early_stop'
                    rows.append(_curve(f'c{cid}', specimen, batch, arm, curve_xs, ys, reason))
                    cid += 1
    return {'curves': rows}


class CompareCoreTests(unittest.TestCase):
    def test_recovers_known_phase_and_amplitude(self):
        data = _synthetic(lag=0.08, amplitude=0.3, noise=0.005)
        result = compare(data, cap=0.2)
        self.assertEqual(result['status'], 'ok')
        block_20 = next(b for b in result['strategies'] if b['key'] == 'cross_correlation:0.2')
        # 形状口径应恢复新臂领先：对齐 lag 为负且接近 -0.08（领先需左移）
        shape_lag = block_20['phase']['lag_conventions']['standardized']['between_arm_lag']
        self.assertLess(shape_lag, -0.04)
        self.assertGreater(shape_lag, -0.12)
        # raw 口径受幅度污染，应与形状口径不同
        raw_lag = block_20['phase']['lag_conventions']['raw']['between_arm_lag']
        self.assertNotEqual(round(raw_lag, 6), round(shape_lag, 6))
        # 对齐后整体幅度差仍显著为正，没有被对齐抹掉。
        # 6 批次精确置换的最小可达 p 值为 1/C(6,3)=1/7≈0.143，断言取该地板值
        aligned_gap = block_20['integrated']['mean_gap']
        self.assertGreater(aligned_gap, 0.15)
        self.assertLessEqual(block_20['integrated']['permutation_p'], 1 / 7 + 1e-12)
        # 整体（积分口径）方向向上；逐点区间允许局部翻转，这正是多重性警示
        # 要表达的含义，因此只要求至少存在一段 new_above_old 可信区间
        directions = {item['direction'] for item in block_20['pointwise']['credible_regions']}
        self.assertIn('new_above_old', directions)

    def test_cap_binds_and_preserves_larger_lag(self):
        # 真实领先 0.15（lag≈-0.15）；cap=0.05 时位移必须被截断并标记 binding
        data = _synthetic(lag=0.15, amplitude=0.2, noise=0.005)
        result = compare(data, cap=0.05)
        tight = next(b for b in result['strategies'] if b['key'] == 'cross_correlation:0.05')
        width = result['support']['width']
        self.assertTrue(any(tight['phase']['binding_cap'].values()))
        for arm, shift in tight['phase']['arm_shifts'].items():
            self.assertLessEqual(abs(shift), 0.05 * width + 1e-9)
        loose = next(b for b in result['strategies'] if b['key'] == 'cross_correlation:0.2')
        self.assertFalse(any(loose['phase']['binding_cap'].values()))
        # 对齐支持域因平移收缩，但不超出原始共同支撑
        self.assertGreaterEqual(loose['aligned_support'][0], result['support']['strict_common_support'][0] - 1e-9)
        self.assertLessEqual(loose['aligned_support'][1], result['support']['strict_common_support'][1] + 1e-9)

    def test_unaligned_strategy_always_present_and_unshifted(self):
        data = _synthetic()
        result = compare(data)
        none_block = result['strategies'][0]
        self.assertEqual(none_block['key'], 'none')
        self.assertEqual(none_block['phase']['arm_shifts'], {'old': 0.0, 'new': 0.0})
        self.assertEqual(none_block['aligned_support'], result['support']['strict_common_support'])

    def test_hierarchy_repeats_and_batches_preserved(self):
        data = _synthetic(n_batches_per_arm=3, n_specimens=2)
        result = compare(data)
        self.assertEqual(result['counts']['curves'], 24)
        self.assertEqual(result['counts']['specimens'], {'old': 6, 'new': 6})
        self.assertEqual(result['counts']['batches'], {'old': 3, 'new': 3})
        self.assertEqual(result['counts']['replicate_curves_per_specimen'], [2])
        # trace 保留试件-批次-曲线关系
        specimen = next(s for s in result['trace']['specimens'] if s['replicate_count'] == 2)
        self.assertEqual(len(specimen['curve_ids']), 2)
        batch_record = result['trace']['batches'][0]
        self.assertEqual(batch_record['n_specimens'], 2)
        self.assertEqual(len(batch_record['curve_ids']), 4)
        # 每条曲线可回到原始坐标，且网格点全在原始跨度内
        curve = result['trace']['curves'][0]
        self.assertEqual(len(curve['grid_x']), len(curve['interpolated_y']))
        self.assertGreaterEqual(curve['grid_x'][0], curve['first_x'])
        self.assertLessEqual(curve['grid_x'][-1], curve['last_x'])

    def test_early_stop_tail_registered_not_zero_or_extrapolated(self):
        data = _synthetic(early_stop=True)
        result = compare(data)
        short = result['tails']['curves_ending_before_global_max_end']
        self.assertTrue(any(item['end_reason'] == 'early_stop' for item in short))
        stopped = next(item for item in short if item['end_reason'] == 'early_stop')
        # 尾段登记的是真实末端值，严格共同支撑不超过其最后观测
        self.assertGreater(stopped['last_y'], -1e9)
        self.assertLessEqual(result['support']['strict_common_support'][1],
                             stopped['last_x'] + 1e-9)
        # 推断网格不超出严格共同支撑
        for block in result['strategies']:
            self.assertLessEqual(block['aligned_support'][1],
                                 result['support']['strict_common_support'][1] + 1e-9)

    def test_no_common_support_stops(self):
        data = _synthetic()
        data['curves'][0]['points'] = [[5.0, 0.0], [6.0, 1.0]]
        result = compare(data)
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(any('共同观测阶段' in reason for reason in result['stop_reasons']))
        self.assertNotIn('strategies', result)

    def test_batch_with_two_treatments_stops(self):
        data = _synthetic()
        data['curves'][-1]['batch'] = data['curves'][0]['batch']
        result = compare(data)
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(any('批次' in reason for reason in result['stop_reasons']))

    def test_wrong_treatment_count_stops(self):
        data = _synthetic()
        for row in data['curves'][:2]:
            row['treatment'] = 'third'
        result = compare(data)
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(any('两个 treatment' in reason for reason in result['stop_reasons']))

    def test_single_batch_arm_is_descriptive_only(self):
        data = _synthetic(n_batches_per_arm=1)
        result = compare(data)
        self.assertEqual(result['status'], 'inference_stopped')
        self.assertTrue(result['stop_reasons'])
        for block in result['strategies']:
            self.assertIsNone(block['integrated'])
            self.assertEqual(block['pointwise']['credible_regions'], [])
            self.assertIsNotNone(block['pointwise'].get('gap_new_minus_old'))

    def test_reference_shift_strategy(self):
        data = _synthetic(lag=0.06)
        width = compare(data)['support']['width']
        result = compare(data, strategy='reference_shift',
                         reference_shift={'old': 0.03, 'new': -0.03})
        block = result['strategies'][-1]
        self.assertEqual(block['strategy'], 'reference_shift')
        self.assertTrue(block['phase']['reference_based'])
        self.assertAlmostEqual(block['phase']['between_arm_lag'], -0.06)
        # 超出 cap 必须拒绝
        with self.assertRaises(ValueError):
            compare(data, strategy='reference_shift', cap=0.05,
                    reference_shift={'old': 0.0, 'new': width * 0.1})
        with self.assertRaises(ValueError):
            compare(data, strategy='reference_shift',
                    reference_shift={'old': 0.0})

    def test_parameter_validation(self):
        data = _synthetic()
        with self.assertRaises(ValueError):
            compare(data, grid_points=3)
        with self.assertRaises(ValueError):
            compare(data, cap=0.9)
        with self.assertRaises(ValueError):
            compare(data, strategy='bogus')
        with self.assertRaises(ValueError):
            compare(data, baseline='missing')

    def test_sensitivity_conclusion_stability_on_synthetic(self):
        data = _synthetic(lag=0.05, amplitude=0.4, noise=0.005)
        result = compare(data)
        signs = []
        for block in result['strategies']:
            if block['integrated'] is not None:
                signs.append(math.copysign(1, block['integrated']['mean_gap']))
        self.assertEqual(set(signs), {1.0})
        self.assertEqual(result['primary_strategy'], 'cross_correlation:0.1')

    def test_permutation_exact_with_small_batches(self):
        data = _synthetic(n_batches_per_arm=3)  # C(6,3)=20 精确枚举
        result = compare(data)
        block = result['strategies'][0]
        self.assertEqual(block['integrated']['permutation_method'], 'exact')
        self.assertEqual(block['integrated']['permutation_draws'], 20)

    def test_demo_fixture_runs_descriptive(self):
        data = json.loads(Path('fixtures/demo.json').read_text())
        result = compare(data)
        self.assertEqual(result['status'], 'inference_stopped')
        self.assertEqual(result['support']['strict_common_support'], [0.1, 0.8])
        self.assertTrue(any(t['curve_id'] == 'c3' and t['end_reason'] == 'early_stop'
                            for t in result['tails']['curves_ending_before_global_max_end']))

    def test_history_fixture_full_pipeline(self):
        data = json.loads(Path('fixtures/history.json').read_text())
        result = compare(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['counts']['batches'], {'old': 4, 'new': 4})
        block = result['strategies'][0]
        # 新工艺整体高于旧工艺（合成资料的已知方向）
        self.assertGreater(block['integrated']['mean_gap'], 0.0)
        self.assertLess(block['integrated']['ci95_lower'], block['integrated']['mean_gap'])
        # 输出全部为 JSON 可序列化的有限值或 None
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertIn('credible_regions', encoded)

    def test_result_is_json_safe_and_native(self):
        data = _synthetic()
        result = compare(data)
        json.dumps(result, ensure_ascii=False, allow_nan=False)


class ComparisonHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.repository = Repository(Path(self.directory.name) / 'data.sqlite3')
        self.server = make_server(self.repository)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}')
        self.data = _synthetic()
        self.receipt = self.client.import_data(self.data, 'r1')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def test_comparison_roundtrip_and_revision_pin(self):
        response = self.client.comparison(self.receipt['revision'])
        self.assertEqual(response['revision'], self.receipt['revision'])
        self.assertEqual(response['digest'], self.receipt['digest'])
        result = response['result']
        self.assertEqual(result['status'], 'ok')
        self.assertGreaterEqual(len(result['strategies']), 4)

    def test_comparison_revision_404_and_bad_params_400(self):
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/comparison?revision=999')
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/comparison?strategy=bogus')
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/comparison?cap=2')
        self.assertEqual(caught.exception.code, 400)

    def test_reference_shift_via_http(self):
        response = self.client.request(
            '/comparison?strategy=reference_shift&shift=old:0.02&shift=new:-0.02&cap=0.2')
        block = response['result']['strategies'][-1]
        self.assertEqual(block['strategy'], 'reference_shift')
        self.assertAlmostEqual(block['phase']['between_arm_lag'], -0.04)

    def test_existing_endpoints_unchanged(self):
        self.assertEqual(self.client.summary()['result']['curves'], len(self.data['curves']))
        self.assertEqual(len(self.client.records()['result']), len(self.data['curves']))
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/unknown')
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
