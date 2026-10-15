# 语义层 · 高频问法 → 正确 SQL 范例（20 条）

> 每条范例均已通过 `scripts/check_few_shot.py` 在 `data/ic_substrate.db` 只读实测。
> 结构固定（`### FSxx` 分节 + 元信息行 + ```sql 代码块 + 为什么），供 schema_linker 按涉及表挑选后拼入生成 prompt。
> 通用约定：业务基准日 **2026-09-24**（运行时动态探测数据最新日，示例日期会自动替换为当前锚点；禁用 `date('now')`）；销售额只算 `order_type='正式单'`；
> 比例乘 `1.0` 防整数除法；表别名固定 `o=订单头 i=订单行 s=出货头 si=出货行 w=工单 p=过站 pr=工序/产品 c=客户`。

---

### FS01. 最近一个月各客户销售金额排名
- 涉及表: fact_sales_order, fact_sales_order_item, dim_customer
- 规则: R1客户按customer_id、只算正式单、基准日锚定
- 适用问法: 客户 销售额 销售金额 金额排名 最近一个月 近一月

```sql
SELECT c.customer_id,
       c.customer_name,
       ROUND(SUM(i.order_qty * i.unit_price), 2) AS amount
FROM fact_sales_order o
JOIN fact_sales_order_item i ON i.order_id = o.order_id
JOIN dim_customer c ON c.customer_id = o.customer_id
WHERE o.order_type = '正式单'
  AND o.order_date >= date('2026-09-24', '-29 day')   -- 最近30天，不用 date('now')
GROUP BY c.customer_id, c.customer_name               -- R1：按规范客户聚合
ORDER BY amount DESC;
```
- 为什么：金额 = 订单行 数量×单价；必须 JOIN dim_customer 用 customer_id 聚合，
  绝不能 GROUP BY customer_name_raw（28 种脏写法会把同一客户拆成多行）；forecast 是预测单不计销售额。

---

### FS02. 某规范客户今年的订单数与总额
- 涉及表: fact_sales_order, fact_sales_order_item, dim_customer
- 规则: R1、只算正式单、规范名反查ID
- 适用问法: 某客户 客户名 今年订单 订单数 订单总额 韦尔 海思

```sql
SELECT c.customer_id,
       c.customer_name,
       COUNT(DISTINCT o.order_id) AS order_cnt,
       ROUND(SUM(i.order_qty * i.unit_price), 2) AS amount
FROM fact_sales_order o
JOIN fact_sales_order_item i ON i.order_id = o.order_id
JOIN dim_customer c ON c.customer_id = o.customer_id
WHERE o.order_type = '正式单'
  AND o.order_date >= '2026-01-01'
  AND c.customer_name = '韦尔股份豪威'      -- 用规范名在 dim_customer 反查，禁止匹配 raw 脏名
GROUP BY c.customer_id, c.customer_name;
```
- 为什么：业务说客户名时，先在 dim_customer 用规范名定位 customer_id（12 个规范名之一）；
  订单数要 COUNT(DISTINCT order_id)（一单多行），金额在订单行上汇总。
  实测：韦尔股份豪威 38 张正式订单 / 56 条订单行 / 516,411.00 元。

---

### FS03. 各产品类型销售额占比
- 涉及表: fact_sales_order, fact_sales_order_item, dim_product
- 规则: 只算正式单、占比分母用窗口合计
- 适用问法: 产品类型 销售额占比 占比 FCBGA FCCSP 销售结构

```sql
WITH t AS (
  SELECT pr.product_type,
         SUM(i.order_qty * i.unit_price) AS amt
  FROM fact_sales_order o
  JOIN fact_sales_order_item i ON i.order_id = o.order_id
  JOIN dim_product pr ON pr.product_id = i.product_id
  WHERE o.order_type = '正式单'
  GROUP BY pr.product_type
)
SELECT product_type,
       ROUND(amt, 2) AS amount,
       ROUND(amt * 100.0 / SUM(amt) OVER (), 2) AS pct   -- SQLite 支持窗口函数
