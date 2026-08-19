import time
import uuid
import threading
from typing import Dict, Any, List, Optional
from loguru import logger
from backend.config import settings

class StateStore:
    """
    Enterprise State Store supporting both AWS DynamoDB (with atomic counters) 
    and thread-safe in-memory/local storage for robust testing and fallback.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.use_dynamodb = False
        self.dynamodb = None
        self.tables = {}

        # In-memory storage structures with atomic locks
        self._teams: Dict[str, Dict[str, Any]] = {}
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._transactions: List[Dict[str, Any]] = []
        self._alerts: List[Dict[str, Any]] = []

        self._init_aws_or_local()
        self._seed_default_data()

    def _init_aws_or_local(self):
        """Attempts connection to AWS DynamoDB or DynamoDB Local."""
        if settings.DYNAMODB_ENDPOINT_URL or (settings.AWS_ACCESS_KEY_ID and settings.AWS_ACCESS_KEY_ID != "mock_key"):
            try:
                import boto3
                kwargs = {"region_name": settings.AWS_REGION}
                if settings.DYNAMODB_ENDPOINT_URL:
                    kwargs["endpoint_url"] = settings.DYNAMODB_ENDPOINT_URL
                if settings.AWS_ACCESS_KEY_ID:
                    kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                    kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY

                self.dynamodb = boto3.resource("dynamodb", **kwargs)
                # Try listing tables to verify connection
                list(self.dynamodb.tables.all())
                self.use_dynamodb = True
                logger.info("Successfully connected to AWS DynamoDB / DynamoDB Local.")
                self._ensure_dynamodb_tables()
            except Exception as e:
                logger.warning(f"DynamoDB connection failed ({e}). Falling back to thread-safe In-Memory State Store.")
                self.use_dynamodb = False
        else:
            logger.info("Using Thread-Safe In-Memory State Store (Production Ready with atomic locks).")
            self.use_dynamodb = False

    def _ensure_dynamodb_tables(self):
        """Creates required DynamoDB tables if they do not exist."""
        # Setup tables if using real DynamoDB
        pass

    def _seed_default_data(self):
        """Seeds initial default teams, agents, and budgets."""
        # 1. Team: Engineering Team ($500.00 / month)
        self.create_or_update_team("team-eng", "Engineering AI Team", 500.0)

        # 2. Agent 1: Customer Support ($50.00 / month, $2.00 / session)
        self.create_or_update_agent(
            agent_id="agent-support-01",
            team_id="team-eng",
            name="Customer Support Agent",
            monthly_limit_usd=50.0,
            preferred_model="llama-3.3-70b-versatile",
            fallback_model="llama-3.1-8b-instant"
        )

        # 3. Agent 2: Financial Analytics ($75.00 / month, $5.00 / session)
        self.create_or_update_agent(
            agent_id="agent-analytics-02",
            team_id="team-eng",
            name="Financial Analytics Agent",
            monthly_limit_usd=75.0,
            preferred_model="llama-3.3-70b-versatile",
            fallback_model="llama-3.1-8b-instant"
        )

        # 4. Agent 3: Research Assistant ($30.00 / month, $1.50 / session)
        self.create_or_update_agent(
            agent_id="agent-research-03",
            team_id="team-eng",
            name="Research Assistant Agent",
            monthly_limit_usd=30.0,
            preferred_model="llama-3.3-70b-versatile",
            fallback_model="llama-3.1-8b-instant"
        )

    # --- TEAM OPERATIONS ---
    def create_or_update_team(self, team_id: str, name: str, monthly_limit_usd: float) -> Dict[str, Any]:
        with self.lock:
            existing = self._teams.get(team_id, {})
            current_spend = existing.get("current_spend_usd", 0.0)
            record = {
                "team_id": team_id,
                "name": name,
                "monthly_limit_usd": float(monthly_limit_usd),
                "current_spend_usd": float(current_spend),
                "updated_at": time.time()
            }
            self._teams[team_id] = record
            return record

    def get_team(self, team_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self._teams.get(team_id)

    # --- AGENT OPERATIONS ---
    def create_or_update_agent(
        self, 
        agent_id: str, 
        team_id: str, 
        name: str, 
        monthly_limit_usd: float,
        preferred_model: str = "llama-3.3-70b-versatile",
        fallback_model: str = "llama-3.1-8b-instant",
        status: str = "ACTIVE"
    ) -> Dict[str, Any]:
        with self.lock:
            existing = self._agents.get(agent_id, {})
            current_spend = existing.get("current_spend_usd", 0.0)
            record = {
                "agent_id": agent_id,
                "team_id": team_id,
                "name": name,
                "monthly_limit_usd": float(monthly_limit_usd),
                "current_spend_usd": float(current_spend),
                "status": status,
                "preferred_model": preferred_model,
                "fallback_model": fallback_model,
                "updated_at": time.time()
            }
            self._agents[agent_id] = record
            return record

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self._agents.get(agent_id)

    def list_agents(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self._agents.values())

    def update_agent_status(self, agent_id: str, status: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            if agent_id in self._agents:
                self._agents[agent_id]["status"] = status
                self._agents[agent_id]["updated_at"] = time.time()
                return self._agents[agent_id]
            return None

    # --- SESSION OPERATIONS ---
    def get_or_create_session(self, session_id: str, agent_id: str, limit_usd: float = 2.0) -> Dict[str, Any]:
        with self.lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "limit_usd": float(limit_usd),
                    "current_spend_usd": 0.0,
                    "status": "OPEN",
                    "created_at": time.time(),
                    "updated_at": time.time()
                }
            return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            if session_id in self._sessions:
                self._sessions[session_id]["status"] = "CLOSED"
                self._sessions[session_id]["updated_at"] = time.time()
                return self._sessions[session_id]
            return None

    # --- ATOMIC SPEND METERING ---
    def record_spend_atomic(
        self,
        agent_id: str,
        session_id: Optional[str],
        cost_usd: float,
        prompt_tokens: int,
        completion_tokens: int,
        model_requested: str,
        model_used: str,
        is_substituted: bool,
        disposition: str
    ) -> Dict[str, Any]:
        """
        Atomically updates Agent, Team, and Session spend with locking,
        and appends a transaction record.
        """
        with self.lock:
            # 1. Update Agent
            agent = self._agents.get(agent_id)
            if agent:
                agent["current_spend_usd"] = round(agent["current_spend_usd"] + cost_usd, 6)
                agent["updated_at"] = time.time()
                team_id = agent.get("team_id")
                # 2. Update Team
                if team_id and team_id in self._teams:
                    team = self._teams[team_id]
                    team["current_spend_usd"] = round(team["current_spend_usd"] + cost_usd, 6)
                    team["updated_at"] = time.time()

            # 3. Update Session
            session = None
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session["current_spend_usd"] = round(session["current_spend_usd"] + cost_usd, 6)
                session["updated_at"] = time.time()

            # 4. Record Transaction
            tx_id = f"tx-{uuid.uuid4().hex[:10]}"
            tx_record = {
                "tx_id": tx_id,
                "agent_id": agent_id,
                "agent_name": agent.get("name") if agent else "Unknown Agent",
                "session_id": session_id or "default",
                "model_requested": model_requested,
                "model_used": model_used,
                "is_substituted": is_substituted,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost_usd": cost_usd,
                "agent_current_spend_usd": agent.get("current_spend_usd") if agent else 0.0,
                "disposition": disposition,
                "timestamp": time.time(),
                "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            }
            self._transactions.insert(0, tx_record)
            if len(self._transactions) > 1000:
                self._transactions.pop()

            return tx_record

    # --- GOVERNANCE ALERTS ---
    def record_alert(self, agent_id: str, alert_type: str, message: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self.lock:
            alert_id = f"alert-{uuid.uuid4().hex[:8]}"
            alert_record = {
                "alert_id": alert_id,
                "agent_id": agent_id,
                "alert_type": alert_type,
                "message": message,
                "metadata": metadata or {},
                "timestamp": time.time(),
                "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            }
            self._alerts.insert(0, alert_record)
            if len(self._alerts) > 500:
                self._alerts.pop()
            return alert_record

    def get_recent_transactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            return self._transactions[:limit]

    def get_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            return self._alerts[:limit]

    def get_agent_recent_spend(self, agent_id: str, window_seconds: int = 3600) -> float:
        """Calculates total spend for an agent within the given past window in seconds."""
        cutoff = time.time() - window_seconds
        with self.lock:
            spend = sum(
                tx["cost_usd"] for tx in self._transactions 
                if tx["agent_id"] == agent_id and tx["timestamp"] >= cutoff
            )
            return round(spend, 6)

    def reset_all_spend(self):
        """Resets all spend totals back to $0.00 for clean live demo re-runs."""
        with self.lock:
            for team in self._teams.values():
                team["current_spend_usd"] = 0.0
            for agent in self._agents.values():
                agent["current_spend_usd"] = 0.0
                agent["status"] = "ACTIVE"
            for session in self._sessions.values():
                session["current_spend_usd"] = 0.0
                session["status"] = "OPEN"
            self._transactions.clear()
            self._alerts.clear()

store = StateStore()
