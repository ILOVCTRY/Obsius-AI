"""情报面板（E9/E10，DESIGN.md §16）测试：全局存储 / 抓取解析 / 打分与简报 / vault 索引 / 学习档案与周计划 / API 端点。

全部不触网：抓取层 getter 注入、LLM 注入假对象；vault 用 tmp 目录。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.intel.config import load_feeds, load_profile, save_profile
from core.intel.fetch import (
    _enrich,
    _poc_signal,
    _poc_url_signal,
    fetch_all,
    fetch_kev,
    fetch_nvd,
    fetch_github_advisories,
    parse_rss,
)
from core.intel.intel_service import (
    compose_brief,
    compose_weekly_plan,
    learning_profile,
    run_refresh,
    score_articles,
    week_start,
)
from core.intel.store import IntelStore
from core.intel.vault import build_tree, index_vault


RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Java 反序列化漏洞实战分析</title><link>https://example.com/a1</link>
<description>&lt;p&gt;原理与复现&lt;/p&gt;</description><pubDate>Wed, 16 Sep 2026 00:00:00 GMT</pubDate></item>
<item><title>重复条目标题</title><link>https://example.com/a2</link><description>d</description></item>
</channel></rss>"""

ATOM_FIXTURE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>atom entry title</title><link href="https://example.com/e1"/>
<summary>sum</summary><updated>2026-09-16T00:00:00Z</updated></entry>
</feed>"""


# ---------- 存储层 ----------

def test_intel_store_upsert_dedup_and_scores(tmp_path):
    s = IntelStore(tmp_path)
    items = [
        {"url": "https://e.com/1", "title": "t1", "source": "nvd", "kind": "cve",
         "summary": "s1", "published_at": "2026-09-15", "is_priority": True},
        {"url": "https://e.com/2", "title": "t2", "source": "FreeBuf", "kind": "article"},
    ]
    assert s.upsert_articles(items) == 2
    assert s.upsert_articles(items) == 0  # URL 幂等
    rows = s.unscored()
    assert len(rows) == 2
    by_url = {r["url"]: r for r in rows}
    cve_id = by_url["https://e.com/1"]["id"]
    art_id = by_url["https://e.com/2"]["id"]
    s.apply_scores([{"id": cve_id, "score": 88.0, "direction": "web",
                     "score_detail": {"by": "llm", "hot": True}},
                    {"id": art_id, "score": 12.0, "direction": ""}])
    assert s.unscored() == []
    arts = s.list_articles(kind="article")
    assert len(arts) == 1 and arts[0]["score"] == 12.0
    top = s.list_articles(limit=5)
    assert top[0]["score"] == 88.0  # 按分倒序
    assert json.loads(top[0]["score_detail"])["hot"] is True
    # 已读/收藏 + 过滤
    s.mark_article(art_id, read=True, starred=True)
    unread = s.list_articles(unread_only=True)
    assert [r["id"] for r in unread] == [cve_id]  # 仅 cve 未读
    assert s.list_articles(starred_only=True)[0]["id"] == art_id
    assert s.mark_article("art-000000000000") is None  # 不存在
    assert s.counts() == {"articles": 2, "unread": 1, "starred": 1}
    s.close()


def test_intel_store_briefs(tmp_path):
    s = IntelStore(tmp_path)
    assert s.get_brief("2026-09-16") is None
    s.save_brief("2026-09-16", "# 简报", {"cves": 3, "by": "llm"})
    s.save_brief("2026-09-16", "# 简报v2", {"cves": 4, "by": "template"})  # 同日覆盖
    b = s.get_brief("2026-09-16")
    assert b["content"] == "# 简报v2" and b["stats"]["cves"] == 4
    s.save_brief("2026-09-15", "旧", {})
    assert [x["date"] for x in s.list_briefs()] == ["2026-09-16", "2026-09-15"]
    assert "content" not in s.list_briefs()[0]  # 归档列表不含全文
    s.close()


# ---------- 抓取层 ----------

def test_parse_rss_and_atom():
    items = parse_rss(RSS_FIXTURE, "测试源")
    assert len(items) == 2
    assert items[0]["title"] == "Java 反序列化漏洞实战分析"  # HTML 已剥离
    assert items[0]["summary"] == "原理与复现"
    assert items[0]["kind"] == "article"
    atom = parse_rss(ATOM_FIXTURE, "atom源")
    assert len(atom) == 1 and atom[0]["url"] == "https://example.com/e1"


def test_structured_sources_with_fake_getter():
    # KEV 只收近 7 天新入条目（fetch_kev 防全量灌池）——日期须动态生成，否则时间炸弹
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def fake_getter(url, timeout):
        if "nvd" in url:
            return 200, json.dumps({"vulnerabilities": [
                {"cve": {"id": "CVE-2026-0001", "published": "2026-09-15T00:00:00.000",
                         "descriptions": [{"lang": "en", "value": "bad bug"}],
                         "references": [
                             {"url": "https://cve.org/cve", "tags": []},
                             {"url": "https://www.exploit-db.com/exploits/9999",
                              "tags": ["Exploit"]}]}}]})
        if "known_exploited" in url:
            return 200, json.dumps({"vulnerabilities": [
                {"cveID": "CVE-2026-0002", "dateAdded": recent,
                 "vulnerabilityName": "In-the-wild bug",
                 "shortDescription": "exploited"}]})
        return 200, json.dumps([{"ghsa_id": "GHSA-xxxx", "cve_id": "CVE-2026-0003",
                                 "summary": "ghsa sum", "html_url": "https://g.co/1",
                                 "published": "2026-09-15T00:00:00Z", "type": "reviewed"}])

    nvd = fetch_nvd(fake_getter)
    assert nvd[0]["title"] == "CVE-2026-0001" and "bad bug" in nvd[0]["summary"]
    assert nvd[0]["has_poc"] is True and "exploit-db" in nvd[0]["poc_url"]
    assert nvd[0]["is_priority"] is True  # 有公开 POC 打优先标
    kev = fetch_kev(fake_getter)
    assert kev[0]["is_priority"] is True and kev[0]["title"].startswith("[KEV]")
    assert kev[0]["has_poc"] is False  # KEV 自身无 references，等 _enrich 合并
    ghsa = fetch_github_advisories(fake_getter)
    assert ghsa[0]["source"] == "ghsa" and "CVE-2026-0003" in ghsa[0]["title"]
    assert ghsa[0]["has_poc"] is False and ghsa[0]["poc_url"] == ""


def test_poc_signal_excludes_and_enrich():
    # 白名单 + 排除前缀 + github 路径关键词
    assert _poc_url_signal("https://www.exploit-db.com/exploits/1") is True
    assert _poc_url_signal("https://github.com/attacker/CVE-2026-1001-poc") is True
    assert _poc_url_signal("https://gist.github.com/x/abc") is False  # 无关键词路径
    assert _poc_url_signal("https://github.com/advisories/GHSA-xxx") is False  # GHSA 详情页
    assert _poc_url_signal("https://github.com/CVEProject/cvelistV5") is False  # cvelist
    assert _poc_url_signal("https://example.com/poc") is False  # 非白名单域
    # tag "Exploit" 优先于白名单 URL
    has, url = _poc_signal([
        {"url": "https://github.com/a/poc", "tags": []},
        {"url": "https://www.exploit-db.com/exploits/9", "tags": ["Exploit"]}])
    assert has and url == "https://www.exploit-db.com/exploits/9"
    # GHSA references 为纯字符串数组
    has2, url2 = _poc_signal(["https://packetstormsecurity.com/files/1"])
    assert has2 and url2 == "https://packetstormsecurity.com/files/1"
    assert _poc_signal([]) == (False, "")
    # _enrich：NVD/GHSA 证据按 CVE id 合并到 KEV；无证据 KEV 保持 False
    items = [
        {"title": "CVE-2026-1001", "url": "https://nvd.nist.gov/vuln/detail/CVE-2026-1001",
         "source": "nvd", "has_poc": True, "poc_url": "https://e.db/1"},
        {"title": "[KEV] CVE-2026-1001 x", "url": "https://nvd.nist.gov/vuln/detail/CVE-2026-1001",
         "source": "kev", "has_poc": False, "poc_url": ""},
        {"title": "[KEV] CVE-2026-1002 y", "url": "https://nvd.nist.gov/vuln/detail/CVE-2026-1002",
         "source": "kev", "has_poc": False, "poc_url": ""},
    ]
    _enrich(items)
    assert items[1]["has_poc"] is True and items[1]["poc_url"] == "https://e.db/1"
    assert items[2]["has_poc"] is False  # 无证据 KEV 不进简报（宁缺毋滥）


def test_store_schema_v3_migration_and_upgrade_upsert(tmp_path):
    # 手工搭 v2 形状库（articles 无 has_poc/poc_url），IntelStore 打开须幂等补列
    db = tmp_path / "intel.db"
    raw = sqlite3.connect(str(db))
    raw.executescript("""
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE articles (
        id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        source TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        published_at TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL,
        score REAL NOT NULL DEFAULT 0, direction TEXT NOT NULL DEFAULT '',
        is_priority INTEGER NOT NULL DEFAULT 0, score_detail TEXT NOT NULL DEFAULT '',
        brief_date TEXT, read INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0);
    INSERT INTO meta VALUES('schema_version','2');
    """)
    raw.execute("INSERT INTO articles(id,url,title,source,kind,fetched_at) "
                "VALUES('art-old','https://e/old','旧KEV','kev','cve','t')")
    raw.commit()
    raw.close()
    s = IntelStore(tmp_path)
    cols = {r[1] for r in sqlite3.connect(str(db)).execute(
        "PRAGMA table_info(articles)").fetchall()}
    assert {"has_poc", "poc_url"} <= cols
    rows = s.list_articles(kind="cve")
    assert rows[0]["has_poc"] == 0 and rows[0]["poc_url"] == ""  # 存量不回填，等重抓补证
    # 升级式 upsert：无证据入池 → 带证据重抓 → 补上且不新增行
    assert s.upsert_articles([{"url": "https://e/old", "title": "旧KEV", "source": "kev",
                               "kind": "cve"}]) == 0
    assert s.upsert_articles([{"url": "https://e/old", "title": "旧KEV", "source": "kev",
                               "kind": "cve", "has_poc": True,
                               "poc_url": "https://e.db/poc"}]) == 0
    row = s.list_articles(kind="cve")[0]
    assert row["has_poc"] == 1 and row["poc_url"] == "https://e.db/poc"
    assert len(s.list_articles()) == 1
    # 只升不降：无证据重抓不清掉已有证据
    s.upsert_articles([{"url": "https://e/old", "title": "旧KEV", "source": "kev",
                        "kind": "cve"}])
    assert s.list_articles(kind="cve")[0]["has_poc"] == 1
    s.close()


def test_fetch_all_tolerates_source_errors():
    def bad_getter(url, timeout):
        raise ConnectionError("断网")

    items, errors = fetch_all(bad_getter, feeds=[{"name": "x", "url": "https://x/rss"}])
    assert items == [] and len(errors) >= 4  # nvd/kev/ghsa/rss 全失败但不抛


# ---------- 打分与简报 ----------

def test_score_articles_rule_and_priority_and_llm():
    profile = {"directions": {"web": 2.0, "pwn": 1.0}}
    items = [
        {"id": "a", "title": "x", "summary": "x", "is_priority": True},
        {"id": "b", "title": "Java 反序列化漏洞复现", "source": "fb",
         "summary": "web 实战利用链分析", "is_priority": False},
        {"id": "c", "title": "无关内容", "summary": "nothing here", "is_priority": False},
    ]
    out = {r["id"]: r for r in score_articles(items, profile, llm=None)}
    assert out["a"]["score"] == 100.0  # 优先项恒满分
    assert out["b"]["direction"] == "web" and out["b"]["score_detail"]["by"] == "rule"
    assert out["b"]["score"] > out["c"]["score"]  # 关键词命中得分更高

    class FakeLLM:
        def chat(self, messages, **kw):
            class R:
                text = json.dumps([{"i": 0, "direction": "pwn", "score": 50, "hot": True},
                                   {"i": 1, "direction": "web", "score": 80, "hot": False}])
            return R()

    out2 = {r["id"]: r for r in score_articles(items[1:], profile, llm=FakeLLM())}
    assert out2["b"]["score"] == 50.0   # pwn 权重 1.0 不放大
    assert out2["c"]["score"] == 100.0  # 80 × web 权重 2.0 → 封顶 100
    assert out2["b"]["score_detail"]["hot"] is True


def test_compose_brief_template_and_llm_fallback():
    cves = [{"title": "CVE-2026-1", "url": "https://e/1", "summary": "s",
             "is_priority": True, "source": "kev"}]
    arts = [{"title": "文章", "url": "https://e/2", "summary": "", "source": "FreeBuf",
             "is_priority": False}]
    content, stats = compose_brief(cves, arts, "2026-09-16", llm=None)
    assert stats["by"] == "template" and "新漏洞 / 在野利用" in content
    assert "🔴 KEV" in content and "CVE-2026-1" in content

    class BoomLLM:
        def chat(self, messages, **kw):
            raise RuntimeError("无 key")

    content2, stats2 = compose_brief(cves, arts, "2026-09-16", llm=BoomLLM())
    assert stats2["by"] == "template"  # LLM 失败降级模板


def test_run_refresh_pipeline(tmp_path):
    store = IntelStore(tmp_path / "intel")

    def fake_getter(url, timeout):
        return 200, RSS_FIXTURE

    profile = load_profile(tmp_path)
    feeds = [{"name": "测试源", "url": "https://fake/rss"}]
    stats = run_refresh(store, profile, feeds, getter=fake_getter, date="2026-09-16")
    assert stats["new"] == 2 and stats["scored"] == 2
    assert len(stats["errors"]) == 3  # nvd/kev/ghsa 真实端点被假 getter 喂了 RSS → 失败但容忍
    brief = store.get_brief("2026-09-16")
    assert brief is not None and "测试源" in brief["content"]
    assert brief["stats"]["by"] == "template"  # 无 LLM 走模板
    # 二次刷新不重复计分
    stats2 = run_refresh(store, profile, feeds, getter=fake_getter, date="2026-09-16")
    assert stats2["new"] == 0 and stats2["scored"] == 0
    store.close()


POC_RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>CVE-2026-1003 详细复现分析</title><link>https://xz.example/repro-1003</link>
<description>poc 复现全过程</description></item>
<item><title>CVE-2026-9999 复现笔记</title><link>https://xz.example/repro-9999</link>
<description>利用链分析</description></item>
</channel></rss>"""


