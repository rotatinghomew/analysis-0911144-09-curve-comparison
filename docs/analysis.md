# 工艺比较分析口径

`GET /comparison` 在不可变资料版本上运行 `curve_comparison.analysis.compare`，回答材料团队的三个问题：整体差异有多大、哪些阶段的差异可信、结论对“怎么对齐”是否敏感。所有结果都能回到原始坐标和具体试件核对。

## 1. 数据层次与推断单位

采样点不是独立样本。分析按三层处理，层次关系完整保留在 `trace` 中：

1. **曲线层**：每条曲线在严格共同支撑的等距网格上做线性插值，网格坐标仍是原始 x，不引入观测之外的点；
2. **试件层**：同一 `specimen` 的重复测量等权平均（`replicate_count` 保留）；
3. **批次层**：同一 `batch` 的试件等权平均。工艺按批次施加，因此**批次是推断单位**，逐点与整体推断都在批次曲线上做，不把同批重复当作独立证据。

每个批次只能对应一个 treatment；同一批次出现两个工艺标签时直接停止比较。

## 2. 严格共同支撑与尾段

* `strict_common_support` = 所有曲线原始跨度 `[首点 x, 末点 x]` 的交集。分析网格只在这个区间内生成。
* 交集之外**不推断、不置零、不外推**。`early_stop` 曲线提前结束的尾段不是 y=0，也不会被补成完整曲线。
* `tails.curves_ending_before_global_max_end` 逐条登记短于全局最大终点的曲线（含 `end_reason`、真实末端坐标与末端值）；`all_curve_endpoints` 给出全部曲线端点，便于工程师判断支撑是怎么被收窄的。
* 没有共同观测阶段（交集为空）时 `status=stopped`，只回各曲线跨度，不产出任何差异数字。

## 3. 相位与幅度的分离

约定对齐映射为 `x_aligned = x_raw + shift`，整条工艺臂共用一个 shift，不允许逐点拉伸或非线性弯曲。

* **相位（阶段错位）**：以 baseline 臂的批次均值曲线为参照，对 comparator 臂做受限滞后扫描，最小化 `comparator(x + lag) ≈ baseline(x)`。baseline 臂 shift 固定为 0，避免参照取总均值时真实滞后被两臂平分。
* **幅度（处理效应的残差部分）**：对齐后两臂在共同网格上的垂直差，即 `pointwise` 间隙与 `integrated` 积分差。
* `phase_amplitude_split` 同时给出无对齐积分差、对齐后积分差与两者之差（对齐吸收掉的部分），工程师可以直接看到“有多少差异被解释为阶段错位、多少留下来是幅度差”。

### 三种滞后口径（同时返回，不替工程师选择）

| 口径 | 做法 | 适用与风险 |
| --- | --- | --- |
| `standardized`（主口径，用于实际 shift） | 两臂曲线各自 z 标准化后比 SSE | 去掉幅度与截距，只比形状 |
| `derivative` | z 标准化后取斜率再比 | 去掉单调抬升，只看曲率/拐点 |
| `raw`（仅对照） | 不做标准化 | 幅度差会泄漏进滞后，偏大时不能当相位用 |

单调饱和曲线上相位与幅度天然部分不可区分：若三种口径分歧大，说明数据本身无法干净分离，应在报告中保留分歧而不是选一个平滑数字。`interpretation.phase_identifiability` 对此有固定提示。

### 对齐不能抹掉处理效应的四道约束

1. shift 有上限：`|shift| ≤ cap × 支撑宽度`，默认 cap=0.10，触顶时 `binding_cap=true`；
2. 每次分析固定同时跑 `none` 与 cap=0.05/0.10/0.20 四个块，无对齐结果永远在最前面；
3. 位移是工艺臂级的单一常数，不吸收曲线内部的形状差异；
4. `reference_shift` 允许工程师用外部依据（设备记录、工艺时间表）直接给定 shift，同样受 cap 约束，并标记 `reference_based=true`，与数据驱动估计并列。

## 4. 可信作用区间与整体检验

仅在每个工艺臂至少有 2 个批次时输出推断，否则 `status=inference_stopped`，只给描述性均值与间隙，置信区间和 p 值为空。

* **逐点**：每个网格点在批次曲线上做 Welch 两臂比较，输出 95% 置信区间与自由度；置信界整体位于 0 同侧的连续网格段整理成 `credible_regions`（含方向与间隙范围）。逐点区间**未做多重性校正**，字段 `basis` 与 `interpretation.multiplicity` 固定声明这一点，局部翻转不应单独解读。
* **整体**：每条批次曲线在共同网格上的梯形积分均值（除以宽度，与区间长度无关），两臂之差给出 Welch 95% 区间、相对百分比，以及以批次为单位的置换检验：组合数 ≤1000 时精确枚举（如 6 批次为 C(6,3)=20），否则用固定种子的 2000 次蒙特卡洛置换，方法与抽样次数随结果返回。
* treatment 是工艺标签而非随机分配承诺，结论统一表述为**总体关联比较**，不单独声称因果处理效应（见 `interpretation.inference_basis`）。

## 5. 停止条件（宁可不报，不造精度）

| 条件 | 状态 | 行为 |
| --- | --- | --- |
| treatment 标签数 ≠ 2 | `stopped` | 只回曲线跨度与原因 |
| 同批次出现多个 treatment | `stopped` | 批次独立性不成立 |
| 严格共同支撑为空 | `stopped` | 无共同阶段可比 |
| 任一工艺臂批次数 < 2 | `inference_stopped` | 只出描述性结果，CI/p 值为空 |
| 批次 < 3 | `ok` 附带提示 | 小样本不稳定性写进 `stop_reasons` |
| 某策略平移后共同网格 < 3 点 | 块级 `feasible=false` | 该块不出数字，其他策略照常 |

## 6. 可追溯字段

* `trace.curves`：每条曲线的 id、specimen、batch、treatment、`end_reason`、原始首尾 x、原始点数，以及在原始坐标网格上的插值序列；
* `trace.specimens` / `trace.batches`：试件→重复曲线、批次→试件→曲线的完整归属；
* 每个策略块的 `arm_aligned_grids` 给出该臂对齐后的实际 x 坐标（= 原始网格 + shift），可把对齐结果一一映射回 `/records` 的原始点；
* 响应外层固定带 `revision` 与 `digest`，客户端可钉住版本，不会在读取时被换成最新资料。

## 7. 参数与默认值

`compare(data, baseline=None, grid_points=51, cap=0.10, strategy='cross_correlation', reference_shift=None)`

* `baseline`：默认取 `old`，不存在时取字典序第一个标签；
* `grid_points`：5–201 的整数；
* `cap`：(0, 0.5] 的支撑宽度比例；敏感性块固定使用 0.05/0.10/0.20，请求其他 cap 时主块回退到 0.10 并在 `cap_note` 说明；
* `strategy`：`none` / `cross_correlation` / `reference_shift`，后者必须提供覆盖两个 treatment 的 shift 映射且受 cap 限制。
