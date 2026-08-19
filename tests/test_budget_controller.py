import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from backend.main import app
from backend.dynamo import store
from backend.config import settings

@pytest.fixture(autouse=True)
def reset_state():
    """Resets the state store before each test for clean isolation."""
    store.reset_all_spend()

@pytest.mark.asyncio
async def test_health_and_metrics_endpoints():
    """Verify production health check and Prometheus metrics endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Health check
        res_health = await ac.get("/health")
        assert res_health.status_code == 200
        data = res_health.json()
        assert data["status"] == "healthy"
        assert "app_name" in data

        # 2. Prometheus metrics
        res_metrics = await ac.get("/metrics")
        assert res_metrics.status_code == 200
        assert b"aivar_governance_requests_total" in res_metrics.content

@pytest.mark.asyncio
async def test_concurrent_budget_tracking_across_three_agents():
    """
    Success Criteria 1: Budget correctly tracked across three agents making concurrent calls.
    Fires simultaneous requests across Agent 1, 2, and 3, ensuring atomic state persistence.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agents = ["agent-support-01", "agent-analytics-02", "agent-research-03"]

        async def send_call(agent_id: str):
            return await ac.post("/v1/chat/completions", json={
                "agent_id": agent_id,
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": "Concurrent test prompt"}]
            })

        # Fire 3 agents in parallel concurrently
        responses = await asyncio.gather(*[send_call(a) for a in agents])

        # Verify all 3 succeeded
        for res in responses:
            assert res.status_code == 200
            assert "choices" in res.json()
            assert res.headers.get("X-Governance-Disposition") == "ALLOWED"

        # Verify state in state store
        for agent_id in agents:
            agent = store.get_agent(agent_id)
            assert agent is not None
            assert agent["current_spend_usd"] > 0.0

        # Verify team spend aggregates all 3
        team = store.get_team("team-eng")
        expected_team_spend = sum(store.get_agent(a)["current_spend_usd"] for a in agents)
        assert round(team["current_spend_usd"], 4) == round(expected_team_spend, 4)

@pytest.mark.asyncio
async def test_warning_fires_at_80_percent_consumed():
    """
    Success Criteria 2: Warning fires at 80% consumed.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-support-01"
        
        # Configure a small monthly budget: $0.0001
        store.create_or_update_agent(
            agent_id=agent_id,
            team_id="team-eng",
            name="Customer Support Agent",
            monthly_limit_usd=0.0001
        )
        
        # Manually seed current spend to 85% ($0.000085)
        store._agents[agent_id]["current_spend_usd"] = 0.000085

        # Dispatch request with model substitution disabled to explicitly test warning disposition
        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Test warning threshold"}],
            "allow_model_substitution": False
        })

        assert res.status_code == 200
        assert res.headers.get("X-Governance-Disposition") == "WARNED"

        # Verify alert record in store
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
        
        # Set monthly budget: $10.00, and spend: $10.00 (100% exhausted)
        store.create_or_update_agent(
            agent_id=agent_id,
            team_id="team-eng",
            name="Analytics Agent",
            monthly_limit_usd=10.0
        )
        store._agents[agent_id]["current_spend_usd"] = 10.00

        # Dispatch request -> Expected HTTP 429 Hard Block
        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Should be blocked"}]
        })

        assert res.status_code == 429
        data = res.json()
        assert data["detail"]["error"]["code"] == "BUDGET_EXHAUSTED"

        # Verify alert record
        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "HARD_BLOCK_100" for a in alerts)

@pytest.mark.asyncio
async def test_session_budget_correctly_closes_session():
    """
    Success Criteria 4: Session budget correctly closes a session that exceeds the per-session limit.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-research-03"
        session_id = "sess-test-999"

        # Create session with $0.50 limit and seed with $0.50 spend
        session = store.get_or_create_session(session_id=session_id, agent_id=agent_id, limit_usd=0.50)
        session["current_spend_usd"] = 0.50

        # Subsequent call on this session should be rejected and session marked CLOSED
        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "session_id": session_id,
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Session budget test"}]
        })

        assert res.status_code == 429
        session_obj = store.get_session(session_id)
        assert session_obj["status"] == "CLOSED"

@pytest.mark.asyncio
async def test_model_substitution_reroutes_to_cheaper_model():
    """
    Success Criteria 5: Model substitution correctly reroutes to the cheaper model under budget pressure.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-support-01"

        # Set limit and seed spend at 85%
        store.create_or_update_agent(
            agent_id=agent_id,
            team_id="team-eng",
            name="Support Agent",
            monthly_limit_usd=1.00,
            preferred_model="llama-3.3-70b-versatile",
            fallback_model="llama-3.1-8b-instant"
        )
        store._agents[agent_id]["current_spend_usd"] = 0.85

        # Request heavy model (llama-3.3-70b-versatile) with substitution enabled (default)
        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Test automatic model fallback under spend stress"}],
            "allow_model_substitution": True
        })

        assert res.status_code == 200
        # Check governance response headers
        assert res.headers.get("X-Governance-Disposition") == "REROUTED"
        assert res.headers.get("X-Governance-Substituted") == "true"
        assert res.headers.get("X-Governance-Model-Requested") == "llama-3.3-70b-versatile"
        assert res.headers.get("X-Governance-Model-Used") == "llama-3.1-8b-instant"

        # Verify alert
        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "MODEL_SUBSTITUTED" for a in alerts)

@pytest.mark.asyncio
async def test_runaway_loop_detector_bonus():
    """
    Bonus Criterion: Detects runaway looping agent (>20% monthly budget in 1 hour) and pauses agent.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        agent_id = "agent-research-03"

        # Monthly limit = $10.00
        store.create_or_update_agent(
            agent_id=agent_id,
            team_id="team-eng",
            name="Research Agent",
            monthly_limit_usd=10.00
        )

        # Simulate transactions within past 1 hour totalling $2.50 (25% of monthly budget in 1 hr)
        store.record_spend_atomic(
            agent_id=agent_id,
            session_id="sess-loop",
            cost_usd=2.50,
            prompt_tokens=1000,
            completion_tokens=1000,
            model_requested="llama-3.3-70b-versatile",
            model_used="llama-3.3-70b-versatile",
            is_substituted=False,
            disposition="ALLOWED"
        )

        # Next request must be detected as a runaway loop and paused for review
        res = await ac.post("/v1/chat/completions", json={
            "agent_id": agent_id,
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Next step of runaway loop"}]
        })

        assert res.status_code == 429
        agent = store.get_agent(agent_id)
        assert agent["status"] == "PAUSED"

        alerts = store.get_recent_alerts()
        assert any(a["alert_type"] == "RUNAWAY_DETECTED" for a in alerts)
