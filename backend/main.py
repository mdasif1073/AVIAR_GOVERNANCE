import json
import asyncio
import time
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, Request, Response, HTTPException, status, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from loguru import logger

from backend.config import settings
from backend.dynamo import store
from backend.pricing import calculate_cost, MODEL_PRICING
from backend.groq_client import llm_client
from backend.engine import engine

app = FastAPI(
    title=settings.APP_NAME,
    description="Enterprise AI Spend Governance Gateway & Agent Budget Controller (PS-8.1)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus Metrics
REQUESTS_TOTAL = Counter("aivar_governance_requests_total", "Total requests evaluated by Budget Gateway", ["disposition", "model"])
BLOCKED_TOTAL = Counter("aivar_governance_blocks_total", "Total blocked requests by reason", ["reason"])
TOKENS_METERED = Counter("aivar_governance_tokens_total", "Total tokens processed", ["model", "type"])
SPEND_USD_TOTAL = Counter("aivar_governance_spend_usd_total", "Total spend in USD", ["agent_id"])
AGENT_SPEND_GAUGE = Gauge("aivar_agent_current_spend_usd", "Current spend in USD per agent", ["agent_id"])

# Active SSE subscribers
subscribers: List[asyncio.Queue] = []

async def broadcast_event(event_type: str, data: Dict[str, Any]):
    """Broadcasts real-time events to all connected dashboard SSE clients."""
    payload = json.dumps({"event": event_type, "data": data, "timestamp": time.time()})
    for queue in list(subscribers):
        try:
            await queue.put(payload)
        except Exception:
            subscribers.remove(queue)

# Pydantic Request Models
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "llama-3.3-70b-versatile"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 512
    agent_id: Optional[str] = Field(default="agent-support-01", description="Agent ID for spend governance")
    session_id: Optional[str] = Field(default=None, description="Optional Session ID for session-level limits")
    session_limit_usd: Optional[float] = Field(default=None, description="Optional per-session budget limit in USD")
    allow_model_substitution: Optional[bool] = Field(default=True, description="Enable automatic fallback to cheaper model")

class ConfigureAgentBudgetRequest(BaseModel):
    agent_id: str
    name: Optional[str] = None
    team_id: Optional[str] = "team-eng"
    monthly_limit_usd: float
    preferred_model: Optional[str] = "llama-3.3-70b-versatile"
    fallback_model: Optional[str] = "llama-3.1-8b-instant"

# --- HEALTH & OBSERVABILITY ENDPOINTS ---
@app.get("/health", tags=["Observability"])
async def health_check():
    """Enterprise health and readiness probe."""
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "state_store": "DynamoDB" if store.use_dynamodb else "In-Memory Thread-Safe",
        "llm_provider": "Groq Cloud (Live)" if llm_client.has_real_key else "Groq Cloud (Simulated)",
        "timestamp": time.time()
    }

@app.get("/metrics", tags=["Observability"])
async def metrics():
    """Prometheus metrics scrape endpoint."""
    # Update gauges with latest spend
    for agent in store.list_agents():
        AGENT_SPEND_GAUGE.labels(agent_id=agent["agent_id"]).set(agent["current_spend_usd"])
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

