import time
from typing import Dict, Any, Tuple, Optional
from loguru import logger
from backend.config import settings
from backend.pricing import calculate_cost, get_cheaper_model
from backend.dynamo import store

class GovernanceEngine:
    """
    Core Governance Policy & Budget Enforcement Engine for PS-8.1.
    Evaluates multi-tier budgets, enforces warnings/hard-blocks,
    applies model substitution, and detects runaway loops.
    """

    def evaluate_pre_execution(
        self,
        agent_id: str,
        session_id: Optional[str],
        requested_model: str,
        allow_substitution: bool = True
    ) -> Dict[str, Any]:
        """
        Pre-flight check before calling LLM.
        Determines:
        1. Whether request is allowed or blocked (100% hard block or session limit).
        2. Whether warning should fire (80% threshold).
        3. Whether model substitution should be applied.
        4. Whether runaway loop condition is triggered.
        """
        agent = store.get_agent(agent_id)
        if not agent:
            # Auto-register if new agent
            agent = store.create_or_update_agent(
                agent_id=agent_id,
                team_id="team-eng",
                name=f"Agent {agent_id}",
                monthly_limit_usd=50.0,
                preferred_model=requested_model,
                fallback_model=get_cheaper_model(requested_model)
            )

        team_id = agent.get("team_id", "team-eng")
        team = store.get_team(team_id)

        # 1. Check Agent Status (e.g. if paused by runaway detector)
        if agent.get("status") == "PAUSED":
            msg = f"Agent '{agent['name']}' is PAUSED due to runaway velocity alert. Human review required."
            return {
                "allowed": False,
                "reason": "AGENT_PAUSED",
                "message": msg,
                "disposition": "BLOCKED"
            }

        # 2. Check Session Limit
        if session_id:
            session = store.get_or_create_session(session_id, agent_id)
            if session.get("status") == "CLOSED":
                return {
                    "allowed": False,
                    "reason": "SESSION_CLOSED",
                    "message": f"Session '{session_id}' has reached its budget limit (${session['limit_usd']:.2f}) and is closed.",
                    "disposition": "BLOCKED"
                }
            if session.get("current_spend_usd", 0.0) >= session.get("limit_usd", 2.0):
                store.close_session(session_id)
                store.record_alert(
                    agent_id=agent_id,
                    alert_type="SESSION_CLOSED",
                    message=f"Session {session_id} spend (${session['current_spend_usd']:.4f}) reached limit (${session['limit_usd']:.2f}). Session closed.",
                    metadata={"session_id": session_id, "spend": session["current_spend_usd"]}
                )
                return {
                    "allowed": False,
                    "reason": "SESSION_BUDGET_EXHAUSTED",
                    "message": f"Session budget of ${session['limit_usd']:.2f} exhausted.",
                    "disposition": "BLOCKED"
                }

        # 3. Check Agent & Team Spend Percentages
        agent_limit = max(1e-9, float(agent.get("monthly_limit_usd", 50.0)))
        agent_spend = float(agent.get("current_spend_usd", 0.0))
        agent_pct = (agent_spend / agent_limit) * 100.0

        team_limit = max(1e-9, float(team.get("monthly_limit_usd", 500.0))) if team else 500.0
        team_spend = float(team.get("current_spend_usd", 0.0)) if team else 0.0
        team_pct = (team_spend / team_limit) * 100.0

        # 4. Check Runaway Velocity (Bonus: >20% monthly budget consumed in 1 hour)
        # Minimum absolute threshold of $0.50 to avoid micro-spend false positives
        recent_1h_spend = store.get_agent_recent_spend(agent_id, window_seconds=settings.RUNAWAY_WINDOW_SECONDS)
        velocity_pct = (recent_1h_spend / agent_limit) * 100.0
        RUNAWAY_MIN_ABS_USD = 0.50  # Must spend at least $0.50/hr to trigger
        if velocity_pct >= settings.RUNAWAY_VELOCITY_PERCENT and recent_1h_spend >= RUNAWAY_MIN_ABS_USD:
            store.update_agent_status(agent_id, "PAUSED")
            store.record_alert(
                agent_id=agent_id,
                alert_type="RUNAWAY_DETECTED",
                message=f"CRITICAL: Agent '{agent['name']}' consumed ${recent_1h_spend:.4f} ({velocity_pct:.1f}% of budget) in 1 hour! Paused for human review.",
                metadata={"recent_spend": recent_1h_spend, "limit": agent_limit, "velocity_pct": velocity_pct}
            )
            return {
                "allowed": False,
                "reason": "RUNAWAY_LOOP_DETECTED",
                "message": f"Agent paused: abnormal velocity detected (${recent_1h_spend:.4f} = {velocity_pct:.1f}%/hr of budget).",
                "disposition": "BLOCKED"
            }

        # 5. Check 100% Hard Block
        if agent_pct >= settings.HARD_BLOCK_THRESHOLD_PERCENT or team_pct >= settings.HARD_BLOCK_THRESHOLD_PERCENT:
            # If 100% blocked, can we substitute to a free/cheaper model if enabled, or hard block?
            # Per PS-8.1: Hard block fires at 100% consumed.
            store.record_alert(
                agent_id=agent_id,
                alert_type="HARD_BLOCK_100",
                message=f"HARD BLOCK: Agent '{agent['name']}' spend (${agent_spend:.4f}) reached 100% of limit (${agent_limit:.2f}).",
                metadata={"agent_id": agent_id, "spend": agent_spend, "limit": agent_limit}
            )
            return {
                "allowed": False,
                "reason": "BUDGET_EXHAUSTED",
                "message": f"Agent monthly budget limit of ${agent_limit:.2f} has been fully exhausted.",
                "disposition": "BLOCKED",
                "agent_pct": agent_pct
            }

        # 6. Check 80% Warning & Model Substitution on Budget Pressure
        is_warning = agent_pct >= settings.WARN_THRESHOLD_PERCENT or team_pct >= settings.WARN_THRESHOLD_PERCENT
        model_to_use = requested_model
        is_substituted = False

        if is_warning:
            store.record_alert(
                agent_id=agent_id,
                alert_type="WARNING_80",
                message=f"WARNING: Agent '{agent['name']}' has consumed {agent_pct:.1f}% of monthly budget (${agent_spend:.4f}/${agent_limit:.2f}).",
                metadata={"agent_id": agent_id, "spend": agent_spend, "limit": agent_limit, "pct": agent_pct}
            )

            # Model Substitution under budget pressure
            if allow_substitution:
                cheaper = get_cheaper_model(requested_model)
                if cheaper.lower() != requested_model.lower():
                    model_to_use = cheaper
                    is_substituted = True
                    store.record_alert(
                        agent_id=agent_id,
                        alert_type="MODEL_SUBSTITUTED",
                        message=f"POLICY ACTION: Model substituted from [{requested_model}] to cost-optimized [{model_to_use}] under budget pressure ({agent_pct:.1f}% consumed).",
                        metadata={"original": requested_model, "substituted": model_to_use}
                    )

        disposition = "REROUTED" if is_substituted else ("WARNED" if is_warning else "ALLOWED")

        return {
            "allowed": True,
            "model_to_use": model_to_use,
            "is_substituted": is_substituted,
            "is_warning": is_warning,
            "agent_pct": agent_pct,
            "team_pct": team_pct,
            "disposition": disposition
        }

    def record_post_execution(
        self,
        agent_id: str,
        session_id: Optional[str],
        requested_model: str,
        model_used: str,
        is_substituted: bool,
        prompt_tokens: int,
        completion_tokens: int,
        disposition: str
    ) -> Dict[str, Any]:
        """
        Calculates exact dollar cost and commits atomic spend update to state store.
        """
        _, _, total_cost = calculate_cost(model_used, prompt_tokens, completion_tokens)
        
        tx = store.record_spend_atomic(
            agent_id=agent_id,
            session_id=session_id,
            cost_usd=total_cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_requested=requested_model,
            model_used=model_used,
            is_substituted=is_substituted,
            disposition=disposition
        )
        return tx

engine = GovernanceEngine()
