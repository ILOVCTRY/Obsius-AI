"""情报打分与简报合成（E9）+ 学习档案与周计划（E10），DESIGN.md §16。

LLM 用 classifier 小模型路由（只喂标题+RSS 摘要段，每条几百 token）；
**LLM 缺席（无 key / 调用失败）不阻塞情报功能**——降级为规则打分与模板简报，
score_detail 标 llm/rule 供前端区分。打分 = 方向相关性（profile 权重）× 技术深度。
E10 隐私红线：周计划等 LLM 入参只含**元数据**（标题/标签/目录/计数），笔记正文永不出本机。
"""

import json
import re
from datetime import datetime, timedelta, timezone

from core.intel.config import DIRECTIONS
from core.intel.fetch import _CVE_RE, fetch_all

MAX_TOKENS_BUDGET = 2000

# 规则打分兜底：方向关键词 × 深度关键词；命中即计分（粗糙但可解释）
_DIRECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web": ("web", "xss", "sql", "注入", "ssrf", "rce", "csrf", "反序列化", "沙箱逃逸",
            "浏览器", "php", "java", "spring", "nginx", "前端"),
    "ai": ("llm", "大模型", "ai", "gpt", "prompt", "越狱", "机器学习", "深度学习",
           "神经网络", "ml", "模型安全"),
    "vehicle": ("车联网", "can", "automotive", "tesla", "车辆", "ota", "tbox", "ecu",
                "autosar"),
    "reverse": ("逆向", "reverse", "反编译", "ida", "ghidra", "静态分析", "混淆",
                "脱壳", "fuzzing", "模糊测试", "符号执行"),
    "android": ("android", "安卓", "apk", "app 逆向", "ios", "移动安全", "frida"),
    "pwn": ("pwn", "堆", "栈溢出", "heap", " exploit", "漏洞利用", "cve", "内核",
            "kernel", "提权", "沙箱"),
    "forensics": ("取证", "forensics", "流量分析", "wireshark", "内存取证", "镜像",
                  "osint", "溯源"),
}
_DEPTH_KEYWORDS = ("源码", "原理", "分析", "复现", "利用链", "实战", "从零", "深入",
                   "详解", "调试", "exp", "poc")
_HOT_KEYWORDS = ("在野利用", "0day", "0-day", "nday", "poc 公开", "exp 公开",
                 "紧急", "预警", "apt", "勒索", "大规模")
# 复现文章特征词（标题/摘要含 CVE id 且命中任一词 → 视为讲复现/利用的文章）
_REPRO_KWS = ("复现", "poc", "exp", "exploit", "利用", "分析")


def _rule_score(item: dict, profile: dict) -> dict:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    best_dir, best_hits = "", 0
    for d, kws in _DIRECTION_KEYWORDS.items():
        hits = sum(1 for k in kws if k.lower() in text)
        if hits > best_hits:
            best_dir, best_hits = d, hits
    depth = sum(1 for k in _DEPTH_KEYWORDS if k.lower() in text)
    hot = sum(1 for k in _HOT_KEYWORDS if k.lower() in text)
    weight = float(profile.get("directions", {}).get(best_dir, 1.0)) if best_dir else 1.0
    base = min(60, best_hits * 15) + depth * 8 + (15 if item.get("is_priority") else 0)
    return {"score": round(min(100.0, base * weight), 1), "direction": best_dir,
            "hot": hot > 0, "by": "rule"}


def score_articles(items: list[dict], profile: dict, llm=None) -> list[dict]:
    """items 为 store 行或抓取候选（含 id/title/summary/is_priority）。返回打分明细。"""
    out: list[dict] = []
    pending: list[tuple[int, dict]] = []
    for i, it in enumerate(items):
        if it.get("is_priority"):
            w = 1.0
            out.append({"id": it["id"], "score": 100.0 * w, "direction": "",
                        "is_priority": True, "score_detail": {"by": "priority"}})
        else:
            pending.append((i, it))
    if pending and llm is not None:
        try:
            out.extend(_llm_scores(pending, profile, llm))
            return out
        except Exception:  # noqa: BLE001 —— LLM 任何失败降级规则
            pass
    for _, it in pending:
        r = _rule_score(it, profile)
        out.append({"id": it["id"], "score": r["score"], "direction": r["direction"],
                    "is_priority": None,
                    "score_detail": {"by": "rule", "hot": r["hot"]}})
    return out