# --- GOVERNED CHAT COMPLETIONS (OPENAI COMPATIBLE) ---
@app.post("/v1/chat/completions", tags=["Governance Gateway"])
@app.post("/api/gateway/chat", tags=["Governance Gateway"])
async def governed_chat_completions(
    req: ChatCompletionRequest,
    x_agent_id: Optional[str] = Header(None),
    x_session_id: Optional[str] = Header(None)
):
    """
    OpenAI-Compatible Chat Completion endpoint with Real-Time Budget Enforcement:
    - Pre-execution threshold checks (80% warning / 100% hard block)
    - Session closure on exhaustion
    - Automatic model substitution under budget stress
    - Post-execution token metering & spend accounting
    """
    agent_id = x_agent_id or req.agent_id or "agent-support-01"
    session_id = x_session_id or req.session_id
    requested_model = req.model or settings.DEFAULT_PRIMARY_MODEL

    # 1. Pre-execution Governance Evaluation
    pre_eval = engine.evaluate_pre_execution(
        agent_id=agent_id,
        session_id=session_id,
        requested_model=requested_model,
        allow_substitution=req.allow_model_substitution,
        session_limit_usd=req.session_limit_usd
    )

    if not pre_eval["allowed"]:
        BLOCKED_TOTAL.labels(reason=pre_eval["reason"]).inc()
        REQUESTS_TOTAL.labels(disposition="BLOCKED", model=requested_model).inc()
        
        await broadcast_event("REQUEST_BLOCKED", {
            "agent_id": agent_id,
            "session_id": session_id,
            "reason": pre_eval["reason"],
            "message": pre_eval["message"]
        })

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": {
                    "message": pre_eval["message"],
                    "type": "governance_budget_exceeded",
                    "code": pre_eval["reason"]
                }
            }
        )

    model_to_use = pre_eval.get("model_to_use", requested_model)
    is_substituted = pre_eval.get("is_substituted", False)
    disposition = pre_eval.get("disposition", "ALLOWED")

    # 2. Dispatch to LLM Provider
    messages_payload = [{"role": m.role, "content": m.content} for m in req.messages]
    llm_resp = await llm_client.chat_completion(
        model=model_to_use,
        messages=messages_payload,
        temperature=req.temperature or 0.7,
        max_tokens=req.max_tokens or 512
    )

    # 3. Extract Token Usage & Commit Spend Transaction
    usage = llm_resp.get("usage", {"prompt_tokens": 50, "completion_tokens": 30})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    tx = engine.record_post_execution(
        agent_id=agent_id,
        session_id=session_id,
        requested_model=requested_model,
        model_used=model_to_use,
        is_substituted=is_substituted,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        disposition=disposition
    )

    # 4. Update Metrics
    REQUESTS_TOTAL.labels(disposition=disposition, model=model_to_use).inc()
    TOKENS_METERED.labels(model=model_to_use, type="prompt").inc(prompt_tokens)
    TOKENS_METERED.labels(model=model_to_use, type="completion").inc(completion_tokens)
    SPEND_USD_TOTAL.labels(agent_id=agent_id).inc(tx["cost_usd"])

    # 5. Broadcast to Real-Time Dashboard
    await broadcast_event("TRANSACTION_PROCESSED", {
        "transaction": tx,
        "agents": store.list_agents(),
        "team": store.get_team("team-eng")
    })

    # 6. Format Response with Governance Headers
    headers = {
        "X-Governance-Disposition": disposition,
        "X-Governance-Agent-Spend-USD": str(tx["agent_current_spend_usd"]),
        "X-Governance-Cost-USD": str(tx["cost_usd"]),
        "X-Governance-Model-Requested": requested_model,
        "X-Governance-Model-Used": model_to_use,
        "X-Governance-Substituted": str(is_substituted).lower()
    }

    return JSONResponse(content=llm_resp, headers=headers)

# --- BUDGET MANAGEMENT & DASHBOARD APIS ---
@app.get("/api/budgets/summary", tags=["Budget Administration"])
async def get_budgets_summary():
    """Returns complete hierarchical view of Team, Agents, and recent transactions."""
    return {
        "team": store.get_team("team-eng"),
        "agents": store.list_agents(),
        "recent_transactions": store.get_recent_transactions(limit=25),
        "recent_alerts": store.get_recent_alerts(limit=20),
        "pricing_catalog": MODEL_PRICING
    }

@app.post("/api/budgets/agent", tags=["Budget Administration"])
async def configure_agent_budget(req: ConfigureAgentBudgetRequest):
    """Dynamically creates or updates an agent's monthly budget limit."""
    updated = store.create_or_update_agent(
        agent_id=req.agent_id,
        team_id=req.team_id or "team-eng",
        name=req.name or f"Agent {req.agent_id}",
        monthly_limit_usd=req.monthly_limit_usd,
        preferred_model=req.preferred_model or "llama-3.3-70b-versatile",
        fallback_model=req.fallback_model or "llama-3.1-8b-instant"
    )
    await broadcast_event("BUDGET_CONFIG_UPDATED", {"agents": store.list_agents()})
    return {"status": "success", "agent": updated}

@app.post("/api/budgets/reset", tags=["Budget Administration"])
async def reset_budgets():
    """Resets all spend back to $0 for fresh live demonstrations."""
    store.reset_all_spend()
    await broadcast_event("STATE_RESET", {
        "team": store.get_team("team-eng"),
        "agents": store.list_agents()
    })
    return {"status": "success", "message": "All budgets and transactions reset to zero."}

@app.get("/api/alerts", tags=["Budget Administration"])
async def get_alerts():
    """Retrieves recent governance alerts."""
    return {"alerts": store.get_recent_alerts(limit=50)}

# --- REAL-TIME SSE STREAM FOR UI ---
@app.get("/api/events/stream", tags=["Observability"])
async def events_stream(request: Request):
    """Server-Sent Events (SSE) feed for live real-time dashboard updates."""
    queue = asyncio.Queue()
    subscribers.append(queue)

    async def event_generator():
        try:
            # Send initial state immediately
            initial_data = json.dumps({
                "event": "INITIAL_STATE",
                "data": {
                    "team": store.get_team("team-eng"),
                    "agents": store.list_agents(),
                    "recent_transactions": store.get_recent_transactions(limit=15),
                    "recent_alerts": store.get_recent_alerts(limit=10)
                }
            })
            yield f"data: {initial_data}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                payload = await queue.get()
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in subscribers:
                subscribers.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Mount Static Dashboard Files
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, tags=["Dashboard UI"])
async def serve_dashboard():
    """Serves the main real-time governance dashboard."""
    with open("frontend/index.html", "r") as f:
        return HTMLResponse(content=f.read())
