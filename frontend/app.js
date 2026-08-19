// State Tracking
let state = {
    team: { monthly_limit_usd: 500.0, current_spend_usd: 0.0 },
    agents: [],
    transactions: [],
    alerts: [],
    totalTokens: 0,
    substitutionCount: 0
};

// Initialize SSE Connection & UI Listeners
document.addEventListener("DOMContentLoaded", () => {
    initEventSource();
    bindSimulationButtons();
});

function initEventSource() {
    const statusEl = document.getElementById("connection-status");
    const eventSource = new EventSource("/api/events/stream");

    eventSource.onopen = () => {
        statusEl.innerText = "GATEWAY LIVE";
        statusEl.parentElement.classList.remove("disconnected");
    };

    eventSource.onmessage = (event) => {
        try {
            const payload = JSON.parse(event.data);
            handleIncomingEvent(payload);
        } catch (err) {
            console.error("Error parsing SSE event:", err);
        }
    };

    eventSource.onerror = () => {
        statusEl.innerText = "RECONNECTING...";
        statusEl.parentElement.classList.add("disconnected");
    };
}

function handleIncomingEvent(payload) {
    const { event, data } = payload;

    if (event === "INITIAL_STATE") {
        state.team = data.team || state.team;
        state.agents = data.agents || [];
        state.transactions = data.recent_transactions || [];
        state.alerts = data.recent_alerts || [];
        recalculateTotals();
        renderAll();
    } else if (event === "TRANSACTION_PROCESSED") {
        if (data.transaction) {
            state.transactions.unshift(data.transaction);
            if (state.transactions.length > 50) state.transactions.pop();
            
            state.totalTokens += data.transaction.total_tokens;
            if (data.transaction.is_substituted) {
                state.substitutionCount += 1;
            }
        }
        if (data.agents) state.agents = data.agents;
        if (data.team) state.team = data.team;
        
        fetchAlerts(); // Sync alerts
        renderAll();
    } else if (event === "REQUEST_BLOCKED" || event === "BUDGET_CONFIG_UPDATED") {
        fetchSummary();
    } else if (event === "STATE_RESET") {
        state.team = data.team;
        state.agents = data.agents;
        state.transactions = [];
        state.alerts = [];
        state.totalTokens = 0;
        state.substitutionCount = 0;
        renderAll();
    }
}

async function fetchSummary() {
    try {
        const res = await fetch("/api/budgets/summary");
        if (res.ok) {
            const data = await res.json();
            state.team = data.team;
            state.agents = data.agents;
            state.transactions = data.recent_transactions;
            state.alerts = data.recent_alerts;
            recalculateTotals();
            renderAll();
        }
    } catch (e) {
        console.error("Error fetching summary:", e);
    }
}

async function fetchAlerts() {
    try {
        const res = await fetch("/api/alerts");
        if (res.ok) {
            const data = await res.json();
            state.alerts = data.alerts;
            renderAlerts();
        }
    } catch (e) {
        console.error("Error fetching alerts:", e);
    }
}

function recalculateTotals() {
    state.totalTokens = state.transactions.reduce((acc, tx) => acc + (tx.total_tokens || 0), 0);
    state.substitutionCount = state.transactions.filter(tx => tx.is_substituted).length;
}

// Render UI Components
function renderAll() {
    renderKPIs();
    renderAgentCards();
    renderAlerts();
    renderLedger();
}