def _llm_scores(pending: list[tuple[int, dict]], profile: dict, llm) -> list[dict]:
    lines = []
    for i, (_, it) in enumerate(pending):
        lines.append(f"{i}. [{it.get('source', '')}] {it.get('title', '')} :: "
                     f"{(it.get('summary') or '')[:150]}")
    sys_p = (
        "你是安全情报打分器。对每条内容输出方向与评分。方向取 "
        f"{list(DIRECTIONS)} 之一或空串。评分 0-100 = 方向相关性 × 技术深度"
        "（用户画像权重已另行处理，你只评内容本身）。热点事件（在野利用/0day/大规模影响）"
        "标 hot=true。只输出 JSON 数组："
        '[{"i":0,"direction":"web","score":80,"hot":false},…]')
    resp = llm.chat(
        [{"role": "user", "content": "\n".join(lines)}],
        system=sys_p, max_tokens=MAX_TOKENS_BUDGET, temperature=0.1)
    m = re.search(r"\[.*\]", resp.text, re.S)
    if not m:
        raise ValueError("classifier 响应无 JSON 数组")
    parsed = json.loads(m.group(0))
    out = []
    for p in parsed:
        i = int(p.get("i", -1))
        if not (0 <= i < len(pending)):
            continue
        it = pending[i][1]
        d = p.get("direction", "")
        w = float(profile.get("directions", {}).get(d, 1.0)) if d else 1.0
        score = round(min(100.0, float(p.get("score", 0)) * w), 1)
        out.append({"id": it["id"], "score": score, "direction": d,
                    "is_priority": None,
                    "score_detail": {"by": "llm", "hot": bool(p.get("hot"))}})
    return out


# ---------- 简报合成 ----------

def compose_brief(cves: list[dict], articles: list[dict], date: str,
                  llm=None, cve_assoc: dict | None = None,
                  repro_extra: list[dict] | None = None) -> tuple[str, dict]:
    """(markdown 全文, stats)。cves/articles 已按优先级+分排好；cves 只含确认有公开
    POC 的条目（无 POC 不进简报，§16.1）；cve_assoc: cve_id→关联复现文章，
    repro_extra: 讲复现但 CVE 不在池中的文章（一并提进漏洞板块）。"""
    cve_assoc = cve_assoc or {}
    repro_extra = repro_extra or []
    stats = {"cves": len(cves), "articles": len(articles),
             "repro_articles": sum(len(v) for v in cve_assoc.values()) + len(repro_extra),
             "by": "template"}
    if llm is not None:
        try:
            content = _llm_brief(cves, articles, date, llm, cve_assoc, repro_extra)
            stats["by"] = "llm"
            return content, stats
        except Exception:  # noqa: BLE001
            pass
    lines = [f"# 每日情报简报 {date}", ""]
    if cves or repro_extra:
        lines += ["## 新漏洞 / 在野利用", ""]
        for it in cves:
            tag = "🔴 KEV" if it.get("is_priority") else "·"
            lines.append(f"- {tag} [{it['title']}]({it['url']})"
                         f"{' — ' + it['summary'][:120] if it.get('summary') else ''}")
            if it.get("poc_url"):
                lines.append(f"  - POC: {it['poc_url']}")
            m = _CVE_RE.search(f"{it.get('title', '')} {it.get('url', '')}")
            for a in cve_assoc.get(m.group(0).upper(), []) if m else []:
                lines.append(f"  - 复现分析: [{a['title']}]({a['url']})"
                             f"（{a.get('source', '')}）")
        for it in repro_extra:
            lines.append(f"- 📄 复现文章（CVE 未在当日漏洞池）：[{it['title']}]({it['url']})"
                         f"（{it.get('source', '')}）"
                         f"{' — ' + it['summary'][:80] if it.get('summary') else ''}")
        lines.append("")
    if articles:
        lines += ["## 社区热点与技术文章", ""]
        for it in articles:
            lines.append(f"- [{it['title']}]({it['url']})（{it.get('source', '')}）")
        lines.append("")
    if not cves and not repro_extra and not articles:
        lines.append("今日暂无入库内容。")
    return "\n".join(lines), stats


