# 语义层 · 指标口径与强制规则

> 本文件是生成 SQL 的**业务口径权威**，所有 SQL 必须严格遵守。
> 列名真实可执行（已对 `data/ic_substrate.db` 校验）；SQL 方言为 **SQLite**。

---

## 0. 时间基准与通用约定

- 数据时间窗：**2026-06-01 ~ 2026-09-24**；统一业务基准日 **ANCHOR = '2026-09-24'**（数据最新日）。
  ⚠️ ANCHOR 由后端运行时按库内 `MAX(业务日期)` 动态探测（见 `skills/text2sql/anchor.py`），数据滚动更新后锚点自动跟进；
  本文档以 2026-09-24 为示例快照编写，拼 prompt 时其中的示例日期会被自动替换为当前锚点。
- 应收账龄另有一个**可配置快照日 `SNAPSHOT_DATE`**（`.env` 显式设置时固定，如切到月末 `'2026-10-31'` 演示逾期；
  未设置时默认跟随动态锚点）；其余指标只用 ANCHOR。
- 问题中的“今天 / 当前 / 本月 / 上月（上个月） / 最近一周 / 最近一个月 / 今年”，一律相对基准日，**禁止用 `date('now')`**（真实运行日晚于数据窗，会查空）：
  - 本月：`date(out_time) >= date('2026-09-24','start of month')`（当月 1 日起）
  - 上月/上个月：ANCHOR 所在月的**上一个完整自然月（即 2026-08 整月）**，用半开区间：
    `date(x) >= date('2026-09-24','start of month','-1 month') AND date(x) < date('2026-09-24','start of month')`
    （下界 2026-08-01、上界 2026-09-01 不含；不要用 `between` 或按 31 天回推，避免月末边界错）
  - 最近一周：`date(x) >= date('2026-09-24','-6 day')`（含基准日共 7 天）
  - 最近一个月：`date(x) >= date('2026-09-24','-29 day')`（含基准日共 30 天）
  - 今年：`date(x) >= date('2026-09-24','start of year')`（锚点所在年 1 月 1 日起）
- 过站/生产类指标的时间列用 **`out_time`（出站时间）**，在制记录 out_time 为 NULL，天然不计入已发生产出。
- **记录级“月度活动比率”与“整批质量水平”要分开**：
  - 报废率、返工占比等**月度活动比率**：按 `date(out_time)` 落在目标区间过滤，**不要再 JOIN `fact_work_order` 过滤 `lot_status='已完工'`**；
    在制批已出站的工序记录也要计入，否则会漏量（实测 9 月返工占比应为 512/156,141，加已完工过滤会错成 255/84,602）。
  - FPY/良率/RTY 等**质量水平**：必须整批排除在制（见强制规则 3），因为要按批次/工序比较质量能力。
  - 两者都只用主记录 `is_rework_pass = 0`、`input_panels > 0`。
  - **全期累计**（问题不带“本月/上月/某月”等时间范围）直接对全部主记录 SUM，**不要额外加 `out_time IS NOT NULL`**；
    只有指定时间区间时才用 `out_time` 过滤。加 `out_time IS NOT NULL` 会排除在制批已预填的记录
    （实测全期投入 833,444→805,892、返工 2,677→2,630），与“全期”口径不符。
- 数量单位：订单/出货为**颗**（order_qty、ship_qty_units、units_per_panel）；过站为**排板 panels**（input_panels 等），不要跨口径混算。
- 比例计算先乘 `1.0` 避免 SQLite 整数除法，例如 `SUM(good_panels)*1.0/SUM(input_panels)`。
- **比率类问题必须同时输出分子、分母、比率三列**（如 `SUM(rework_panels) AS rework_panels, SUM(input_panels) AS input_panels, ROUND(...,4) AS rework_rate`）；
  只返回比率会导致结果集核对失败，也无法解释口径。占比/率/达成率类问题同理，除非问题明确只要一个值。
