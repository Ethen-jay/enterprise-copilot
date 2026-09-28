# 语义层 · 22 表结构说明书（自动生成，请勿手改）

> - 列名/类型/注释来源：`data_model/schema_postgres.sql`（PostgreSQL DDL，列名唯一权威）
> - 行数来源：`data/ic_substrate.db` 只读实测；重新生成：`python scripts/build_semantic.py`
> - 生成时间：2026-09-27 22:09；数据时间窗：**2026-06-01 ~ 2026-09-24**
>
> **方言提醒（写 SQL 必读）**：DDL 是 PostgreSQL 类型（VARCHAR/NUMERIC/SMALLINT/TIMESTAMP），
> 但执行库是 **SQLite**。生成 SQL 必须用 SQLite 方言：日期处理用 `date()/datetime()/strftime()`，
> 禁止使用 PostgreSQL 专属语法（`::` 类型转换、`INTERVAL`、`DATE_TRUNC`、`ILIKE` 等）。


## 一、主数据表（dim_ 开头）（10 张）

### 1. dim_customer —— 客户=fabless芯片公司;销售订单中存在客户名脏写法

实测 **12** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| customer_id | VARCHAR(12) | PK |  |
| customer_name | VARCHAR(80) | NOT NULL | 规范名 |
| region | VARCHAR(20) |  | 华东/华南/华北/海外 |
| customer_tier | VARCHAR(10) |  | 战略/重点/普通 |
| sales_contact | VARCHAR(40) |  |  |

### 2. dim_supplier

实测 **7** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| supplier_id | VARCHAR(12) | PK |  |
| supplier_name | VARCHAR(80) |  |  |
| material_category | VARCHAR(30) |  | ABF膜/铜箔/干膜/药水/钻针/阻焊 |
| region | VARCHAR(20) |  |  |
| rating | VARCHAR(10) |  |  |

### 3. dim_material

实测 **8** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| material_id | VARCHAR(12) | PK |  |
| material_name | VARCHAR(60) |  |  |
| category | VARCHAR(20) |  | core芯板/PP半固化片/铜箔/ABF膜/干膜/阻焊/药水 |
| unit | VARCHAR(10) |  |  |
| standard_cost | NUMERIC(10,2) |  |  |

### 4. dim_product

实测 **8** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| product_id | VARCHAR(14) | PK |  |
| product_name | VARCHAR(60) |  |  |
| product_type | VARCHAR(12) |  | FCBGA/FCCSP/WBCSP/RF |
| layer_count | INT |  | 层数 |
| body_size_mm | VARCHAR(20) |  |  |
| line_width_um | INT |  |  |
| line_space_um | INT |  |  |
| surface_finish | VARCHAR(16) |  | ENEPIG/OSP/ENIG/软金/沉银IT |
| core_material | VARCHAR(8) |  | ABF/BT |
| units_per_panel | INT |  | 每排板产出颗数 |

### 5. dim_process

实测 **17** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| process_id | VARCHAR(6) | PK |  |
| process_seq | INT | NOT NULL |  |
| process_name | VARCHAR(40) |  |  |
| process_stage | VARCHAR(16) |  | 内层/压合/钻孔/沉铜电镀/外层/阻焊/表处/成型/电测/终检包装 |
| is_key_process | SMALLINT |  |  |
| avg_cycle_min | INT |  |  |

### 6. dim_line

实测 **3** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| line_id | VARCHAR(8) | PK |  |
| line_name | VARCHAR(30) |  |  |
| factory | VARCHAR(20) |  | 秦皇岛一厂/秦皇岛二厂 |
| product_scope | VARCHAR(40) |  |  |

### 7. dim_equipment

实测 **51** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| equipment_id | VARCHAR(10) | PK |  |
| equipment_name | VARCHAR(50) |  |  |
| process_id | VARCHAR(6) | FK→dim_process |  |
| line_id | VARCHAR(8) | FK→dim_line |  |
| equipment_model | VARCHAR(40) |  |  |
| status | VARCHAR(10) |  | 运行/维修/停用 |
| install_date | DATE |  |  |

