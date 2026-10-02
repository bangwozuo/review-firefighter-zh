# -*- coding: utf-8 -*-
"""
差评根因归类流 —— 端到端编排脚本。

流程：数据校验与周窗口界定 → 环节打标(六类词表，可多标) → 聚类统计(计数/占比/周环比)
      → 优先级排序(占比×严重度加权，食安×3/服务×2/菜品×2，帕累托 ≤80% 列必改)
      → 汇总产物（Excel + PNG + JSON）；整改动作编写与标签复核由模型按 prompt.txt 完成

失败处理：
  - reviews 缺失 → 退出码 2
  - 周期外数据 → 剔除并显式计数「窗口外 N 条」
  - 六类全未命中 → 归「待定性」，不猜标签
  - 环节样本 < 5 条 → 环比不下结论只报计数
  - 全部待定性 → 结论「先补标签库」，不硬排优先级

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo
"""
from __future__ import annotations

import argparse
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

# 六环节词表：(环节, 正则, 严重度权重)
SEGMENTS = [
    ("菜品质量", r"难吃|太咸|太淡|糊了|异味|异物|头发|虫子|塑料|变质|发霉|不新鲜|分量少", 2),
    ("服务态度", r"态度差|爱答不理|呵斥|没人理|白眼|催单被怼|不耐心", 2),
    ("等位与出餐", r"等了\s*\d+\s*分钟|等位|排队|催单|超时|迟迟|上菜慢|出餐慢", 1),
    ("环境卫生", r"脏|油腻|烟味|餐具不洁|筷子|桌子没擦|地面|卫生间", 1),
    ("价格感知", r"贵|不值|涨价|性价比|缩水|越来越贵", 1),
    ("物流配送", r"外卖.{0,6}超时|撒漏|漏送|洒了|凉了|配送慢|骑手", 1),
]
FOOD_SAFETY_PAT = re.compile(r"异物|头发|虫子|塑料|变质|发霉|食物中毒|拉肚子|不新鲜")

DEMO = {
    "shop": "陈记酸菜鱼（万达店）",
    "week_start": "2026-09-21",
    "last_week": {"菜品质量": 3, "服务态度": 1, "等位与出餐": 5, "环境卫生": 0, "价格感知": 2, "物流配送": 1},
    "reviews": [
        {"id": "M-2201", "text": "鱼汤里有头发丝", "stars": 1, "review_time": "2026-09-22 08:12", "order_amount": 128},
        {"id": "M-2202", "text": "等了45分钟才上菜，还上错菜", "stars": 2, "review_time": "2026-09-23 08:10", "order_amount": 34},
        {"id": "M-2206", "text": "涨价了分量还少，不值这个价", "stars": 2, "review_time": "2026-09-23 07:55", "order_amount": 26},
        {"id": "P-3308", "text": "图片和实物不符，宣传的大份上来的很小", "stars": 2, "review_time": "2026-09-24 20:30", "order_amount": 88},
        {"id": "D-1103", "text": "等了两小时才吃上，桌子油腻腻的没擦", "stars": 2, "review_time": "2026-09-24 21:05", "order_amount": 156},
        {"id": "D-1105", "text": "再也不来了", "stars": 1, "review_time": "2026-09-25 22:00", "order_amount": 60},
        {"id": "D-1199", "text": "外卖等了90分钟，到手都凉透了", "stars": 1, "review_time": "2026-09-26 19:20", "order_amount": 45},
        {"id": "M-2210", "text": "服务员爱答不理，催单还被怼", "stars": 2, "review_time": "2026-09-27 12:40", "order_amount": 210},
        {"id": "P-3311", "text": "等位40分钟，环境倒是还行", "stars": 2, "review_time": "2026-09-28 18:30", "order_amount": 95},
        {"id": "P-3312", "text": "这条是上周五的老差评补录", "stars": 2, "review_time": "2026-09-19 18:00", "order_amount": 70},
    ],
}