- 全库订单币种只有 **RMB** 一种，销售额无需换汇。
- **`order_type='forecast'` 是预测单（131 张），不是真实订单**：凡问“订单数 / 订单金额 / 销售额 / 排名 / 占比”，
  默认只统计 `order_type='正式单'`（289 张），除非问题明确要求包含预测。

---

## 1. 指标定义

### 1.1 销售额 / 订单金额
- 单订单行金额 = `fact_sales_order_item.order_qty * fact_sales_order_item.unit_price`（颗 × 单价）。
- 客户维度必须 JOIN `dim_customer` 按 `customer_id` 聚合（见强制规则 1）。
- 产品维度：订单行 JOIN `dim_product` 用 `product_type / product_name`。

### 1.2 FPY 一次通过率
```
FPY = (input_panels − scrap_panels − rework_panels) / input_panels
    = good_panels / input_panels        -- 主记录中恒等（实测守恒：833444 = 823850 + 6917 + 2677）
```
- 只取主记录（非返工重过站）：`is_rework_pass = 0`。
- 工序级 FPY：按 `process_id` 分组求 SUM 后计算；JOIN `dim_process` 取工序名/顺序。

### 1.3 RTY 滚动直通率
- 定义：同一批次流经 **全部 17 道工序**（dim_process 全量 P01~P17）的**主记录** FPY 逐站连乘。
  > 注意：**不是**只取 `is_key_process=1` 的 11 道关键工序——11 站口径实测中位数 0.8505，与基线不符；
  > 17 站口径实测均值 0.8179 / 中位数 **0.8237 ≈ 82%**，与基线精确吻合。
  > `is_key_process=1`（11 道）只用于“关键工序”筛选，不用于 RTY 连乘。
- 返工重过站（`is_rework_pass = 1`，共 1391 行）**不参与**连乘。
- **只统计已完工批次**（见强制规则 3）。⚠️ 不能靠“缺站”识别在制：在制批的未来工序记录
  虽然 `out_time IS NULL`，但 input/good 已预填了数值（218 批照样凑得齐 17 站、能算出 RTY≈0.86）。
  必须显式 `JOIN fact_work_order ... WHERE lot_status='已完工'`（已完工批 out_time 全部非空）。
- 问“某工单 RTY”时，若该批次在制，不给完整 RTY，只可对其已出站工序算阶段直通率并注明在制。
- 全厂/产品口径：**先逐批次算 RTY，再对批次集合取中位数/均值**；不要把所有批次的投入产出汇总后只连乘一次。
- **SQL 落地条件（生成时必须逐条体现）**：
  - 主记录：`p.is_rework_pass = 0`；非零分母：`p.input_panels > 0`；
  - 整批已完工：`JOIN fact_work_order w ON w.lot_id = p.lot_id AND w.lot_status = '已完工'`；
  - 17 站齐全：`p.process_id IN ('P01','P02',...,'P17')` + `HAVING COUNT(DISTINCT p.process_id) = 17`；
  - 时间归属：问“本月/某月 RTY”时，按该批 **P17 主记录 out_time** 所在月归属批次
    （`MAX(CASE WHEN process_id='P17' THEN out_time END)`），**不要按每道工序 out_time 分别过滤**，否则会截断连乘；
  - 分组：先按 `lot_id` 算 RTY，再按产品/产线取 AVG 或中位数；不得先汇总再连乘。
- **部门交叉**：`fact_process_record.department` 是逐条过站记录的操作员部门快照，且实测 17 道工序都出现过 5 个部门的操作记录，
  不能把工序固定映射到部门，**因此 RTY 不能按部门拆分**。问“部门 RTY”时应返回按部门聚合的 FPY
  （`SUM(good_panels)*1.0/SUM(input_panels)`），并注明这是部门过站良率、不是 17 站连乘的全流程 RTY；
  跨部门支援人员按其 `dim_employee.department` 主部门计入操作记录，不做二次分摊。
- SQLite 没有连乘聚合函数，**固定用“MAX(CASE) 透视后相乘”写法**（不依赖 ln/exp，兼容更稳）：