### 8. dim_employee

实测 **40** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| employee_id | VARCHAR(10) | PK |  |
| employee_name | VARCHAR(30) |  |  |
| department | VARCHAR(20) |  | 制造/品质/设备/生管/业务 |
| role | VARCHAR(20) |  |  |

### 9. dim_shift

实测 **2** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| shift_id | VARCHAR(4) | PK |  |
| shift_name | VARCHAR(10) |  | 白班/夜班 |
| start_time | VARCHAR(8) |  |  |
| end_time | VARCHAR(8) |  |  |

### 10. dim_defect_code

实测 **19** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| defect_code | VARCHAR(14) | PK |  |
| defect_name | VARCHAR(40) |  |  |
| process_id | VARCHAR(6) | FK→dim_process |  |
| defect_category | VARCHAR(16) |  | 开路/短路/外观/尺寸/孔/铜/阻焊/表面处理 |


## 二、ERP 事实表（销售/出货/库存/应收）（6 张）

### 11. fact_sales_order

实测 **420** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| order_id | VARCHAR(14) | PK |  |
| customer_id | VARCHAR(12) | FK→dim_customer |  |
| customer_name_raw | VARCHAR(80) |  | 单据上的原始客户名(含脏写法) |
| order_date | DATE |  |  |
| order_type | VARCHAR(10) |  | forecast/正式单 |
| currency | VARCHAR(6) |  |  |
| order_status | VARCHAR(10) |  |  |

### 12. fact_sales_order_item

实测 **628** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| item_id | VARCHAR(14) | PK |  |
| order_id | VARCHAR(14) | FK→fact_sales_order |  |
| product_id | VARCHAR(14) | FK→dim_product |  |
| order_qty | INT |  | 颗 |
| unit_price | NUMERIC(10,4) |  |  |
| due_date | DATE |  |  |

### 13. fact_shipment

实测 **301** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| shipment_id | VARCHAR(14) | PK |  |
| order_id | VARCHAR(14) | FK→fact_sales_order |  |
| customer_id | VARCHAR(12) |  |  |
| ship_date | DATE |  |  |
| status | VARCHAR(10) |  |  |

### 14. fact_shipment_item

实测 **1,582** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| ship_item_id | VARCHAR(14) | PK |  |
| shipment_id | VARCHAR(14) | FK→fact_shipment |  |
| product_id | VARCHAR(14) |  |  |
| lot_id | VARCHAR(16) |  |  |
| ship_qty_units | INT |  |  |

### 15. fact_inventory

实测 **608** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| inventory_id | VARCHAR(12) | PK |  |
| material_id | VARCHAR(12) |  |  |
| stock_type | VARCHAR(10) |  | 原料/成品 |
| lot_no | VARCHAR(16) |  |  |
| warehouse | VARCHAR(20) |  |  |
| qty_on_hand | INT |  |  |
| update_date | DATE |  |  |

### 16. fact_receivable

实测 **301** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| receivable_id | VARCHAR(12) | PK |  |
| order_id | VARCHAR(14) |  |  |
| customer_id | VARCHAR(12) |  |  |
| amount | NUMERIC(14,2) |  |  |
| due_date | DATE |  |  |
| received_amount | NUMERIC(14,2) |  |  |
| receive_date | DATE |  |  |
| status | VARCHAR(12) |  | 已结清/部分/未收 |


## 三、MES 事实表（工单/过站/品质/设备/客诉）（6 张）

### 17. fact_work_order

实测 **1,800** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| lot_id | VARCHAR(16) | PK | 批次号(两种格式混用) |
| item_id | VARCHAR(14) | FK→fact_sales_order_item |  |
| product_id | VARCHAR(14) |  |  |
| line_id | VARCHAR(8) |  |  |
| planned_panels | INT |  |  |
| release_date | DATE |  |  |
| planned_end | DATE |  |  |
| lot_status | VARCHAR(8) |  | 已完工/在制 |
| priority | VARCHAR(8) |  |  |

