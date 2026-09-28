-- =====================================================================
-- IC载板企业 ERP+MES 数据模型 (PostgreSQL 12+)
-- 场景：礼鼎风格 IC substrate 厂；时间窗 2026-06-01 ~ 2026-09-24
-- 22 张表。配套 SQLite 开发库由 scripts/generate_data.py 同结构生成。
-- =====================================================================

-- ---------- 主数据 ----------
CREATE TABLE dim_customer (
  customer_id   VARCHAR(12) PRIMARY KEY,
  customer_name VARCHAR(80) NOT NULL,   -- 规范名
  region        VARCHAR(20),            -- 华东/华南/华北/海外
  customer_tier VARCHAR(10),            -- 战略/重点/普通
  sales_contact VARCHAR(40)
);
COMMENT ON TABLE dim_customer IS '客户=fabless芯片公司;销售订单中存在客户名脏写法';

CREATE TABLE dim_supplier (
  supplier_id       VARCHAR(12) PRIMARY KEY,
  supplier_name     VARCHAR(80),
  material_category VARCHAR(30),        -- ABF膜/铜箔/干膜/药水/钻针/阻焊
  region            VARCHAR(20),
  rating            VARCHAR(10)
);

CREATE TABLE dim_material (
  material_id   VARCHAR(12) PRIMARY KEY,
  material_name VARCHAR(60),
  category      VARCHAR(20),            -- core芯板/PP半固化片/铜箔/ABF膜/干膜/阻焊/药水
  unit          VARCHAR(10),
  standard_cost NUMERIC(10,2)
);

CREATE TABLE dim_product (
  product_id      VARCHAR(14) PRIMARY KEY,
  product_name    VARCHAR(60),
  product_type    VARCHAR(12),          -- FCBGA/FCCSP/WBCSP/RF
  layer_count     INT,                  -- 层数
  body_size_mm    VARCHAR(20),
  line_width_um   INT,
  line_space_um   INT,
  surface_finish  VARCHAR(16),          -- ENEPIG/OSP/ENIG/软金/沉银IT
  core_material   VARCHAR(8),           -- ABF/BT
  units_per_panel INT                   -- 每排板产出颗数
);

CREATE TABLE dim_process (
  process_id     VARCHAR(6) PRIMARY KEY,
  process_seq    INT NOT NULL,
  process_name   VARCHAR(40),
  process_stage  VARCHAR(16),           -- 内层/压合/钻孔/沉铜电镀/外层/阻焊/表处/成型/电测/终检包装
  is_key_process SMALLINT DEFAULT 0,
  avg_cycle_min  INT
);

CREATE TABLE dim_line (
  line_id       VARCHAR(8) PRIMARY KEY,
  line_name     VARCHAR(30),
  factory       VARCHAR(20),            -- 秦皇岛一厂/秦皇岛二厂
  product_scope VARCHAR(40)
);

CREATE TABLE dim_equipment (
  equipment_id    VARCHAR(10) PRIMARY KEY,
  equipment_name  VARCHAR(50),
  process_id      VARCHAR(6) REFERENCES dim_process(process_id),
  line_id         VARCHAR(8) REFERENCES dim_line(line_id),
  equipment_model VARCHAR(40),
  status          VARCHAR(10),          -- 运行/维修/停用
  install_date    DATE
);

CREATE TABLE dim_employee (
  employee_id   VARCHAR(10) PRIMARY KEY,
  employee_name VARCHAR(30),
  department    VARCHAR(20),            -- 制造/品质/设备/生管/业务
  role          VARCHAR(20)
);

CREATE TABLE dim_shift (
  shift_id   VARCHAR(4) PRIMARY KEY,
  shift_name VARCHAR(10),               -- 白班/夜班
  start_time VARCHAR(8),
  end_time   VARCHAR(8)
);

CREATE TABLE dim_defect_code (
  defect_code      VARCHAR(14) PRIMARY KEY,
  defect_name      VARCHAR(40),
  process_id       VARCHAR(6) REFERENCES dim_process(process_id),
  defect_category  VARCHAR(16)          -- 开路/短路/外观/尺寸/孔/铜/阻焊/表面处理
);

-- ---------- ERP 事实 ----------
CREATE TABLE fact_sales_order (
  order_id        VARCHAR(14) PRIMARY KEY,
  customer_id     VARCHAR(12) REFERENCES dim_customer(customer_id),
  customer_name_raw VARCHAR(80),        -- 单据上的原始客户名(含脏写法)
  order_date      DATE,
  order_type      VARCHAR(10),          -- forecast/正式单
  currency        VARCHAR(6),
  order_status    VARCHAR(10)
);