FROM t
ORDER BY amt DESC;
```
- 为什么：产品维度在订单行 item 上（item.product_id），不是订单头；占比分母是全体正式单销售额，
  用 `SUM() OVER()` 一次算出，避免二次扫描。实测：FCBGA 253.9 万占比最高。

---

### FS04. 销售额 Top5 客户（同一客户不得出现多行）
- 涉及表: fact_sales_order, fact_sales_order_item, dim_customer
- 规则: R1、只算正式单
- 适用问法: top5 销售额最高 大客户 客户排名 前五

```sql
SELECT c.customer_id,
       c.customer_name,
       ROUND(SUM(i.order_qty * i.unit_price), 2) AS amount
FROM fact_sales_order o
JOIN fact_sales_order_item i ON i.order_id = o.order_id
JOIN dim_customer c ON c.customer_id = o.customer_id
WHERE o.order_type = '正式单'
GROUP BY c.customer_id, c.customer_name
ORDER BY amount DESC
LIMIT 5;
```
- 为什么：验收点是结果最多 5 行、每行都是规范客户；若按 customer_name_raw 分组，
  Top5 会出现同一客户的多个脏写法各占一行（直接判错）。

---

### FS05. 某工单（批次）整体 RTY
- 涉及表: fact_process_record, fact_work_order
- 规则: R2过滤返工、R3已完工、17站连乘、按lot_id等值过滤
- 适用问法: 工单 批次 lot RTY 直通率 整体良率 260600002 WO000001

```sql
WITH fpy AS (
  SELECT process_id,
         good_panels * 1.0 / input_panels AS f
  FROM fact_process_record
  WHERE lot_id = '260600002'        -- 批次号直接等值匹配，WO/26 两种格式都一样，不截切
    AND is_rework_pass = 0          -- R2：排除返工重过站
)
SELECT '260600002' AS lot_id,
       (SELECT lot_status FROM fact_work_order WHERE lot_id = '260600002') AS lot_status,
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
HAVING COUNT(DISTINCT process_id) = 17;   -- 17 站齐全才给完整 RTY；在制批应提示未完工
```
- 为什么：RTY = 全部 17 道工序主记录 FPY 连乘（不是 11 道 is_key_process）；
  SQLite 无 PRODUCT 聚合，用 MAX(CASE) 透视相乘；若查出 lot_status='在制'，不返回完整 RTY，只答阶段直通率。

---

### FS06. 最近完工工单 RTY 中位数（基线≈82%）
- 涉及表: fact_process_record, fact_work_order
- 规则: R2、R3整批排除在制、17站、逐批次RTY再取中位数
- 适用问法: RTY 中位数 完工工单 全厂良率 滚动直通率 平均RTY

```sql
WITH fpy AS (
  SELECT p.lot_id,
         p.process_id,
         p.good_panels * 1.0 / p.input_panels AS f
  FROM fact_process_record p
  JOIN fact_work_order w ON w.lot_id = p.lot_id
  WHERE w.lot_status = '已完工'      -- R3：必须显式限定已完工，不能靠缺站判断（在制批数值已预填）
    AND p.is_rework_pass = 0
),
lot_rty AS (
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
  HAVING COUNT(DISTINCT process_id) = 17
),
ordered AS (
  SELECT rty,
         ROW_NUMBER() OVER (ORDER BY rty) AS rn,
         COUNT(*) OVER () AS n
  FROM lot_rty
),
mid AS (
  SELECT rty FROM ordered WHERE rn IN ((n + 1) / 2, (n + 2) / 2)   -- 偶数批取中间两值平均
)
SELECT (SELECT COUNT(*) FROM lot_rty) AS lot_cnt,
       ROUND((SELECT AVG(rty) FROM lot_rty), 4) AS avg_rty,
       ROUND(AVG(mid.rty), 4) AS median_rty