function renderKPIs() {
    const teamSpend = state.team?.current_spend_usd || 0.0;
    const teamLimit = state.team?.monthly_limit_usd || 500.0;
    const teamPct = Math.min(100, (teamSpend / teamLimit) * 100);

    document.getElementById("kpi-team-spend").innerText = `$${teamSpend.toFixed(4)}`;
    document.getElementById("kpi-team-limit").innerText = `/ $${teamLimit.toFixed(2)}`;
    
    const teamBar = document.getElementById("kpi-team-bar");
    teamBar.style.width = `${teamPct}%`;
    teamBar.className = "progress-bar" + (teamPct >= 100 ? " danger" : teamPct >= 80 ? " warn" : "");

    const statusEl = document.getElementById("kpi-team-status");
    statusEl.innerText = teamPct >= 100 ? "EXHAUSTED" : teamPct >= 80 ? "WARNING" : "NORMAL";
    statusEl.className = "status-indicator " + (teamPct >= 100 ? "danger" : teamPct >= 80 ? "warn" : "safe");
    document.getElementById("kpi-team-pct").innerText = `${teamPct.toFixed(1)}% Consumed`;

    // Counts
    let warnCount = 0;
    let blockCount = 0;
    state.agents.forEach(a => {
        const pct = (a.current_spend_usd / a.monthly_limit_usd) * 100;
        if (pct >= 100 || a.status === "PAUSED") blockCount++;
        else if (pct >= 80) warnCount++;
    });

    document.getElementById("kpi-active-agents").innerText = state.agents.length;
    document.getElementById("kpi-warn-count").innerText = `${warnCount} Warning`;
    document.getElementById("kpi-block-count").innerText = `${blockCount} Blocked/Paused`;
    document.getElementById("kpi-total-tokens").innerText = state.totalTokens.toLocaleString();
    document.getElementById("kpi-sub-count").innerText = state.substitutionCount;
}

function renderAgentCards() {
    const container = document.getElementById("agent-cards-list");
    document.getElementById("agent-count-badge").innerText = `${state.agents.length} Agents`;

    if (!state.agents.length) {
        container.innerHTML = `<div class="empty-state">No agents registered in gateway.</div>`;
        return;
    }

    container.innerHTML = state.agents.map(agent => {
        const limit = agent.monthly_limit_usd || 50.0;
        const spend = agent.current_spend_usd || 0.0;
        const pct = Math.min(100, (spend / limit) * 100);
        
        let statusClass = "safe";
        let statusLabel = agent.status || "ACTIVE";
        let barClass = "";

        if (agent.status === "PAUSED") {
            statusClass = "danger";
            statusLabel = "PAUSED (RUNAWAY)";
            barClass = "danger";
        } else if (pct >= 100) {
            statusClass = "danger";
            statusLabel = "EXHAUSTED";
            barClass = "danger";
        } else if (pct >= 80) {
            statusClass = "warn";
            statusLabel = "WARNING (80%+)";
            barClass = "warn";
        }

        return `
            <div class="glass-panel agent-card">
                <div class="agent-card-header">
                    <div>
                        <div class="agent-name">${agent.name}</div>
                        <div class="agent-id-tag">${agent.agent_id}</div>
                    </div>
                    <span class="agent-status-badge ${statusClass}">${statusLabel}</span>
                </div>

                <div class="agent-spend-stat">
                    <span class="agent-spend-val">$${spend.toFixed(4)}</span>
                    <span class="agent-limit-val">Limit: $${limit.toFixed(2)}/mo (${pct.toFixed(1)}%)</span>
                </div>

                <div class="progress-bar-container">
                    <div class="progress-bar ${barClass}" style="width: ${pct}%"></div>
                </div>

                <div class="agent-models-row">
                    <div class="model-tag">Preferred: <span>${agent.preferred_model}</span></div>
                    <div class="model-tag">Fallback: <span>${agent.fallback_model}</span></div>
                </div>
            </div>
        `;
    }).join("");
}

function renderAlerts() {
    const listEl = document.getElementById("alerts-list");
    if (!state.alerts.length) {
        listEl.innerHTML = `<div class="empty-state">No governance violations detected. System operating within budget policy.</div>`;
        return;
    }

    listEl.innerHTML = state.alerts.map(a => {
        let typeClass = "warn";
        if (a.alert_type === "HARD_BLOCK_100" || a.alert_type === "RUNAWAY_DETECTED" || a.alert_type === "SESSION_CLOSED") {
            typeClass = "danger";
        } else if (a.alert_type === "MODEL_SUBSTITUTED") {
            typeClass = "sub";
        }

        return `
            <div class="alert-item ${typeClass}">
                <div><strong>[${a.alert_type}]</strong> ${a.message}</div>
                <div class="alert-meta">
                    <span>Target: ${a.agent_id}</span>
                    <span>${a.created_at_iso || "Just now"}</span>
                </div>
            </div>
        `;
    }).join("");
}