CREATE TABLE fact_sales_order_item (
  item_id     VARCHAR(14) PRIMARY KEY,
  order_id    VARCHAR(14) REFERENCES fact_sales_order(order_id),
  product_id  VARCHAR(14) REFERENCES dim_product(product_id),
  order_qty   INT,                      -- 颗
  unit_price  NUMERIC(10,4),
  due_date    DATE
);

CREATE TABLE fact_shipment (
  shipment_id VARCHAR(14) PRIMARY KEY,
  order_id    VARCHAR(14) REFERENCES fact_sales_order(order_id),
  customer_id VARCHAR(12),
  ship_date   DATE,
  status      VARCHAR(10)
);

CREATE TABLE fact_shipment_item (
  ship_item_id VARCHAR(14) PRIMARY KEY,
  shipment_id  VARCHAR(14) REFERENCES fact_shipment(shipment_id),
  product_id   VARCHAR(14),
  lot_id       VARCHAR(16),
  ship_qty_units INT
);

CREATE TABLE fact_inventory (
  inventory_id  VARCHAR(12) PRIMARY KEY,
  material_id   VARCHAR(12),
  stock_type    VARCHAR(10),            -- 原料/成品
  lot_no        VARCHAR(16),
  warehouse     VARCHAR(20),
  qty_on_hand   INT,
  update_date   DATE
);

CREATE TABLE fact_receivable (
  receivable_id    VARCHAR(12) PRIMARY KEY,
  order_id         VARCHAR(14),
  customer_id      VARCHAR(12),
  amount           NUMERIC(14,2),
  due_date         DATE,
  received_amount  NUMERIC(14,2),
  receive_date     DATE,
  status           VARCHAR(12)          -- 已结清/部分/未收
);

-- ---------- MES 事实 ----------
CREATE TABLE fact_work_order (
  lot_id         VARCHAR(16) PRIMARY KEY,   -- 批次号(两种格式混用)
  item_id        VARCHAR(14) REFERENCES fact_sales_order_item(item_id),
  product_id     VARCHAR(14),
  line_id        VARCHAR(8),
  planned_panels INT,
  release_date   DATE,
  planned_end    DATE,
  lot_status     VARCHAR(8),                -- 已完工/在制
  priority       VARCHAR(8)
);

CREATE TABLE fact_process_record (
  record_id     BIGINT PRIMARY KEY,
  lot_id        VARCHAR(16),
  process_id    VARCHAR(6),
  equipment_id  VARCHAR(10),
  shift_id      VARCHAR(4),
  operator_id   VARCHAR(10),
  in_time       TIMESTAMP,
  out_time      TIMESTAMP,                 -- 在制批次当前工序为NULL
  input_panels  INT,
  good_panels   INT,                       -- 主记录=一次通过;返工过站is_rework_pass=1
  scrap_panels  INT,
  rework_panels INT,
  is_rework_pass SMALLINT DEFAULT 0,       -- 1=返工重过站(口径陷阱)
  remark        VARCHAR(30)
);

CREATE TABLE fact_defect (
  defect_record_id BIGINT PRIMARY KEY,
  record_id   BIGINT,
  lot_id      VARCHAR(16),
  process_id  VARCHAR(6),
  defect_code VARCHAR(14),
  defect_qty  INT,
  detect_method VARCHAR(10)                -- AOI/电测/目检/X-Ray
);

CREATE TABLE fact_etest (
  etest_id      VARCHAR(14) PRIMARY KEY,
  lot_id        VARCHAR(16),
  test_time     TIMESTAMP,
  equipment_id  VARCHAR(10),
  tested_panels INT,
  open_fail     INT,
  short_fail    INT,
  isolation_fail INT,
  pass_panels   INT
);

CREATE TABLE fact_equipment_downtime (
  downtime_id  VARCHAR(12) PRIMARY KEY,
  equipment_id VARCHAR(10),
  start_time   TIMESTAMP,
  end_time     TIMESTAMP,
  duration_min INT,
  reason       VARCHAR(12)                 -- 故障/换型/待料/保养/动力
);

CREATE TABLE fact_customer_complaint (
  complaint_id    VARCHAR(12) PRIMARY KEY,
  customer_id     VARCHAR(12),
  shipment_id     VARCHAR(14),
  product_id      VARCHAR(14),
  complaint_date  DATE,
  defect_summary  VARCHAR(60),
  dppm            INT,
  status          VARCHAR(12)              -- 8D关闭/处理中
);

-- 常用索引
CREATE INDEX idx_pr_lot_proc ON fact_process_record(lot_id, process_id);
CREATE INDEX idx_pr_proc_time ON fact_process_record(process_id, in_time);
CREATE INDEX idx_def_lot ON fact_defect(lot_id);
CREATE INDEX idx_wo_item ON fact_work_order(item_id);
CREATE INDEX idx_ship_lot ON fact_shipment_item(lot_id);
