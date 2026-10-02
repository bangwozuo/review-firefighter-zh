# -*- coding: utf-8 -*-
"""
月度口碑报告流 —— 端到端编排脚本。

流程：月窗口校验与去重 → 评分趋势(周均分序列, 无数据周断线) → 根因分布(六环节词表)
      → 回复率对账(差评回复率目标100% + 黄金2小时达标率) → 竞店对比(公开榜单口径)
      → 汇总产物（Word 一页报告 + PNG 趋势图 + JSON）；结论与下月动作由模型按 prompt.txt 编写

失败处理：
  - reviews 缺失 → 退出码 2
  - 星级越界(非1-5) → 退出码 2
  - 上月基期缺失 → 环比标注「基期缺失」，不编造
  - 周内 0 条评价 → 「无数据」断线，不补零画成 0 分
  - 竞店数据缺失 → 区块标注缺失，不编造竞店分数
  - reply_status 缺失 → 按「未回复」保守计入缺口

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(WF_DIR))
sys.path.insert(0, os.path.join(REPO, "lib"))

try:
    import assettools as at
except ImportError:  # pragma: no cover
    print("[错误] 未找到 lib/assettools.py", file=sys.stderr)
    sys.exit(2)

# 六环节词表（与 negative-review-classify-flow 同源）
SEGMENTS = [
    ("菜品质量", r"难吃|太咸|太淡|糊了|异味|异物|头发|虫子|塑料|变质|发霉|不新鲜|分量少"),
    ("服务态度", r"态度差|爱答不理|呵斥|没人理|白眼|催单被怼|不耐心"),
    ("等位与出餐", r"等了\s*\d+\s*分钟|等了[一两三四五六七八九十\d]+小时|等位|排队|催单|超时|迟迟|上菜慢|出餐慢"),
    ("环境卫生", r"脏|油腻|烟味|餐具不洁|桌子没擦|地面|卫生间"),
    ("价格感知", r"贵|不值|涨价|性价比|缩水|越来越贵"),
    ("物流配送", r"外卖.{0,6}超时|撒漏|漏送|洒了|凉了|配送慢|骑手"),
]

DEMO = {
    "shop": "陈记酸菜鱼（万达店）",
    "month": "2026-09",
    "last_month": {"月均分": 4.3, "好评率": 0.82, "差评数": 14},
    "competitors": [
        {"shop": "江渔儿酸菜鱼（同商圈）", "stars": 4.5, "review_count": 3200},
        {"shop": "太二酸菜鱼（同商圈）", "stars": 4.6, "review_count": 5100},
    ],
    "reviews": [
        {"id": f"R-{i:03d}", "stars": s, "text": t, "review_time": d, "reply_status": rs}
        for i, (s, t, d, rs) in enumerate([
            (5, "酸菜很正鱼片嫩", "2026-09-02 12:10", "已回复"),
            (4, "环境好服务好", "2026-09-03 19:30", "已回复"),
            (5, "第三次来了，性价比高", "2026-09-05 18:44", "已回复"),
            (2, "等了45分钟才上菜，上错菜", "2026-09-08 12:05", "未回复"),
            (4, "味道不错，下次还来", "2026-09-09 19:02", "已回复"),
            (1, "鱼汤里有头发丝", "2026-09-12 08:12", "已回复"),
            (3, "口味一般，上菜稍慢", "2026-09-13 20:11", "未回复"),
            (2, "等了两小时，桌子油腻腻", "2026-09-16 21:05", "未回复"),
            (5, "小陈服务周到，主动分鱼", "2026-09-18 18:30", "已回复"),
            (2, "涨价了分量还少，不值", "2026-09-20 12:22", "未回复"),
            (4, "酸菜鱼很正", "2026-09-22 19:40", "已回复"),
            (1, "服务员爱答不理，催单被怼", "2026-09-25 12:55", "未回复"),
            (5, "环境好，值得推荐", "2026-09-27 20:15", "已回复"),
            (2, "外卖等了90分钟，到手凉透了", "2026-09-28 19:20", "未回复"),
        ], start=1)
    ],
}


def step1_validate(payload):
    """步骤 1：月窗口校验与去重。星级越界退出码 2。"""
    reviews = payload.get("reviews")
    if not reviews:
        print("[失败] 缺少 reviews。请补数后重跑。", file=sys.stderr)
        sys.exit(2)
    month = str(payload.get("month") or "")
    seen, rows, outside = set(), [], 0
    for r in reviews:
        rid = str(r.get("id", ""))
        if rid in seen:
            continue
        seen.add(rid)
        stars = r.get("stars")
        if stars is None or not 1 <= int(stars) <= 5:
            print(f"[失败] 星级越界：{rid} → {stars}（限 1-5）。退出码 2。", file=sys.stderr)
            sys.exit(2)
        t = str(r.get("review_time") or "")
        if month and t and not t.startswith(month):
            outside += 1
            continue
        rows.append(r)
    print(f"[步骤1] 月窗口校验通过：{len(rows)} 条（窗口外剔除 {outside} 条，去重 {len(reviews) - len(rows) - outside} 条）")
    return rows, outside


def week_of(date_str, month):
    try:
        d = datetime.datetime.fromisoformat(str(date_str))
    except Exception:
        return None
    first = datetime.date.fromisoformat(month + "-01")
    idx = (d.date() - first).days // 7 + 1
    return f"W{min(idx, 5)}"


def step2_trend(rows, last_month, month):
    """步骤 2：评分趋势（周均分；无数据周「无数据」断线）。"""
    weeks = {}
    for r in rows:
        w = week_of(r.get("review_time", ""), month)
        weeks.setdefault(w, []).append(int(r["stars"]))
    series = []
    for w in ["W1", "W2", "W3", "W4", "W5"]:
        vals = weeks.get(w)
        series.append({"周": w, "周均分": round(sum(vals) / len(vals), 2) if vals else "无数据",
                       "评价量": len(vals) if vals else 0})
    cur = sum(int(r["stars"]) for r in rows) / len(rows)
    trend = {"周序列": series, "月均分": round(cur, 2)}
    lm = (last_month or {}).get("月均分")
    if lm:
        trend["环比"] = f"{(cur - lm) / lm * 100:+.1f}%（上月 {lm}）"
    else:
        trend["环比"] = "基期缺失"
    n_small = len(rows) < 10
    print(f"[步骤2] 趋势完成：月均分 {trend['月均分']}，环比 {trend['环比']}"
          + ("，样本量小仅作参考" if n_small else ""))
    return trend, n_small


def step3_causes(rows):
    """步骤 3：根因分布（六环节词表）。"""
    import collections
    counts = collections.Counter()
    for r in rows:
        if int(r["stars"]) >= 4:
            continue
        t = str(r.get("text", ""))
        hit = False
        for seg, pat in SEGMENTS:
            if re.search(pat, t):
                counts[seg] += 1
                hit = True
        if not hit:
            counts["待定性"] += 1
    total = sum(counts.values()) or 1
    dist = {k: {"条数": v, "占比": f"{v / total * 100:.0f}%"} for k, v in counts.most_common()}
    top2 = [k for k, _ in counts.most_common(2)]
    print(f"[步骤3] 根因分布完成：Top2 = {top2}")
    return dist, top2


def step4_reply(rows):
    """步骤 4：回复率对账（差评回复率目标 100%）。"""
    bad = [r for r in rows if int(r["stars"]) <= 3]
    bad_unreplied = [r["id"] for r in bad if r.get("reply_status") != "已回复"]
    # 黄金 2 小时达标率：以输入 reply_status 为准，无响应时间字段时按批次口径说明
    summary = {
        "差评数": len(bad),
        "差评已回复": len(bad) - len(bad_unreplied),
        "差评回复率": f"{(len(bad) - len(bad_unreplied)) / len(bad) * 100:.0f}%" if bad else "（本月 0 差评）",
        "回复率目标": "100%（平台排序加权因子）",
        "未回复差评ID": bad_unreplied or "（无缺口）",
    }
    print(f"[步骤4] 回复率对账完成：差评 {len(bad)} 条，未回复 {len(bad_unreplied)} 条")
    return summary


def step5_compete(competitors, month_avg):
    """步骤 5：竞店对比（公开榜单口径）。"""
    if not competitors:
        print("[步骤5] 竞店数据未提供 —— 对比区块标注缺失，不编造")
        return {"竞店对比": "竞店数据未提供", "抄录口径": "平台公开榜单，人工抄录需注日期"}
    rows = []
    for c in competitors:
        diff = round(month_avg - float(c["stars"]), 2)
        rows.append({"竞店": c["shop"], "评分": c["stars"], "评论量": c.get("review_count", ""),
                     "差距": f"{diff:+.2f}", "定位": "对比优势" if diff >= 0 else "追赶目标"})
    print(f"[步骤5] 竞店对比完成：{len(rows)} 家")
    return {"竞店对比": rows, "抄录口径": "平台公开榜单，人工抄录需注日期"}


def build(payload, outdir):
    month = str(payload.get("month") or "")
    rows, outside = step1_validate(payload)
    trend, n_small = step2_trend(rows, payload.get("last_month"), month)
    dist, top2 = step3_causes(rows)
    reply = step4_reply(rows)
    comp = step5_compete(payload.get("competitors"), trend["月均分"])

    avg_rate = sum(1 for r in rows if int(r["stars"]) >= 4) / len(rows)
    summary = {
        "门店": payload.get("shop", "未提供"),
        "报告月份": month,
        "评价总量": len(rows),
        "月均分": trend["月均分"],
        "环比": trend["环比"],
        "好评率": f"{avg_rate * 100:.0f}%",
        "差评回复率": reply["差评回复率"],
        "根因Top2": top2,
        "样本量提示": "样本量小，趋势仅作参考" if n_small else "—",
        "AI 标识": "AI 生成内容",
    }

    at.ensure_outdir(outdir)
    # 折线图：无数据周断线（NaN 不连线）
    xs = [s["周"] for s in trend["周序列"]]
    ys = [float(s["周均分"]) if s["周均分"] != "无数据" else float("nan") for s in trend["周序列"]]
    png = at.line_chart(os.path.join(outdir, "评分趋势.png"), xs,
                        {"周均分": ys},
                        title=f"周均分趋势（{month}）", ylabel="星级均分")

    # Word 一页报告（sections 结构见 assettools.write_docx）
    def tbl(cols, rows_):
        return {"cols": cols, "rows": rows_}

    comp_is_list = isinstance(comp["竞店对比"], list)
    sections = [
        {"heading": "一、核心指标",
         "table": tbl(["指标", "数值"], [
             ["评价总量", len(rows)],
             ["月均分", f"{trend['月均分']}（环比 {trend['环比']}）"],
             ["好评率", f"{avg_rate * 100:.0f}%"],
             ["差评回复率", f"{reply['差评回复率']}（目标 100%）"],
             ["根因 Top2", "、".join(top2)],
         ])},
        {"heading": "二、评分趋势（周均分，无数据周断线）",
         "image": os.path.join(outdir, "评分趋势.png"),
         "table": tbl(["周"] + xs, [
             ["周均分"] + [str(s["周均分"]) for s in trend["周序列"]],
             ["评价量"] + [str(s["评价量"]) for s in trend["周序列"]],
         ])},
        {"heading": "三、根因分布",
         "table": tbl(["环节", "条数", "占比"],
                      [[k, v["条数"], v["占比"]] for k, v in dist.items()])},
        {"heading": "四、回复率缺口",
         "table": tbl(["项", "内容"], [
             ["未回复差评ID", str(reply["未回复差评ID"])],
             ["说明", "未回复差评需在 2 小时黄金窗口内补回，回复率目标 100%"],
         ])},
        {"heading": "五、竞店对比",
         "table": (tbl(["竞店", "评分", "差距", "定位"],
                       [[c["竞店"], c["评分"], c["差距"], c["定位"]] for c in comp["竞店对比"]])
                   if comp_is_list else
                   tbl(["说明", ], [comp["竞店对比"], comp["抄录口径"]]))},
        {"heading": "六、下月三个必改动作（模型按 prompt.txt 编写，待店主确认）",
         "table": tbl(["#", "动作", "指标", "责任人"], [
             ["1", "（依据 Top1 根因编写）", "该环节月差评减半", "厨师长"],
             ["2", "（依据 Top2 根因编写）", "该环节月差评减半", "前厅主管"],
             ["3", "（依据回复率缺口编写）", "差评回复率 100%", "店长"],
         ]),
         "bullets": [f"数据截至 {at.stamp()[:10]}；竞店口径：{comp['抄录口径']}"]},
    ]
    docx = at.write_docx(os.path.join(outdir, "月度口碑报告.docx"),
                         title=f"月度口碑报告（{payload.get('shop', '')} · {month}）",
                         sections=sections, subtitle="AI 生成内容 · 数值以脚本输出为准 · 供店主内部使用")

    js = at.write_json({"summary": summary, "trend": trend, "causes": dist,
                        "reply": reply, "competitors": comp, "generated_at": at.stamp(),
                        "note": "统计以脚本输出为准；结论与下月动作由模型按 prompt.txt 编写"},
                       os.path.join(outdir, "monthly_report.json"))
    return {"files": [docx, png, js], "summary": summary}


def main():
    ap = argparse.ArgumentParser(description="月度口碑报告流")
    ap.add_argument("--input", help="输入 JSON（shop/month/last_month/competitors/reviews）")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    payload = DEMO if a.demo else at.read_json(a.input) if a.input else ap.error("需要 --input / --demo 之一")
    r = build(payload, a.outdir)
    s = r["summary"]
    print(f"{s['门店']} {s['报告月份']} —— 月均分 {s['月均分']}（{s['环比']}），好评率 {s['好评率']}，"
          f"差评回复率 {s['差评回复率']}，Top2 根因：{'、'.join(s['根因Top2'])}")
    for f in r["files"]:
        print(" 产物:", f)
    at.emit(r)


if __name__ == "__main__":
    main()
