# -*- coding: utf-8 -*-
"""
评价采集与分级流 —— 端到端编排脚本。

流程：数据校验与去重 → 情感打分(星级+词表双通道) → 三级分桶(差/中/好评)
      → SLA 队列排序(响应窗口倒计时) → 汇总产物（Excel + PNG + JSON）

失败处理：
  - 必需字段缺失(stars/text) → 退出码 2，列缺失清单（不补默认值、不猜星级）
  - 平台名非法 → 退出码 2
  - 某桶为 0 → 显式写 0 条，不静默跳过
  - 食安/人身攻击词命中 → 无论星级按差评最高优先级，单打「转人工」标

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo
"""
from __future__ import annotations

import argparse
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

VALID_PLATFORMS = {"美团", "点评", "抖音"}

# 强负面词表：命中即按差评处理（无论星级），其中食安/攻击词触发「转人工」
STRONG_NEG = [
    (r"变质|发霉|馊|异物|头发丝|塑料|虫子|拉肚子|腹泻|食物中毒|过期", "食安", True),
    (r"垃圾|骗子|黑店|坑人|恶心|宰客|神经病|服务态度差|爱答不理|呵斥", "攻击/服务", False),
    (r"超时|等了\d+分钟|迟迟不到|漏送|撒漏|破损|凉了", "履约", False),
    (r"难吃|太咸|太淡|糊了|分量少|不值|失望|再也不来|避雷", "口味/期望", False),
]
POS_WORDS = r"好吃|很赞|不错|满意|推荐|回购|环境好|服务好|下次还来|分量足|性价比"

SLA = {"差评": "2 小时内", "中评": "24 小时内", "好评": "48 小时内"}
BUCKET_ORDER = {"差评": 0, "中评": 1, "好评": 2}

DEMO = {
    "shop": "陈记酸菜鱼（万达店）",
    "collect_at": "2026-09-30 09:00",
    "channel": "A",
    "reviews": [
        {"id": "M-2201", "platform": "美团", "stars": 1, "text": "鱼汤里有头发丝，这还怎么吃，太恶心了", "review_time": "2026-09-30 08:12", "reply_status": "未回复", "order_id": "MT90012"},
        {"id": "M-2202", "platform": "美团", "stars": 2, "text": "等了45分钟才上菜，孩子都饿哭了，味道还行", "review_time": "2026-09-30 07:40", "reply_status": "未回复", "order_id": "MT90018"},
        {"id": "D-1103", "platform": "抖音", "stars": 3, "text": "真是绝了，等了两小时才吃上，真是谢谢了", "review_time": "2026-09-29 21:05", "reply_status": "未回复", "order_id": "DY10033"},
        {"id": "D-1104", "platform": "抖音", "stars": 4, "text": "酸菜很正，鱼片嫩，下次还来", "review_time": "2026-09-29 20:11", "reply_status": "未回复", "order_id": "DY10040"},
        {"id": "P-3305", "platform": "点评", "stars": 5, "text": "环境好服务好，性价比高，值得推荐", "review_time": "2026-09-29 19:30", "reply_status": "未回复", "order_id": "DP20077"},
        {"id": "P-3306", "platform": "点评", "stars": 3, "text": "口味一般，上菜稍慢，还可以接受吧", "review_time": "2026-09-29 19:02", "reply_status": "已回复", "order_id": "DP20081"},
        {"id": "M-2201", "platform": "美团", "stars": 1, "text": "鱼汤里有头发丝，这还怎么吃，太恶心了", "review_time": "2026-09-30 08:12", "reply_status": "未回复", "order_id": "MT90012"},  # 重复，应去重
    ],
}


