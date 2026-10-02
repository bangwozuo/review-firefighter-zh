# 测试报告（review-collect-grade-flow）

## 一、结构校验

| 项 | 结果 |
|---|---|
| 四件套齐全（SKILL.md / prompt.txt / schema.json / examples/input.json） | ✅ PASS |
| SKILL.md 九段齐全 + ID 行未改动 | ✅ PASS |
| DAG 节点为本仓真实 slug（platform-review-collect / negative-review-extinguish-flow / positive-review-thanks-flow 等） | ✅ PASS |
| 每步含输入/处理/输出/失败处理 | ✅ PASS |
| 无占位符残留 | ✅ PASS |

## 二、脚本实跑（真实执行）

**命令**：

```bash
python scripts/run_flow.py --demo --outdir out
python scripts/run_flow.py --input examples/input.json --outdir out
```

**运行环境**：Python 3.13（WorkBuddy 内置）/ openpyxl / matplotlib

| 项 | 结果 |
|---|---|
| 退出码 | 0 |
| 步骤日志 | 步骤1 去重 → 步骤2 双通道打分 → 步骤3 分桶 → 步骤4 SLA 排序 → 步骤5 分派，全部通过 |
| 分桶结果 | 差评 2 / 中评 2 / 好评 2（demo 含 1 条重复评价被正确去重） |
| 产物 1 | `out/分级处理队列.xlsx`（差评行标红 + 转人工标红） |
| 产物 2 | `out/评价星级分布.png` 三桶饼图 |
| 产物 3 | `out/collect_grade.json` 机器可读 |

### 判定正确性核对（真实输出）

| 场景 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 1 星 +「头发丝」 | 差评 + 转人工（食安） | 一致 | ✅ |
| 3 星 +「等了45分钟」类履约词 | 冲突取更负 | 一致 | ✅ |
| 3 星 +「真是绝了，等了两小时」 | 中评 + 反讽疑似复核 | 一致 | ✅ |
| 重复评价 ID | 去重 | 去重 1 条 | ✅ |
| stars 缺失 | 退出码 2 不补默认值 | 一致 | ✅ |

## 三、边界与已知限制

| 限制 | 说明 |
|---|---|
| 词表非穷尽 | 长尾负面表达依赖模型按 prompt.txt 语境复核 |
| 反讽识别 | 脚本只按特征句（「真是绝了」等）打疑似标，最终由人工复核 |
| 通道 B 延迟 | 人工导出数据 T+1，响应窗口按发现时刻起算并标注 |

## 四、结论

**通过。** T3 五步编排真实可跑，产物齐全；食安拦截、冲突改判、反讽标注、去重、
失败退出五类行为全部按设计生效。

---

*测试报告基于真实实跑输出生成 · 2026-09-30*
