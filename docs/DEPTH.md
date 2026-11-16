# 冷链轨迹深度登记

规模：中型。使用者：冷链质控员。核心规则：超过8°C的线性温度轨迹按阈值交点与交接端点切分积分，长缺测区间不插值。

## 功能模块
| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 轨迹采信 | 采信字节与保留行号 | `/intake`、`/api/intake` | `app/modules/intake/reader.py` `app/modules/intake/service.py` | `tests/test_intake.py` |
| M2 探头校准 | 归一时间和修正温度 | `/contract`、`/api/contract` | `app/modules/contract/normalize.py` `app/modules/contract/service.py` | `tests/test_contract.py` |
| M3 交接区间 | 切分责任段与裁决冲突 | `/model`、`/api/model` | `app/modules/model/intervals.py` `app/modules/model/service.py` | `tests/test_model.py` |
| M4 超温积分 | 阈值交点与梯形积分 | `/calculate`、`/api/calculate` | `app/modules/calculate/integral.py` `app/modules/calculate/service.py` | `tests/test_calculate.py` |
| M5 质量隔离 | 筛查坏行与导出证据 | `/quarantine`、`/api/quarantine` | `app/modules/quarantine/rules.py` `app/modules/quarantine/service.py` | `tests/test_quarantine.py` |
| M6 责任对账 | 行数分钟和积分守恒 | `/reconcile`、`/api/reconcile` | `app/modules/reconcile/compare.py` `app/modules/reconcile/service.py` | `tests/test_reconcile.py` |
| M7 分日关账 | 切分业务日、发布责任账、恢复版本 | `/closing`、`/api/closing` | `app/modules/closing/calendar.py` `app/modules/closing/ledger.py` `app/modules/closing/service.py` | `tests/test_closing.py` |
| M8 批次复核 | 内容去重与版本复核 | `/batches`、`/api/batches` | `app/modules/batches/review.py` `app/modules/batches/service.py` | `tests/test_batches.py` |

## 陷阱登记
| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 混合编码 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_legacy_encoding` |
| TRAP-02 | BOM表头 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/intake/reader.py` | `tests/test_intake.py::test_bom_sample` |
| TRAP-06 | 全角温度 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/contract/normalize.py` | `tests/test_contract.py::test_fullwidth` |
| TRAP-07 | 空值哨兵 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/contract/normalize.py` | `tests/test_contract.py::test_missing` |
| TRAP-11 | 偏移一致 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/contract/normalize.py` | `tests/test_contract.py::test_timezone_equivalence` |
| TRAP-13 | 乱序到达 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/model/service.py` | `tests/test_model.py::test_late_kept` |
| TRAP-14 | 同键冲突 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/model/service.py` | `tests/test_model.py::test_conflict_all_isolated` |
| TRAP-15 | 温标混用 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/contract/normalize.py` | `tests/test_contract.py::test_fahrenheit_correction` |
| TRAP-23 | 交接边界 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/model/intervals.py` | `tests/test_model.py::test_half_open_owner` |
| TRAP-26 | 陈旧复核 | 静默误算或覆盖 | 原值保留并确定性判定；由脏样例触发 | `app/modules/batches/service.py` | `tests/test_batches.py::test_optimistic_review` |
| TRAP-27 | 跨零点批次 | 整段算进首日或重复计数 | 按当地业务日切分，同一分钟只进一天，次日10:00为截止 | `app/modules/closing/calendar.py` `app/modules/closing/ledger.py` | `tests/test_closing.py::test_cross_midnight_splits_days` |
| TRAP-28 | 长缺测计入达标 | 把未知分钟当正常分钟卖给财务 | 未知单列，`unknown_counts_as_compliant`恒为假 | `app/modules/closing/ledger.py` | `tests/test_closing.py::test_long_gap_is_unknown_not_compliant` |
| TRAP-29 | 交接迟到 | 用迟到记录补齐责任，事后改账 | 超过次日截止的交接保留证据但不参与归属，进入未覆盖并列入待补 | `app/modules/closing/ledger.py` | `tests/test_closing.py::test_late_handover_is_held_not_attributed` |
| TRAP-30 | 关账重试重复计分钟 | 每次重试累加一份责任分钟 | 以(业务日,批次,策略,规则版本)作幂等键，内容相同不新增版本 | `app/modules/closing/service.py` `app/store.py` | `tests/test_closing.py::test_replay_does_not_add_minutes` |
| TRAP-31 | 发布失败半写 | 新版写了一半，旧版被清空 | 单个IMMEDIATE事务内构建与切换，异常回滚保留旧版 | `app/store.py` | `tests/test_closing.py::test_publish_failure_keeps_previous_version` |
| TRAP-32 | 未覆盖被分摊 | 交接缺口或重叠被任选一方承担 | 未覆盖与争议单列，`balanced`只校验分类守恒 | `app/modules/closing/ledger.py` | `tests/test_closing.py::test_overlap_is_disputed` |

## 对账规则
输入数据行=有效采样+隔离行+空行。责任覆盖分钟=正常+超温+未知。各责任方热暴露积分之和等于总积分；展示保留六位小数，守恒容差0.00001。

分日账本另按业务日切分：分钟=可证实+缺测未知+责任未覆盖，三者互不折算；责任方超温分钟之和等于账本超温分钟。未知、未覆盖、迟到交接都只单列，不进入任一责任方的达标分钟。

## 流水线与恢复
每个业务模块可用同一份内置样例独立试跑。文件超过2MB或10000条会拒绝。编码与结构先校验，坏行隔离后继续。SQLite仅在完整计算成功后事务写入。重复运行的输入摘要、校准摘要、交接摘要、规则版本及参数完全一致时复用批次。重新打开数据库保留批次；旧版本复核返回409。

分日关账的发布是独立的原子切换：业务日、批次、策略与规则版本构成幂等键，同一键重试返回原版本；新批次或新策略生成新版本，旧版转为历史。发布前先在只读预演中构建同一份账，正式账与展示结果逐项一致。切换失败则事务回滚，旧版保持有效。恢复历史版本会生成新的审计版本，其分钟数与目标版本相同，重复恢复不再新增版本。原始采样按内容摘要单独留存，已确认记录按业务日与版本留存，均不受发布、回滚或恢复影响。

## 已知不覆盖
不连接实时探头，不外推首尾采样，不将缺测判断成温度合格。交接责任冲突会阻止复核。GB18030只是明确的设备编码兼容链，不推断任意字符集。关账只按已收到的交接记录判定责任，不预测未到的交接归属于谁；若业务日跨零点，账本按本地日切分，但需两天都发布后才构成完整交付。