def step1_validate(payload):
    """步骤 1：数据校验与去重。字段缺失即失败，不补默认值。"""
    reviews = payload.get("reviews")
    if not reviews:
        print("[失败] 步骤1 缺少 reviews（评价数组）。请补数后重跑。", file=sys.stderr)
        sys.exit(2)
    missing, seen, rows = [], set(), []
    for r in reviews:
        rid = str(r.get("id", "")).strip()
        if not rid:
            missing.append("id")
            continue
        if rid in seen:
            continue
        seen.add(rid)
        pf = str(r.get("platform", "")).strip()
        if pf not in VALID_PLATFORMS:
            print(f"[失败] 步骤1 平台名非法：{rid} → {pf}（限 美团/点评/抖音）。", file=sys.stderr)
            sys.exit(2)
        if r.get("stars") is None:
            missing.append(f"{rid}.stars")
            continue
        if r.get("text") is None:
            missing.append(f"{rid}.text")
            continue
        rows.append(dict(r))
    if missing:
        print(f"[失败] 步骤1 必需字段缺失：{'、'.join(sorted(set(missing)))}。不补默认值，请补数后重跑。",
              file=sys.stderr)
        sys.exit(2)
    print(f"[步骤1] 校验通过：{len(rows)} 条评价（已按评价ID去重 {len(reviews) - len(rows)} 条）")
    return rows


def step2_score(rows):
    """步骤 2：情感打分（星级 + 词表双通道，冲突取更负）。"""
    for r in rows:
        text = str(r["text"])
        hits, manual = [], False
        for pat, label, is_manual in STRONG_NEG:
            found = re.findall(pat, text)
            if found:
                hits.append(f"{label}:{'/'.join(sorted(set(found))[:2])}")
                manual = manual or is_manual
        neg_hits = len(hits)
        pos_hits = re.findall(POS_WORDS, text)
        stars = int(r["stars"])
        # 双通道冲突：星级 ≥3 但词表命中强负面 → 取更负一档
        r["_strong_neg"] = neg_hits
        r["_pos"] = len(pos_hits)
        r["_hits"] = hits
        r["_manual"] = manual
        r["_conflict"] = stars >= 3 and neg_hits > 0
        r["_no_text"] = not text.strip()
    print("[步骤2] 双通道打分完成（星级+词表，冲突取更负）")
    return rows


def step3_bucket(rows):
    """步骤 3：三级分桶。食安/攻击词命中 → 无论星级按差评，食安另打转人工标。"""
    for r in rows:
        stars = int(r["stars"])
        if r["_strong_neg"] or stars <= 2:
            r["桶位"] = "差评"
        elif stars == 3:
            r["桶位"] = "中评"
        else:
            r["桶位"] = "好评"
        if r["_no_text"]:
            r["_note"] = "简版：无文字，细节待私信确认"
        elif r["_conflict"]:
            r["_note"] = "星级与文本冲突，按更负处理"
        else:
            r["_note"] = ""
    counts = {"差评": 0, "中评": 0, "好评": 0}
    for r in rows:
        counts[r["桶位"]] += 1
    print(f"[步骤3] 分桶完成：差评 {counts['差评']} / 中评 {counts['中评']} / 好评 {counts['好评']}")
    return rows, counts


def step4_sla(rows):
    """步骤 4：SLA 队列排序——差评(2h) > 中评(24h) > 好评(48h)；未回复优先、时间升序。"""
    for r in rows:
        r["响应窗口"] = SLA[r["桶位"]]
        if r["桶位"] == "差评" and r["_manual"]:
            r["响应窗口"] = "2 小时内（转人工）"
        if str(r.get("review_time", "")):
            r["_t"] = str(r["review_time"])
        else:
            r["_t"] = "9999"
            r["_note"] = (r["_note"] + "；发布时间缺失，排本桶末位").strip("；")
    rows.sort(key=lambda x: (BUCKET_ORDER[x["桶位"]], 0 if x.get("reply_status") == "未回复" else 1, x["_t"]))
    print("[步骤4] SLA 队列排序完成（差评 2 小时窗口优先，未回复置顶）")
    return rows


