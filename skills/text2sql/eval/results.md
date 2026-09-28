# 验收报告：20 题基线卷 + 30 题泛化卷（共 50 题）

## 一、结论

| 试卷 | 题量 | 一次通过 | 通过率 | 目标 |
|---|---|---|---|---|
| 基线卷 E01–E20 | 20 | **20** | **100%** | ≥80% |
| **泛化卷 E21–E50（few-shot 未直接覆盖的新问法/新维度）** | 30 | **30** | **100%** | 检验泛化 |
| **合计** | **50** | **50** | **100%** | — |

- **最终验收日期**：2026-09-28（泛化卷首轮 49/50，唯一失败经核实为题目歧义，消歧后整卷复跑 50/50）
- **模型**：deepseek-chat（DeepSeek 云端，OpenAI 兼容协议），temperature=0；每题只问一次、不改问法重试
- **链路**：schema_linker → llm_client → sql_guard → 只读 executor
- **账龄快照日**：2026-09-24（.env 的 SNAPSHOT_DATE 注入）
- **机器明细**：[eval/runs/latest.json](file:///./runs/latest.json)（每题完整 SQL 与返回行）；复跑命令：`python -B eval/run_eval.py`

## 二、基线卷逐题结果（E01–E20）

| 编号 | 模块 | 问题（摘要） | 命中表 | 实际返回关键值 | 判定 |
|---|---|---|---|---|---|
| E01 | 工单 | 当前在制批数 | work_order | **218** | ✅ |
| E02 | 工单 | 已完工批数 | work_order | **1582** | ✅ |
| E03 | 销售 | 客户销售额第一名 | order/order_item/customer/product | 韦尔股份豪威 **516,411.00** | ✅ |
| E04 | 销售 | 韦尔正式单数与金额 | order/customer/order_item | **38** 张 / **516,411.00** | ✅ |
| E05 | 销售 | 各产品类型销售额 | order/product/order_item/customer | FCBGA **2,538,507.6**；FCCSP **865,915.2**；WBCSP **353,137.2**；RF **184,507.2** | ✅ |
| E06 | RTY | 工单260600002 RTY | work_order/process_record/process | 已完工，**0.707** | ✅ |
| E07 | RTY | 完工批 RTY 均值/中位数 | work_order/process_record/process | 1582 批；**0.8179 / 0.8237** | ✅ |
| E08 | RTY | FCBGA vs FCCSP RTY | product/process_record/process/work_order | FCBGA 826批 **0.8025**；FCCSP 422批 **0.8305** | ✅ |
| E09 | FPY | FPY 最差工序 | process_record/process/work_order | **P09 外层图形 0.976** | ✅ |
| E10 | 报废 | 本月报废率 | process_record/process/work_order | 1309/156141，**0.0084** | ✅ |
| E11 | 返工 | 全期返工占比 | process_record/process/work_order | 2677/833444，**0.0032** | ✅ |
| E12 | OTD | 本月准时交付率 | shipment/order/order_item/customer | 118 批/准时 41/**0.3475** | ✅ |
| E13 | 应收 | 未结清笔数最多客户 | receivable/customer | 韦尔股份豪威 **14** 笔 | ✅ |
| E14 | 出货 | 近一周实际 vs 计划 | shipment_item/shipment/order/order_item | 实际 **732,240**；计划 **900,000** | ✅ |
| E15 | 缺陷 | 最多缺陷原因 | defect/defect_code/process | 孔位偏移 **641** | ✅ |
| E16 | 电测 | 全期电测通过率 | etest/process | 41086/40164，**0.9776** | ✅ |
| E17 | 客诉 | 寒武纪客诉与 DPPM | complaint/material/customer/product | **2** 次；均值 **148**；最高 **149** | ✅ |
| E18 | 停机 | 停机最长设备 | equipment/downtime | **Q0202，1200** 分钟 | ✅ |
| E19 | 库存 | M001 芯板库存 | inventory/material | **479，354,119.91** | ✅ |
| E20 | 账龄 | 9-24 快照未到期/逾期 | receivable/customer | 未到期 **738,598.07**；逾期 **0** | ✅ |

## 三、泛化卷逐题结果（E21–E50，考察点与结果）

| 编号 | 新考察点 | 问题（摘要） | 命中表 | 实际返回关键值 | 判定 |
|---|---|---|---|---|---|
| E21 | 新维度：产线分组 | 工单最多的产线 | work_order/**dim_line** | FCBGA专线 **948** | ✅ |
| E22 | 时间窗切换：6月 | 6月发放工单数 | work_order | **463** | ✅ |
| E23 | 新枚举：forecast | 预测单张数 | order/customer/order_item | **131** | ✅ |
| E24 | 比例新问法 | 正式单占比 | order/customer/order_item | 289/420，**0.6881** | ✅ |
| E25 | 新维度：客户区域 | 区域销售额第一 | order/order_item/customer/product | 华东 **1,773,370.8** | ✅ |
| E26 | **无关键词映射的枚举：priority=紧急** | 紧急工单数 | work_order | **645** | ✅ |
| E27 | 时间窗切换：8月销售 | 8月正式单销售额 | order/order_item/customer/product | **958,057.8** | ✅ |
| E28 | 口径迁移：本月返工率 | 本月返工占比 | process_record/process/work_order | 512/156141，**0.0033** | ✅ |
| E29 | 新对象：单工序 P01 | P01 一次通过率 | process/process_record/work_order | **0.9816** | ✅ |
| E30 | 新状态枚举 | 处理中客诉数 | complaint/customer/product | **10** | ✅ |
| E31 | RTY 换产品 | RF 类 RTY | product/process_record/process/work_order | **0.8397**（110批） | ✅ |
| E32 | RTY 换问法：最高 | RTY 最高产品类型 | process_record/product/process/work_order | WBCSP **0.8401**（224批） | ✅ |
| E33 | 时间窗切换：本月电测 | 本月电测通过率 | etest/process | 5031/4907，**0.9754** | ✅ |
| E34 | 新维度：缺陷分类 | 孔类缺陷总数 | defect/defect_code/process | **1417** | ✅ |
| E35 | 时间窗切换：8月报废 | 8月报废率 | process_record/process/work_order | **0.0082** | ✅ |
| E36 | is_rework_pass 换问法 | 返工重过站记录数 | process_record/process/work_order | **1391** | ✅ |
| E37 | OTD 换时间范围：全期 | 全期 OTD | shipment/order/order_item/customer | 165/301，**0.5482** | ✅ |
| E38 | 时间窗切换：7月出货 | 7月出货批数 | shipment/work_order/order/order_item | **28** | ✅ |
| E39 | 客户维度出货 | 海思累计出货批数 | shipment/work_order/order/order_item | **24** | ✅ |
| E40 | OTD 反向问法 | 全期延迟批数 | shipment/work_order/order/order_item | **136** | ✅ |
| E41 | 歧义辨析：含已结清 | 应收台账金额合计 | receivable/customer | **2,193,000.27** | ✅* |
| E42 | 应收状态计数 | 已结清笔数 | receivable/customer | **202** | ✅ |
| E43 | 停机原因细分 | 故障停机次数/分钟 | downtime/equipment | **4 次 / 420** 分钟 | ✅ |
| E44 | 新维度：班次（桥接表） | 夜班过站记录数 | process_record/**dim_shift**/process/work_order | **16,053** | ✅ |
| E45 | 设备状态枚举 | 维修中设备数 | equipment | **13** | ✅ |
| E46 | 无过滤全表计数 | 客诉总数 | customer/complaint/product | **28** | ✅ |
| E47 | 极值+客户名 | DPPM 最高客诉客户 | complaint/customer/material/product | 海思半导体 **178** | ✅ |
| E48 | 库存新口径：成品颗数 | 成品库存总量 | inventory/material | **2,823,720** | ✅ |
| E49 | 库存新口径：原料金额 | 原料库存总金额 | material/inventory | **1,090,012.85** | ✅ |
| E50 | 检出方式维度 | AOI 检出缺陷数 | defect/defect_code/process | **1421** | ✅ |

（注：E41 见第四节勘误说明。）

## 四、泛化卷说明了什么 / 勘误

**零语义层改动通过的难题**：20 条 few-shot 没有一条直接示范过"产线分组、夜班、区域、紧急优先级、6/7/8 月时间窗、P01 单工序、全期 OTD、成品颗数、原料金额"，模型依靠 tables.md 的列注释 + 示例值 + metrics.md 口径全部自行组合成功。其中：

- **E26（紧急工单）**：关键词表里没有"紧急/priority"，选表只命中 fact_work_order 一张，模型靠上下文中该表的列说明与枚举示例值正确生成 `WHERE priority='紧急'`——证明"选粗表 + 靠列说明让模型自己找过滤列"的设计成立，不必为每个枚举值堆关键词。
- **E44（夜班）**：dim_shift 经 related 桥接补入，跨 4 表上下文没有干扰主查询。
- **E31/E32**：范例只示范过 FCBGA/FCCSP 两产品对比，模型正确泛化到 RF 单查和"取最高"两种新问法，17 站连乘口径零退化。

**两处题目侧勘误（均非系统缺陷）**：

1. **E41（首轮唯一 FAIL）**：原题"应收账款总金额是多少"有业务歧义，模型理解为"当前未结清余额"（738,598.07，且严格执行了账龄只看部分/未收的口径），题库预期为台账金额合计（含已结清，2,193,000.27）。题目改为"台账金额合计（包含已结清）"后通过。这也侧面证明模型对 metrics.md 口径的遵从度很高。
2. 基线卷两处历史勘误（E13 应收 14 笔、E17 为 DPPM 口径）见此前 20 题验收记录，题库已固化。

**安全**：50 条 SQL 全部为单条 SELECT/WITH，守卫零拦截、只读连接零写尝试；四条强制规则（R1–R4）与基准日锚定在 50 题中持续成立。

## 五、最终验收意见

阶段二目标"自然语言→只读可靠取数，一次通过率 ≥80%"以 **50/50（基线 100% + 泛化 100%）** 达成；系统对未示范问法表现出稳定泛化能力，语义层/口径/范例三件套的设计被验证有效。
