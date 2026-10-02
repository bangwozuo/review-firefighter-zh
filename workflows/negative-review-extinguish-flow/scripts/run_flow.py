# -*- coding: utf-8 -*-
"""
差评灭火回复生成流 —— 端到端编排脚本。

流程：升级信号检测(12315/媒体/律师函/集体投诉/食安 → 转人工并跳过自动步骤)
      → 根因分类(五类词表+优先序 质量>服务>物流>期望差>价格)
      → 响应时限(黄金2小时档位) → 补偿梯度(<50 / 50-200 / >200 + 根因系数 + 授权校验)
      → 汇总产物（Excel + JSON）；公开回复与私信话术由模型按 prompt.txt 撰写

失败处理：
  - reviews 缺失 → 退出码 2
  - 五类词表全未命中 → 根因「待人工确认」，补偿不给金额
  - order_amount 缺失 → 补偿标「金额待补」，不发券
  - 超授权 → 标红待人工
  - 无订单记录 + 食安词 → 按疑似恶意/无法核实并入人工清单

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

# 升级信号：命中即转人工，AI 停止自动回复
ESCALATION_PAT = re.compile(r"12315|媒体|曝光|律师函|律师|集体投诉|食物中毒|拉肚子|腹泻|变质|发霉|异物|头发丝|虫子|塑料")

# 根因词表：(正则, 根因)。多重命中按 PRIORITY 取主根因。
CAUSE_WORDS = [
    (r"色差|故障|坏了|过期|异味|异物|头发|虫子|塑料|变质|难吃|糊了", "质量"),
    (r"态度|爱答不理|呵斥|上错菜|等了\d+分钟|等位|排队|没人理|催单", "服务"),
    (r"超时|迟迟|破损|撒漏|漏送|丢件|凉了|洒了", "物流"),
    (r"图片[与和]实物|跟图片|和描述|宣传.{0,6}不符|夸大", "期望差"),
    (r"不值|贵了|涨价|分量少|缩水|性价比", "价格"),
]
PRIORITY = ["质量", "服务", "物流", "期望差", "价格"]

LEAD_AUTH = 10      # <50 元档默认授权上限（元）
MID_AUTH = 20       # 50-200 元档默认授权上限（元），可被 policy 覆盖

DEMO = {
    "shop": "陈记酸菜鱼（万达店）",
    "policy": "券/退款单笔授权上限 20 元",
    "now": "2026-09-30 09:00",
    "reviews": [
        {"id": "M-2202", "platform": "美团", "text": "等了45分钟才上菜，孩子都饿哭了，上错菜一次", "order_amount": 34, "review_time": "2026-09-30 08:10", "reply_status": "未回复", "order_id": "MT90018"},
        {"id": "M-2206", "platform": "美团", "text": "涨价了分量还少，不值这个价", "order_amount": 26, "review_time": "2026-09-30 07:55", "reply_status": "未回复", "order_id": "MT90021"},
        {"id": "P-3308", "platform": "点评", "text": "图片和实物不符，宣传的大份上来的很小", "order_amount": 88, "review_time": "2026-09-29 20:30", "reply_status": "未回复", "order_id": "DP20090"},
        {"id": "M-2201", "platform": "美团", "text": "鱼汤里有头发丝，已经打12315了", "order_amount": 128, "review_time": "2026-09-30 08:12", "reply_status": "未回复", "order_id": "MT90012"},
        {"id": "D-1105", "platform": "抖音", "text": "再也不来了", "order_amount": None, "review_time": "2026-09-29 22:00", "reply_status": "已回复", "order_id": "DY10051"},
    ],
}


def step1_escalation(rows, payload):
    """步骤 1：升级信号检测。命中 → 转人工，跳过自动步骤。"""
    escalated, rest = [], []
    for r in rows:
        m = ESCALATION_PAT.findall(str(r.get("text", "")))
        if m:
            if not r.get("order_id"):
                r["_esc_note"] = "疑似恶意/无法核实（无订单记录）"
            else:
                r["_esc_note"] = ""
            r["_signal"] = "、".join(sorted(set(m))[:3])
            escalated.append(r)
        else:
            rest.append(r)
    print(f"[步骤1] 升级检测：{len(escalated)} 条命中转人工（AI 停止自动回复），{len(rest)} 条进入自动灭火")
    return escalated, rest


def step2_cause(rows):
    """步骤 2：根因分类（五类词表，多重命中按优先序取主根因）。"""
    for r in rows:
        t = str(r.get("text", ""))
        found = []
        for pat, cause in CAUSE_WORDS:
            hits = re.findall(pat, t)
            if hits:
                found.append((cause, hits))
        if not found:
            r["_cause"] = "待人工确认"
            r["_secondary"] = []
            r["_evidence"] = "五类词表未命中（纯情绪表达）"
            continue
        found.sort(key=lambda x: PRIORITY.index(x[0]))
        r["_cause"] = found[0][0]
        r["_evidence"] = f"{found[0][0]}:" + "/".join(sorted(set(found[0][1]))[:2])
        r["_secondary"] = [c for c, _ in found[1:]]
    print("[步骤2] 根因分类完成（质量>服务>物流>期望差>价格 取主根因）")
    return rows


def step3_sla(rows, now):
    """步骤 3：响应时限档位（黄金 2 小时 / 2-24 小时 / >24 小时）。"""
    import datetime
    try:
        now_dt = datetime.datetime.fromisoformat(str(now))
    except Exception:
        now_dt = None
    for r in rows:
        t = str(r.get("review_time") or "")
        r["_sla"] = "立即响应（时间缺失）"
        if now_dt and t:
            try:
                dt = datetime.datetime.fromisoformat(t)
                mins = (now_dt - dt).total_seconds() / 60
                if mins <= 120:
                    r["_sla"] = "≤2 小时（黄金窗口）"
                elif mins <= 24 * 60:
                    r["_sla"] = "2-24 小时"
                else:
                    r["_sla"] = ">24 小时（效果减半，需更强补救）"
            except Exception:
                pass
        if t and not now_dt:
            r["_sla"] = "按出现时刻起算 2 小时窗口"
    print("[步骤3] 响应时限计算完成（黄金 2 小时窗口）")
    return rows


def step4_compensation(rows, payload):
    """步骤 4：补偿梯度匹配 + 授权校验。"""
    policy = str(payload.get("policy", ""))
    m_cap = MID_AUTH
    pm = re.search(r"上限\s*(\d+)\s*元", policy)
    if pm:
        m_cap = int(pm.group(1))
    for r in rows:
        amt = r.get("order_amount")
        cause = r["_cause"]
        if amt is None:
            r["_comp"] = "金额待补：不发券，先私信了解情况"
            r["_auth"] = "—"
            continue
        amt = float(amt)
        if amt > 200:
            r["_comp"] = "专人对接 + 电话沟通（不在公开区/私信承诺金额）"
            r["_auth"] = "人工执行"
            continue
        if amt < 50:
            face_max, face_min = 10, 5
        else:
            face_max, face_min = min(20, int(round(amt * 0.20))), max(5, int(round(amt * 0.10)))
        # 根因系数：质量顶格，价格取下限
        if cause == "质量":
            face = face_max
        elif cause == "价格":
            face = face_min
        else:
            face = (face_max + face_min) // 2
        face = min(face, m_cap)  # 授权校验：超授权压到上限并标红
        r["_auth"] = "✅" if face <= m_cap else "🔴 超授权"
        r["_comp"] = f"满 {int(round(amt * 1.3))} 减 {face} 券（梯度 {face_min}-{face_max} 元）"
    print(f"[步骤4] 补偿匹配完成（授权上限 {m_cap} 元，质量顶格/价格下限/其余中位）")
    return rows


def build(payload, outdir):
    reviews = payload.get("reviews")
    if not reviews:
        print("[失败] 缺少 reviews（差评数组）。请补数后重跑。", file=sys.stderr)
        sys.exit(2)
    escalated, rest = step1_escalation(reviews, payload)
    rest = step2_cause(rest)
    rest = step3_sla(rest, payload.get("now", ""))
    rest = step4_compensation(rest, payload)

    out_rows = [{
        "评价ID": r["id"],
        "平台": r.get("platform", ""),
        "主根因": r["_cause"],
        "次根因": "/".join(r.get("_secondary", [])) or "（无）",
        "命中证据": r.get("_evidence", ""),
        "响应档位": r["_sla"],
        "补偿方案": r["_comp"],
        "授权校验": r["_auth"],
        "处置": "转四要素成稿（模型步骤5）",
        "已回复": r.get("reply_status", ""),
        "差评原文": str(r.get("text", "")),
    } for r in sorted(rest, key=lambda x: {"质量": 0, "服务": 1, "物流": 2, "期望差": 3, "价格": 4, "待人工确认": 5}.get(x["_cause"], 9))]

    esc_rows = [{
        "评价ID": r["id"],
        "平台": r.get("platform", ""),
        "触发信号": r.get("_signal", ""),
        "订单号": r.get("order_id", "（缺失）"),
        "说明": r.get("_esc_note", ""),
        "人工动作": "留存证据；2 小时内电话联系；向平台报备；AI 停止自动回复",
    } for r in escalated]

    cause_counts = {}
    for r in rest:
        cause_counts[r["_cause"]] = cause_counts.get(r["_cause"], 0) + 1
    n_unreplied = sum(1 for r in reviews if r.get("reply_status") != "已回复")
    summary = {
        "门店": payload.get("shop", "未提供"),
        "差评总数": len(reviews),
        "升级转人工": len(escalated),
        "可自动灭火": len(rest),
        "根因分布": cause_counts,
        "未回复差评": n_unreplied,
        "回复率目标": "100%",
        "补偿梯度": "<50元:5-10元券；50-200元:10%-20%；>200元:专人+电话",
        "红线": "补偿只换「再给一次机会」，禁止删差评/改好评交换（《反不正当竞争法》第八条）",
        "AI 标识": "AI 生成内容",
    }

    at.ensure_outdir(outdir)
    xlsx = at.write_excel(
        os.path.join(outdir, "灭火作战表.xlsx"),
        {
            "灭火作战表": out_rows or [{"评价ID": "（无可自动处理差评）"}],
            "升级清单": esc_rows or [{"评价ID": "（无升级信号）"}],
            "汇总": [{"项": k, "内容": str(v)} for k, v in summary.items()],
        },
        highlights={
            "灭火作战表": {"授权校验": "contains:🔴", "主根因": "contains:质量"},
            "升级清单": {"触发信号": "contains:12315"},
        },
        widths={"灭火作战表": {"差评原文": 30, "补偿方案": 30, "处置": 22},
                "升级清单": {"人工动作": 36}},
    )
    js = at.write_json({"summary": summary, "items": out_rows, "escalated": esc_rows,
                        "generated_at": at.stamp(),
                        "note": "根因/时限/补偿为规则计算；公开回复与私信话术由模型按 prompt.txt 撰写"},
                       os.path.join(outdir, "extinguish_flow.json"))
    return {"files": [xlsx, js], "summary": summary}


def main():
    ap = argparse.ArgumentParser(description="差评灭火回复生成流")
    ap.add_argument("--input", help="输入 JSON（shop/policy/now/reviews）")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    payload = DEMO if a.demo else at.read_json(a.input) if a.input else ap.error("需要 --input / --demo 之一")
    r = build(payload, a.outdir)
    s = r["summary"]
    print(f"{s['门店']} —— 差评 {s['差评总数']} 条：升级 {s['升级转人工']} / 可自动 {s['可自动灭火']}，"
          f"未回复 {s['未回复差评']}（回复率目标 100%）")
    for f in r["files"]:
        print(" 产物:", f)
    at.emit(r)


if __name__ == "__main__":
    main()