def test_run_refresh_poc_filter_and_repro_assoc(tmp_path):
    """简报「新漏洞 / 在野利用」只收确认 POC 条目；复现文章挂子行或提进板块。"""
    store = IntelStore(tmp_path / "intel")
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def fake_getter(url, timeout):
        if "nvd" in url:
            return 200, json.dumps({"vulnerabilities": [
                {"cve": {"id": "CVE-2026-1001", "published": f"{recent}T00:00:00.000",
                         "descriptions": [{"lang": "en", "value": "pwned"}],
                         "references": [{"url": "https://www.exploit-db.com/exploits/7",
                                         "tags": ["Exploit"]}]}}]})
        if "known_exploited" in url:
            return 200, json.dumps({"vulnerabilities": [
                {"cveID": "CVE-2026-1001", "dateAdded": recent,
                 "vulnerabilityName": "With POC", "shortDescription": "exploited"},
                {"cveID": "CVE-2026-1002", "dateAdded": recent,
                 "vulnerabilityName": "No POC", "shortDescription": "no public poc"}]})
        if "advisories" in url:
            return 200, json.dumps([
                {"ghsa_id": "GHSA-p", "cve_id": "CVE-2026-1003", "summary": "s",
                 "html_url": "https://github.com/advisories/GHSA-p",
                 "published": f"{recent}T00:00:00Z", "type": "reviewed",
                 "references": ["https://github.com/a/CVE-2026-1003-poc"]},
                {"ghsa_id": "GHSA-n", "cve_id": "CVE-2026-1004", "summary": "s2",
                 "html_url": "https://github.com/advisories/GHSA-n",
                 "published": f"{recent}T00:00:00Z", "type": "reviewed",
                 "references": ["https://github.com/advisories/GHSA-n"]}])
        return 200, POC_RSS_FIXTURE

    profile = load_profile(tmp_path)
    feeds = [{"name": "复现源", "url": "https://fake/rss"}]
    stats = run_refresh(store, profile, feeds, getter=fake_getter, date="2026-09-16")
    assert stats["errors"] == []  # 三个结构化源 + RSS 全成功
    brief = store.get_brief("2026-09-16")
    content = brief["content"]
    # 有 POC 的进简报：1001（NVD tag）、1003（GHSA 白名单）
    assert "CVE-2026-1001" in content and "exploit-db" in content  # POC 子行
    assert "CVE-2026-1003" in content and "复现分析" in content \
        and "repro-1003" in content  # 复现文章子行
    # 无 POC 的不进简报（1002 KEV 无证据、1004 GHSA 仅 advisory 页）
    assert "CVE-2026-1002" not in content and "CVE-2026-1004" not in content
    assert "No POC" not in content
    # 9999 不在池且已排进文章板块 → 不重复提升，留在「社区热点与技术文章」
    assert "CVE-2026-9999" in content and "repro-9999" in content
    assert "社区热点与技术文章" in content
    assert brief["stats"]["repro_articles"] == 1  # 仅 repro-1003 关联命中
    # 被过滤条目仍在池中（只是不进简报）
    pool_titles = {r["title"] for r in store.list_articles(kind="cve", limit=50)}
    assert any("CVE-2026-1002" in t for t in pool_titles)
    assert any("CVE-2026-1004" in t for t in pool_titles)
    store.close()


