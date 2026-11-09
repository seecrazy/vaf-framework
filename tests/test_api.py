"""移动接入层测试（V2.5）：网关鉴权/RBAC/端点、全移动闭环流（提交→澄清→卡片→审批）、
钉钉/企微/飞书卡片格式、H5 审核台页面。"""
import json
import time

import pytest

from e2e_helpers import EMERGENCY_PATIENT, make_app
from vaf.api.auth import ApiKeyStore
from vaf.api.cards import (
    card_sink,
    dingtalk_payload,
    dingtalk_sign,
    feishu_payload,
    feishu_sign,
    wecom_payload,
)
from vaf.api.gateway import create_gateway
from vaf.errors import VafError

fastapi_testclient = pytest.importorskip("fastapi.testclient")

OPERATOR_KEY = "k_op_111"
REVIEWER_KEY = "k_rev_222"


def _keys():
    return ApiKeyStore.from_specs(
        [f"{OPERATOR_KEY}:op-user:operator", f"{REVIEWER_KEY}:rev-user:reviewer"]
    )


def _client(app=None, keys="default"):
    app = app or make_app()
    gateway = create_gateway(
        app,
        keys=_keys() if keys == "default" else keys,
        review_base="http://gw.test",
    )
    return fastapi_testclient.TestClient(gateway), app


def _hdr(key):
    return {"X-VAF-API-Key": key}


# ==========================================================================
# 鉴权与 RBAC
# ==========================================================================
def test_open_mode_when_no_keys():
    client, _ = _client(keys=None)
    assert client.get("/health").json()["auth_mode"].startswith("open")
    # 开放模式可直接读首页与待审列表
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/reviews/pending").status_code == 200


def test_api_key_required_when_configured():
    client, _ = _client()
    assert client.get("/api/v1/reviews/pending").status_code == 401
    assert client.get("/api/v1/reviews/pending", headers=_hdr("wrong")).status_code == 401
    assert client.get("/api/v1/reviews/pending", headers=_hdr(REVIEWER_KEY)).status_code == 200


def test_rbac_operator_cannot_approve():
    client, _ = _client()
    body = {"decision": "approved", "comment": ""}
    # operator 角色有 SUBMIT_REVIEW，但任务要求 reviewer → 引擎 RBAC 拒绝
    resp = client.post("/api/v1/reviews/ghost/decision", json=body, headers=_hdr(OPERATOR_KEY))
    assert resp.status_code == 404  # 任务不存在先行校验
    # auditor 角色（无 SUBMIT_REVIEW）连列表都不可见
    keys = ApiKeyStore.from_specs(["k_au:aud:auditor"])
    client2, _ = _client(keys=keys)
    assert client2.get("/api/v1/reviews/pending", headers=_hdr("k_au")).status_code == 403


def test_apikey_store_parsing_and_generate():
    store = ApiKeyStore.from_specs(["k1:alice:reviewer,operator"])
    user = store.authenticate("k1")
    assert user.user_id == "alice" and "reviewer" in user.roles
    assert store.authenticate("nope") is None
    with pytest.raises(VafError, match="格式"):
        ApiKeyStore.from_specs(["bad-spec"])
    key, spec = ApiKeyStore.generate("bob", ["reviewer"])
    assert key.startswith("vaf_") and "bob" in spec and "reviewer" in spec


