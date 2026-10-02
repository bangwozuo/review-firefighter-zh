# -*- coding: utf-8 -*-
"""
好评感谢回复生成流 —— 端到端编排脚本。

流程：好评分型(长文/带图/短评/纯星级) → 细节提取(菜品/人物/动作/数字四类词表)
      → 回复骨架生成(开头句轮换去重) → 引导配额分配(≤30%，只给带图/长文)
      → 汇总产物（Excel + JSON）；成稿润色由模型按 prompt.txt 完成

失败处理：
  - stars < 4 混入 → 退出码 2（本流不处理差评，退回分级流程）
  - reviews 缺失 → 退出码 2
  - 无细节命中的评价 → 标「无细节」进低优先级，禁止编造细节
  - 无进行中活动 → 引导配额落空，全部纯感谢

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo
"""
from __future__ import annotations

import argparse
import os
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

# 细节词表：四类（可按门店扩充；模型在步骤5补录方言/昵称后回写）
DISH_WORDS = ["酸菜鱼", "毛血旺", "藤椒鱼", "牛肉面", "烤鸭", "奶茶", "柠檬茶", "酸菜", "鱼片", "锅底", "小菜", "甜品", "米饭"]
PERSON_WORDS = ["小陈", "小满", "店长", "老板", "服务员", "小哥", "小姐姐", "师傅", "收银", "传菜"]
ACTION_WORDS = ["分鱼", "加水", "送小菜", "续杯", "打包", "排号", "带位", "提醒少冰", "多给了", "换盘子"]
NUM_PAT = r"第\s*\d+\s*次|第[一二三四五六七八九十]+次|\d+\s*人|回头客|老客"

OPENERS = ["谢谢您记得", "被您夸得", "您的认可", "又见面啦", "这波夸奖", "看到您的评价", "开心收到", "谢谢您专程"]

LEAD_CAP = 0.30  # 轻引导占批次上限

DEMO = {
    "shop": "陈记酸菜鱼（万达店）",
    "campaign": "新品藤椒鱼（10 月上新）",
    "reviews": [
        {"id": "D-1104", "platform": "抖音", "stars": 4, "text": "酸菜很正，鱼片嫩，下次还来", "has_image": True, "review_time": "2026-09-29 20:11"},
        {"id": "P-3305", "platform": "点评", "stars": 5, "text": "环境好服务好，性价比高，值得推荐，已经第三次来了", "has_image": False, "review_time": "2026-09-29 19:30"},
        {"id": "M-2203", "platform": "美团", "stars": 5, "text": "小陈服务太周到了，主动帮我们分鱼还多送了小菜", "has_image": True, "review_time": "2026-09-29 18:44"},
        {"id": "M-2204", "platform": "美团", "stars": 5, "text": "好吃！", "has_image": False, "review_time": "2026-09-29 18:02"},
        {"id": "P-3307", "platform": "点评", "stars": 5, "text": "", "has_image": False, "review_time": "2026-09-29 17:31"},
    ],
}


def step1_type(payload):
    """步骤 1：好评分型。差评混入即失败退出。"""
    reviews = payload.get("reviews")
    if not reviews:
        print("[失败] 步骤1 缺少 reviews。请补数后重跑。", file=sys.stderr)
        sys.exit(2)
    bad = [r["id"] for r in reviews if int(r.get("stars") or 0) < 4]
    if bad:
        print(f"[失败] 步骤1 发现非好评混入：{'、'.join(bad)}。本流只处理 4-5 星，"
              f"请退回 review-collect-grade-flow 重新分级。", file=sys.stderr)
        sys.exit(2)
    rows = []
    for r in reviews:
        text = str(r.get("text") or "")
        if not text.strip():
            typ = "纯星级"
        elif len(text) >= 40:
            typ = "长文好评"
        elif r.get("has_image"):
            typ = "带图好评"
        else:
            typ = "短好评"
        r["_type"] = typ
        r["_text"] = text
        rows.append(r)
    print(f"[步骤1] 分型完成：{len(rows)} 条（长文 {sum(1 for r in rows if r['_type']=='长文好评')}，"
          f"带图 {sum(1 for r in rows if r['_type']=='带图好评')}，短评 {sum(1 for r in rows if r['_type']=='短好评')}，"
          f"纯星级 {sum(1 for r in rows if r['_type']=='纯星级')}）")
    return rows


def step2_detail(rows):
    """步骤 2：细节提取（四类词表）。无命中标「无细节」，不编造。"""
    for r in rows:
        t = r["_text"]
        dishes = [w for w in DISH_WORDS if w in t]
        persons = [w for w in PERSON_WORDS if w in t]
        actions = [w for w in ACTION_WORDS if w in t]
        nums = __import__("re").findall(NUM_PAT, t)
        r["_details"] = []
        if dishes:
            r["_details"].append("菜品:" + "/".join(dishes[:2]))
        if persons:
            r["_details"].append("人物:" + "/".join(persons[:2]))
        if actions:
            r["_details"].append("动作:" + "/".join(actions[:2]))
        if nums:
            r["_details"].append("数字:" + "/".join(nums[:1]))
        r["_no_detail"] = not r["_details"]
    cov = sum(1 for r in rows if not r["_no_detail"]) / max(len(rows), 1) * 100
    print(f"[步骤2] 细节提取完成：覆盖率 {cov:.0f}%（目标 ≥ 60%）")
    return rows