def test_compose_brief_repro_extra_and_empty_section():
    # repro_extra 提进板块；CVE 无 POC 且无 repro_extra 时整段省略
    repro = [{"title": "CVE-2026-7777 复现", "url": "https://xz.example/1",
              "summary": "详细复现", "source": "先知社区"}]
    content, stats = compose_brief([], [], "2026-09-16", llm=None,
                                   repro_extra=repro)
    assert "## 新漏洞 / 在野利用" in content and "复现文章" in content \
        and "CVE-2026-7777" in content
    assert stats["repro_articles"] == 1
    content2, _ = compose_brief([], [{"title": "文章", "url": "https://e/2",
                                      "source": "FreeBuf"}], "2026-09-16", llm=None)
    assert "新漏洞" not in content2  # 无 POC 日板块留空（不写聚合提示）
    content3, _ = compose_brief([], [], "2026-09-16", llm=None)
    assert "今日暂无入库内容" in content3


# ---------- API 端点 ----------

@pytest.fixture()
def intel_client(tmp_path):
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"),
                     intel_dir=str(tmp_path / "intel"))
    return tmp_path, TestClient(app)


def test_intel_feeds_profile_endpoints(intel_client):
    _, c = intel_client
    r = c.get("/api/intel/feeds")
    assert r.status_code == 200 and len(r.json()["feeds"]) >= 5  # 种子默认源
    r = c.put("/api/intel/feeds", json={"feeds": [
        {"name": "自定义", "url": "https://custom/rss"}, {"name": "坏行", "url": " "}]}
    ).json()
    assert r["status"] == "ok" and len(r["feeds"]) == 1  # 空 url 剔除
    prof = {"directions": {"web": 2.5, "pwn": 0.5}, "stage": "入门"}
    assert c.put("/api/intel/profile", json=prof).json()["profile"]["directions"]["web"] == 2.5
    assert c.get("/api/intel/profile").json()["stage"] == "入门"
    # 非法权重 422（负数在服务端钳制而非 422？——save_profile 钳制，此处验证不炸）
    r = c.put("/api/intel/profile", json={"directions": {"web": -1}, "stage": ""})
    assert r.status_code == 200 and r.json()["profile"]["directions"]["web"] == 0.0


