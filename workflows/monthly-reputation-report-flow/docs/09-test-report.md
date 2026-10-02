# 测试报告（monthly-reputation-report-flow）

## 一、结构校验

| 项 | 结果 |
|---|---|
| 四件套齐全（SKILL.md / prompt.txt / schema.json / examples/input.json） | ✅ PASS |
| SKILL.md 九段齐全 + ID 行未改动 | ✅ PASS |
| DAG 引用本仓真实原子技能（platform-review-collect / review-reply-generate / negative-review-script-lib / reply-compliance-review） | ✅ PASS |
| 每步含输入/处理/输出/失败处理 | ✅ PASS |
| 无占位符残留 | ✅ PASS |

## 二、脚本实跑（真实执行）

**命令**：

```bash
python scripts/run_flow.py --demo --outdir out
python scripts/run_flow.py --input examples/input.json --outdir out
```

**运行环境**：Python 3.13（WorkBuddy 内置）/ openpyxl / matplotlib / python-docx

| 项 | 结果 |
|---|---|
| 退出码 | 0 |
| 步骤日志 | 月窗口校验 → 趋势 → 根因分布 → 回复率对账 → 竞店对比，全部通过 |
| 趋势 | W1 4.67 / W2 2.5 / W3 3.0 / W4 3.0 / W5 无数据断线；月均分 3.21（-25.2%） |
| 根因 | 等位与出餐 4 条（44%）Top1，菜品质量 Top2 |
| 回复率 | 7 条差评仅 1 条已回复 = 14%，缺口 ID 清单完整 |
| 竞店 | 2 家，差距 -1.29 / -1.39，列追赶目标 |
| 产物 1 | `out/月度口碑报告.docx`（六区块 + 内嵌趋势图，python-docx 实生成） |
| 产物 2 | `out/评分趋势.png`（周均分折线，W5 断线） |
| 产物 3 | `out/monthly_report.json` |

### 判定正确性核对（真实输出）

| 场景 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 0 评价周（W5） | 「无数据」断线不补零 | 一致（折线与表格） | ✅ |
| 回复率缺口 | 未回复差评 ID 全部列出（6 条） | 一致 | ✅ |
| 基期缺失 | 环比标注「基期缺失」不编造 | 一致（docs/04 示例 2 实测） | ✅ |
| 竞店缺失 | 对比区块标注「未提供」 | 一致（docs/04 示例 2 实测） | ✅ |
| 星级越界 9 | 退出码 2 不出报告 | 一致（docs/04 示例 3 实测） | ✅ |
| 小样本 | 全月 <10 条标注「样本量小」 | 一致（docs/04 示例 2 实测） | ✅ |

## 三、边界与已知限制

| 限制 | 说明 |
|---|---|
| 周切分口径 | 按月内每 7 天为一周（W1-W5），非严格自然周；跨月评价剔除 |
| 竞店数据为人工抄录 | 平台公开榜单口径，脚本不校验抄录日期新鲜度 |
| 黄金窗口达标率 | 输入未含响应完成时间时按批次口径说明，不精确到条 |

## 四、结论

**通过。** T3 六步编排真实可跑，产物含 Word/PNG/JSON 三类真实文件；断线、缺口、
缺失标注、越界退出四类行为全部按设计生效。本仓 9 资产的 T1/T3 脚本链路至此全部
具备端到端真实产物。

---

*测试报告基于真实实跑输出生成 · 2026-09-30*
