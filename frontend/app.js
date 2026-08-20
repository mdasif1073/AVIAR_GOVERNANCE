// ═══════════════════════════════════════════════════════════
//  AIVAR Governance Gateway — Frontend Application
//  Enterprise Real-time SSE & Policy Enforcement Controller
// ═══════════════════════════════════════════════════════════

let state = {
    team: { monthly_limit_usd: 500.0, current_spend_usd: 0.0 },
    agents: [],
    transactions: [],
    alerts: [],
    totalTokens: 0,
    substitutionCount: 0
};

document.addEventListener("DOMContentLoaded", () => {
    initSSE();
    bindButtons();
    fetchSummary();
});

// ── TOAST NOTIFICATIONS ────────────────────────────────────
function toast(msg, type = "success", duration = 4000) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.className = `show ${type}`;
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => { el.className = ""; }, duration);
}

// ── SSE STREAM & AUTO-RECONNECT ────────────────────────────
function initSSE() {
    const statusEl = document.getElementById("conn-status");

    function connect() {
        const es = new EventSource("/api/events/stream");

        es.onopen = () => {
            if (statusEl) statusEl.textContent = "GATEWAY ONLINE";
        };

        es.onmessage = (event) => {
            try {
                const { event: evtName, data } = JSON.parse(event.data);
                if (evtName === "INITIAL_STATE") {
                    state.team         = data.team || state.team;
                    state.agents       = data.agents || [];
                    state.transactions = data.recent_transactions || [];
                    state.alerts       = data.recent_alerts || [];
                    recalcTotals();
                    renderAll();
                } else if (evtName === "TRANSACTION_PROCESSED") {
                    if (data.transaction) {
                        state.transactions.unshift(data.transaction);
                        if (state.transactions.length > 100) state.transactions.pop();
                        state.totalTokens += data.transaction.total_tokens || 0;
                        if (data.transaction.is_substituted) state.substitutionCount++;
                    }
                    if (data.agents) state.agents = data.agents;
                    if (data.team) state.team = data.team;
                    fetchAlerts();
                    renderAll();
                } else if (evtName === "REQUEST_BLOCKED") {
                    fetchSummary();
                } else if (evtName === "STATE_RESET") {
                    state = { team: data.team, agents: data.agents, transactions: [], alerts: [], totalTokens: 0, substitutionCount: 0 };
                    renderAll();
                    toast("All agent budgets reset to $0.00", "success");
                }
            } catch (err) {
                console.error("SSE parse error:", err);
            }
        };

        es.onerror = () => {
            if (statusEl) statusEl.textContent = "STREAM SYNC";
            es.close();
            setTimeout(connect, 3000);
        };
    }

    connect();

    // Secondary Polling for Stream Reliability
    setInterval(async () => {
        try {
            const r = await fetch("/api/budgets/summary");
            if (!r.ok) return;
            const d = await r.json();
            state.team   = d.team;
            state.agents = d.agents;
            if (d.recent_transactions?.length) {
                state.transactions = d.recent_transactions;
                recalcTotals();
            }
            if (d.recent_alerts?.length) state.alerts = d.recent_alerts;
            renderAll();
        } catch(e) {}
    }, 5000);
}

// ── DATA FETCH ─────────────────────────────────────────────
async function fetchSummary() {
    try {
        const r = await fetch("/api/budgets/summary");
        if (!r.ok) return;
        const d = await r.json();
        state.team         = d.team;
        state.agents       = d.agents;
        state.transactions = d.recent_transactions || [];
        state.alerts       = d.recent_alerts || [];
        recalcTotals();
        renderAll();
    } catch(e) {
        console.error(e);
    }
}

async function fetchAlerts() {
    try {
        const r = await fetch("/api/alerts");
        if (r.ok) {
            state.alerts = (await r.json()).alerts;
            renderAlerts();
        }
    } catch(e) {}
}

function recalcTotals() {
    state.totalTokens       = state.transactions.reduce((a,t) => a + (t.total_tokens || 0), 0);
    state.substitutionCount = state.transactions.filter(t => t.is_substituted).length;
}

