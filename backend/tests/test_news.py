"""Tests for the goal-11 news pipeline: ingest (synopsis cap, dedupe, no article-body
fetch), the curation prompt-builder contract + id-validation dispose, code-random
serendipity, the feedback loop, gating, and the non-Google posture.

The curator (the news runtime LLM) and every network fetch are fully mocked — no API
key, no network. The guardrail tests are gate-critical: the news module imports NO
Google client, the prompt serializes ONLY {id,title,synopsis,source,published_at}, the
synopsis is length-capped, and ingestion never fetches an article page for its body.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import random

import pytest

from app.auth import service as auth_svc
from app.news import curator, gating, ingest
from app.news import service as news_svc
from app.news.ingest import RawItem
from app.news.models import SELECTED, NewsItem
from app.news.schema import CurationPick, CurationResult


def run(coro):
    return asyncio.run(coro)


def _item(id_, title, synopsis="", source="rss", published=None):
    return NewsItem(
        id=id_,
        user_id=1,
        source=source,
        feed="Feed",
        title=title,
        url=f"https://x.com/{id_}",
        canonical_url=f"https://x.com/{id_}",
        synopsis=synopsis,
        published_at=published,
    )


# ── Synopsis capture (the "no body through the synopsis field" guard) ─────────


def test_capture_synopsis_strips_html_and_caps_length():
    full_article = "<p>" + ("lorem ipsum dolor " * 500) + "</p>"
    s = ingest.capture_synopsis(full_article)
    assert "<" not in s and ">" not in s  # HTML stripped
    # Length-capped so a full-text feed can't smuggle a whole body through.
    assert len(s) <= curator.config.SYNOPSIS_MAX_CHARS + 1
    assert len(s) < len(full_article)


def test_capture_synopsis_empty():
    assert ingest.capture_synopsis(None) == ""
    assert ingest.capture_synopsis("") == ""


# ── Dedupe (URL + fuzzy title, across sources) ────────────────────────────────


def test_dedupe_by_canonical_url_and_fuzzy_title():
    items = [
        RawItem(
            "rss", "Ars", "Big Model Ships Today", "https://a.com/1?utm_source=rss"
        ),
        RawItem("hn", "HN", "Big Model Ships Today!", "https://a.com/1?utm_source=hn"),
        RawItem("guardian", "G", "A Wholly Different Piece", "https://b.com/2"),
    ]
    out = ingest.dedupe(items)
    assert len(out) == 2  # the two "Big Model" rows collapse to one
    assert {i.feed for i in out} == {"Ars", "G"}


def test_canonical_url_drops_tracking_and_fragment():
    a = ingest.canonical_url("https://Example.com/x/?utm_source=a&id=5#frag")
    b = ingest.canonical_url("https://example.com/x?id=5")
    assert a == b


# ── Parsers ───────────────────────────────────────────────────────────────────


def test_parse_rss_extracts_title_link_synopsis():
    rss = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>T1</title><link>https://a.com/1</link>"
        "<description>&lt;b&gt;hello&lt;/b&gt; world</description></item>"
        "</channel></rss>"
    )
    out = ingest.parse_rss(rss, "MyFeed")
    assert len(out) == 1
    assert out[0].title == "T1"
    assert out[0].url == "https://a.com/1"
    assert out[0].synopsis == "hello world"  # HTML stripped
    assert out[0].source == "rss" and out[0].feed == "MyFeed"


def test_parse_hn_skips_urlless_and_has_no_synopsis():
    hits = [
        {
            "title": "Story A",
            "url": "https://a.com/a",
            "created_at": "2026-07-28T00:00:00Z",
        },
        {"title": "Ask HN: no url", "url": ""},  # skipped — nothing to link out to
    ]
    out = ingest.parse_hn(hits)
    assert len(out) == 1
    assert out[0].synopsis == ""  # HN carries no standfirst


def test_parse_guardian_uses_trailtext():
    payload = {
        "response": {
            "results": [
                {
                    "webTitle": "G Title",
                    "webUrl": "https://g.com/1",
                    "webPublicationDate": "2026-07-28T10:00:00Z",
                    "fields": {"trailText": "the standfirst"},
                }
            ]
        }
    }
    out = ingest.parse_guardian(payload)
    assert out[0].synopsis == "the standfirst"
    assert out[0].source == "guardian"


# ── Curation prompt-builder contract ──────────────────────────────────────────


def test_candidate_payload_serializes_exact_field_set():
    items = [_item(1, "Title", synopsis="short")]
    payload = curator.build_candidate_payload(items)
    assert set(payload[0].keys()) == set(curator.CANDIDATE_FIELDS)
    assert set(payload[0].keys()) == {
        "id",
        "title",
        "synopsis",
        "source",
        "published_at",
    }
    # No article-body / fetched-content field is ever serialized.
    assert "body" not in payload[0] and "content" not in payload[0]


def test_candidate_payload_synopsis_is_capped_for_full_text_feed():
    body = "<p>" + ("word " * 1000) + "</p>"
    raw = RawItem("rss", "Feed", "T", "https://x.com/1", synopsis=body)
    item = _item(1, "T", synopsis=raw.synopsis)  # raw.synopsis already capped at ingest
    payload = curator.build_candidate_payload([item])
    assert len(payload[0]["synopsis"]) <= curator.config.SYNOPSIS_MAX_CHARS + 1


def test_build_prompt_contains_profile_and_candidates_no_google():
    payload = curator.build_candidate_payload([_item(1, "T", synopsis="s")])
    system, user = curator.build_prompt(payload, "I like frontier models")
    assert "I like frontier models" in user
    assert '"id": "1"' in user or '"id":"1"' in user
    lowered = (system + user).lower()
    assert "google" not in lowered and "gmail" not in lowered


# ── Dispose: id validation + code-random serendipity ──────────────────────────


def test_dispose_validates_ids_and_flags_serendipity():
    candidates = [_item(i, f"T{i}") for i in range(1, 11)]  # ids 1..10
    result = CurationResult(
        picks=[
            CurationPick(id="1", why="matters", domain="science"),
            CurationPick(id="999", why="alien", domain="other"),  # not a candidate
            CurationPick(id="2", why="also", domain="technology"),
        ]
    )
    rng = random.Random(42)
    news_svc._dispose_curation(candidates, result, rng)

    by_id = {c.id: c for c in candidates}
    # Valid picks selected with their why/domain; alien id never touched anything.
    assert by_id[1].status == SELECTED and by_id[1].why_line == "matters"
    assert by_id[1].domain == "science"
    assert by_id[2].status == SELECTED
    picks = [c for c in candidates if c.status == SELECTED and not c.is_serendipity]
    assert {c.id for c in picks} == {1, 2}  # 999 dropped, not invented

    serendipity = [c for c in candidates if c.is_serendipity]
    assert len(serendipity) == news_svc.config.SERENDIPITY_SLOTS
    # Serendipity comes from the NON-picked pool only.
    assert all(c.id not in {1, 2} for c in serendipity)
    assert all(c.status == SELECTED for c in serendipity)
    assert all(c.why_line is None for c in serendipity)  # off-profile: no why


def test_dispose_serendipity_is_deterministic_under_seed():
    def picked_serendipity(seed):
        cands = [_item(i, f"T{i}") for i in range(1, 11)]
        res = CurationResult(picks=[CurationPick(id="1", why="w", domain="other")])
        news_svc._dispose_curation(cands, res, random.Random(seed))
        return sorted(c.id for c in cands if c.is_serendipity)

    assert picked_serendipity(7) == picked_serendipity(7)  # reproducible


def test_dispose_empty_picks_falls_back_to_recency():
    candidates = [_item(i, f"T{i}") for i in range(1, 6)]
    news_svc._dispose_curation(candidates, CurationResult(picks=[]), random.Random(1))
    assert any(c.status == SELECTED for c in candidates)  # feed never empty


# ── Ingestion never fetches an article body ───────────────────────────────────


def test_ingest_makes_no_per_article_fetch(monkeypatch):
    requested: list[str] = []
    rss_body = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Story</title><link>https://article.example/deep/piece</link>"
        "<description>synopsis only</description></item></channel></rss>"
    )

    async def fake_text(url):
        requested.append(url)
        return rss_body

    async def fake_json(url):
        requested.append(url)
        return {"hits": []}

    monkeypatch.setattr(ingest, "_fetch_text", fake_text)
    monkeypatch.setattr(ingest, "_fetch_json", fake_json)

    feed_url = "https://feed.example/rss"
    items = run(ingest.fetch_all([feed_url]))

    # The article URL is stored but NEVER fetched — only feed/section endpoints are hit.
    assert any(i.url == "https://article.example/deep/piece" for i in items)
    assert "https://article.example/deep/piece" not in requested
    assert feed_url in requested


def test_fetch_all_pulls_all_three_sources(monkeypatch):
    monkeypatch.setattr(ingest.config, "GUARDIAN_API_KEY", "test-key")
    rss = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>RSS Story</title><link>https://r.com/1</link>"
        "<description>d</description></item></channel></rss>"
    )
    guardian = {
        "response": {
            "results": [
                {
                    "webTitle": "Guardian Story",
                    "webUrl": "https://g.com/1",
                    "fields": {"trailText": "t"},
                }
            ]
        }
    }
    hn = {"hits": [{"title": "HN Story", "url": "https://h.com/1"}]}

    async def fake_text(url):
        return rss

    async def fake_json(url):
        return guardian if "guardianapis" in url else hn

    monkeypatch.setattr(ingest, "_fetch_text", fake_text)
    monkeypatch.setattr(ingest, "_fetch_json", fake_json)

    items = run(ingest.fetch_all(["https://feed.example/rss"]))
    sources = {i.source for i in items}
    assert sources == {"rss", "hn", "guardian"}  # all three types present


def test_fetch_all_survives_a_dead_feed(monkeypatch):
    good_rss = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Alive</title><link>https://ok.com/1</link>"
        "<description>d</description></item></channel></rss>"
    )

    async def fake_text(url):
        if "dead" in url:
            raise RuntimeError("404 feed is down")  # one feed dies
        return good_rss

    async def fake_json(url):
        return {"hits": []}

    monkeypatch.setattr(ingest, "_fetch_text", fake_text)
    monkeypatch.setattr(ingest, "_fetch_json", fake_json)

    items = run(
        ingest.fetch_all(["https://dead.example/rss", "https://ok.example/rss"])
    )
    # The dead feed is skipped; the healthy one still ingests — run completes.
    assert any(i.title == "Alive" for i in items)


# ── End-to-end daily run (mocked ingest + curator) ────────────────────────────


@pytest.fixture
def fake_pipeline(monkeypatch):
    async def fake_fetch_all(urls):
        return [
            RawItem("rss", "Ars", f"Story {i}", f"https://x.com/{i}", synopsis=f"s{i}")
            for i in range(8)
        ]

    async def fake_curate(items, profile_md):
        return CurationResult(
            picks=[
                CurationPick(id=str(items[0].id), why="top pick", domain="science"),
                CurationPick(id=str(items[1].id), why="second", domain="technology"),
            ]
        )

    monkeypatch.setattr(news_svc.ingest, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(news_svc.curator, "curate", fake_curate)


def test_run_daily_persists_selected_feed(session, user_a, fake_pipeline):
    tally = run(news_svc.run_daily(session, user_a.id, rng=random.Random(0)))
    assert tally["selected"] >= 2
    assert tally["serendipity"] == news_svc.config.SERENDIPITY_SLOTS

    items = news_svc.feed_items(session, user_a.id)
    assert items  # feed populated
    top = items[0]
    assert top.status == SELECTED and top.why_line == "top pick"
    # ordering by score ascending
    scores = [i.score for i in items]
    assert scores == sorted(scores)
    assert any(i.is_serendipity for i in items)


def test_run_daily_dedupes_across_runs(session, user_a, fake_pipeline):
    run(news_svc.run_daily(session, user_a.id, rng=random.Random(0)))
    first = len(session.exec(__import__("sqlmodel").select(NewsItem)).all())
    # A second run fetches the same URLs → no new candidates ingested.
    tally2 = run(news_svc.run_daily(session, user_a.id, rng=random.Random(0)))
    second = len(session.exec(__import__("sqlmodel").select(NewsItem)).all())
    assert tally2["new_candidates"] == 0
    assert first == second


# ── Feedback loop + weekly rewrite ────────────────────────────────────────────


def test_feedback_upsert_and_map(session, user_a, fake_pipeline):
    run(news_svc.run_daily(session, user_a.id, rng=random.Random(0)))
    item = news_svc.feed_items(session, user_a.id)[0]
    news_svc.set_feedback(session, user_a.id, item.id, 1, "loved it")
    news_svc.set_feedback(session, user_a.id, item.id, -1, "changed my mind")  # upsert
    fbmap = news_svc.feedback_map(session, user_a.id, [item.id])
    assert fbmap[item.id].vote == -1
    assert fbmap[item.id].comment == "changed my mind"


def test_feedback_cross_tenant_blocked(session, user_a, user_b, fake_pipeline):
    from app.errors import ApiError

    run(news_svc.run_daily(session, user_a.id, rng=random.Random(0)))
    item = news_svc.feed_items(session, user_a.id)[0]
    with pytest.raises(ApiError):
        news_svc.set_feedback(session, user_b.id, item.id, 1, None)


def test_weekly_rewrite_incorporates_comment(
    session, user_a, fake_pipeline, monkeypatch
):
    run(news_svc.run_daily(session, user_a.id, rng=random.Random(0)))
    item = news_svc.feed_items(session, user_a.id)[0]
    news_svc.set_feedback(session, user_a.id, item.id, -1, "too incremental")

    captured = {}

    async def fake_rewrite(current, feedback):
        captured["feedback"] = feedback
        return "# Rewritten profile\nsharper on capability jumps"

    monkeypatch.setattr(news_svc.profile, "rewrite", fake_rewrite)
    out = run(news_svc.run_weekly(session, user_a.id))
    assert out["rewritten"] is True
    prof = news_svc.get_or_create_profile(session, user_a.id)
    assert "Rewritten profile" in prof.profile_md
    assert prof.profile_prev_md != prof.profile_md  # previous version retained
    assert any("too incremental" in (f.comment or "") for f in captured["feedback"])


def test_profile_set_and_revert(session, user_a):
    news_svc.set_profile(session, user_a.id, "v1")
    news_svc.set_profile(session, user_a.id, "v2")
    news_svc.revert_profile(session, user_a.id)
    prof = news_svc.get_or_create_profile(session, user_a.id)
    assert prof.profile_md == "v1"


# ── Gating + posture ──────────────────────────────────────────────────────────


def test_gating_reads_db_flag(session, user_a):
    """News is off until the superuser flips the per-user flag on the allowlist row."""
    assert not gating.is_news_enabled(session, user_a)
    auth_svc.add_allowed(session, user_a.email, added_by="admin@example.com")
    assert not gating.is_news_enabled(session, user_a)  # row exists, flag still off
    auth_svc.set_feature(session, user_a.email, "news", True)
    assert gating.is_news_enabled(session, user_a)
    auth_svc.set_feature(session, user_a.email, "news", False)
    assert not gating.is_news_enabled(session, user_a)


def test_gating_superuser_always_enabled(session, user_a):
    """A superuser has News on with no allowlist row and no flag set."""
    user_a.is_superuser = True
    session.add(user_a)
    session.commit()
    assert gating.is_news_enabled(session, user_a)


def test_set_feature_unknown_and_missing(session, user_a):
    with pytest.raises(KeyError):
        auth_svc.set_feature(session, user_a.email, "nope", True)
    with pytest.raises(LookupError):
        auth_svc.set_feature(session, "stranger@example.com", "news", True)


def test_news_endpoints_gated(auth, session, user_a):
    client = auth.as_user(user_a)
    assert client.get("/news").status_code == 403  # not enabled → invisible
    auth_svc.add_allowed(session, user_a.email, added_by="admin@example.com")
    auth_svc.set_feature(session, user_a.email, "news", True)
    assert client.get("/news").status_code == 200


def test_me_reports_news_enabled(auth, session, user_a):
    client = auth.as_user(user_a)
    assert client.get("/auth/me").json()["news_enabled"] is False
    auth_svc.add_allowed(session, user_a.email, added_by="admin@example.com")
    auth_svc.set_feature(session, user_a.email, "news", True)
    assert client.get("/auth/me").json()["news_enabled"] is True


def test_feature_toggle_endpoint_superuser_only(auth, session, user_a, user_b):
    """PUT /settings/allowed-emails/{email}/features is superuser-gated and toggles
    the flag; a non-superuser is 403."""
    auth_svc.add_allowed(session, user_b.email, added_by="admin@example.com")
    # Non-superuser (user_a) cannot toggle.
    non_su = auth.as_user(user_a)
    r = non_su.put(
        f"/settings/allowed-emails/{user_b.email}/features",
        json={"feature": "news", "enabled": True},
    )
    assert r.status_code == 403
    # Superuser can.
    user_a.is_superuser = True
    session.add(user_a)
    session.commit()
    su = auth.as_user(user_a)
    r = su.put(
        f"/settings/allowed-emails/{user_b.email}/features",
        json={"feature": "news", "enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["features"]["news"] is True


_NEWS_MODULES = (
    "config",
    "gating",
    "ingest",
    "curator",
    "profile",
    "schema",
    "service",
    "scheduler",
    "models",
)


def _news_module_sources():
    import importlib

    return {
        n: inspect.getsource(importlib.import_module(f"app.news.{n}"))
        for n in _NEWS_MODULES
    }


def test_news_module_imports_no_google_client():
    """Statically: NO news module imports anything from app.google — the news pipeline
    is entirely non-Google (goal-11 posture; the AST/write-dependency test is unchanged
    because news adds zero Google API calls)."""
    for name, src in _news_module_sources().items():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "app.google"
            ):
                pytest.fail(f"{name} imports from app.google: {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.google"), name


def test_news_service_calls_no_anthropic_write_directly():
    """The service disposes; it never calls Anthropic itself — the LLM lives behind
    curator/profile (LLM-proposes / code-disposes)."""
    src = inspect.getsource(news_svc)
    assert "anthropic" not in src.lower()