def test_intel_fetch_job_and_briefs(intel_client):
    tmp_path, c = intel_client
    # 注入假 getter + 假 classifier（Job 在后台线程跑，state 注入即可）
    app = c.app
    app.state.intel_getter = lambda url, timeout: (200, RSS_FIXTURE)

    class FakeLLM:
        def chat(self, messages, **kw):
            class R:
                text = "# LLM 简报"
            return R()

    app.state.intel_llm = FakeLLM()
    job = c.post("/api/intel/fetch").json()["job_id"]
    for _ in range(100):
        j = c.get(f"/api/jobs/{job}").json()
        if j["status"] != "running":
            break
    assert j["status"] == "done", j
    assert j["result"]["new"] == 2
    today = j["result"].get("cves", 0)
    r = c.get("/api/intel/briefs")
    assert r.status_code == 200 and len(r.json()["briefs"]) == 1
    date = r.json()["briefs"][0]["date"]
    brief = c.get(f"/api/intel/briefs/{date}").json()
    assert brief["stats"]["by"] == "llm"  # 假 classifier 生效
    assert c.get("/api/intel/briefs/1999-01-01").status_code == 404
    assert today >= 0


def test_intel_articles_endpoints(intel_client):
    tmp_path, c = intel_client
    # 直接铺数据（绕过抓取）
    from core.intel.store import IntelStore
    store = IntelStore(tmp_path / "intel")
    store.upsert_articles([{"url": "https://e/1", "title": "t1", "source": "s",
                            "kind": "article", "summary": "x"}])
    aid = store.list_articles()[0]["id"]
    store.apply_scores([{"id": aid, "score": 50.0, "direction": "web"}])
    store.close()

    arts = c.get("/api/intel/articles", params={"kind": "article"}).json()["articles"]
    assert len(arts) == 1 and arts[0]["score"] == 50.0
    assert c.get("/api/intel/articles", params={"kind": "cve"}).json()["articles"] == []
    r = c.patch(f"/api/intel/articles/{aid}", json={"read": True, "starred": True})
    assert r.json()["read"] == 1 and r.json()["starred"] == 1
    assert c.get("/api/intel/articles", params={"unread": "true"}).json()["articles"] == []
    assert c.patch("/api/intel/articles/art-nonexistent", json={"read": True}).status_code == 404
    overview = c.get("/api/intel/overview").json()
    assert overview["counts"]["articles"] == 1 and overview["today"] is None


