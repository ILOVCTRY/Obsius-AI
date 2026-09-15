"""情报面板（E9，DESIGN.md §16）测试：全局存储 / 抓取解析 / 打分与简报 / API 端点。

全部不触网：抓取层 getter 注入、LLM 注入假对象。
"""

import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.intel.config import load_feeds, load_profile, save_profile
from core.intel.fetch import fetch_all, fetch_kev, fetch_nvd, fetch_github_advisories, parse_rss
from core.intel.intel_service import compose_brief, run_refresh, score_articles
from core.intel.store import IntelStore


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
    def fake_getter(url, timeout):
        if "nvd" in url:
            return 200, json.dumps({"vulnerabilities": [
                {"cve": {"id": "CVE-2026-0001", "published": "2026-09-15T00:00:00.000",
                         "descriptions": [{"lang": "en", "value": "bad bug"}]}}]})
        if "known_exploited" in url:
            return 200, json.dumps({"vulnerabilities": [
                {"cveID": "CVE-2026-0002", "dateAdded": "2026-09-15",
                 "vulnerabilityName": "In-the-wild bug",
                 "shortDescription": "exploited"}]})
        return 200, json.dumps([{"ghsa_id": "GHSA-xxxx", "cve_id": "CVE-2026-0003",
                                 "summary": "ghsa sum", "html_url": "https://g.co/1",
                                 "published": "2026-09-15T00:00:00Z", "type": "reviewed"}])

    nvd = fetch_nvd(fake_getter)
    assert nvd[0]["title"] == "CVE-2026-0001" and "bad bug" in nvd[0]["summary"]
    kev = fetch_kev(fake_getter)
    assert kev[0]["is_priority"] is True and kev[0]["title"].startswith("[KEV]")
    ghsa = fetch_github_advisories(fake_getter)
    assert ghsa[0]["source"] == "ghsa" and "CVE-2026-0003" in ghsa[0]["title"]


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