FROM mid;
```
- 为什么：先逐批次算 RTY 再对批次集合取中位数，不能把全厂投入产出汇总后只连乘一次。
  实测：1582 批，均值 0.8435，**中位数 0.8237 ≈ 82%**（若算成 0.85 说明误用 11 站或混入在制批）。

---

### FS07. 各工序 FPY 排名，找出最差工序
- 涉及表: fact_process_record, dim_process, fact_work_order
- 规则: R2、R3、工序级FPY用SUM后相除
- 适用问法: 工序 工站 FPY 排名 最差 最弱 瓶颈工序 一次通过率

```sql
SELECT pr.process_seq,
       p.process_id,
       pr.process_name,
       SUM(p.input_panels) AS input_panels,
       ROUND(SUM(p.good_panels) * 1.0 / SUM(p.input_panels), 4) AS fpy
FROM fact_process_record p
JOIN fact_work_order w ON w.lot_id = p.lot_id
JOIN dim_process pr ON pr.process_id = p.process_id
WHERE w.lot_status = '已完工'
  AND p.is_rework_pass = 0
GROUP BY p.process_id, pr.process_seq, pr.process_name
ORDER BY fpy ASC;                  -- 升序：第一行即最差工序
```
- 为什么：工序级 FPY 是“先按工序 SUM(good)/SUM(input) 再相除”，不是各批次 FPY 简单平均；
  实测最差为 P09 外层图形（0.9760），其次 P15 电测（0.9776）。

---

### FS08. 本月报废率
- 涉及表: fact_process_record
- 规则: R2过滤返工、时间按out_time、基准日锚定
- 适用问法: 本月 报废率 报废 scrap 9月

```sql
SELECT SUM(scrap_panels) AS scrap_panels,
       SUM(input_panels) AS input_panels,
       ROUND(SUM(scrap_panels) * 1.0 / SUM(input_panels), 4) AS scrap_rate
FROM fact_process_record
WHERE is_rework_pass = 0
  AND date(out_time) >= date('2026-09-24', 'start of month');   -- 2026-09-01 起
```
- 为什么：报废率 = scrap/input；生产时间口径用出站时间 out_time；过滤返工重过站。
  实测 2026-09：6,917 口径月份下 1309/156141 ≈ 0.84%。

---

### FS09. FCBGA 与 FCCSP 平均 RTY 对比（FCBGA 应偏低）
- 涉及表: fact_process_record, fact_work_order, dim_product
- 规则: R2、R3、17站连乘、按product_type聚合
- 适用问法: FCBGA FCCSP 对比 RTY 良率对比 产品类型良率

```sql
WITH fpy AS (
  SELECT p.lot_id,
         p.process_id,
         p.good_panels * 1.0 / p.input_panels AS f
  FROM fact_process_record p
  JOIN fact_work_order w ON w.lot_id = p.lot_id
  WHERE w.lot_status = '已完工'
    AND p.is_rework_pass = 0
),
lot_rty AS (
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
  HAVING COUNT(DISTINCT process_id) = 17
)
SELECT pr.product_type,
       COUNT(*) AS lot_cnt,
       ROUND(AVG(r.rty), 4) AS avg_rty
FROM lot_rty r
JOIN fact_work_order w ON w.lot_id = r.lot_id
JOIN dim_product pr ON pr.product_id = w.product_id
WHERE pr.product_type IN ('FCBGA', 'FCCSP')
GROUP BY pr.product_type
ORDER BY avg_rty DESC;
```
- 为什么：产品类型经 fact_work_order.product_id → dim_product 关联；
  实测 FCBGA 0.8025（826 批）< FCCSP 0.8305（422 批），FCBGA 层数多工艺难、良率偏低，结论与业务预期一致。

---

### FS10. 当前在制批次数（基线 218）
- 涉及表: fact_work_order
- 规则: R3、lot_status口径
- 适用问法: 在制 在制品 未完工 批次数 当前多少批 WIP

```sql
SELECT COUNT(*) AS wip_lots
FROM fact_work_order
WHERE lot_status = '在制';
```
- 为什么：标准答案走工单状态（实测 218）；等价口径是
  `SELECT COUNT(DISTINCT lot_id) FROM fact_process_record WHERE out_time IS NULL`（同样 218），
  两口径实测一一对应。注意 NULL 记录有 1050 行但只涉及 218 个批次，不能直接数行数。

---

### FS11. 返工量占总投入比例
- 涉及表: fact_process_record
- 规则: R2、主记录口径
- 适用问法: 返工 返工量 返工率 重过站 占比

```sql
SELECT SUM(rework_panels) AS rework_panels,
       SUM(input_panels) AS input_panels,
       ROUND(SUM(rework_panels) * 1.0 / SUM(input_panels), 4) AS rework_rate