```sql
-- RTY 标准模板：17 道工序全连乘 + 显式限定已完工（中位数≈0.82）
WITH fpy AS (
  SELECT p.lot_id, p.process_id, p.good_panels * 1.0 / p.input_panels AS f
  FROM fact_process_record p
  JOIN fact_work_order w ON w.lot_id = p.lot_id
  WHERE w.lot_status = '已完工'        -- 强制规则3：显式排除在制，不能靠缺站判断
    AND p.is_rework_pass = 0           -- 强制规则2：排除返工重过站
    AND p.process_id IN ('P01','P02','P03','P04','P05','P06','P07','P08',
                         'P09','P10','P11','P12','P13','P14','P15','P16','P17')
), lot_rty AS (
  SELECT lot_id,
         MAX(CASE WHEN process_id='P01' THEN f END) * MAX(CASE WHEN process_id='P02' THEN f END)
       * MAX(CASE WHEN process_id='P03' THEN f END) * MAX(CASE WHEN process_id='P04' THEN f END)
       * MAX(CASE WHEN process_id='P05' THEN f END) * MAX(CASE WHEN process_id='P06' THEN f END)
       * MAX(CASE WHEN process_id='P07' THEN f END) * MAX(CASE WHEN process_id='P08' THEN f END)
       * MAX(CASE WHEN process_id='P09' THEN f END) * MAX(CASE WHEN process_id='P10' THEN f END)
       * MAX(CASE WHEN process_id='P11' THEN f END) * MAX(CASE WHEN process_id='P12' THEN f END)
       * MAX(CASE WHEN process_id='P13' THEN f END) * MAX(CASE WHEN process_id='P14' THEN f END)
       * MAX(CASE WHEN process_id='P15' THEN f END) * MAX(CASE WHEN process_id='P16' THEN f END)
       * MAX(CASE WHEN process_id='P17' THEN f END) AS rty
  FROM fpy
  GROUP BY lot_id
  HAVING COUNT(DISTINCT process_id) = 17   -- 双保险：17 站齐全
)
SELECT * FROM lot_rty;
-- 中位数：对上表 ORDER BY rty 后用 ROW_NUMBER() 取中间值（SQLite 无 MEDIAN）；
-- FCBGA/FCCSP 对比：lot_rty 再 JOIN fact_work_order→dim_product，按 product_type 分组 AVG(rty)。
```

### 1.4 报废率
```
报废率 = SUM(scrap_panels) / SUM(input_panels)      -- 同样过滤 is_rework_pass = 0
```
- 时间口径：`date(out_time)` 在目标区间；**不 JOIN `fact_work_order` 过滤 `lot_status`**。
  月度报废率是已出站记录的报废活动量，在制批已出站工序也计入（实测 9 月 0.84%、8 月 0.82%）；
  加已完工过滤会漏掉在制批当月产出，与基线不符。全期累计则**不加任何 out_time 条件**（见 §0）。
- 分母：同期 `SUM(input_panels)`，且 `input_panels > 0`。

### 1.5 返工占比
```
返工量占比 = SUM(rework_panels) / SUM(input_panels)  -- 主记录(is_rework_pass=0)上的一次返工数
```
`is_rework_pass = 1` 的记录表示“返工后的重过站过程”，用于统计返工规模/次数，其 good_panels 不得计入一次产出。
- 时间口径：`date(out_time)` 在目标区间；**不 JOIN `fact_work_order` 过滤 `lot_status='已完工'`**。
  返工占比是“当期已出站记录的返工活动量”，在制批已出站工序也计入（实测 9 月 512/156,141 = 0.33%）；
  加已完工过滤会漏成 255/84,602，结果集不一致。全期累计则**不加任何 out_time 条件**：
  实测全期返工 2,677、投入 833,444（加了 `out_time IS NOT NULL` 会错成 2,630 / 805,892）。
- 分母：同期 `SUM(input_panels)`，且 `input_panels > 0`；只取 `is_rework_pass = 0`。

