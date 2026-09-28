# 语义层 · 指标口径与强制规则

> 本文件是生成 SQL 的**业务口径权威**，所有 SQL 必须严格遵守。
> 列名真实可执行（已对 `data/ic_substrate.db` 校验）；SQL 方言为 **SQLite**。

---

## 0. 时间基准与通用约定

- 数据时间窗：**2026-06-01 ~ 2026-09-24**；统一业务基准日 **ANCHOR = '2026-09-24'**（数据窗口末日）。
- 应收账龄另有一个**可配置快照日 `SNAPSHOT_DATE`**（阶段 D 在 `app/config.py` 实现，默认 `'2026-09-24'`），
  用于按财务观察时点重算账龄（如切到月末 `'2026-10-31'` 演示逾期）；其余指标只用固定 ANCHOR。
- 问题中的“今天 / 当前 / 本月 / 最近一周 / 最近一个月 / 今年”，一律相对基准日，**禁止用 `date('now')`**（真实运行日晚于数据窗，会查空）：
  - 本月：`date(out_time) >= date('2026-09-24','start of month')`（即 2026-09-01 起）
  - 最近一周：`date(x) >= date('2026-09-24','-6 day')`（含基准日共 7 天）
  - 最近一个月：`date(x) >= date('2026-09-24','-29 day')`（含基准日共 30 天）
  - 今年：`date(x) >= '2026-01-01'`
- 过站/生产类指标的时间列用 **`out_time`（出站时间）**，在制记录 out_time 为 NULL，天然不计入已发生产出。
- 数量单位：订单/出货为**颗**（order_qty、ship_qty_units、units_per_panel）；过站为**排板 panels**（input_panels 等），不要跨口径混算。
- 比例计算先乘 `1.0` 避免 SQLite 整数除法，例如 `SUM(good_panels)*1.0/SUM(input_panels)`。
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

### 1.5 返工占比
```
返工量占比 = SUM(rework_panels) / SUM(input_panels)  -- 主记录(is_rework_pass=0)上的一次返工数
```
`is_rework_pass = 1` 的记录表示“返工后的重过站过程”，用于统计返工规模/次数，其 good_panels 不得计入一次产出。

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

### 1.8 DPPM 百万不良数
```
DPPM = 不良数 / 交付数 × 10^6
```
- 客诉口径：`fact_customer_complaint.dppm` 有**现成字段**，按 customer_id / product_id 聚合即可。
- 电测口径：不良 = `tested_panels - pass_panels`（= open_fail + short_fail + isolation_fail），
  `DPPM = (tested_panels - pass_panels) * 1.0 / tested_panels * 1000000`。

### 1.9 电测（P15）通过率
```
电测通过率 = SUM(pass_panels) * 1.0 / SUM(tested_panels)     -- 表 fact_etest，电测即工序 P15
```

### 1.10 库存数量 / 金额
- 数量：`fact_inventory.qty_on_hand`，按 `stock_type`（原料/成品）、`warehouse`、`material_id` 聚合。
- 金额：**只有原料可算**（8 种原料行），`qty_on_hand * dim_material.standard_cost`，按 material_id JOIN。
- 成品库存 600 行无成本/产品列，**只报数量**，禁止硬 JOIN 造金额。

### 1.11 应收账龄 / 逾期金额（双向账龄表，锚点可配置）
- **账龄只分析未结清账款**：`status IN ('部分','未收')`（共 99 笔），未收余额 = `amount - received_amount`；
  已结清 200 笔余额为 0，不入账龄表（标准会计口径）。
- 快照日 = `SNAPSHOT_DATE`（默认 **'2026-09-24'**，由 config 注入；下文 SQL 用字面日期示例）。
  ⚠️ 实测数据事实：99 笔的 due_date 全部 ≥ 2026-09-24（分布 9-24 ~ 11-23），
  故 **9-24 快照下逾期 = 0 笔 / 0 元，未到期余额 738,598.07 元——这是正确结果，不是查询出错**。
  切到月末快照 `SNAPSHOT_DATE='2026-10-31'` 时实测：逾期 54 笔 / 389,373.20 元（验收演示用）。
- 注意 receive_date 含晚于 9-24 的日期（数据按 11 月全知视角生成），账龄**只看 due_date 与 status，不看 receive_date**。
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

---

## 2. 四条强制规则（埋点陷阱，违反即判错）

1. **客户聚合永远走 `dim_customer.customer_id`。**
   JOIN dim_customer 后 SELECT/GROUP BY 用 `customer_id`（展示可附带 `customer_name` 规范名）；
   **禁止直接用 `fact_sales_order.customer_name_raw` 分组**——单据上有 28 种脏写法，规范客户只有 12 个，
   直接按脏名字段 GROUP BY 会把同一客户拆成多行（Top5 客户题必须只剩 12 个规范名以内的行）。

2. **算产出 / 良率必须排除返工重过站：`is_rework_pass = 0`。**
   不排除则 `SUM(good_panels)` 把返工后的产出重复累计，虚增约 0.8%~1%，FPY/RTY/报废率全部失真。

3. **统计“已完工”必须排除在制批次，且要整批排除。**
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