# ---------- E10：vault 配置 / 索引 / 学习档案 / 周计划 ----------

def test_profile_config_preserves_unknown_keys(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({
        "directions": {"web": 2.0}, "stage": "进阶", "my_note": "手编字段",
        "vault": {"path": "D:/vault", "enabled": True}}), encoding="utf-8")
    prof = load_profile(tmp_path)
    assert prof["my_note"] == "手编字段"  # 未知键透传不丢
    assert prof["vault"] == {"path": "D:/vault", "enabled": True}
    assert prof["directions"]["pwn"] == 1.0  # 漏项补默认
    out = save_profile(prof, tmp_path)
    assert out["my_note"] == "手编字段"
    assert out["vault"]["enabled"] is True
    assert out["directions"]["web"] == 2.0 and out["directions"]["ai"] == 1.0
    # vault 归一化：路径字符串化、enabled 强转 bool
    out2 = save_profile({"vault": {"path": 123, "enabled": "yes"}}, tmp_path)
    assert out2["vault"]["path"] == "123" and out2["vault"]["enabled"] is True


def test_intel_store_schema_v2_migration(tmp_path):
    s = IntelStore(tmp_path)
    s.close()
    # 模拟 v1 旧库：删新表 + 版本号回 1
    raw = sqlite3.connect(str(tmp_path / "intel.db"))
    raw.execute("DROP TABLE vault_notes")
    raw.execute("DROP TABLE learning_plans")
    raw.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    s2 = IntelStore(tmp_path)  # DDL 全 IF NOT EXISTS → 幂等迁移
    s2.replace_notes([{"path": "a.md", "title": "t"}])
    assert s2.note_stats()["notes"] == 1
    assert s2.latest_plan() is None
    s2.close()