### 1.6 OEE 设备综合效率
```
OEE = 时间稼动率 × 性能稼动率 × 良率
时间稼动率 = (计划生产时间 − 停机时长) / 计划生产时间
```
- 停机时长取 `fact_equipment_downtime.duration_min`（原因：故障/换型/待料/保养/动力）。
- 计划生产时间与性能稼动所需节拍用 `dim_process.avg_cycle_min` 近似；数据不全时在结果中注明口径，不要编造列。

### 1.7 OTD 准时交付率（已确认口径）
```
OTD = 准时出货批数 / 应出货批数
准时判定：fact_shipment.ship_date <= 该订单的交期 due_date
```
- **`fact_shipment` 没有 scheduled_date 列**；交期取 `fact_sales_order_item.due_date`。
- 一个订单可能有多行（628 行 / 420 单），出货单只挂 order_id，故**按订单粒度取 `MIN(due_date)`** 作为应出货日：

```sql
SELECT
  SUM(CASE WHEN s.ship_date <= d.due_date THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS otd
FROM fact_shipment s
JOIN (
  SELECT order_id, MIN(due_date) AS due_date
  FROM fact_sales_order_item GROUP BY order_id
) d ON d.order_id = s.order_id;
```

**落地条件与边界：**
- 分子：`SUM(CASE WHEN date(s.ship_date) <= date(d.due_date) THEN 1 ELSE 0 END)`；分母：`COUNT(*)`（出货记录数，不是订单数）。
- 时间口径：按 **实际出货日 `s.ship_date`** 过滤（本月 = 月初 ~ 锚点）；不要用 `due_date` 过滤，否则口径变成“本月应交付”。
- 状态：`s.status = '已出货'`（当前 301 行全为该状态）；不按 `order_type` 过滤，当前口径含 88 条 forecast 出货。
  若问题明确要求“只算正式单”，再 `JOIN fact_sales_order` 加 `order_type='正式单'`，并在答案中注明口径变化。
- 空值：`s.ship_date IS NOT NULL`、`d.due_date IS NOT NULL`；无订单行的出货在 INNER JOIN 后被排除（当前 0 行）。
- 多行订单：交期按订单粒度 `MIN(due_date)`；当前每单最多 1 条出货，未来多批出货按**出货记录**逐条计数，不按订单去重。
- **部门交叉**：`fact_shipment` 没有部门字段，订单/出货也没有销售归属字段，**OTD 不能按部门拆分**；
  `dim_customer.sales_contact` 取值是“业务32”这类编号，与 `dim_employee` 对不上，禁止反推部门。按客户/区域统计。

### 1.8 DPPM 百万不良数
```
DPPM = 不良数 / 交付数 × 10^6
```
- 客诉口径：`fact_customer_complaint.dppm` 有**现成字段**，按 customer_id / product_id 聚合即可。
- 电测口径：不良 = `tested_panels - pass_panels`（**以此为准**），
  `DPPM = (tested_panels - pass_panels) * 1.0 / tested_panels * 1000000`。
  ⚠️ 实测 `open_fail + short_fail + isolation_fail` 与 `tested_panels - pass_panels` **不守恒**
  （1,582 行中只有 376 行相等；全期总不良 922，而三个分项之和 2,545）。三个失败列仅用于
  “开路/短路/绝缘”分项展示，**不得相加当总不良**，也不得用它们反推通过率。

### 1.9 电测（P15）通过率
```
电测通过率 = SUM(pass_panels) * 1.0 / SUM(tested_panels)     -- 表 fact_etest，电测即工序 P15
```
**落地条件与边界：**
- 只查 `fact_etest`；分子 `SUM(pass_panels)`，分母 `SUM(tested_panels)`；比例先乘 `1.0`。
- 时间口径：`test_time`（电测执行时间），**不是生产 `out_time`**；本月 = `date(test_time)` 在月初 ~ 锚点之间。
- 粒度：每批 1 行（1,582 批，均为已完工批）；不要 JOIN `fact_process_record` 的返工重过站记录，否则会放大分母。
- 空值/异常：`tested_panels > 0`；实测 `pass_panels` 无 NULL 且不超过 `tested_panels`。
- 产品分组：`fact_etest.lot_id → fact_work_order.product_id → dim_product.product_type`（lot_id 唯一，不会放大）。
- **部门交叉**：`fact_etest` 无部门字段；如需按部门，唯一可行路径是
  `JOIN fact_process_record p ON p.lot_id = e.lot_id AND p.process_id='P15' AND p.is_rework_pass=0`
  取 `p.department`（实测每批唯一，1,582 批全可匹配）；这是“电测过站操作员所属部门”，不是品质部专属，
  跨部门支援人员按主部门计入，不做二次分摊。