FROM fact_process_record
WHERE is_rework_pass = 0;
```
- 为什么：返工量取主记录上的 rework_panels（一次检验就判的返工数），实测 2,677 / 833,444 ≈ 0.32%；
  is_rework_pass=1 的 1391 行是“返工后重过站过程”，用于统计返工次数，其产出不得计入一次良率。

---

### FS12. 本月准时交付率 OTD
- 涉及表: fact_shipment, fact_sales_order_item
- 规则: ship_date≤MIN(due_date)、基准日锚定
- 适用问法: 本月 OTD 准时交付率 准时率 交付达成

```sql
SELECT COUNT(*) AS ship_cnt,
       SUM(CASE WHEN s.ship_date <= d.due_date THEN 1 ELSE 0 END) AS on_time_cnt,
       ROUND(SUM(CASE WHEN s.ship_date <= d.due_date THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS otd
FROM fact_shipment s
JOIN (
  SELECT order_id, MIN(due_date) AS due_date      -- fact_shipment 无计划日，交期取订单行最早 due_date
  FROM fact_sales_order_item
  GROUP BY order_id
) d ON d.order_id = s.order_id
WHERE s.ship_date >= date('2026-09-24', 'start of month');
```
- 为什么：fact_shipment 没有 scheduled_date；一单多行时按订单粒度取 MIN(due_date) 为应出货日。
  实测 2026-09：118 批中准时 41 批，OTD≈34.75%（全期 165/301≈54.82%）。

---

### FS13. 哪些客户订单延迟交付过
- 涉及表: fact_shipment, fact_sales_order_item, dim_customer
- 规则: R1、ship_date>due_date
- 适用问法: 延迟 延期 逾期交付 迟交 哪些客户 延误

```sql
SELECT c.customer_id,
       c.customer_name,
       COUNT(DISTINCT s.order_id) AS late_orders
FROM fact_shipment s
JOIN (
  SELECT order_id, MIN(due_date) AS due_date
  FROM fact_sales_order_item
  GROUP BY order_id
) d ON d.order_id = s.order_id
JOIN dim_customer c ON c.customer_id = s.customer_id
WHERE s.ship_date > d.due_date
GROUP BY c.customer_id, c.customer_name
ORDER BY late_orders DESC;
```
- 为什么：延迟判定与 OTD 相反（实际出货晚于订单交期）；客户按 customer_id 聚合展示规范名。

---

### FS14. 最近一周实际出货量 vs 计划出货量
- 涉及表: fact_shipment, fact_shipment_item, fact_sales_order_item
- 规则: 基准日锚定、计划侧近似口径需注明
- 适用问法: 最近一周 近一周 实际出货 计划出货 出货量 对比 达成

```sql
SELECT
  (SELECT COALESCE(SUM(si.ship_qty_units), 0)
   FROM fact_shipment s
   JOIN fact_shipment_item si ON si.shipment_id = s.shipment_id
   WHERE s.ship_date BETWEEN date('2026-09-24', '-6 day') AND '2026-09-24'
  ) AS actual_units,
  (SELECT COALESCE(SUM(i.order_qty), 0)
   FROM fact_sales_order_item i
   WHERE i.due_date BETWEEN date('2026-09-24', '-6 day') AND '2026-09-24'
  ) AS planned_units;
```
- 为什么：实际出货走出货行 ship_qty_units；库里没有独立“计划出货量表”，
  计划侧用“订单交期 due_date 落在本周的订单数量 order_qty”近似（面试需主动说明这是近似口径）。
  实测：实际 732,240 颗 / 计划 900,000 颗（36 批）。

---

### FS15. 缺陷类型频次 Top10
- 涉及表: fact_defect, dim_defect_code
- 规则: 按defect_code关联字典取规范名
- 适用问法: 缺陷 不良 频次 top10 缺陷类型 不良排行 AOI

```sql
SELECT dc.defect_code,
       dc.defect_name,
       dc.defect_category,
       SUM(d.defect_qty) AS defect_qty
FROM fact_defect d
JOIN dim_defect_code dc ON dc.defect_code = d.defect_code
GROUP BY dc.defect_code, dc.defect_name, dc.defect_category
ORDER BY defect_qty DESC
LIMIT 10;
```
- 为什么：fact_defect 只有缺陷代码，中文名/分类必须 JOIN dim_defect_code；
  频次按 defect_qty 求和（记录条数会低估批量缺陷）。实测第一为“孔位偏移”641。

---

### FS16. 电测（P15）通过率
- 涉及表: fact_etest
- 规则: pass/tested口径
- 适用问法: 电测 电性测试 飞针 P15 通过率 电测良率

```sql
SELECT COUNT(*) AS test_lots,
       SUM(tested_panels) AS tested_panels,
       SUM(pass_panels) AS pass_panels,
       ROUND(SUM(pass_panels) * 1.0 / SUM(tested_panels), 4) AS pass_rate,
       ROUND((SUM(tested_panels) - SUM(pass_panels)) * 1.0 / SUM(tested_panels) * 1000000, 0) AS dppm
FROM fact_etest;
```
- 为什么：电测结果直接查 fact_etest（每行一个批次），通过率 = pass_panels/tested_panels；
  不良 = tested − pass（= open+short+isolation）。实测全期 40,164/41,086 = 97.76%（本月 97.54%）。

---

### FS17. 某客户 / 批次 DPPM
- 涉及表: fact_customer_complaint, dim_customer
- 规则: 优先用现成dppm字段、R1
- 适用问法: DPPM 百万不良 客诉 客户品质 批次不良

```sql
SELECT c.customer_id,
       c.customer_name,
       COUNT(*) AS complaint_cnt,
       ROUND(AVG(x.dppm), 1) AS avg_dppm,
       MAX(x.dppm) AS max_dppm
FROM fact_customer_complaint x
JOIN dim_customer c ON c.customer_id = x.customer_id
GROUP BY c.customer_id, c.customer_name
ORDER BY avg_dppm DESC;
```
- 为什么：客诉表已有统计好的 dppm 字段（28 条客诉），按客户聚合取均值/峰值即可，不要自己重造分母；
  若问“某批次制程 DPPM”，改用电测表：(tested_panels−pass_panels)*1.0/tested_panels*1000000。

---

### FS18. 停机时间最长 Top5 设备及原因
- 涉及表: fact_equipment_downtime, dim_equipment
- 规则: 按equipment_id关联设备名
- 适用问法: 停机 停机最长 设备停机 停机原因 top5 OEE 故障 换型

```sql
SELECT e.equipment_id,
       e.equipment_name,
       SUM(dt.duration_min) AS total_down_min,
       COUNT(*) AS down_times,
       (SELECT r2.reason
        FROM fact_equipment_downtime r2
        WHERE r2.equipment_id = dt.equipment_id
        ORDER BY r2.duration_min DESC
        LIMIT 1) AS longest_reason
FROM fact_equipment_downtime dt
JOIN dim_equipment e ON e.equipment_id = dt.equipment_id
GROUP BY e.equipment_id, e.equipment_name
ORDER BY total_down_min DESC
LIMIT 5;
```
- 为什么：问“停机最长的设备”按设备汇总停机时长（相关子查询取该设备最长一次停机的原因）；
  若问“最长的单次停机事件”，则不 GROUP BY，直接按 duration_min DESC 取明细
  （单次最长 480 分钟，多条并列）。

---

### FS19. 各类物料库存数量 / 金额
- 涉及表: fact_inventory, dim_material
- 规则: 仅原料算金额、成品只报数量
- 适用问法: 库存 存货 物料 库存数量 库存金额 原料 成品 仓库

```sql
SELECT i.stock_type,
       m.material_id,
       m.material_name,
       m.category,
       SUM(i.qty_on_hand) AS qty_on_hand,
       ROUND(SUM(i.qty_on_hand * COALESCE(m.standard_cost, 0)), 2) AS amount
FROM fact_inventory i
LEFT JOIN dim_material m ON m.material_id = i.material_id
GROUP BY i.stock_type, m.material_id, m.material_name, m.category
ORDER BY i.stock_type, amount DESC;
```
- 为什么：dim_material 只有 8 种原料有 standard_cost，故用 LEFT JOIN + COALESCE：
  原料行金额 = 数量×标准成本；成品行 material_id 关联不到、金额为 0（成品无成本列，只报数量）。
  实测：原料 2,463 单位、成品 2,823,720 颗。

---

### FS20. 各客户应收账龄 / 逾期金额
- 涉及表: fact_receivable, dim_customer
- 规则: R1、只看未结清、SNAPSHOT_DATE配置化、双向账龄
- 适用问法: 应收 账龄 逾期 未收 欠款 回款 账期

```sql
SELECT c.customer_id,
       c.customer_name,
       COUNT(*) AS bills,
       ROUND(SUM(x.bal), 2) AS outstanding,
       ROUND(SUM(CASE WHEN x.d BETWEEN 1  AND 30 THEN x.bal ELSE 0 END), 2) AS overdue_1_30,
       ROUND(SUM(CASE WHEN x.d BETWEEN 31 AND 60 THEN x.bal ELSE 0 END), 2) AS overdue_31_60,
       ROUND(SUM(CASE WHEN x.d BETWEEN 61 AND 90 THEN x.bal ELSE 0 END), 2) AS overdue_61_90,
       ROUND(SUM(CASE WHEN x.d > 90             THEN x.bal ELSE 0 END), 2) AS overdue_90p,
       SUM(CASE WHEN x.nd BETWEEN 0  AND 7  THEN 1 ELSE 0 END) AS due_in_0_7,
       SUM(CASE WHEN x.nd BETWEEN 8  AND 30 THEN 1 ELSE 0 END) AS due_in_8_30,
       SUM(CASE WHEN x.nd BETWEEN 31 AND 60 THEN 1 ELSE 0 END) AS due_in_31_60
FROM (
  SELECT r.customer_id,
         r.amount - r.received_amount AS bal,
         CASE WHEN r.due_date < '2026-09-24'
              THEN CAST(julianday('2026-09-24') - julianday(r.due_date) AS INT) END AS d,
         CASE WHEN r.due_date >= '2026-09-24'
              THEN CAST(julianday(r.due_date) - julianday('2026-09-24') AS INT) END AS nd
  FROM fact_receivable r
  WHERE r.status IN ('部分', '未收')
) x
JOIN dim_customer c ON c.customer_id = x.customer_id
GROUP BY c.customer_id, c.customer_name
ORDER BY outstanding DESC;
```
- 为什么：账龄只分析未结清账款（99 笔）；快照日由 config.SNAPSHOT_DATE 注入（默认 2026-09-24）。
  默认快照下所有账款尚未到期（逾期四桶为 0 是正确结果，未到期 738,598.07 元）；
  切到 2026-10-31 时逾期 389,373.20 元。receive_date 含未来日期，不参与账龄计算。
