import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from backend.main import app
from backend.dynamo import store
from backend.config import settings

# Real available models on this Groq account
PRIMARY_MODEL  = "openai/gpt-oss-120b"
FALLBACK_MODEL = "openai/gpt-oss-20b"

@pytest.fixture(autouse=True)
def reset_state():
    """
    Resets state and forces in-memory mode before every test.
    This ensures deterministic test results regardless of DynamoDB state,
    and avoids flaky network calls during testing.
    """
    original_dynamo_flag = store.use_dynamodb
    store.use_dynamodb = False   # Force in-memory for test isolation
    store.reset_all_spend()
    yield
    store.use_dynamodb = original_dynamo_flag


@pytest.mark.asyncio
async def test_health_and_metrics_endpoints():
    """Verify production health check and Prometheus metrics endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res_health = await ac.get("/health")
        assert res_health.status_code == 200
        data = res_health.json()
        assert data["status"] == "healthy"
        assert "app_name" in data

        res_metrics = await ac.get("/metrics")
        assert res_metrics.status_code == 200
        assert b"aivar_governance_requests_total" in res_metrics.content


@pytest.mark.asyncio
async def test_concurrent_budget_tracking_across_three_agents():
    """
    Success Criteria 1: Budget correctly tracked across 3 agents making concurrent calls.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agents = ["agent-support-01", "agent-analytics-02", "agent-research-03"]

        async def send_call(agent_id: str):
            return await ac.post("/v1/chat/completions", json={
                "agent_id": agent_id,
                "model": PRIMARY_MODEL,
                "messages": [{"role": "user", "content": "Concurrent test prompt"}]
            })

        responses = await asyncio.gather(*[send_call(a) for a in agents])

        for res in responses:
            assert res.status_code == 200
            assert "choices" in res.json()
            assert res.headers.get("X-Governance-Disposition") == "ALLOWED"

        # Verify all 3 agents have nonzero spend
        for agent_id in agents:
            agent = store.get_agent(agent_id)
            assert agent is not None
            assert agent["current_spend_usd"] > 0.0

        # Verify team spend = sum of agent spends
        team = store.get_team("team-eng")
        expected = sum(store.get_agent(a)["current_spend_usd"] for a in agents)
        assert round(team["current_spend_usd"], 6) == round(expected, 6)


@pytest.mark.asyncio
async def test_warning_fires_at_80_percent_consumed():
    """
    Success Criteria 2: Warning fires at 80% consumed.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-support-01"
        store.create_or_update_agent(
            agent_id=agent_id, team_id="team-eng",
            name="Customer Support Agent",
            monthly_limit_usd=0.0002,
            preferred_model=PRIMARY_MODEL,
            fallback_model=FALLBACK_MODEL
        )
        # Seed spend at 85% in memory directly
        store._agents[agent_id]["current_spend_usd"] = 0.000170  # 85% of 0.0002

        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": PRIMARY_MODEL,
            "messages": [{"role": "user", "content": "Test warning threshold"}],
            "allow_model_substitution": False   # Disable substitution to test WARNED disposition
        })

        assert res.status_code == 200
        assert res.headers.get("X-Governance-Disposition") == "WARNED"
        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "WARNING_80" for a in alerts)


@pytest.mark.asyncio
async def test_hard_block_fires_at_100_percent_consumed():
    """
    Success Criteria 3: Hard block fires at 100% consumed.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-analytics-02"
        store.create_or_update_agent(
            agent_id=agent_id, team_id="team-eng",
            name="Analytics Agent", monthly_limit_usd=10.0,
            preferred_model=PRIMARY_MODEL, fallback_model=FALLBACK_MODEL
        )
        store._agents[agent_id]["current_spend_usd"] = 10.00  # 100% exhausted

        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": PRIMARY_MODEL,
            "messages": [{"role": "user", "content": "Should be blocked"}]
        })

        assert res.status_code == 429
        assert res.json()["detail"]["error"]["code"] == "BUDGET_EXHAUSTED"
        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "HARD_BLOCK_100" for a in alerts)


@pytest.mark.asyncio
async def test_session_budget_correctly_closes_session():
    """
    Success Criteria 4: Session budget correctly closes a session.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id  = "agent-research-03"
        session_id = "sess-test-999"

        session = store.get_or_create_session(session_id=session_id, agent_id=agent_id, limit_usd=0.50)
        session["current_spend_usd"] = 0.50  # At limit

        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id, "session_id": session_id,
            "model": PRIMARY_MODEL,
            "messages": [{"role": "user", "content": "Session budget test"}]
        })

        assert res.status_code == 429
        session_obj = store.get_session(session_id)
        assert session_obj["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_model_substitution_reroutes_to_cheaper_model():
    """
    Success Criteria 5: Model substitution reroutes to cheaper model under budget pressure.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-support-01"
        store.create_or_update_agent(
            agent_id=agent_id, team_id="team-eng", name="Support Agent",
            monthly_limit_usd=1.00,
            preferred_model=PRIMARY_MODEL,
            fallback_model=FALLBACK_MODEL
        )
        store._agents[agent_id]["current_spend_usd"] = 0.85  # 85% consumed

        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": PRIMARY_MODEL,
            "messages": [{"role": "user", "content": "Test automatic model fallback"}],
            "allow_model_substitution": True
        })

        assert res.status_code == 200
        assert res.headers.get("X-Governance-Disposition") == "REROUTED"
        assert res.headers.get("X-Governance-Substituted") == "true"
        assert res.headers.get("X-Governance-Model-Requested") == PRIMARY_MODEL
        assert res.headers.get("X-Governance-Model-Used") == FALLBACK_MODEL
        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "MODEL_SUBSTITUTED" for a in alerts)


@pytest.mark.asyncio
async def test_runaway_loop_detector_bonus():
    """
    Bonus: Detects runaway agent (>20% monthly budget in 1 hour) and pauses it.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-research-03"
        store.create_or_update_agent(
            agent_id=agent_id, team_id="team-eng", name="Research Agent",
            monthly_limit_usd=10.00,
            preferred_model=PRIMARY_MODEL, fallback_model=FALLBACK_MODEL
        )
        # Simulate $2.50 spend in past 1 hour (25% of $10 budget = runaway)
        store.record_spend_atomic(
            agent_id=agent_id, session_id="sess-loop",
            cost_usd=2.50, prompt_tokens=1000, completion_tokens=1000,
            model_requested=PRIMARY_MODEL, model_used=PRIMARY_MODEL,
            is_substituted=False, disposition="ALLOWED"
        )

        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": PRIMARY_MODEL,
            "messages": [{"role": "user", "content": "Next step of runaway loop"}]
        })

        assert res.status_code == 429
        assert store.get_agent(agent_id)["status"] == "PAUSED"
        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "RUNAWAY_DETECTED" for a in alerts)
