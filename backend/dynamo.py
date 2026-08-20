import time
import uuid
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
from loguru import logger
from typing import Dict, Any, List, Optional
from backend.config import settings


def _sanitize_for_dynamodb(obj):
    import decimal
    if isinstance(obj, float):
        return decimal.Decimal(str(round(obj, 8)))
    elif isinstance(obj, dict):
        return {k: _sanitize_for_dynamodb(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_dynamodb(x) for x in obj]
    return obj


class DynamoStateStore:
    """
    Production-grade AWS DynamoDB State Store for AIVAR Budget Controller.
    Uses conditional atomic expression updates to prevent race conditions
    under high concurrency. Falls back to thread-safe in-memory store
    only when AWS credentials are not configured.
    """

    TABLE_AGENTS       = "aivar_agent_budgets"
    TABLE_SESSIONS     = "aivar_sessions"
    TABLE_TRANSACTIONS = "aivar_transactions"
    TABLE_ALERTS       = "aivar_alerts"

    def __init__(self):
        self.use_dynamodb = False
        self.dynamodb      = None
        self._tables: Dict[str, Any] = {}

        # In-memory fallback (used only when no real AWS creds provided)
        import threading
        self._lock     = threading.Lock()
        self._teams: Dict[str, Dict] = {}
        self._agents: Dict[str, Dict] = {}
        self._sessions: Dict[str, Dict] = {}
        self._transactions: List[Dict] = []
        self._alerts: List[Dict] = []

        self._connect()
        self._seed_default_data()

    # ─────────────────────────────────────────────────────────
    # CONNECTION & TABLE SETUP
    # ─────────────────────────────────────────────────────────
    def _connect(self):
        has_real_key = (
            settings.AWS_ACCESS_KEY_ID
            and settings.AWS_ACCESS_KEY_ID not in ("mock_key", "", None)
        )
        if not has_real_key and not settings.DYNAMODB_ENDPOINT_URL:
            logger.info("No AWS credentials found — using thread-safe In-Memory State Store.")
            return

        try:
            kwargs = {"region_name": settings.AWS_REGION}
            if settings.DYNAMODB_ENDPOINT_URL:
                kwargs["endpoint_url"] = settings.DYNAMODB_ENDPOINT_URL
            if settings.AWS_ACCESS_KEY_ID:
                kwargs["aws_access_key_id"]     = settings.AWS_ACCESS_KEY_ID
                kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY

            self.dynamodb = boto3.resource("dynamodb", **kwargs)
            # Verify connection with a lightweight call
            list(self.dynamodb.tables.all())
            self.use_dynamodb = True
            logger.info(f"✅ Connected to AWS DynamoDB (region: {settings.AWS_REGION})")
            self._ensure_tables()
        except Exception as e:
            logger.warning(f"DynamoDB connection failed: {e}. Falling back to In-Memory Store.")
            self.use_dynamodb = False

    def _ensure_tables(self):
        """Creates all required DynamoDB tables if they don't already exist."""
        schemas = [
            {
                "TableName": self.TABLE_AGENTS,
                "KeySchema": [{"AttributeName": "agent_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "agent_id", "AttributeType": "S"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
            {
                "TableName": self.TABLE_SESSIONS,
                "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "session_id", "AttributeType": "S"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
            {
                "TableName": self.TABLE_TRANSACTIONS,
                "KeySchema": [
                    {"AttributeName": "agent_id",  "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "agent_id",  "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
            {
                "TableName": self.TABLE_ALERTS,
                "KeySchema": [
                    {"AttributeName": "agent_id",  "KeyType": "HASH"},
                    {"AttributeName": "alert_id",  "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "agent_id", "AttributeType": "S"},
                    {"AttributeName": "alert_id", "AttributeType": "S"},
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
        ]
        for schema in schemas:
            table_name = schema["TableName"]
            try:
                tbl = self.dynamodb.create_table(**schema)
                tbl.wait_until_exists()
                logger.info(f"Created DynamoDB table: {table_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceInUseException":
                    logger.debug(f"Table already exists: {table_name}")
                else:
                    logger.error(f"Error creating table {table_name}: {e}")
            self._tables[table_name] = self.dynamodb.Table(table_name)

    # ─────────────────────────────────────────────────────────
    # SEED DEFAULT DATA
    # ─────────────────────────────────────────────────────────
    def _seed_default_data(self):
        self.create_or_update_team("team-eng", "Engineering AI Team", 500.0)
        self.create_or_update_agent("agent-support-01",   "team-eng", "Customer Support Agent",    50.0,  "openai/gpt-oss-120b", "openai/gpt-oss-20b")
        self.create_or_update_agent("agent-analytics-02", "team-eng", "Financial Analytics Agent", 75.0,  "openai/gpt-oss-120b", "openai/gpt-oss-20b")
        self.create_or_update_agent("agent-research-03",  "team-eng", "Research Assistant Agent",  30.0,  "openai/gpt-oss-120b", "openai/gpt-oss-20b")

    # ─────────────────────────────────────────────────────────
    # TEAM OPERATIONS  (in-memory aggregation layer)
    # Teams are virtual — spend is aggregated from agent records.
    # ─────────────────────────────────────────────────────────
    def create_or_update_team(self, team_id: str, name: str, monthly_limit_usd: float) -> Dict:
        with self._lock:
            existing = self._teams.get(team_id, {})
            self._teams[team_id] = {
                "team_id": team_id, "name": name,
                "monthly_limit_usd": float(monthly_limit_usd),
                "current_spend_usd": existing.get("current_spend_usd", 0.0),
                "updated_at": time.time(),
            }
        return self._teams[team_id]

    def get_team(self, team_id: str) -> Optional[Dict]:
        """Returns team record with spend re-aggregated from live agent data."""
        with self._lock:
            team = self._teams.get(team_id)
            if team:
                # Recompute team spend from agents for accuracy
                agents_in_team = [a for a in self._agents.values() if a.get("team_id") == team_id]
                team["current_spend_usd"] = round(sum(a.get("current_spend_usd", 0.0) for a in agents_in_team), 8)
            return team

    # ─────────────────────────────────────────────────────────
    # AGENT OPERATIONS
    # ─────────────────────────────────────────────────────────
    def create_or_update_agent(
        self, agent_id: str, team_id: str, name: str, monthly_limit_usd: float,
        preferred_model: str = "llama-3.3-70b-versatile",
        fallback_model: str = "llama-3.1-8b-instant",
        status: str = "ACTIVE"
    ) -> Dict:
        if self.use_dynamodb:
            try:
                self._tables[self.TABLE_AGENTS].put_item(
                    Item={
                        "agent_id": agent_id, "team_id": team_id, "name": name,
                        "monthly_limit_usd": str(monthly_limit_usd),
                        "current_spend_usd": str(0),
                        "status": status,
                        "preferred_model": preferred_model,
                        "fallback_model": fallback_model,
                        "updated_at": str(time.time()),
                    },
                    ConditionExpression="attribute_not_exists(agent_id)"
                )
                logger.info(f"DynamoDB: created agent {agent_id}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # Agent already exists — update config only, preserve live spend
                    self._tables[self.TABLE_AGENTS].update_item(
                        Key={"agent_id": agent_id},
                        UpdateExpression="SET monthly_limit_usd = :l, preferred_model = :pm, fallback_model = :fm, updated_at = :u",
                        ExpressionAttributeValues={
                            ":l": str(monthly_limit_usd), ":pm": preferred_model,
                            ":fm": fallback_model, ":u": str(time.time())
                        }
                    )
                else:
                    logger.error(f"DynamoDB create_or_update_agent error: {e}")
                    raise

        with self._lock:
            existing = self._agents.get(agent_id, {})
            self._agents[agent_id] = {
                "agent_id": agent_id, "team_id": team_id, "name": name,
                "monthly_limit_usd": float(monthly_limit_usd),
                "current_spend_usd": existing.get("current_spend_usd", 0.0),
                "status": status,
                "preferred_model": preferred_model,
                "fallback_model": fallback_model,
                "updated_at": time.time(),
            }
        return self._agents[agent_id]

    def get_agent(self, agent_id: str) -> Optional[Dict]:
        if self.use_dynamodb:
            try:
                resp = self._tables[self.TABLE_AGENTS].get_item(Key={"agent_id": agent_id})
                item = resp.get("Item")
                if item:
                    parsed = {**item, "monthly_limit_usd": float(item.get("monthly_limit_usd", 50)), "current_spend_usd": float(item.get("current_spend_usd", 0))}
                    with self._lock:
                        self._agents[agent_id] = parsed
                    return parsed
            except Exception as e:
                logger.warning(f"DynamoDB get_agent failed: {e}")
        with self._lock:
            return self._agents.get(agent_id)

    def list_agents(self) -> List[Dict]:
        if self.use_dynamodb:
            try:
                resp = self._tables[self.TABLE_AGENTS].scan(
                    FilterExpression=Attr("status").exists() & Attr("team_id").exists()
                )
                agents = []
                for item in resp.get("Items", []):
                    parsed = {**item, "monthly_limit_usd": float(item.get("monthly_limit_usd", 50)), "current_spend_usd": float(item.get("current_spend_usd", 0))}
                    agents.append(parsed)
                    with self._lock:
                        self._agents[item["agent_id"]] = parsed
                if agents:
                    return agents
            except Exception as e:
                logger.warning(f"DynamoDB list_agents failed: {e}")
        with self._lock:
            return list(self._agents.values())

    def update_agent_status(self, agent_id: str, status: str) -> Optional[Dict]:
        if self.use_dynamodb:
            try:
                self._tables[self.TABLE_AGENTS].update_item(
                    Key={"agent_id": agent_id},
                    UpdateExpression="SET #s = :s, updated_at = :u",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": status, ":u": str(time.time())}
                )
            except Exception as e:
                logger.warning(f"DynamoDB update_agent_status failed: {e}")
        with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id]["status"] = status
                self._agents[agent_id]["updated_at"] = time.time()
                return self._agents[agent_id]
        return None

    # ─────────────────────────────────────────────────────────
    # SESSION OPERATIONS
    # ─────────────────────────────────────────────────────────
    def get_or_create_session(self, session_id: str, agent_id: str, limit_usd: float = 2.0) -> Dict:
        if self.use_dynamodb:
            try:
                self._tables[self.TABLE_SESSIONS].put_item(
                    Item={
                        "session_id": session_id, "agent_id": agent_id,
                        "limit_usd": str(limit_usd), "current_spend_usd": "0",
                        "status": "OPEN", "created_at": str(time.time()), "updated_at": str(time.time()),
                    },
                    ConditionExpression="attribute_not_exists(session_id)"
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise

        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {
                    "session_id": session_id, "agent_id": agent_id,
                    "limit_usd": float(limit_usd), "current_spend_usd": 0.0,
                    "status": "OPEN", "created_at": time.time(), "updated_at": time.time(),
                }
            else:
                if limit_usd != 2.0:
                    self._sessions[session_id]["limit_usd"] = float(limit_usd)
            return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[Dict]:
        if self.use_dynamodb:
            try:
                resp = self._tables[self.TABLE_SESSIONS].get_item(Key={"session_id": session_id})
                item = resp.get("Item")
                if item:
                    parsed = {**item, "limit_usd": float(item.get("limit_usd", 2)), "current_spend_usd": float(item.get("current_spend_usd", 0))}
                    with self._lock:
                        self._sessions[session_id] = parsed
                    return parsed
            except Exception as e:
                logger.warning(f"DynamoDB get_session failed: {e}")
        with self._lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> Optional[Dict]:
        if self.use_dynamodb:
            try:
                self._tables[self.TABLE_SESSIONS].update_item(
                    Key={"session_id": session_id},
                    UpdateExpression="SET #st = :st, updated_at = :u",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={":st": "CLOSED", ":u": str(time.time())}
                )
            except Exception as e:
                logger.warning(f"DynamoDB close_session failed: {e}")
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["status"] = "CLOSED"
                self._sessions[session_id]["updated_at"] = time.time()
                return self._sessions[session_id]
        return None

    # ─────────────────────────────────────────────────────────
    # ATOMIC SPEND METERING (core operation)
    # ─────────────────────────────────────────────────────────
    def record_spend_atomic(
        self, agent_id: str, session_id: Optional[str], cost_usd: float,
        prompt_tokens: int, completion_tokens: int,
        model_requested: str, model_used: str, is_substituted: bool, disposition: str
    ) -> Dict:
        import decimal
        cost_dec = decimal.Decimal(str(round(cost_usd, 8)))

        # 1. Atomic ADD on DynamoDB (prevents race conditions at cloud scale)
        if self.use_dynamodb:
            try:
                self._tables[self.TABLE_AGENTS].update_item(
                    Key={"agent_id": agent_id},
                    UpdateExpression="ADD current_spend_usd :c SET updated_at = :u",
                    ExpressionAttributeValues={":c": cost_dec, ":u": str(time.time())}
                )
                if session_id:
                    self._tables[self.TABLE_SESSIONS].update_item(
                        Key={"session_id": session_id},
                        UpdateExpression="ADD current_spend_usd :c SET updated_at = :u",
                        ExpressionAttributeValues={":c": cost_dec, ":u": str(time.time())}
                    )
            except Exception as e:
                logger.warning(f"DynamoDB atomic spend update failed: {e}")

        # 2. Update in-memory mirror (for low-latency reads / SSE stream)
        with self._lock:
            agent = self._agents.get(agent_id, {})
            if agent:
                agent["current_spend_usd"] = round(agent.get("current_spend_usd", 0.0) + cost_usd, 8)
                agent["updated_at"] = time.time()
                team_id = agent.get("team_id", "team-eng")
                if team_id in self._teams:
                    self._teams[team_id]["current_spend_usd"] = round(self._teams[team_id].get("current_spend_usd", 0.0) + cost_usd, 8)
                    self._teams[team_id]["updated_at"] = time.time()
            if session_id and session_id in self._sessions:
                self._sessions[session_id]["current_spend_usd"] = round(self._sessions[session_id].get("current_spend_usd", 0.0) + cost_usd, 8)
                self._sessions[session_id]["updated_at"] = time.time()

        # 3. Write transaction record
        tx_id      = f"tx-{uuid.uuid4().hex[:10]}"
        created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        tx_record  = {
            "tx_id": tx_id, "agent_id": agent_id,
            "agent_name": agent.get("name", "Unknown Agent"),
            "session_id": session_id or "default",
            "model_requested": model_requested, "model_used": model_used,
            "is_substituted": is_substituted,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": cost_usd,
            "agent_current_spend_usd": agent.get("current_spend_usd", 0.0),
            "disposition": disposition,
            "timestamp": time.time(), "created_at_iso": created_at,
        }

        if self.use_dynamodb:
            try:
                self._tables[self.TABLE_TRANSACTIONS].put_item(Item={
                    **tx_record,
                    "cost_usd": cost_dec,
                    "agent_current_spend_usd": decimal.Decimal(str(round(agent.get("current_spend_usd", 0.0), 8))),
                    "timestamp": str(time.time()),
                    "created_at": created_at,
                    "is_substituted": is_substituted,
                })
            except Exception as e:
                logger.warning(f"DynamoDB put transaction failed: {e}")

        with self._lock:
            self._transactions.insert(0, tx_record)
            if len(self._transactions) > 500:
                self._transactions.pop()

        return tx_record

    # ─────────────────────────────────────────────────────────
    # GOVERNANCE ALERTS
    # ─────────────────────────────────────────────────────────
    def record_alert(self, agent_id: str, alert_type: str, message: str, metadata: Optional[Dict] = None) -> Dict:
        alert_id   = f"alert-{uuid.uuid4().hex[:8]}"
        created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        record = {
            "alert_id": alert_id, "agent_id": agent_id,
            "alert_type": alert_type, "message": message,
            "metadata": metadata or {}, "timestamp": time.time(),
            "created_at_iso": created_at,
        }
        if self.use_dynamodb:
            try:
                db_item = _sanitize_for_dynamodb({
                    **record, "timestamp": str(time.time())
                })
                self._tables[self.TABLE_ALERTS].put_item(Item=db_item)
                logger.info(f"DynamoDB saved alert {alert_id} for agent {agent_id}")
            except Exception as e:
                logger.warning(f"DynamoDB put alert failed: {e}")
        with self._lock:
            self._alerts.insert(0, record)
            if len(self._alerts) > 500:
                self._alerts.pop()
        return record

    # ─────────────────────────────────────────────────────────
    # READ HELPERS
    # ─────────────────────────────────────────────────────────
    def get_recent_transactions(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return self._transactions[:limit]

    def get_recent_alerts(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return self._alerts[:limit]

    def get_agent_recent_spend(self, agent_id: str, window_seconds: int = 3600) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            return round(sum(tx["cost_usd"] for tx in self._transactions if tx["agent_id"] == agent_id and tx["timestamp"] >= cutoff), 8)

    def reset_all_spend(self):
        """Resets all spend to $0. Recreates tables on DynamoDB for a clean demo."""
        if self.use_dynamodb:
            try:
                for agent in self.list_agents():
                    self._tables[self.TABLE_AGENTS].update_item(
                        Key={"agent_id": agent["agent_id"]},
                        UpdateExpression="SET current_spend_usd = :z, #s = :a, updated_at = :u",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":z": 0, ":a": "ACTIVE", ":u": str(time.time())}
                    )
            except Exception as e:
                logger.warning(f"DynamoDB reset failed: {e}")
        with self._lock:
            for a in self._agents.values():
                a["current_spend_usd"] = 0.0
                a["status"] = "ACTIVE"
            for t in self._teams.values():
                t["current_spend_usd"] = 0.0
            for s in self._sessions.values():
                s["current_spend_usd"] = 0.0
                s["status"] = "OPEN"
            self._transactions.clear()
            self._alerts.clear()


store = DynamoStateStore()