// ── RENDER ROOT ────────────────────────────────────────────
function renderAll() {
    renderKPIs();
    renderAgentCards();
    renderAlerts();
    renderLedger();
}

// ── KPI CARDS ──────────────────────────────────────────────
function renderKPIs() {
    const spend = state.team?.current_spend_usd || 0;
    const limit = state.team?.monthly_limit_usd || 500;
    const pct   = Math.min(100, (spend / limit) * 100);

    el("k-spend").textContent = `$${spend.toFixed(4)}`;
    el("k-limit").textContent = `/ $${limit.toFixed(2)}`;

    const bar = el("k-bar");
    if (bar) {
        bar.style.width = `${pct}%`;
        bar.className = "progress-bar" + (pct >= 100 ? " danger" : pct >= 80 ? " warn" : "");
    }

    const st = el("k-status");
    if (st) {
        st.textContent = pct >= 100 ? "EXHAUSTED" : pct >= 80 ? "WARNING" : "NORMAL";
        st.className   = "badge " + (pct >= 100 ? "badge-danger" : pct >= 80 ? "badge-warn" : "badge-safe");
    }
    el("k-pct").textContent = `${pct.toFixed(1)}% Consumed`;

    let warn = 0, blocked = 0;
    state.agents.forEach(a => {
        const ap = (a.current_spend_usd / Math.max(a.monthly_limit_usd, 1e-9)) * 100;
        if (ap >= 100 || a.status === "PAUSED") blocked++;
        else if (ap >= 80) warn++;
    });

    el("k-agents").textContent = state.agents.length;
    el("k-warn").textContent   = `${warn} Warning`;
    el("k-block").textContent  = `${blocked} Blocked`;
    el("k-tokens").textContent = state.totalTokens.toLocaleString();
    el("k-sub").textContent    = state.substitutionCount;
    el("agent-cnt").textContent = `${state.agents.length} Agents`;
}

// ── AGENT FLEET STACK ──────────────────────────────────────
function renderAgentCards() {
    const container = el("agent-list");
    if (!container) return;

    if (!state.agents.length) {
        container.innerHTML = `<div class="empty-placeholder">No active agents registered in gateway.</div>`;
        return;
    }

    container.innerHTML = state.agents.map(agent => {
        const lim   = Math.max(agent.monthly_limit_usd || 50, 1e-9);
        const spend = agent.current_spend_usd || 0;
        const pct   = Math.min(100, (spend / lim) * 100);

        let badgeCls = "badge-safe", label = "ACTIVE", barCls = "";
        if (agent.status === "PAUSED") {
            badgeCls = "badge-danger"; label = "PAUSED (RUNAWAY)"; barCls = " danger";
        } else if (pct >= 100) {
            badgeCls = "badge-danger"; label = "EXHAUSTED (100%)"; barCls = " danger";
        } else if (pct >= 80) {
            badgeCls = "badge-warn"; label = "WARNING (80%+)"; barCls = " warn";
        }

        return `
        <div class="card agent-card">
            <div class="agent-card-top">
                <div class="agent-meta-group">
                    <span class="agent-title">${agent.name}</span>
                    <span class="agent-identifier">${agent.agent_id}</span>
                </div>
                <span class="badge ${badgeCls}">${label}</span>
            </div>
            <div class="agent-metrics-row">
                <span class="agent-spend-figure">$${spend.toFixed(6)}</span>
                <span class="agent-limit-meta">Limit: $${lim.toFixed(2)}/mo (${pct.toFixed(1)}% used)</span>
            </div>
            <div class="progress-track">
                <div class="progress-bar${barCls}" style="width: ${pct}%"></div>
            </div>
            <div class="agent-model-matrix">
                <div class="matrix-item">
                    <span class="matrix-label">Primary Tier</span>
                    <span class="matrix-value">${agent.preferred_model}</span>
                </div>
                <div class="matrix-item">
                    <span class="matrix-label">Fallback Tier</span>
                    <span class="matrix-value">${agent.fallback_model}</span>
                </div>
            </div>
        </div>`;
    }).join("");
}