### 1.10 库存数量 / 金额
- 数量：`fact_inventory.qty_on_hand`，按 `stock_type`（原料/成品）、`warehouse`、`material_id` 聚合。
- 金额：**只有原料可算**（8 种原料行），`qty_on_hand * dim_material.standard_cost`，按 material_id JOIN。
- 成品库存 600 行无成本/产品列，**只报数量**，禁止硬 JOIN 造金额。

### 1.11 应收账龄 / 逾期金额（双向账龄表，锚点可配置）
- **账龄只分析未结清账款**：`status IN ('部分','未收')`（共 99 笔），未收余额 = `amount - COALESCE(received_amount, 0)`；
  已结清 200 笔余额为 0，不入账龄表（标准会计口径）。
- 快照日 = `SNAPSHOT_DATE`（运行时以 system prompt 注入的值为准：`.env` 显式设置时固定，否则跟随动态锚点；下文 SQL 用字面日期示例）。
  ⚠️ 实测数据事实：99 笔的 due_date 全部 ≥ 2026-09-24（分布 9-24 ~ 11-23），
  故 **9-24 快照下逾期 = 0 笔 / 0 元，未到期余额 738,598.07 元——这是正确结果，不是查询出错**。
  切到月末快照 `SNAPSHOT_DATE='2026-10-31'` 时实测：逾期 54 笔 / 389,373.20 元（验收演示用）。
- 注意 receive_date 含晚于 9-24 的日期（实测 101 行），账龄**只看 due_date 与 status，不看 receive_date**。
- **SQL 落地条件（生成时必须逐条体现）**：
  - 余额：`amount - COALESCE(received_amount, 0)`，且 `余额 > 0`（当前无 0/负余额行，保留为防脏数据条件）；
  - 状态：`r.status IN ('部分','未收')`，已结清不计入；
  - 空值：`r.due_date IS NOT NULL`；
  - 边界：`due_date = 快照日` 算未到期（`>=`）；逾期天数 = `julianday(快照日)-julianday(due_date)`，未到期天数反向；
  - 分组：按 `dim_customer.customer_id` 聚合（禁止按 `customer_name_raw`）；分桶金额统一写
    `SUM(CASE WHEN ... THEN 余额 ELSE 0 END)`，保证金额与笔数口径一致。
- **部门交叉**：`fact_receivable` 没有部门/销售负责人字段，**账龄不能按部门拆分**；
  `dim_customer.sales_contact` 与 `dim_employee` 值域不对应，禁止反推。账龄只按客户/区域/时间维度回答。
- **双向分桶**（一个 CASE 同时表达已逾期/未到期，保证任何快照日结果都有内容）：
  - 已逾期（due_date < 快照日）：1–30 天 / 31–60 天 / 61–90 天 / 90 天以上
  - 未到期（due_date ≥ 快照日）：7 天内到期 / 8–30 天 / 31–60 天 / 60 天以上
  - 到期日当天（= 快照日）算未到期（信用期到日终）。

