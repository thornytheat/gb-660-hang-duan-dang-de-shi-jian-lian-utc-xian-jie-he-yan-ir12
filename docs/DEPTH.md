# 深度登记 航班时刻运行台

- 规模：中型
- 核心规则：用机场 IANA 时区解析 DST 重叠与跳时，所有航链在 UTC 上校验先后和衔接。
- 使用者与工作流：运行数据员导入航段，确认机场时区，处理当地时刻歧义，构建 UTC 航链并核对转机。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 航段采信 | 导入航段并隔离重复冲突 | POST /api/legs/import | `app/modules/m1_legs/parser.py` `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py` |
| M2 机场时区 | 查询日期对应的时区版本 | POST /api/airports | `app/modules/m2_airports/registry.py` `app/modules/m2_airports/contracts.py` | `tests/test_m2_airports.py` |
| M3 当地时刻 | 解析偏移、重叠与跳时 | POST /api/local-time/resolve | `app/modules/m3_localtime/resolve.py` `app/modules/m3_localtime/evidence.py` | `tests/test_m3_localtime.py` |
| M4 UTC 航链 | 换算航段并校验先后顺序 | POST /api/itineraries/build | `app/modules/m4_itinerary/builder.py` `app/modules/m4_itinerary/validate.py` | `tests/test_m4_itinerary.py` |
| M5 衔接判定 | 核对航站楼换乘与等待 | POST /api/connections/check | `app/modules/m5_connections/rules.py` `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py` |
| M6 批次导出 | 核对航段守恒并导出时刻 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 共享航班 | 跨批次采信、归并裁决与撤销补偿 | POST /api/codeshare/import | `app/modules/m7_codeshare/contracts.py` `app/modules/m7_codeshare/plan.py` `app/modules/m7_codeshare/service.py` | `tests/test_m7_codeshare.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-03 | 尾行记录不完整 | 半条航段进入换算 | 完整边界检查后隔离 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_truncated_tail_detected` |
| TRAP-05 | 必填时刻缺失 | 空时间在深层报错 | 采信阶段给原因码和行号 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-07 | 空当地时刻 | 被转换成午夜 | 空值不参与航链 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-09 | 超大外部行程号 | JSON 数字往返丢精度 | 始终按字符串保形 | `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py::test_external_id_keeps_large_digits` |
| TRAP-11 | 偏移与 Z 混用 | 同一时刻先后次序错误 | 统一到 UTC 后输出 Z | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_utc_serializes_with_z` |
| TRAP-12 | DST 重叠与跳时 | 重复小时覆盖或虚构时刻 | 往返验证并显式 fold | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_dst_overlap_has_two_candidates` |
| TRAP-13 | 航段乱序 | 输入顺序被当成航程顺序 | 按 UTC 起飞时刻稳定排序 | `app/modules/m4_itinerary/builder.py` | `tests/test_m4_itinerary.py::test_order_by_utc_is_stable` |
| TRAP-14 | 同航段时刻冲突 | 后写覆盖原计划 | 冲突版本进入隔离区 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_conflicting_duplicate_detected` |
| TRAP-23 | 衔接窗口端点 | 结束时刻被重复计入 | 使用半开区间 | `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py::test_half_open_window_excludes_end` |
| TRAP-25 | 时区版本重叠 | 随机选中一版偏移 | 多个生效版本立即拒绝 | `app/modules/m2_airports/registry.py` | `tests/test_m2_airports.py::test_overlapping_timezone_versions_rejected` |
| TRAP-31 | 共享航班重复计数 | 按营销记录计航段，同一实际航段重复入链 | 按承运方声明归并为一条实际航段 | `app/modules/m7_codeshare/plan.py` | `tests/test_m7_codeshare.py::test_declared_codeshare_counts_one_actual_leg` |
| TRAP-32 | 相同时刻被当成共享 | 用起飞时刻猜测共享关系 | 只认 operating 声明，时刻相同不合并 | `app/modules/m7_codeshare/contracts.py` | `tests/test_m7_codeshare.py::test_identical_times_without_declaration_do_not_merge` |
| TRAP-33 | 直接归并丢失营销来源 | 合并后只留一条记录 | 营销记录逐条保留，归并只改变实际航段计数 | `app/modules/m7_codeshare/service.py` | `tests/test_m7_codeshare.py::test_merge_keeps_marketing_evidence_after_fold` |
| TRAP-34 | 来源冲突被静默采信 | 后到来源覆盖原计划 | 冲突进入待裁决并保留双方证据 | `app/modules/m7_codeshare/service.py` | `tests/test_m7_codeshare.py::test_conflicting_declaration_is_pending_not_merged` |
| TRAP-35 | 撤销归并留下悬空引用 | 航链仍指向已退役的归并航段 | 拆回原航段并重指向引用，无法唯一确定则挂起 | `app/modules/m7_codeshare/plan.py` | `tests/test_m7_codeshare.py::test_compensation_splits_legs_and_repairs_chain` |
| TRAP-36 | 并发裁决覆盖旧版本 | 后写覆盖前写 | 版本不匹配返回 VERSION_CONFLICT | `app/modules/m7_codeshare/service.py` | `tests/test_m7_codeshare.py::test_stale_adjudication_rejected` |
| TRAP-37 | 补偿中断后无法恢复 | 重放从头扣增，计数翻倍 | 步骤落库，从断点续跑；同一 op_id 不重复扣增 | `app/modules/m7_codeshare/service.py` | `tests/test_m7_codeshare.py::test_compensation_interrupt_then_resume` |

## 对账规则

- 输入航段 = 有效航段 + 冲突航段 + 隔离航段。
- 行程航段 = 可衔接航段 + 不可衔接航段。
- 到达 UTC 必须晚于起飞 UTC，飞行时长不得超过 18 小时。
- 共享航班：营销记录 = 实际航段 + 被归并吸收 + 未结算；未结算必须全部是待裁决，且无记录被重复计入。

## 已知不覆盖

- 不计算天气和空管延误，输入只描述计划时刻。
- 共享关系只认承运方声明，不按时刻、航线或航班号相似度推断。
- 不扩展出票与运价规则；销售、结算与票面承运人不参与本模块。