def _llm_brief(cves: list[dict], articles: list[dict], date: str, llm,
               cve_assoc: dict | None = None,
               repro_extra: list[dict] | None = None) -> str:
    cve_assoc = cve_assoc or {}
    repro_extra = repro_extra or []

    def _line(it: dict) -> str:
        ln = (f"- [{'KEV' if it.get('is_priority') else it.get('source', '')}] "
              f"{it['title']} :: {(it.get('summary') or '')[:200]}")
        if it.get("poc_url"):
            ln += f" :: poc={it['poc_url']}"
        m = _CVE_RE.search(f"{it.get('title', '')} {it.get('url', '')}")
        for a in cve_assoc.get(m.group(0).upper(), []) if m else []:
            ln += f" :: 复现分析={a['url']}"
        return ln

    sys_p = (
        "你是中文安全情报编辑。把给定素材合成为当日简报 markdown："
        "两段式（## 新漏洞 / 在野利用、## 社区热点与技术文章）。"
        "第一段只收录素材中带 POC 线索（KEV/在野利用/POC 链接）的 CVE 条目，"
        "素材未给 POC 证据的 CVE 一律不得出现；每条一行 `- [标题](url) —— 一句话中文点评`"
        "（点评讲清为什么值得关注），素材附带的 POC 链接与复现文章链接原样保留为缩进子行"
        " `- POC: [..](..)` / `- 复现分析: [..](..)`；第一段为空时省略该段，"
        "不要写「今日无新漏洞」之类聚合提示。KEV/在野利用排最前；"
        "不新增素材里没有的信息，不编造链接。直接输出 markdown。")
    body = ["## 新漏洞 / 在野利用"]
    body += [_line(it) for it in cves]
    body += [f"- 复现文章（CVE 未在当日漏洞池）：{it['title']} :: "
             f"{(it.get('summary') or '')[:150]} :: url={it['url']}"
             for it in repro_extra]
    body += [""]
    body += ["## 社区热点与技术文章"] + [_line(it) for it in articles]
    resp = llm.chat([{"role": "user", "content": f"日期：{date}\n\n" + "\n".join(body)}],
                    system=sys_p, max_tokens=MAX_TOKENS_BUDGET, temperature=0.3)
    text = resp.text.strip()
    if not text:
        raise ValueError("classifier 简报为空")
    return text


# ---------- 刷新管线（抓取 → 入池 → 打分 → 当日简报） ----------

def _top(store, kind: str, limit: int) -> list[dict]:
    return store.list_articles(kind=kind, limit=limit)


def run_refresh(store, profile: dict, feeds: list[dict], *, llm=None,
                getter=None, date: str | None = None) -> dict:
    """整批刷新并落当日简报；返回 stats（写进简报 + Job 结果）。"""
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items, errors = fetch_all(getter, feeds)
    new_n = store.upsert_articles(items)
    rows = store.unscored()
    store.apply_scores(score_articles(rows, profile, llm))
    # 推送配比（定稿）：热点事件 1–2 条 + 技术文章 3–5 篇
    hot, tech = [], []
    for a in _top(store, "article", 40):
        (hot if (a.get("score_detail") and json.loads(a["score_detail"]).get("hot"))
         else tech).append(a)
    picked_articles = hot[:2] + tech[:5]
    cves = [a for a in _top(store, "cve", 40) if a.get("has_poc")][:8]  # 有 POC 才进简报
    # 复现文章关联（选材层匹配，compose_brief 保持纯渲染）
    pool_ids: set[str] = set()
    for c in cves:
        m = _CVE_RE.search(f"{c.get('title', '')} {c.get('url', '')}")
        if m:
            pool_ids.add(m.group(0).upper())
    assoc: dict[str, list[dict]] = {}
    repro_extra: list[dict] = []
    picked_urls = {a["url"] for a in picked_articles}
    for a in _top(store, "article", 40):
        text = f"{a.get('title', '')} {a.get('summary', '')}"
        ids = {i.upper() for i in _CVE_RE.findall(text)}
        if not ids or not any(k in text.lower() for k in _REPRO_KWS):
            continue
        hit = ids & pool_ids
        if hit:
            for cid in hit:
                assoc.setdefault(cid, []).append(a)
            picked_urls.discard(a["url"])  # 已挂子行，主列表去重
        elif a["url"] not in picked_urls:
            repro_extra.append(a)          # 讲复现但 CVE 不在池 → 提进漏洞板块
    picked_articles = [a for a in picked_articles if a["url"] in picked_urls]
    content, stats = compose_brief(cves, picked_articles, date, llm,
                                   cve_assoc=assoc, repro_extra=repro_extra[:3])
    stats.update({"new": new_n, "errors": errors, "scored": len(rows)})
    store.save_brief(date, content, stats)
    return stats