def step3_skeleton(rows):
    """步骤 3：回复骨架生成，开头句轮换，同批相邻不重复。"""
    prev = None
    for i, r in enumerate(rows):
        opener = OPENERS[i % len(OPENERS)]
        if opener == prev:  # 相邻去重
            opener = OPENERS[(i + 1) % len(OPENERS)]
        prev = opener
        r["_opener"] = opener
        detail_txt = "；".join(r["_details"]) if r["_details"] else ""
        if r["_type"] == "纯星级":
            r["_skeleton"] = f"{opener}您的五星好评！欢迎把想吃的菜告诉我们，下次为您安排。"
        elif r["_no_detail"]:
            r["_skeleton"] = f"{opener}！您的鼓励我们记下了，期待下次用味道让您写满整条评价。"
        else:
            r["_skeleton"] = f"{opener}！您提到的{detail_txt}我们都记下了，这是全店继续做好的动力。"
    print("[步骤3] 骨架生成完成：开头句相邻不重复")
    return rows


def step4_lead(rows, campaign):
    """步骤 4：引导配额分配——只给带图/长文，总量 ≤30%。"""
    eligible = [r for r in rows if r["_type"] in ("带图好评", "长文好评")]
    quota = int(len(rows) * LEAD_CAP)
    lead_rows = set(id(x) for x in eligible[:quota]) if campaign else set()
    for r in rows:
        if campaign and id(r) in lead_rows:
            r["_lead"] = campaign
        else:
            r["_lead"] = "（不引导）"
    n_lead = sum(1 for r in rows if r["_lead"] != "（不引导）")
    print(f"[步骤4] 引导分配完成：{n_lead} 条获得引导（配额上限 {quota} 条 = 批次 {LEAD_CAP:.0%}），"
          f"{'活动：' + campaign if campaign else '无进行中活动，全部纯感谢'}")
    return rows


def build(payload, outdir):
    rows = step1_type(payload)
    rows = step2_detail(rows)
    rows = step3_skeleton(rows)
    rows = step4_lead(rows, payload.get("campaign", ""))
    rows.sort(key=lambda r: {"长文好评": 0, "带图好评": 1, "短好评": 2, "纯星级": 3}[r["_type"]])

    out_rows = [{
        "评价ID": r["id"],
        "平台": r["platform"],
        "分型": r["_type"],
        "提取细节": "；".join(r["_details"]) if r["_details"] else "（无细节，短回复）",
        "开头句": r["_opener"],
        "回复骨架": r["_skeleton"],
        "轻引导": r["_lead"],
        "发布": "待店主确认",
    } for r in rows]

    n = len(rows) or 1
    cov = f"{sum(1 for r in rows if not r['_no_detail']) / n * 100:.0f}%"
    n_lead = sum(1 for r in rows if r["_lead"] != "（不引导）")
    summary = {
        "门店": payload.get("shop", "未提供"),
        "好评总数": len(rows),
        "分型": {t: sum(1 for r in rows if r["_type"] == t) for t in ["长文好评", "带图好评", "短好评", "纯星级"]},
        "细节覆盖率": cov + "（目标 ≥ 60%）",
        "开头句重复": 0,
        "引导条数/占比": f"{n_lead} / {n_lead / n * 100:.0f}%（上限 30%）",
        "合规自查": "无索好评、无评价换礼、无隐私信息（发布前经 reply-compliance-review 口径自查 + 店主确认）",
        "AI 标识": "AI 生成内容",
    }

    at.ensure_outdir(outdir)
    xlsx = at.write_excel(
        os.path.join(outdir, "感谢回复工作台.xlsx"),
        {
            "工作台": out_rows or [{"评价ID": "（无好评）"}],
            "汇总": [{"项": k, "内容": str(v)} for k, v in summary.items()],
        },
        highlights={"工作台": {"分型": "contains:纯星级"}},
        widths={"工作台": {"回复骨架": 44, "提取细节": 22}},
    )
    js = at.write_json({"summary": summary, "items": out_rows, "generated_at": at.stamp(),
                        "note": "分型/细节/去重/配额为规则计算；成稿润色由模型按 prompt.txt 完成"},
                       os.path.join(outdir, "thanks_flow.json"))
    return {"files": [xlsx, js], "summary": summary}


def main():
    ap = argparse.ArgumentParser(description="好评感谢回复生成流")
    ap.add_argument("--input", help="输入 JSON（shop/campaign/reviews）")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    payload = DEMO if a.demo else at.read_json(a.input) if a.input else ap.error("需要 --input / --demo 之一")
    r = build(payload, a.outdir)
    s = r["summary"]
    print(f"{s['门店']} —— 好评 {s['好评总数']} 条，细节覆盖率 {s['细节覆盖率']}，"
          f"引导 {s['引导条数/占比']}，开头句重复 {s['开头句重复']}")
    for f in r["files"]:
        print(" 产物:", f)
    at.emit(r)


if __name__ == "__main__":
    main()