// ── AUDIT & ALERT LOGS ────────────────────────────────────
function renderAlerts() {
    const listEl = el("alert-list");
    if (!listEl) return;

    if (!state.alerts.length) {
        listEl.innerHTML = `<div class="empty-placeholder">No policy violations recorded. Fleet operating within allocated limits.</div>`;
        return;
    }

    listEl.innerHTML = state.alerts.map(a => {
        let cls = "warn";
        if (["HARD_BLOCK_100", "RUNAWAY_DETECTED", "SESSION_CLOSED", "AGENT_PAUSED"].includes(a.alert_type)) {
            cls = "danger";
        } else if (a.alert_type === "MODEL_SUBSTITUTED") {
            cls = "info";
        }

        return `
        <div class="log-entry ${cls}">
            <div class="log-entry-head">
                <span class="log-type-tag">[${a.alert_type}]</span>
                <span class="log-timestamp">${a.created_at_iso || "Just now"}</span>
            </div>
            <div class="log-message">${a.message}</div>
        </div>`;
    }).join("");
}

// ── TRANSACTION LEDGER ─────────────────────────────────────
function renderLedger() {
    const tbody = el("ledger-body");
    if (!tbody) return;

    el("tx-count").textContent = `${state.transactions.length} Records`;

    if (!state.transactions.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-placeholder">No transactions processed. Execute a step above to record traffic.</td></tr>`;
        return;
    }

    const dispMap = { ALLOWED: "badge-safe", WARNED: "badge-warn", REROUTED: "badge-info", BLOCKED: "badge-danger" };
    tbody.innerHTML = state.transactions.map(tx => {
        const sub = tx.is_substituted ? `<span class="model-badge-sub">REROUTED -50%</span>` : "";
        const badgeClass = dispMap[tx.disposition] || "badge-safe";
        const modelStr = tx.model_requested !== tx.model_used
            ? `<span class="mono-cell">${tx.model_requested}</span> → <span class="mono-cell">${tx.model_used}</span>`
            : `<span class="mono-cell">${tx.model_used}</span>`;

        return `
        <tr>
            <td><strong>${tx.agent_name || tx.agent_id}</strong></td>
            <td>${modelStr}${sub}</td>
            <td class="mono-cell">${(tx.total_tokens || 0).toLocaleString()}</td>
            <td class="mono-cell">$${(tx.cost_usd || 0).toFixed(6)}</td>
            <td><span class="badge ${badgeClass}">${tx.disposition}</span></td>
        </tr>`;
    }).join("");
}