# ---------- E10：学习档案与周计划（§16.3；LLM 只喂元数据） ----------

def infer_direction(text: str) -> str:
    """标题/标签/路径 → 主方向（复用 E9 关键词表，命中多者胜；无命中返回空）。"""
    text = text.lower()
    best_dir, best_hits = "", 0
    for d, kws in _DIRECTION_KEYWORDS.items():
        hits = sum(1 for k in kws if k.lower() in text)
        if hits > best_hits:
            best_dir, best_hits = d, hits
    return best_dir


def week_start(date: str | None = None) -> str:
    """ISO 周起始日（周一）YYYY-MM-DD，learning_plans 主键。"""
    d = (datetime.strptime(date, "%Y-%m-%d").date() if date
         else datetime.now(timezone.utc).date())
    return (d - timedelta(days=d.weekday())).isoformat()


def learning_profile(store, profile: dict) -> dict:
    """三来源聚合：声明画像 + vault 元数据推断 + 平台学习记录（文章已读/收藏）。
    F5：vault 方向判定 category 精确命中 DIRECTIONS 优先（用户手工维护最可靠）→
    未命中回退 infer_direction（title/tags/path 关键词表）；vault 来源附 top tags 频次
    （cap 15，全元数据，隐私红线不变）。"""
    declared = {"directions": profile.get("directions", {}), "stage": profile.get("stage", "")}
    # vault 推断：category 精确命中优先 → title+tags+path 关键词回退（**不读正文**）
    vault_by: dict[str, dict] = {d: {"notes": 0, "last_active": ""} for d in DIRECTIONS}
    vault_total = 0
    tag_freq: dict[str, int] = {}
    for n in store.list_notes():
        vault_total += 1
        cat = str(n.get("category") or "")
        d = cat if cat in DIRECTIONS else infer_direction(
            f"{n.get('title', '')} {' '.join(n.get('tags', []))} {n['path']}")
        if d:
            vault_by[d]["notes"] += 1
            if n.get("mtime", "") > vault_by[d]["last_active"]:
                vault_by[d]["last_active"] = n["mtime"]
        for t in n.get("tags", []):
            key = str(t).strip().lower()
            if key:
                tag_freq[key] = tag_freq.get(key, 0) + 1
    top_tags = sorted(tag_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    platform = store.platform_direction_counts()
    return {"declared": declared,
            "vault": {"total": vault_total, "by_direction": vault_by,
                      "top_tags": [{"tag": t, "count": c} for t, c in top_tags]},
            "platform": platform}


def compose_weekly_plan(agg: dict, brief: dict | None, articles: list[dict],
                        week: str, llm=None) -> tuple[str, dict]:
    """(markdown 周计划, stats)。入参只含元数据——笔记正文在此前已被剥除。"""
    stats = {"by": "template", "week": week,
             "brief_date": brief.get("date") if brief else None,
             "articles": len(articles)}
    if llm is not None:
        try:
            content = _llm_weekly_plan(agg, brief, articles, week, llm)
            stats["by"] = "llm"
            return content, stats
        except Exception:  # noqa: BLE001
            pass
    lines = [f"# 周学习计划（{week} 起）", ""]
    stage = agg["declared"].get("stage") or "（未声明阶段，可在设置页画像里填写）"
    weights = agg["declared"].get("directions", {})
    lines += [f"**学习阶段**：{stage}",
              "**方向权重**（兴趣权重优先，明显缺口提醒自己补一句即可）：" +
              " ".join(f"{d}={w}" for d, w in weights.items()),
              "", "## 本周建议", ""]
    hot = [a for a in articles if a.get("is_priority")][:3]
    if hot:
        lines.append("- **优先跟进热点**：" + "；".join(a["title"] for a in hot))
    for a in articles[:5]:
        lines.append(f"- 读：[{a['title']}]（{a.get('source', '')}"
                     f"{'·' + a['direction'] if a.get('direction') else ''}）")
    vault_dirs = {d: v for d, v in agg["vault"]["by_direction"].items() if v["notes"]}
    if vault_dirs:
        lines.append("- vault 近况：" + "、".join(
            f"{d} {v['notes']} 篇（最近 {v['last_active'][:10]}）"
            for d, v in sorted(vault_dirs.items())))
    top_tags = agg.get("vault", {}).get("top_tags") or []
    if top_tags:
        lines.append("- 高频笔记主题（tag 频次 top）："
                     + "、".join(f"{t['tag']}×{t['count']}" for t in top_tags[:8]))
    lines.append("- 练：任选一篇技术文章动手复现，把笔记整理回 vault（本平台不代写）。")
    lines.append("")
    return "\n".join(lines), stats


def _llm_weekly_plan(agg: dict, brief: dict | None, articles: list[dict],
                     week: str, llm) -> str:
    """LLM 入参构造：**仅元数据**（计数/标题/方向/阶段声明），无任何笔记正文。"""
    stage = agg["declared"].get("stage") or "未声明"
    weights = agg["declared"].get("directions", {})
    vault_lines = [f"- {d}：{v['notes']} 篇，最近活跃 {v['last_active'][:10] or '无'}"
                   for d, v in agg["vault"]["by_direction"].items() if v["notes"]]
    plat_lines = [f"- {d}：已读 {v['read']} / 收藏 {v['starred']}"
                  for d, v in agg["platform"].items() if v.get("total")]
    brief_lines = []
    if brief:
        for ln in brief.get("content", "").splitlines():
            if ln.startswith("- "):
                brief_lines.append(ln[:150])
    art_lines = [f"- [{a.get('source', '')}] {a['title']}"
                 f"{'·' + a['direction'] if a.get('direction') else ''}（分 {a.get('score', 0)}）"
                 for a in articles[:15]]
    tag_lines = [f"- {t['tag']}：{t['count']} 篇"
                 for t in (agg.get("vault", {}).get("top_tags") or [])[:10]]
    sys_p = (
        "你是中文安全学习教练。按给定素材生成本周学习计划 markdown：三节——"
        "「本周重点」（结合画像方向权重与当周简报热点，2-3 条；兴趣权重优先，"
        "明显缺口提醒一句，不硬编码偏向）、"
        "「推荐阅读」（从文章清单挑 3-5 篇并说明理由）、「练习」（可动手的实操建议）。"
        "不编造素材里没有的信息。直接输出 markdown。")
    body = [f"周起始：{week}", f"学习阶段：{stage}",
            "方向权重：" + " ".join(f"{d}={w}" for d, w in weights.items())]
    if vault_lines:
        body.append("vault 笔记分布（仅元数据）：\n" + "\n".join(vault_lines))
    if tag_lines:
        body.append("高频笔记主题 tag 频次（仅元数据）：\n" + "\n".join(tag_lines))
    if plat_lines:
        body.append("平台阅读记录：\n" + "\n".join(plat_lines))
    if brief_lines:
        body.append("当周简报要点：\n" + "\n".join(brief_lines[:8]))
    if art_lines:
        body.append("高分文章清单（标题元数据）：\n" + "\n".join(art_lines))
    resp = llm.chat([{"role": "user", "content": "\n\n".join(body)}],
                    system=sys_p, max_tokens=MAX_TOKENS_BUDGET, temperature=0.4)
    text = resp.text.strip()
    if not text:
        raise ValueError("classifier 周计划为空")
    return text