### 18. fact_process_record

实测 **31,991** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| record_id | BIGINT | PK |  |
| lot_id | VARCHAR(16) |  |  |
| process_id | VARCHAR(6) |  |  |
| equipment_id | VARCHAR(10) |  |  |
| shift_id | VARCHAR(4) |  |  |
| operator_id | VARCHAR(10) |  |  |
| in_time | TIMESTAMP |  |  |
| out_time | TIMESTAMP |  | 在制批次当前工序为NULL |
| input_panels | INT |  |  |
| good_panels | INT |  | 主记录=一次通过;返工过站is_rework_pass=1 |
| scrap_panels | INT |  |  |
| rework_panels | INT |  |  |
| is_rework_pass | SMALLINT |  | 1=返工重过站(口径陷阱) |
| remark | VARCHAR(30) |  |  |

### 19. fact_defect

实测 **4,761** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| defect_record_id | BIGINT | PK |  |
| record_id | BIGINT |  |  |
| lot_id | VARCHAR(16) |  |  |
| process_id | VARCHAR(6) |  |  |
| defect_code | VARCHAR(14) |  |  |
| defect_qty | INT |  |  |
| detect_method | VARCHAR(10) |  | AOI/电测/目检/X-Ray |

### 20. fact_etest

实测 **1,582** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| etest_id | VARCHAR(14) | PK |  |
| lot_id | VARCHAR(16) |  |  |
| test_time | TIMESTAMP |  |  |
| equipment_id | VARCHAR(10) |  |  |
| tested_panels | INT |  |  |
| open_fail | INT |  |  |
| short_fail | INT |  |  |
| isolation_fail | INT |  |  |
| pass_panels | INT |  |  |

### 21. fact_equipment_downtime

实测 **49** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| downtime_id | VARCHAR(12) | PK |  |
| equipment_id | VARCHAR(10) |  |  |
| start_time | TIMESTAMP |  |  |
| end_time | TIMESTAMP |  |  |
| duration_min | INT |  |  |
| reason | VARCHAR(12) |  | 故障/换型/待料/保养/动力 |

### 22. fact_customer_complaint

实测 **28** 行。

| 列名 | 类型 | 约束 | 注释 |
|---|---|---|---|
| complaint_id | VARCHAR(12) | PK |  |
| customer_id | VARCHAR(12) |  |  |
| shipment_id | VARCHAR(14) |  |  |
| product_id | VARCHAR(14) |  |  |
| complaint_date | DATE |  |  |
| defect_summary | VARCHAR(60) |  |  |
| dppm | INT |  |  |
| status | VARCHAR(12) |  | 8D关闭/处理中 |


## 四、表间关联速查（外键，JOIN 时按这些列）

- `dim_defect_code.process_id` → `dim_process.process_id`
- `dim_equipment.line_id` → `dim_line.line_id`
- `dim_equipment.process_id` → `dim_process.process_id`
- `fact_sales_order.customer_id` → `dim_customer.customer_id`
- `fact_sales_order_item.order_id` → `fact_sales_order.order_id`
- `fact_sales_order_item.product_id` → `dim_product.product_id`
- `fact_shipment.order_id` → `fact_sales_order.order_id`
- `fact_shipment_item.shipment_id` → `fact_shipment.shipment_id`
- `fact_work_order.item_id` → `fact_sales_order_item.item_id`

> 注意（非外键但必须按 ID 关联的列）：
> - `fact_shipment.customer_id`、`fact_shipment_item.product_id`、
>   `fact_work_order.product_id`、`fact_work_order.line_id`、
>   `fact_process_record.lot_id / process_id / equipment_id`、
>   `fact_inventory.material_id`、`fact_receivable.customer_id` 等在 DDL 中未声明外键，
>   但语义上关联同名主数据/事实表，**按 ID 列关联，禁止在单号/名称上做字符串截切匹配**。
