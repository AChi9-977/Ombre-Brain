"""Regression coverage for the token/search/batch/merge upgrade."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.anchor.core import pulse
from tools.breath.catalog import surface_catalog
from tools.breath.search import surface_search
from tools.dream import dispatch as dream_dispatch
from tools.trace.core import trace_core


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 0)


class DisabledEmbedding:
    enabled = False


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {"breath_max_results": 5, "dream_max_tokens": 6000}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = NoopDecay()
    rt.embedding_engine = DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


@pytest.mark.asyncio
async def test_catalog_has_rich_one_line_metadata_without_content(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="never expose this body",
        name="rich catalog",
        domain=["work"],
        importance=8,
        valence=0.7,
        arousal=0.2,
    )
    install_runtime(bucket_mgr)

    output = await surface_catalog()

    line = next(row for row in output.splitlines() if "rich catalog" in row)
    assert f"ID:{bucket_id}" in line
    assert "情感:V0.7/A0.2" in line
    assert "更新:" in line
    assert "never expose" not in output


@pytest.mark.asyncio
async def test_search_core_does_not_consume_regular_limit_and_reports_omitted(bucket_mgr):
    install_runtime(bucket_mgr)
    core_ids = [
        await bucket_mgr.create(content="needle core", name=f"core {i}", pinned=True)
        for i in range(2)
    ]
    regular_ids = [
        await bucket_mgr.create(content=f"needle regular {i}", name=f"regular {i}")
        for i in range(7)
    ]

    output = await surface_search(
        query="needle",
        max_results=5,
        max_tokens=20000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )

    assert all(bucket_id in output for bucket_id in core_ids)
    assert sum(bucket_id in output for bucket_id in regular_ids) == 5
    assert "还有 2 个相关桶未显示" in output


@pytest.mark.asyncio
async def test_literal_content_ranks_above_semantic_only_candidate(bucket_mgr):
    literal_id = await bucket_mgr.create(content="那次和小明的不愉快", name="普通记忆")
    semantic_id = await bucket_mgr.create(content="完全无关的内容", name="另一条")

    results = await bucket_mgr.search(
        "小明",
        limit=10,
        vector_scores={literal_id: 0.0, semantic_id: 1.0},
    )

    assert [item["id"] for item in results[:2]] == [literal_id, semantic_id]


@pytest.mark.asyncio
async def test_search_date_filter_uses_stable_updated_at(bucket_mgr):
    bucket_id = await bucket_mgr.create(content="date needle")
    install_runtime(bucket_mgr)
    created = await bucket_mgr.get(bucket_id)
    updated_at = created["metadata"]["updated_at"]

    future = (datetime.now() + timedelta(days=2)).date().isoformat()
    output = await surface_search(
        query="date needle",
        max_results=5,
        max_tokens=20000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
        date_from=future,
    )
    assert "没有匹配到" in output

    await bucket_mgr.touch_many([bucket_id], ripple=False)
    touched = await bucket_mgr.get(bucket_id)
    assert touched["metadata"]["updated_at"] == updated_at


@pytest.mark.asyncio
async def test_pulse_defaults_to_core_plus_top_fifteen(bucket_mgr):
    install_runtime(bucket_mgr)
    core_id = await bucket_mgr.create(content="core pulse", name="pulse core", pinned=True)
    regular_ids = [
        await bucket_mgr.create(content=f"pulse {i}", name=f"pulse {i}", importance=(i % 8) + 1)
        for i in range(20)
    ]

    output = await pulse()

    assert core_id in output
    assert sum(bucket_id in output for bucket_id in regular_ids) == 15
    assert "还有 5 个非钉选桶未显示" in output
    assert output.rfind("=== 我现在的记忆 ===") > output.find("=== 记忆列表 ===")


@pytest.mark.asyncio
async def test_dream_returns_five_summaries_and_explicit_full_detail():
    now = datetime.now()
    buckets = []
    for i in range(7):
        text = f"body-{i}-" + ("x" * 260)
        stamp = (now - timedelta(minutes=i)).isoformat(timespec="seconds")
        buckets.append({
            "id": f"b{i}",
            "content": text,
            "metadata": {
                "name": f"bucket {i}",
                "type": "dynamic",
                "domain": ["test"],
                "importance": 5,
                "created": stamp,
                "last_active": stamp,
            },
        })

    class Manager:
        async def list_all(self, include_archive=False):
            return buckets

    install_runtime(Manager())
    summary = await dream_dispatch()
    detailed = await dream_dispatch(detail_ids="b5")

    assert summary.count("摘要（原文摘录）") == 5
    assert "body-5" not in summary
    assert "[全文] body-5-" in detailed
    assert ("x" * 220) in detailed


@pytest.mark.asyncio
async def test_trace_batch_prevalidates_and_updates_metadata(bucket_mgr):
    first = await bucket_mgr.create(content="first")
    second = await bucket_mgr.create(content="second")
    install_runtime(bucket_mgr)

    result = await trace_core(f"{first},{second}", resolved=1)

    assert "批量 trace 完成" in result
    assert (await bucket_mgr.get(first))["metadata"]["resolved"] is True
    assert (await bucket_mgr.get(second))["metadata"]["resolved"] is True
    rejected = await trace_core(f"{first},{second}", content="unsafe replacement")
    assert "批量 trace 不允许" in rejected


@pytest.mark.asyncio
async def test_trace_merge_archives_source_and_preserves_both_contents(bucket_mgr):
    source = await bucket_mgr.create(
        content="source body", tags=["source"], domain=["source-domain"], importance=8,
        valence=0.2, arousal=0.8,
    )
    target = await bucket_mgr.create(
        content="target body", tags=["target"], domain=["target-domain"], importance=5,
        valence=0.8, arousal=0.2,
    )
    install_runtime(bucket_mgr)

    result = await trace_core(source, merge=target)
    merged = await bucket_mgr.get(target)
    archived_source = await bucket_mgr.get(source)

    assert "源桶已归档" in result
    assert "target body" in merged["content"] and "source body" in merged["content"]
    assert set(merged["metadata"]["tags"]) == {"source", "target"}
    assert merged["metadata"]["importance"] == 8
    assert merged["metadata"]["valence"] == pytest.approx(0.5)
    assert archived_source["metadata"]["type"] == "archived"
    assert archived_source["metadata"]["merged_into"] == target


@pytest.mark.asyncio
async def test_trace_merge_rejects_pinned_source_or_target(bucket_mgr):
    source = await bucket_mgr.create(content="ordinary")
    target = await bucket_mgr.create(content="protected", pinned=True)
    install_runtime(bucket_mgr)

    result = await trace_core(source, merge=target)

    assert "禁止合并" in result
    assert (await bucket_mgr.get(source))["metadata"]["type"] != "archived"
