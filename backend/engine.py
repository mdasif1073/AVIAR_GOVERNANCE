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
        allow_substitution: bool = True,
        session_limit_usd: Optional[float] = None
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
            lim = session_limit_usd if session_limit_usd is not None else 2.0
            session = store.get_or_create_session(session_id, agent_id, limit_usd=lim)
            if session_limit_usd is not None and session.get("limit_usd") != session_limit_usd:
                session["limit_usd"] = float(session_limit_usd)

            sess_spend = float(session.get("current_spend_usd", 0.0))
            sess_limit = float(session.get("limit_usd", 2.0))

            if session.get("status") == "CLOSED" or sess_spend >= sess_limit:
                store.close_session(session_id)
                store.record_alert(
                    agent_id=agent_id,
                    alert_type="SESSION_CLOSED",
                    message=f"Session '{session_id}' spend (${sess_spend:.6f}) reached limit (${sess_limit:.6f}). Session closed.",
                    metadata={"session_id": session_id, "spend": sess_spend, "limit": sess_limit}
                )
                return {
                    "allowed": False,
                    "reason": "SESSION_BUDGET_EXHAUSTED",
                    "message": f"Session budget of ${sess_limit:.6f} exhausted.",
                    "disposition": "BLOCKED"
                }

        # 3. Check Agent & Team Spend Percentages
        agent_limit = max(1e-9, float(agent.get("monthly_limit_usd", 50.0)))
        agent_spend = float(agent.get("current_spend_usd", 0.0))
        agent_pct = (agent_spend / agent_limit) * 100.0

        team_limit = max(1e-9, float(team.get("monthly_limit_usd", 500.0))) if team else 500.0
        team_spend = float(team.get("current_spend_usd", 0.0)) if team else 0.0
        team_pct = (team_spend / team_limit) * 100.0

        # 4. Check 100% Hard Block
        if agent_pct >= settings.HARD_BLOCK_THRESHOLD_PERCENT or team_pct >= settings.HARD_BLOCK_THRESHOLD_PERCENT:
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

        # 5. Check 80% Warning & Model Substitution on Budget Pressure
        #    (Evaluated BEFORE runaway detector so substitution fires independently)
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

            # If substitution was applied, return immediately — skip runaway check
            # so that 80% warning scenario and runaway scenario remain independent
            disposition = "REROUTED" if is_substituted else "WARNED"
            return {
                "allowed": True,
                "model_to_use": model_to_use,
                "is_substituted": is_substituted,
                "is_warning": is_warning,
                "agent_pct": agent_pct,
                "team_pct": team_pct,
                "disposition": disposition
            }

        # 6. Check Runaway Velocity (Bonus: >20% monthly budget consumed in 1 hour)
        #    Only reached if agent is NOT in the 80-99% warning band
        recent_1h_spend = store.get_agent_recent_spend(agent_id, window_seconds=settings.RUNAWAY_WINDOW_SECONDS)
        velocity_pct = (recent_1h_spend / agent_limit) * 100.0
        runaway_min = max(agent_limit * 0.01, 1e-6)   # 1% of limit, but at least $0.000001
        if velocity_pct >= settings.RUNAWAY_VELOCITY_PERCENT and recent_1h_spend >= runaway_min:
            store.update_agent_status(agent_id, "PAUSED")
            store.record_alert(
                agent_id=agent_id,
                alert_type="RUNAWAY_DETECTED",
                message=f"CRITICAL: Agent '{agent['name']}' consumed ${recent_1h_spend:.6f} ({velocity_pct:.1f}% of budget) in 1 hour! Paused for human review.",
                metadata={"recent_spend": recent_1h_spend, "limit": agent_limit, "velocity_pct": velocity_pct}
            )
            return {
                "allowed": False,
                "reason": "RUNAWAY_LOOP_DETECTED",
                "message": f"Agent paused: abnormal velocity detected (${recent_1h_spend:.6f} = {velocity_pct:.1f}%/hr of budget).",
                "disposition": "BLOCKED"
            }

        # 7. Normal — allowed with no warnings
        return {
            "allowed": True,
            "model_to_use": requested_model,
            "is_substituted": False,
            "is_warning": False,
            "agent_pct": agent_pct,
            "team_pct": team_pct,
            "disposition": "ALLOWED"
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