```sql
-- 各客户应收双向账龄表（已实测可执行；'2026-09-24' 由 config.SNAPSHOT_DATE 注入替换）
SELECT c.customer_id, c.customer_name,
       COUNT(*) AS bills,
       ROUND(SUM(x.bal), 2) AS outstanding,
       ROUND(SUM(CASE WHEN x.d BETWEEN 1  AND 30 THEN x.bal ELSE 0 END), 2) AS overdue_1_30,
       ROUND(SUM(CASE WHEN x.d BETWEEN 31 AND 60 THEN x.bal ELSE 0 END), 2) AS overdue_31_60,
       ROUND(SUM(CASE WHEN x.d BETWEEN 61 AND 90 THEN x.bal ELSE 0 END), 2) AS overdue_61_90,
       ROUND(SUM(CASE WHEN x.d > 90             THEN x.bal ELSE 0 END), 2) AS overdue_90p,
       SUM(CASE WHEN x.nd BETWEEN 0  AND 7  THEN 1 ELSE 0 END) AS due_in_0_7,
       SUM(CASE WHEN x.nd BETWEEN 8  AND 30 THEN 1 ELSE 0 END) AS due_in_8_30,
       SUM(CASE WHEN x.nd BETWEEN 31 AND 60 THEN 1 ELSE 0 END) AS due_in_31_60,
       SUM(CASE WHEN x.nd > 60             THEN 1 ELSE 0 END) AS due_in_60p
FROM (
  SELECT r.customer_id,
         r.amount - r.received_amount AS bal,                       -- 未收余额
         CASE WHEN r.due_date < '2026-09-24'                        -- d：已过期天数(仅逾期行非NULL)
              THEN CAST(julianday('2026-09-24') - julianday(r.due_date) AS INT) END AS d,
         CASE WHEN r.due_date >= '2026-09-24'                       -- nd：距到期天数(仅未到期行非NULL)
              THEN CAST(julianday(r.due_date) - julianday('2026-09-24') AS INT) END AS nd
  FROM fact_receivable r
  WHERE r.status IN ('部分','未收')
) x
JOIN dim_customer c ON c.customer_id = x.customer_id           -- 强制规则1：按 customer_id 聚合
GROUP BY c.customer_id, c.customer_name
ORDER BY outstanding DESC;
-- 实测基线：9-24 快照 -> 99笔/738,598.07元，逾期四桶全 0；未到期 0-7天18笔、8-30天30笔、31-60天51笔。
--          10-31 快照 -> 逾期 389,373.20 元（1-30天 259,921.87 + 31-60天 129,451.33）。
```

### 1.12 部门维度（按部门统计产量 / 不良 / 工时）
- **部门字段**：`fact_process_record.department`，取值 `制造 / 品质 / 设备 / 生管 / 业务`；
  它是过站操作员所属部门的**记录时点快照**（由 `operator_id` → `dim_employee.department` 回填）。
- **归属规则**：每条过站记录归属其 `operator_id` 的主部门；不按设备、产线、班次或工序名称反推部门。
  现有 31,991 条记录 `operator_id` 100% 可解析，`department` 无 NULL（覆盖率 100%）。
- **跨部门人员**：员工以 `dim_employee.department` 为唯一主部门；跨部门支援/借调人员的过站记录
  计入其主部门，**不做二次分摊**（如需“支援工时”，应另建借调/工时表，禁止用时间或班次推断）。
  同一批次跨工序流经多个部门时，各段分别统计，故部门产量是**过站产出量**，
  各部门/各工序产量不可相加当作全厂唯一排板产量（全厂口径按工序或批次另算）。
- **“部门 / 事业部”问法统一按该字段回答**：现有组织主数据没有独立的事业部层级，
  禁止以“没有部门/事业部字段”为由拒答，也不要臆造不存在的事业部层级。
- **产量**：部门产量 = 该部门各工序 `SUM(good_panels)`（配合 `is_rework_pass = 0`，
  时间口径用 `out_time`）。
- **不良率**：`(SUM(scrap_panels) + SUM(rework_panels)) * 1.0 / SUM(input_panels)`，
  按 `department` 分组先汇总再相除，同样只取主记录。
- **工时**：单条过站工时 = `(julianday(out_time) - julianday(in_time)) * 24` 小时；
  只统计 `out_time IS NOT NULL` 的记录（在制批次为空），部门月度工时 = SUM 该时长。
  默认口径**含返工重过站记录**（不按 `is_rework_pass` 过滤，返工也是实际发生的人工投入）；
  若组织另有确认口径（如只算主记录），以「指标口径字典」的部门条目为准。
- **默认值规则**：若未来出现 `operator_id` 缺失或不在员工主数据，`department` 记为 `未归属`，
  统计时单列展示，**不并入任何部门**。