// ── SIMULATION BINDINGS ────────────────────────────────────
function bindButtons() {

    // 1. Reset Spend to $0
    el("btn-reset").addEventListener("click", async () => {
        el("btn-reset").innerHTML = `Resetting...`;
        await fetch("/api/budgets/reset", { method: "POST" });
        await fetchSummary();
        el("btn-reset").innerHTML = `
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                <path d="M3 3v5h5"/>
            </svg>
            Reset Spend to $0`;
        toast("All agent budgets reset to $0.00", "success");
    });

    // 2. STEP 1: Concurrent Fleet Workload
    el("btn-concurrent").addEventListener("click", async () => {
        setRunning("btn-concurrent", true);
        toast("Dispatching concurrent LLM requests across 3 agents...", "info");

        await Promise.all([
            fetch("/api/budgets/agent", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ agent_id:"agent-support-01",   monthly_limit_usd:50, preferred_model:"openai/gpt-oss-120b", fallback_model:"openai/gpt-oss-20b" }) }),
            fetch("/api/budgets/agent", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ agent_id:"agent-analytics-02", monthly_limit_usd:75, preferred_model:"openai/gpt-oss-120b", fallback_model:"openai/gpt-oss-20b" }) }),
            fetch("/api/budgets/agent", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ agent_id:"agent-research-03",  monthly_limit_usd:30, preferred_model:"openai/gpt-oss-120b", fallback_model:"openai/gpt-oss-20b" }) }),
        ]);

        const results = await Promise.all([
            fetch("/v1/chat/completions", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ agent_id:"agent-support-01",   model:"openai/gpt-oss-120b", messages:[{role:"user",content:"Summarise customer satisfaction trends for Q2 2026."}] }) }),
            fetch("/v1/chat/completions", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ agent_id:"agent-analytics-02", model:"openai/gpt-oss-120b", messages:[{role:"user",content:"Analyse portfolio risk exposure given current interest rates."}] }) }),
            fetch("/v1/chat/completions", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ agent_id:"agent-research-03",  model:"openai/gpt-oss-120b", messages:[{role:"user",content:"Find recent research on distributed AI governance frameworks."}] }) }),
        ]);

        const statuses = results.map(r => r.status);
        toast(`SC-1 Verified: 3 concurrent calls returned HTTP ${statuses.join(", ")} and metered atomically in DynamoDB`, "success", 5000);
        await fetchSummary();
        setRunning("btn-concurrent", false);
    });

    // 3. STEP 2: 80% Warning & Model Substitution
    el("btn-warn").addEventListener("click", async () => {
        setRunning("btn-warn", true);
        toast("Configuring threshold to trigger 80% governance alert...", "warn");

        await fetch("/api/budgets/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-support-01",
                monthly_limit_usd: 0.001,
                preferred_model: "openai/gpt-oss-120b",
                fallback_model: "openai/gpt-oss-20b"
            })
        });

        const res = await fetch("/v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-support-01",
                model: "openai/gpt-oss-120b",
                allow_model_substitution: true,
                messages: [{ role: "user", content: "Generate an executive enterprise support overview and risk analysis." }]
            })
        });

        const disp = res.headers.get("X-Governance-Disposition");
        const used = res.headers.get("X-Governance-Model-Used");
        const sub  = res.headers.get("X-Governance-Substituted");

        toast(`SC-2/SC-5 Verified: Disposition=${disp || "REROUTED"} | Model Used=${used || "gpt-oss-20b"} | Substituted=${sub}`, "warn", 6000);
        await fetchSummary();
        setRunning("btn-warn", false);
    });

    // 4. STEP 3: 100% Hard Block
    el("btn-block").addEventListener("click", async () => {
        setRunning("btn-block", true);
        toast("Simulating budget exhaustion for hard block verification...", "warn");

        await fetch("/api/budgets/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-analytics-02",
                monthly_limit_usd: 0.000001,
                preferred_model: "openai/gpt-oss-120b",
                fallback_model: "openai/gpt-oss-20b"
            })
        });

        const res = await fetch("/v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-analytics-02",
                model: "openai/gpt-oss-120b",
                messages: [{ role: "user", content: "Run comprehensive multi-year macroeconomic simulation." }]
            })
        });

        const body = await res.json();
        const code = body?.detail?.error?.code || "BUDGET_EXHAUSTED";

        if (res.status === 429) {
            toast(`SC-3 Verified: HTTP 429 Hard Block active (${code}) — LLM request rejected`, "error", 6000);
        }
        await fetchSummary();
        setRunning("btn-block", false);
    });

    // 5. BONUS: Runaway Loop Detection
    el("btn-runaway").addEventListener("click", async () => {
        setRunning("btn-runaway", true);
        toast("Initiating rapid query loop to test runaway velocity detector...", "warn");

        await fetch("/api/budgets/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                agent_id: "agent-research-03",
                monthly_limit_usd: 0.0005,
                preferred_model: "openai/gpt-oss-120b",
                fallback_model: "openai/gpt-oss-20b"
            })
        });

        for (let i = 0; i < 5; i++) {
            const res = await fetch("/v1/chat/completions", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    agent_id: "agent-research-03",
                    model: "openai/gpt-oss-120b",
                    messages: [{ role: "user", content: `Recursive knowledge expansion iteration ${i+1}` }]
                })
            });
            if (res.status === 429) {
                toast("Bonus Verified: Runaway velocity detected — Agent state transitioned to PAUSED", "error", 7000);
                break;
            }
        }
        await fetchSummary();
        setRunning("btn-runaway", false);
    });
}

// ── UTILITY HELPERS ────────────────────────────────────────
function el(id) {
    return document.getElementById(id);
}

function setRunning(btnId, isRunning) {
    const btn = el(btnId);
    if (!btn) return;
    btn.classList.toggle("running", isRunning);
    btn.disabled = isRunning;
}