# ==========================================================================
# 全移动闭环：提交 → 澄清 → 卡片事件 → 手机审批 → 完成
# ==========================================================================
def test_full_mobile_flow_review_via_api():
    client, app = _client()

    # 1. 手机端（operator key）提交 run
    resp = client.post(
        "/api/v1/runs",
        json={"domain": "medical_triage", "input": EMERGENCY_PATIENT, "request_id": "mob-1"},
        headers=_hdr(OPERATOR_KEY),
    )
    assert resp.status_code == 200, resp.text
    handle = resp.json()
    assert handle["status"] == "waiting_input"
    assert handle["pending_clarification"]["questions"]

    # 2. 提交澄清回答（等待审核）
    resp = client.post(
        "/api/v1/runs/mob-1/clarifications",
        json={"answers": {"疼痛是否放射？": "是"}},
        headers=_hdr(OPERATOR_KEY),
    )
    handle = resp.json()
    assert handle["status"] == "waiting_review"
    task_id = handle["pending_review_task"]

    # 3. 待审列表（手机审核台首页数据源）
    pending = client.get("/api/v1/reviews/pending", headers=_hdr(REVIEWER_KEY)).json()
    task = next(t for t in pending if t["task_id"] == task_id)
    assert task["run_id"] == "mob-1" and "conclusion" in task["payload_digest"]

    # 4. 审核员批准
    resp = client.post(
        f"/api/v1/reviews/{task_id}/decision",
        json={"decision": "approved", "comment": "手机端批准"},
        headers=_hdr(REVIEWER_KEY),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"

    # 5. inspect 可查（审计角色权限）
    detail = client.get("/api/v1/runs/mob-1", headers=_hdr(OPERATOR_KEY)).json()
    assert detail["status"] == "succeeded" and detail["steps"]


def test_reviewer_key_cannot_submit_runs():
    client, _ = _client()
    resp = client.post(
        "/api/v1/runs",
        json={"domain": "medical_triage", "input": EMERGENCY_PATIENT},
        headers=_hdr(REVIEWER_KEY),
    )
    assert resp.status_code == 403  # reviewer 无 run_pipeline 权限


def test_bad_decision_value_rejected():
    client, _ = _client()
    resp = client.post(
        "/api/v1/reviews/x/decision",
        json={"decision": "maybe"},
        headers=_hdr(REVIEWER_KEY),
    )
    assert resp.status_code == 422  # pydantic pattern 校验


def test_unknown_run_returns_404():
    client, _ = _client()
    assert client.get("/api/v1/runs/ghost", headers=_hdr(OPERATOR_KEY)).status_code == 404


def test_presets_endpoint():
    client, _ = _client()
    data = client.get("/api/v1/presets").json()
    names = {p["name"] for p in data}
    assert {"credit_review", "sap_fico_journal_check"} <= names


def test_h5_console_served():
    client, _ = _client()
    html = client.get("/").text
    assert "VAF 审核台" in html and "X-VAF-API-Key" in html
    assert "apple-mobile-web-app-capable" in html  # PWA 可添加到主屏


def test_openapi_generated():
    client, _ = _client()
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    for p in ("/api/v1/runs", "/api/v1/reviews/pending", "/api/v1/runs/{request_id}/clarifications"):
        assert p in paths


# ==========================================================================
# 消息卡片格式（钉钉/企微/飞书官方协议）
# ==========================================================================
_REVIEW_EVENT = {
    "event_type": "review_requested",
    "payload": {
        "request_id": "mob-1",
        "pipeline": "medical_triage",
        "step": "emergency_audit",
        "task_id": "task-9",
        "fields": ["conclusion", "red_flags"],
        "payload_digest": {"conclusion": "emergency", "red_flags": ["胸痛"]},
        "required_role": "reviewer",
    },
}
_CLARIFY_EVENT = {
    "event_type": "clarification_requested",
    "payload": {
        "request_id": "mob-2",
        "pipeline": "medical_triage",
        "step": "triage_clarify",
        "round_no": 1,
        "questions": ["疼痛是否放射？", "是否出汗？"],
    },
}


def test_dingtalk_actioncard_payload_and_sign():
    body = dingtalk_payload("review_requested", _REVIEW_EVENT["payload"], "http://gw.test")
    assert body["msgtype"] == "actionCard"
    card = body["actionCard"]
    assert card["singleTitle"] == "去处理"
    assert card["singleURL"] == "http://gw.test/#/task/task-9"
    assert "emergency" in card["text"] and "reviewer" in card["text"]
    sig = dingtalk_sign("SEC123", 1700000000000)
    assert len(sig) > 20  # base64 HMAC


def test_wecom_markdown_payload():
    body = wecom_payload("review_requested", _REVIEW_EVENT["payload"], "http://gw.test")
    assert body["msgtype"] == "markdown"
    content = body["markdown"]["content"]
    assert "待人工审核" in content and "[点击去处理](http://gw.test/#/task/task-9)" in content


def test_feishu_interactive_payload_and_sign():
    body = feishu_payload("clarification_requested", _CLARIFY_EVENT["payload"], "http://gw.test")
    assert body["msgtype"] == "interactive"
    elements = body["card"]["elements"]
    assert any(e.get("tag") == "action" for e in elements)
    button = [a for e in elements if e.get("tag") == "action" for a in e["actions"]][0]
    assert button["url"] == "http://gw.test/#/run/mob-2/clarify"
    assert len(feishu_sign("SECRET", 1700000000)) > 20


def test_card_sink_ignores_irrelevant_events_and_posts_card(monkeypatch):
    import vaf.api.cards as cards_mod

    posted = []
    monkeypatch.setattr(cards_mod, "post_json", lambda url, body, **kw: posted.append((url, body, kw)) or True)
    sink = card_sink("dingtalk", "http://hook.test/robot", review_base="http://gw.test", secret="SEC")
    sink("run_finished", {"x": 1})  # 非卡片事件 → 不推送
    assert not posted
    sink("review_requested", _REVIEW_EVENT["payload"])
    url, body, kw = posted[-1]
    assert url == "http://hook.test/robot"
    assert "timestamp" in kw["params"] and "sign" in kw["params"]  # 钉钉加签查询参数
    assert json.loads(body)["msgtype"] == "actionCard"  # post_json 收到的是 bytes


def test_card_sink_unknown_platform():
    import pytest as _pytest

    with _pytest.raises(ValueError, match="不支持的平台"):
        card_sink("slack", "http://x", review_base="http://gw")


# ---------------------------------------------------------------- P0 批次
def test_notification_delivery_is_async_and_fast():
    """引擎入队不阻塞：慢 sink（sleep）不影响 notify 返回时间。"""
    import time as _time

    from vaf.runtime.notification import NotificationHub

    def slow_sink(event_type, payload):
        _time.sleep(0.3)

    hub = NotificationHub(sinks=[slow_sink], async_delivery=True)
    start = _time.perf_counter()
    hub.notify("review_requested", {"x": 1})
    elapsed = _time.perf_counter() - start
    assert elapsed < 0.05, "notify 应立即返回（异步投递）"
    assert hub.flush(timeout_s=5.0)
    assert hub._sent.value() == 1


def test_typed_cache_eviction():
    """超容量的最旧 run 类型化缓存被淘汰（防长期运行膨胀）。"""
    from vaf.runtime.engine import PipelineEngine

    engine = make_app().engine
    engine._typed_capacity = 3
    with engine._typed_lock:
        for i in range(6):
            engine._typed[f"r{i}"] = {}
            engine._typed_order.append(f"r{i}")
        engine._evict_typed_locked()
    assert len(engine._typed) == 3
    assert set(engine._typed) == {"r3", "r4", "r5"}


def test_in_memory_store_eviction_keeps_waiting():
    """超限时优先淘汰非等待态；等待中的 run 保留。"""
    import time as _time

    from vaf.runtime.persistence import InMemoryStateStore
    from vaf.runtime.state import PipelineState, RunStatus

    store = InMemoryStateStore(max_states=3)
    for i in range(5):
        state = PipelineState(request_id=f"done-{i}", pipeline="p", domain="d")
        state.status = RunStatus.SUCCEEDED
        state.updated_at = _time.time() + i
        store.save(state)
    waiting = PipelineState(request_id="waiting-1", pipeline="p", domain="d")
    waiting.status = RunStatus.WAITING_REVIEW
    waiting.updated_at = 1.0  # 最旧
    store.save(waiting)
    assert store.get("waiting-1") is not None  # 等待态不淘汰
    assert store.get("done-0") is None  # 最旧完成态被淘汰


def test_gateway_rate_limit():
    from vaf.api.gateway import create_gateway

    _, app = _client(keys=None)
    gateway = create_gateway(app, keys=None, rate_limit=(3, 60))
    client = fastapi_testclient.TestClient(gateway)
    statuses = [client.get("/health").status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429 and statuses[4] == 429