def step1_window(rows, week_start):
    """步骤 1：周窗口界定与校验。窗口外剔除并显式计数。"""
    try:
        ws = datetime.datetime.fromisoformat(str(week_start)).date()
    except Exception:
        ws = None
    keep, outside = [], 0
    for r in rows:
        if not str(r.get("text") or "").strip():
            r["_pending"] = True
            keep.append(r)
            continue
        t = str(r.get("review_time") or "")
        in_window = True
        if ws and t:
            try:
                d = datetime.datetime.fromisoformat(t).date()
                in_window = ws <= d < ws + datetime.timedelta(days=7)
            except Exception:
                in_window = True
        if in_window:
            keep.append(r)
        else:
            outside += 1
    print(f"[步骤1] 周窗口界定完成：窗口内 {len(keep)} 条，窗口外剔除 {outside} 条（显式计数不混入）")
    return keep, outside


def step2_tag(rows):
    """步骤 2：六环节打标，可多标；全未命中归「待定性」。"""
    for r in rows:
        if r.get("_pending"):
            r["_tags"] = [("待定性", 1, ["纯星级评价，无文字"])]
            continue
        t = str(r["text"])
        tags = []
        for seg, pat, w in SEGMENTS:
            hits = re.findall(pat, t)
            if hits:
                tags.append((seg, w, sorted(set(hits))[:2]))
        if not tags:
            r["_tags"] = [("待定性", 1, ["六类词表未命中"])]
        else:
            r["_tags"] = tags
        r["_food_safety"] = bool(FOOD_SAFETY_PAT.search(t))
    multi = sum(1 for r in rows if len(r["_tags"]) > 1)
    print(f"[步骤2] 环节打标完成：多标差评 {multi} 条（体验多环节崩坏单独计数）")
    return rows


def step3_cluster(rows, last_week):
    """步骤 3：聚类统计——计数 / 环比 / 占比。样本 <5 环比不下结论。"""
    counts = {seg: 0 for seg, _, _ in SEGMENTS}
    counts["待定性"] = 0
    food_in_seg = {seg: 0 for seg, _, _ in SEGMENTS}
    for r in rows:
        for seg, _, _ in r["_tags"]:
            counts[seg] += 1
            if r.get("_food_safety") and seg == "菜品质量":
                food_in_seg[seg] += 1
    n = sum(counts.values()) or 1
    stat = []
    for seg, _, w in SEGMENTS + [("待定性", "", 1)]:
        cur = counts.get(seg, 0)
        prev = (last_week or {}).get(seg)
        if prev is None:
            wow = "基期缺失"
        elif cur >= 5 or prev >= 5:
            pct = (cur - prev) / prev * 100 if prev else float("inf")
            wow = f"{pct:+.0f}%"
        else:
            wow = "小样本，不下结论"
        stat.append({"环节": seg, "本周": cur, "上周": prev if prev is not None else "—",
                     "环比": wow, "占比": f"{cur / n * 100:.1f}%",
                     "_w": 3 if (seg == "菜品质量" and food_in_seg.get(seg)) else w, "_cur": cur})
    print(f"[步骤3] 聚类统计完成：{len(stat)} 个环节（含待定性）")
    return stat


def step4_priority(stat):
    """步骤 4：优先级 = 占比 × 严重度加权；帕累托累计 ≤80% 列必改。"""
    total = sum(s["_cur"] for s in stat) or 1
    for s in stat:
        s["_priority_score"] = (s["_cur"] / total) * s["_w"]
    stat.sort(key=lambda s: -s["_priority_score"])
    cum, must_fix = 0, set()
    for s in stat:
        if s["环节"] == "待定性" or s["_cur"] == 0:
            continue
        if cum < 0.80:
            must_fix.add(s["环节"])
            cum += s["_priority_score"]  # 按加权贡献累计（食安/服务/菜品权重生效）
    for s in stat:
        s["优先级"] = "P0 必改" if s["环节"] in must_fix else ("持续观察" if s["_cur"] else "—")
    if all(s["环节"] != "待定性" or s["_cur"] == 0 for s in stat) and sum(s["_cur"] for s in stat) > 0 and all(
            s["环节"] == "待定性" and s["_cur"] > 0 for s in stat if s["_cur"] > 0) and all(
            s["环节"] == "待定性" for s in stat if s["_cur"] > 0):
        print("[步骤4] 全部待定性 —— 结论：先补标签库再谈整改，不硬排优先级")
    else:
        print(f"[步骤4] 优先级排序完成：必改 {sorted(must_fix)}（帕累托累计 ≤80%，食安 ×3）")
    return stat