def test_vault_index_and_search(tmp_path):
    vault = tmp_path / "vault"
    (vault / "notes" / "web").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    (vault / "notes" / "web" / "proxy.md").write_text(
        "---\ntitle: 代理探测笔记\ntags: web, proxy\ncategory: Web\n---\n# 标题无用\n"
        "正文含 SECRET_BODY_TOKEN 不外发。#内联标签\n", encoding="utf-8")
    (vault / "notes" / "pwn.md").write_text("# 堆题入门\nheap 基础\n", encoding="utf-8")
    (vault / ".obsidian" / "app.json.md").write_text("配置", encoding="utf-8")

    notes = index_vault(vault)
    assert len(notes) == 2  # .obsidian 跳过
    by_path = {n["path"]: n for n in notes}
    assert by_path["notes/web/proxy.md"]["title"] == "代理探测笔记"
    assert "web" in by_path["notes/web/proxy.md"]["tags"]
    assert "内联标签" in by_path["notes/web/proxy.md"]["tags"]
    assert by_path["notes/web/proxy.md"]["category"] == "web"  # F5：fm category 归一小写入
    assert by_path["notes/pwn.md"]["category"] == ""  # 无 frontmatter → 空
    assert by_path["notes/pwn.md"]["title"] == "堆题入门"  # 无 frontmatter 取首个 h1

    s = IntelStore(tmp_path / "intel")
    assert s.replace_notes(notes) == 2
    assert s.note_stats()["notes"] == 2
    hits = s.search_notes("SECRET_BODY_TOKEN")
    assert len(hits) == 1 and "SECRET_BODY_TOKEN" in hits[0]["snippet"]
    assert "content" not in hits[0]  # 搜索结果不返回全文
    assert s.search_notes("%") == []  # LIKE 通配符转义 → 字面匹配无命中
    assert s.search_notes("  ") == []
    tree = build_tree(s.list_notes())
    dirs = {d["name"]: d for d in tree if "children" in d}
    assert "notes" in dirs and len(dirs["notes"]["children"]) == 2
    s.close()