- **适用边界（哪些指标不能按部门拆）**：
  - 可用 `department` 直接分组的记录级指标：产量、FPY、不良率、报废率、返修率、部门工时。
  - **不可拆**：RTY（批次级 17 站连乘，且同一工序由多部门人员操作）、OTD（`fact_shipment` 无部门字段）、
    应收账龄（`fact_receivable` 无部门/销售归属）、客诉（无部门字段）。这些问法只能按客户/产品/产线/时间等其他维度回答，
    并说明“数据不含部门归属”；**禁止**用 `operator_id`、`sales_contact` 或工序名称反推部门。
  - 电测：`fact_etest` 无部门字段，需 `lot_id → fact_process_record(P15, is_rework_pass=0).department` 关联，
    每批唯一（1,582 批全可匹配），见 1.9。
  - 同一工序可跨部门：实测 P01~P17 每道工序都出现过 5 个部门的操作记录，禁止把工序固定映射为某个部门。
  - 按部门统计时**直接 GROUP BY `fact_process_record.department`，不要再 JOIN `dim_employee`**：
    该列已按 `operator_id` 回填；再 JOIN 会因员工主数据多行/改名而放大或改变口径。

---

## 2. 四条强制规则（埋点陷阱，违反即判错）

1. **客户聚合永远走 `dim_customer.customer_id`。**
   JOIN dim_customer 后 SELECT/GROUP BY 用 `customer_id`（展示可附带 `customer_name` 规范名）；
   **禁止直接用 `fact_sales_order.customer_name_raw` 分组**——单据上有 28 种脏写法，规范客户只有 12 个，
   直接按脏名字段 GROUP BY 会把同一客户拆成多行（Top5 客户题必须只剩 12 个规范名以内的行）。

2. **算产出 / 良率必须排除返工重过站：`is_rework_pass = 0`。**
   不排除则 `SUM(good_panels)` 把返工后的产出重复累计，虚增约 0.8%~1%，FPY/RTY/报废率全部失真。

3. **统计“已完工”必须排除在制批次，且要整批排除——但只对质量水平类指标（RTY/FPY/良率）生效。**
   **月度报废率、返工占比等记录级活动比率不要套这条**：它们按 `out_time` 归期，只取 `is_rework_pass=0`，
   不加 `lot_status` 过滤（加了会漏掉在制批当月已出站记录，实测返工占比 512→255）。
   在制判定：`fact_process_record.out_time IS NULL`（未出站记录共 1050 行、涉及 218 个批次），
   或工单表 `fact_work_order.lot_status = '在制'`（218 批；已完工 1582 批）。两者实测一一对应。
   正确做法：用 `lot_id IN (SELECT lot_id FROM fact_work_order WHERE lot_status='已完工')`
   或 NOT EXISTS(out_time IS NULL) **把在制批的所有记录一起排除**；
   不能只加 `WHERE out_time IS NOT NULL`——在制批已出站工序的记录仍会混入，算出“半成品 RTY”。
   另注意：在制批未来工序记录虽有 out_time NULL，但 input/good 已预填数值，不能靠“数值缺失/缺站”识别。

4. **批次号 `lot_id` 有两种格式，只按 ID 等值关联，禁止截切匹配。**
   `WO%` 前缀 600 个（如 WO000001）、`26%` 前缀 1200 个（如 260600002），合计 1800，无其他格式。
   过站、缺陷、电测、出货行全部通过 `lot_id` 等值 JOIN，不许用 substr/like 前缀拼接。

---

## 3. SQLite 方言红线

- 日期只用 `date() / datetime() / julianday() / strftime()` 及 `date('2026-09-24','-6 day')` 这类修饰符。
- 禁用 PostgreSQL 专属写法：`::` 类型转换、`INTERVAL '7 days'`、`DATE_TRUNC`、`ILIKE`、`NOW()`、`generate_series`。
- 只输出**一条** SELECT/WITH 语句；非聚合明细查询必须带 `LIMIT 100`（具体要求见生成 prompt）。