def step5_dispatch(rows):
    """步骤 5：处置分派（规则部分；语境反讽复核由模型按 prompt.txt 完成）。"""
    for r in rows:
        if r["桶位"] == "差评":
            if r["_manual"] or re.search(r"12315|律师函|媒体|曝光|集体投诉|食物中毒", str(r["text"])):
                r["处置"] = "转人工（升级信号），AI 停止自动回复"
            else:
                r["处置"] = "转差评灭火流程（negative-review-extinguish-flow）"
        elif r["桶位"] == "中评":
            r["处置"] = "提取改进点转周复盘"
        else:
            r["处置"] = "转好评感谢回复生成（positive-review-thanks-flow）"
        if "真是绝了" in str(r["text"]) or r["_note"].startswith("星级与文本冲突"):
            r["处置"] += "；⚠ 反讽疑似，人工复核改桶"
    print("[步骤5] 处置分派完成（食安/升级信号已拦截为转人工）")
    return rows


def build(payload, outdir):
    rows = step1_validate(payload)
    rows = step2_score(rows)
    rows, counts = step3_bucket(rows)
    rows = step4_sla(rows)
    rows = step5_dispatch(rows)

    out_rows = [{
        "评价ID": r["id"],
        "平台": r["platform"],
        "星级": int(r["stars"]),
        "桶位": r["桶位"],
        "强负面词": "；".join(r["_hits"]) or "（无）",
        "已回复": r.get("reply_status", ""),
        "响应窗口": r["响应窗口"],
        "处置": r["处置"],
        "备注": r["_note"],
        "评价正文": str(r["text"]),
    } for r in rows]

    n_bad_unreplied = sum(1 for r in rows if r["桶位"] == "差评" and r.get("reply_status") == "未回复")
    n_manual = sum(1 for r in rows if "转人工" in r["处置"])
    n = len(rows) or 1
    summary = {
        "门店": payload.get("shop", "未提供"),
        "采集批次": payload.get("collect_at", ""),
        "采集通道": payload.get("channel", "A"),
        "评价总量": len(rows),
        "差评": counts["差评"],
        "中评": counts["中评"],
        "好评": counts["好评"],
        "好评占比": f"{counts['好评'] / n * 100:.1f}%",
        "好评率健康线": "本地生活门店 ≥ 85%，低于则查根因分布",
        "未回复差评": n_bad_unreplied,
        "差评回复率目标": "100%（硬指标）",
        "转人工条数": n_manual,
        "AI 标识": "AI 生成内容",
    }

    at.ensure_outdir(outdir)
    xlsx = at.write_excel(
        os.path.join(outdir, "分级处理队列.xlsx"),
        {
            "处理队列": out_rows or [{"评价ID": "（无评价）"}],
            "汇总": [{"项": k, "内容": str(v)} for k, v in summary.items()],
        },
        highlights={
            "处理队列": {"桶位": "contains:差评", "响应窗口": "contains:转人工"},
        },
        widths={"处理队列": {"评价正文": 32, "处置": 38, "响应窗口": 18}},
    )
    pie = at.pie_chart(
        os.path.join(outdir, "评价星级分布.png"),
        [k for k in ["差评", "中评", "好评"] if counts[k]],
        [counts[k] for k in ["差评", "中评", "好评"] if counts[k]],
        title=f"评价三级分桶（{payload.get('shop', '')}）",
    )
    js = at.write_json({"summary": summary, "items": out_rows, "generated_at": at.stamp(),
                        "note": "分桶/排序为规则计算；反讽与语境复核由模型按 prompt.txt 复核"},
                       os.path.join(outdir, "collect_grade.json"))
    return {"files": [xlsx, pie, js], "summary": summary}


def main():
    ap = argparse.ArgumentParser(description="评价采集与分级流")
    ap.add_argument("--input", help="输入 JSON（shop/collect_at/channel/reviews）")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    payload = DEMO if a.demo else at.read_json(a.input) if a.input else ap.error("需要 --input / --demo 之一")
    r = build(payload, a.outdir)
    s = r["summary"]
    print(f"{s['门店']} —— 共 {s['评价总量']} 条：差评 {s['差评']} / 中评 {s['中评']} / 好评 {s['好评']}，"
          f"未回复差评 {s['未回复差评']} 条（回复率目标 100%），转人工 {s['转人工条数']} 条")
    for f in r["files"]:
        print(" 产物:", f)
    at.emit(r)


if __name__ == "__main__":
    main()