def test_learning_profile_aggregation(tmp_path):
    s = IntelStore(tmp_path / "intel")
    s.replace_notes([
        {"path": "a.md", "title": "堆溢出利用笔记", "tags": ["pwn"],
         "mtime": "2026-09-10T00:00:00+00:00"},
        {"path": "b.md", "title": "XSS 挖掘", "tags": [],
         "mtime": "2026-09-12T00:00:00+00:00"},
        {"path": "c.md", "title": "无方向", "tags": [],
         "mtime": "2026-09-13T00:00:00+00:00"},
    ])
    s.upsert_articles([{"url": "https://e/1", "title": "t", "source": "s",
                        "kind": "article"}])
    aid = s.list_articles()[0]["id"]
    s.apply_scores([{"id": aid, "score": 60.0, "direction": "web"}])
    s.mark_article(aid, read=True, starred=True)
    prof = {"directions": {"web": 1.0}, "stage": "入门"}
    agg = learning_profile(s, prof)
    assert agg["declared"]["stage"] == "入门"
    assert agg["vault"]["total"] == 3
    assert agg["vault"]["by_direction"]["pwn"]["notes"] == 1
    assert agg["vault"]["by_direction"]["web"]["notes"] == 1
    assert agg["vault"]["by_direction"]["web"]["last_active"].startswith("2026-09-12")
    assert agg["platform"]["web"]["read"] == 1 and agg["platform"]["web"]["starred"] == 1
    s.close()


def test_learning_profile_category_priority_and_top_tags(tmp_path):
    """F5：category 精确命中 DIRECTIONS 优先（不被关键词推断覆盖）；未命中回退
    infer_direction；top_tags 频次（cap 15）。全元数据，正文不入参。"""
    s = IntelStore(tmp_path / "intel")
    s.replace_notes([
        # category="web" 精确命中：即使标题全是 pwn 词也归 web（用户手工维护最可靠）
        {"path": "a.md", "title": "堆溢出利用笔记", "tags": ["pwn"], "category": "web",
         "mtime": "2026-09-10T00:00:00+00:00"},
        # category 不在 DIRECTIONS → 回退关键词推断（标题含 pwn 词 → pwn）
        {"path": "b.md", "title": "堆题练习", "tags": ["pwn"], "category": "meow",
         "mtime": "2026-09-11T00:00:00+00:00"},
        {"path": "c.md", "title": "XSS 挖掘", "tags": ["web", "xss"],
         "mtime": "2026-09-12T00:00:00+00:00"},
    ])
    prof = {"directions": {"web": 1.0}, "stage": ""}
    agg = learning_profile(s, prof)
    assert agg["vault"]["by_direction"]["web"]["notes"] == 2  # a(category) + c(推断)
    assert agg["vault"]["by_direction"]["pwn"]["notes"] == 1  # b 回退推断
    tags = {t["tag"]: t["count"] for t in agg["vault"]["top_tags"]}
    assert tags == {"pwn": 2, "web": 1, "xss": 1}
    s.close()


