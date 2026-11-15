# 本地接口约定

所有接口使用 UTF-8 JSON，错误响应为带 error 字段的对象。GET /health 返回进程健康状态。POST /imports 接收 request_key 与 data，data 遵循业务资料契约；新导入返回 201，完全相同的请求重放返回 200。相同请求号对应不同资料返回 409；非法资料返回 400，不留下部分版本。

GET /revisions 列出 revision 和内容摘要。GET /records 返回业务原始记录；GET /summary 返回已有概览，两者都返回 revision、digest 和 result。可加 revision 查询参数固定历史资料，不提供时读取最新资料；资料不存在为 404。摘要对对象键顺序不敏感，对数组顺序敏感；资料内容相同可共享版本，导入请求回执相互独立。

GET /comparison 返回面向工艺比较的分析结果，口径见 docs/analysis.md，同样带 revision、digest 包装并支持 revision 钉版。查询参数：strategy=none|cross_correlation|reference_shift（默认 cross_correlation）、cap（支撑宽度比例，默认 0.10，敏感性固定另跑 0.05/0.10/0.20）、grid_points（默认 51）、baseline（默认 old）。reference_shift 必须附带两个 shift=treatment:value 参数，例如 shift=old:0.02&shift=new:-0.02。参数非法返回 400；资料缺失或 revision 不存在返回 404。分析无法成立时 HTTP 仍为 200，result.status 为 stopped 或 inference_stopped 并带 stop_reasons，不产出推断数字。

未知路径为 404。现有客户端依赖这些路径和包装字段；后续增加业务分析接口时保留既有行为，不能把资料导入成功等同于业务分析已经形成结论。每个分析产物应声明所用数据版本，不能在客户端读取时偷偷替换成最新资料。