function renderLedger() {
    const tbody = document.getElementById("ledger-body");
    document.getElementById("ledger-count").innerText = `${state.transactions.length} Transactions`;

    if (!state.transactions.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-table-state">Awaiting transactions... Click a simulation button above to begin.</td></tr>`;
        return;
    }

    tbody.innerHTML = state.transactions.map(tx => {
        const subBadge = tx.is_substituted 
            ? `<span class="sub-pill">REROUTED (-90%)</span>` 
            : "";
        
        let dispClass = "allowed";
        if (tx.disposition === "WARNED") dispClass = "warned";
        else if (tx.disposition === "REROUTED") dispClass = "rerouted";
        else if (tx.disposition === "BLOCKED") dispClass = "blocked";

        return `
            <tr>
                <td><strong>${tx.agent_name || tx.agent_id}</strong></td>
                <td>
                    <span class="model-pill">${tx.model_used}</span>
                    ${subBadge}
                </td>
                <td style="font-family: var(--font-mono)">${tx.total_tokens}</td>
                <td style="font-family: var(--font-mono); font-weight: 700;">$${tx.cost_usd.toFixed(6)}</td>
                <td><span class="disposition-badge ${dispClass}">${tx.disposition}</span></td>
            </tr>
        `;
    }).join("");
}

// Bind Simulation & Verification Buttons
function bindSimulationButtons() {
    // 1. Reset Demo
    document.getElementById("btn-reset").addEventListener("click", async () => {
        await fetch("/api/budgets/reset", { method: "POST" });
    });

    // 2. Test Concurrent Calls (Fires 3 agents simultaneously)
    document.getElementById("btn-sim-concurrent").addEventListener("click", async () => {
        const agents = ["agent-support-01", "agent-analytics-02", "agent-research-03"];
        const prompts = [
            "Provide a summary of customer retention metrics for Q2.",
            "Analyze portfolio risk exposure given recent macroeconomic rate adjustments.",
            "Find research papers on distributed LLM state governance."
        ];

        await Promise.all(agents.map((agentId, idx) => {
            return fetch("/v1/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    agent_id: agentId,
                    model: "llama-3.3-70b-versatile",
                    messages: [{ role: "user", content: prompts[idx] }]
                })
            });
        }));
    });

    // 3. Test 80% Warning & Automatic Model Substitution
    document.getElementById("btn-sim-warn").addEventListener("click", async () => {
        // Set Agent 1 limit lower for quick demo verification
        await fetch("/api/budgets/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-support-01",
                monthly_limit_usd: 0.0003, // Small limit to immediately hit 80% on next call
                preferred_model: "llama-3.3-70b-versatile",
                fallback_model: "llama-3.1-8b-instant"
            })
        });

        // Fire call that crosses 80%
        await fetch("/v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-support-01",
                model: "llama-3.3-70b-versatile",
                messages: [{ role: "user", content: "Customer asking for full system diagnostics and account records." }]
            })
        });
    });

    // 4. Test 100% Hard Block
    document.getElementById("btn-sim-block").addEventListener("click", async () => {
        // Configure Agent 2 limit to an already exceeded threshold
        await fetch("/api/budgets/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-analytics-02",
                monthly_limit_usd: 0.00001,
                preferred_model: "llama-3.3-70b-versatile",
                fallback_model: "llama-3.1-8b-instant"
            })
        });

        // Fire call which will be rejected with HTTP 429
        try {
            const res = await fetch("/v1/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    agent_id: "agent-analytics-02",
                    model: "llama-3.3-70b-versatile",
                    messages: [{ role: "user", content: "Run comprehensive multi-year financial regression analysis." }]
                })
            });
            if (res.status === 429) {
                const body = await res.json();
                console.log("Verified: Request hard-blocked correctly with HTTP 429:", body);
            }
        } catch (e) {
            console.log("Caught expected 429 block:", e);
        }
    });

    // 5. Test Runaway Loop Detection
    document.getElementById("btn-sim-runaway").addEventListener("click", async () => {
        // Configure Agent 3 with standard limit
        await fetch("/api/budgets/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-research-03",
                monthly_limit_usd: 0.0005,
                preferred_model: "llama-3.3-70b-versatile",
                fallback_model: "llama-3.1-8b-instant"
            })
        });

        // Fire 5 rapid sequential calls to trigger velocity spike (>20% in 1 hr)
        for (let i = 0; i < 5; i++) {
            await fetch("/v1/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    agent_id: "agent-research-03",
                    model: "llama-3.3-70b-versatile",
                    messages: [{ role: "user", content: `Recursive search step #${i + 1}` }]
                })
            });
        }
    });
}