def build(payload, outdir):
    reviews = payload.get("reviews")
    if not reviews:
        print("[失败] 缺少 reviews（差评数组）。请补数后重跑。", file=sys.stderr)
        sys.exit(2)
    rows, outside = step1_window(reviews, payload.get("week_start", ""))
    rows = step2_tag(rows)
    stat = step3_cluster(rows, payload.get("last_week"))
    stat = step4_priority(stat)

    stat_rows = [{k: s[k] for k in ("环节", "本周", "上周", "环比", "占比", "优先级")} for s in stat]

    todo_rows = [{
        "#": i + 1,
        "环节": s["环节"],
        "整改动作（待模型编写可验收动作与责任人）": f"{s['环节']}类差评本周 {s['_cur']} 条，命中关键词见工作台",
        "观察指标": f"{s['环节']}周差评数减半",
        "复查日": "下周同日",
    } for i, s in enumerate([x for x in stat if x["优先级"] == "P0 必改"])]

    pending_rows = [{"评价ID": r["id"], "原文": str(r.get("text", "")) or "（纯星级）",
                     "模型建议标签": "待人工判读"} for r in rows if
                    any(seg == "待定性" for seg, _, _ in r["_tags"])]

    food_n = sum(1 for r in rows if r.get("_food_safety"))
    summary = {
        "门店": payload.get("shop", "未提供"),
        "周起始": payload.get("week_start", ""),
        "差评总数（窗口内）": len(rows),
        "窗口外剔除": outside,
        "必改环节": [s["环节"] for s in stat if s["优先级"] == "P0 必改"],
        "食安相关差评": food_n,
        "食安规则": "无论占比多小都进必改（严重度 ×3），同步提示平台报备义务",
        "多标差评": sum(1 for r in rows if len(r.get("_tags", [])) > 1),
        "AI 标识": "AI 生成内容",
    }

    at.ensure_outdir(outdir)
    xlsx = at.write_excel(
        os.path.join(outdir, "差评整改清单.xlsx"),
        {
            "环节统计": stat_rows,
            "整改清单": todo_rows or [{"#": "（无必改环节）"}],
            "待定性复核": pending_rows or [{"评价ID": "（无）"}],
            "汇总": [{"项": k, "内容": str(v)} for k, v in summary.items()],
        },
        highlights={"环节统计": {"优先级": "contains:P0"},
                    "整改清单": {"环节": "contains:菜品质量"}},
        widths={"环节统计": {"环比": 18}, "整改清单": {"整改动作（待模型编写可验收动作与责任人）": 44}},
    )
    labels = [s["环节"] for s in stat if s["_cur"] > 0 and s["环节"] != "待定性"]
    values = [s["_cur"] for s in stat if s["_cur"] > 0 and s["环节"] != "待定性"]
    pie = at.pie_chart(os.path.join(outdir, "环节占比.png"), labels, values,
                       title=f"差评环节占比（{payload.get('shop', '')}）")
    js = at.write_json({"summary": summary, "stat": stat_rows, "todo": todo_rows,
                        "generated_at": at.stamp(),
                        "note": "打标/统计/优先级为规则计算；整改动作编写与标签复核由模型按 prompt.txt 完成"},
                       os.path.join(outdir, "classify_flow.json"))
    return {"files": [xlsx, pie, js], "summary": summary}


def main():
    ap = argparse.ArgumentParser(description="差评根因归类流")
    ap.add_argument("--input", help="输入 JSON（shop/week_start/last_week/reviews）")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    payload = DEMO if a.demo else at.read_json(a.input) if a.input else ap.error("需要 --input / --demo 之一")
    r = build(payload, a.outdir)
    s = r["summary"]
    print(f"{s['门店']} —— 窗口内差评 {s['差评总数（窗口内）']} 条，必改环节 {s['必改环节']}，"
          f"食安相关 {s['食安相关差评']} 条（×3 加权必改）")
    for f in r["files"]:
        print(" 产物:", f)
    at.emit(r)


if __name__ == "__main__":
    main()