def test_vault_category_migration_from_v3(tmp_path):
    """F5：v3 旧库（vault_notes 无 category 列）打开后幂等补列。"""
    import sqlite3
    db = tmp_path / "old" / "intel.db"
    (tmp_path / "old").mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE vault_notes(path TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '', mtime TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '3');
        """
    )
    conn.commit()
    conn.close()
    s = IntelStore(tmp_path / "old")
    s.replace_notes([{"path": "a.md", "title": "t", "category": "web"}])
    assert s.list_notes()[0]["category"] == "web"
    s.close()


def test_week_start_iso_monday():
    assert week_start("2026-09-16") == "2026-09-14"  # 周三 → 周一
    assert week_start("2026-09-14") == "2026-09-14"


def test_weekly_plan_privacy_and_fallback():
    """隐私红线测试：LLM 入参只含元数据，笔记正文（SECRET_BODY_TOKEN）绝不外发。"""
    captured = []

    class FakeLLM:
        def chat(self, messages, **kw):
            captured.append(messages[0]["content"] + str(kw.get("system", "")))

            class R:
                text = "# LLM 周计划"

            return R()

    class BoomLLM:
        def chat(self, messages, **kw):
            raise RuntimeError("无 key")

    agg = {"declared": {"directions": {"web": 2.0}, "stage": "实战"},
           "vault": {"total": 1, "by_direction": {
               "web": {"notes": 1, "last_active": "2026-09-12T00:00:00+00:00"}},
               "top_tags": [{"tag": "xss", "count": 3}]},
           "platform": {"web": {"total": 1, "read": 1, "starred": 0}}}
    articles = [{"title": "Java 反序列化复现", "source": "FreeBuf", "direction": "web",
                 "score": 90.0, "is_priority": False}]
    content, stats = compose_weekly_plan(agg, None, articles, "2026-09-14", llm=FakeLLM())
    assert stats["by"] == "llm" and content == "# LLM 周计划"
    assert len(captured) == 1
    assert "SECRET_BODY_TOKEN" not in captured[0]  # 正文不出本机
    assert "实战" in captured[0] and "Java 反序列化复现" in captured[0]  # 元数据在
    assert "xss" in captured[0] and "tag 频次" in captured[0]  # F5：tag 频次入参在
    # LLM 失败 → 模板降级
    content2, stats2 = compose_weekly_plan(agg, None, articles, "2026-09-14",
                                           llm=BoomLLM())
    assert stats2["by"] == "template" and "周学习计划" in content2


def test_vault_endpoints(intel_client):
    tmp_path, c = intel_client
    vault = tmp_path / "myvault"
    vault.mkdir()
    (vault / "a.md").write_text("---\ntitle: 笔记A\ntags: web\n---\n正文v1",
                                encoding="utf-8")
    assert c.get("/api/intel/vault").json()["configured"] is False
    r = c.put("/api/intel/vault", json={"path": str(vault), "enabled": True}).json()
    assert r["status"] == "ok" and r["vault"]["enabled"] is True
    job = r["index_job_id"]
    assert job  # 保存即自动索引
    for _ in range(100):
        j = c.get(f"/api/jobs/{job}").json()
        if j["status"] != "running":
            break
    assert j["status"] == "done" and j["result"]["indexed"] == 1
    info = c.get("/api/intel/vault").json()
    assert info["notes"] == 1 and info["configured"] is True
    tree = c.get("/api/intel/vault/tree").json()
    assert tree["configured"] is True and tree["tree"][0]["name"] == "a.md"
    hits = c.get("/api/intel/vault/search", params={"q": "正文v1"}).json()["hits"]
    assert len(hits) == 1 and hits[0]["title"] == "笔记A"
    # 手动重建（新增文件验证全量重建）
    (vault / "b.md").write_text("# 笔记B\n内容", encoding="utf-8")
    job2 = c.post("/api/intel/vault/index").json()["job_id"]
    for _ in range(100):
        j2 = c.get(f"/api/jobs/{job2}").json()
        if j2["status"] != "running":
            break
    assert j2["status"] == "done" and j2["result"]["indexed"] == 2
    # 置空路径后 configured False；未配置时手动索引 422
    assert c.put("/api/intel/vault",
                 json={"path": "", "enabled": False}).json()["index_job_id"] is None
    assert c.get("/api/intel/vault").json()["configured"] is False
    assert c.post("/api/intel/vault/index").status_code == 422


def test_learning_plan_endpoint(intel_client):
    tmp_path, c = intel_client
    app = c.app
    captured = []

    class FakeLLM:
        def chat(self, messages, **kw):
            captured.append(messages[0]["content"])

            class R:
                text = "# 本周计划：读一篇 web 文章"

            return R()

    app.state.intel_llm = FakeLLM()
    r = c.post("/api/intel/learning/plan").json()
    week = r["week"]
    assert week == week_start()
    for _ in range(100):
        j = c.get(f"/api/jobs/{r['job_id']}").json()
        if j["status"] != "running":
            break
    assert j["status"] == "done", j
    plan = c.get("/api/intel/learning/plan").json()
    assert plan["week"] == week and "本周计划" in plan["content"]
    assert plan["inputs"]["by"] == "llm"
    assert len(captured) == 1
    plans = c.get("/api/intel/learning/plans").json()["plans"]
    assert plans[0]["week"] == week and "content" not in plans[0]  # 归档不含全文
    assert c.get("/api/intel/learning/plan",
                 params={"week": "1999-01-01"}).status_code == 404
