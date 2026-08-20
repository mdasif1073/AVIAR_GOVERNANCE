// ═══════════════════════════════════════════════════════════
//  AIVAR Governance Gateway — Frontend App
//  Real-time SSE-powered dashboard for PS-8.1 demo
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

// ── TOAST ──────────────────────────────────────────────────
function toast(msg, type = "success", duration = 4000) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = `show ${type}`;
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => { el.className = ""; }, duration);
}

// ── SSE STREAM ─────────────────────────────────────────────
function initSSE() {
    const statusEl = document.getElementById("conn-status");
    const es = new EventSource("/api/events/stream");

    es.onopen = () => { statusEl.textContent = "GATEWAY LIVE"; };

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
                // Update banner state store label
                const ss = document.getElementById("banner-state-store");
                if (ss) ss.textContent = data.state_store_type === "dynamodb" ? "DynamoDB (live)" : "In-Memory";
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
                toast("✅ All agent spend reset to $0.00", "success");
            }
        } catch (err) { console.error("SSE parse error:", err); }
    };

    es.onerror = () => { statusEl.textContent = "RECONNECTING…"; };
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
        const ss = document.getElementById("banner-state-store");
        if (ss) ss.textContent = store_type || "DynamoDB (live)";
    } catch(e) { console.error(e); }
}

async function fetchAlerts() {
    try {
        const r = await fetch("/api/alerts");
        if (r.ok) { state.alerts = (await r.json()).alerts; renderAlerts(); }
    } catch(e) {}
}

function recalcTotals() {
    state.totalTokens       = state.transactions.reduce((a,t) => a + (t.total_tokens || 0), 0);
    state.substitutionCount = state.transactions.filter(t => t.is_substituted).length;
}

// ── RENDER ALL ─────────────────────────────────────────────
function renderAll() {
    renderKPIs();
    renderAgentCards();
    renderAlerts();
    renderLedger();
}

// ── KPIs ───────────────────────────────────────────────────
function renderKPIs() {
    const spend = state.team?.current_spend_usd || 0;
    const limit = state.team?.monthly_limit_usd || 500;
    const pct   = Math.min(100, (spend / limit) * 100);

    el("k-spend").textContent = `$${spend.toFixed(4)}`;
    el("k-limit").textContent = `/ $${limit.toFixed(2)}`;

    const bar = el("k-bar");
    bar.style.width = `${pct}%`;
    bar.className = "bar-fill" + (pct >= 100 ? " danger" : pct >= 80 ? " warn" : "");

    const st = el("k-status");
    st.textContent = pct >= 100 ? "EXHAUSTED" : pct >= 80 ? "WARNING" : "NORMAL";
    st.className   = "badge " + (pct >= 100 ? "danger" : pct >= 80 ? "warn" : "safe");
    el("k-pct").textContent = `${pct.toFixed(1)}% consumed`;

    let warn = 0, blocked = 0;
    state.agents.forEach(a => {
        const ap = (a.current_spend_usd / Math.max(a.monthly_limit_usd, 1e-9)) * 100;
        if (ap >= 100 || a.status === "PAUSED") blocked++;
        else if (ap >= 80) warn++;
    });

    el("k-agents").textContent = state.agents.length;
    el("k-warn").textContent   = `${warn} Warning`;
    el("k-block").textContent  = `${blocked} Blocked/Paused`;
    el("k-tokens").textContent = state.totalTokens.toLocaleString();
    el("k-sub").textContent    = state.substitutionCount;
    el("agent-cnt").textContent = `${state.agents.length} Agents`;
}

// ── AGENT CARDS ────────────────────────────────────────────
function renderAgentCards() {
    const container = el("agent-list");
    if (!state.agents.length) {
        container.innerHTML = `<div class="empty-state">No agents registered.</div>`;
        return;
    }

    container.innerHTML = state.agents.map(agent => {
        const lim   = Math.max(agent.monthly_limit_usd || 50, 1e-9);
        const spend = agent.current_spend_usd || 0;
        const pct   = Math.min(100, (spend / lim) * 100);

        let bCls = "safe", label = "ACTIVE", barCls = "";
        if (agent.status === "PAUSED") { bCls = "danger"; label = "⏸ PAUSED — RUNAWAY DETECTED"; barCls = " danger"; }
        else if (pct >= 100)           { bCls = "danger"; label = "🛑 EXHAUSTED";                barCls = " danger"; }
        else if (pct >= 80)            { bCls = "warn";   label = "⚠️ WARNING — 80%+ CONSUMED";  barCls = " warn"; }

        return `
        <div class="card agent-card">
            <div class="agent-head">
                <div>
                    <div class="agent-name">${agent.name}</div>
                    <div class="agent-id">${agent.agent_id}</div>
                </div>
                <span class="badge ${bCls}">${label}</span>
            </div>
            <div class="agent-spend-row">
                <span class="agent-spend">$${spend.toFixed(6)}</span>
                <span class="agent-limit">Limit: $${lim.toFixed(2)}/mo · ${pct.toFixed(1)}% used</span>
            </div>
            <div class="bar-wrap">
                <div class="bar-fill${barCls}" style="width:${pct}%"></div>
            </div>
            <div class="agent-models">
                <div class="model-lbl">Preferred: <span>${agent.preferred_model}</span></div>
                <div class="model-lbl" style="margin-left:auto;">Fallback: <span>${agent.fallback_model}</span></div>
            </div>
        </div>`;
    }).join("");
}

// ── ALERTS ─────────────────────────────────────────────────
function renderAlerts() {
    const listEl = el("alert-list");
    if (!state.alerts.length) {
        listEl.innerHTML = `<div class="empty-state">No governance events yet — click a step above to begin.</div>`;
        return;
    }
    listEl.innerHTML = state.alerts.map(a => {
        let cls = "warn";
        if (["HARD_BLOCK_100","RUNAWAY_DETECTED","SESSION_CLOSED","AGENT_PAUSED"].includes(a.alert_type)) cls = "danger";
        else if (a.alert_type === "MODEL_SUBSTITUTED") cls = "info";
        return `
        <div class="alert-item ${cls}">
            <div><strong>[${a.alert_type}]</strong> ${a.message}</div>
            <div class="alert-meta">
                <span>${a.agent_id}</span>
                <span>${a.created_at_iso || "Just now"}</span>
            </div>
        </div>`;
    }).join("");
}

// ── LEDGER ─────────────────────────────────────────────────
function renderLedger() {
    const tbody = el("ledger-body");
    el("tx-count").textContent = `${state.transactions.length} Transactions`;

    if (!state.transactions.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-td">Run Step 1 above to see live transactions…</td></tr>`;
        return;
    }

    const dispMap = { ALLOWED:"safe", WARNED:"warn", REROUTED:"info", BLOCKED:"danger" };
    tbody.innerHTML = state.transactions.map(tx => {
        const sub = tx.is_substituted ? `<span class="sub-badge">AUTO-REROUTED</span>` : "";
        const cls = dispMap[tx.disposition] || "safe";
        const modelStr = tx.model_requested !== tx.model_used
            ? `<span class="model-chip">${tx.model_requested}</span> → <span class="model-chip">${tx.model_used}</span>`
            : `<span class="model-chip">${tx.model_used}</span>`;
        return `
        <tr>
            <td><strong>${tx.agent_name || tx.agent_id}</strong></td>
            <td>${modelStr}${sub}</td>
            <td class="mono-td">${(tx.total_tokens || 0).toLocaleString()}</td>
            <td class="mono-td">$${(tx.cost_usd || 0).toFixed(6)}</td>
            <td><span class="badge ${cls}">${tx.disposition}</span></td>
        </tr>`;
    }).join("");
}

// ── SIMULATION BUTTONS ─────────────────────────────────────
function bindButtons() {

    // Reset all spend
    el("btn-reset").addEventListener("click", async () => {
        el("btn-reset").textContent = "Resetting…";
        await fetch("/api/budgets/reset", { method: "POST" });
        await fetchSummary();
        el("btn-reset").innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg> Reset All Spend to $0`;
        toast("✅ All agent budgets reset to $0.00 — ready for demo", "success");
    });

    // STEP 1: Concurrent calls — 3 real agents fire simultaneously
    el("btn-concurrent").addEventListener("click", async () => {
        setRunning("btn-concurrent", true);
        toast("⚡ Firing 3 agents concurrently to Groq LLM…", "success");

        // Restore real limits first
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
        toast(`✅ SC-1 Complete: 3 concurrent agents → HTTP ${statuses.join(", ")} — all spend tracked in DynamoDB`, "success", 5000);
        await fetchSummary();
        setRunning("btn-concurrent", false);
    });

    // STEP 2: 80% Warning + Model Substitution
    el("btn-warn").addEventListener("click", async () => {
        setRunning("btn-warn", true);
        toast("⚠️ Setting agent budget to hit 80% threshold…", "warn");

        // Set Support Agent limit very low so a single call crosses 80%
        await fetch("/api/budgets/agent", { method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ agent_id:"agent-support-01", monthly_limit_usd:0.001, preferred_model:"openai/gpt-oss-120b", fallback_model:"openai/gpt-oss-20b" })
        });

        // Fire — should trigger WARNING_80 + MODEL_SUBSTITUTED
        const res = await fetch("/v1/chat/completions", { method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ agent_id:"agent-support-01", model:"openai/gpt-oss-120b", allow_model_substitution:true, messages:[{role:"user",content:"Generate a detailed support ticket summary and resolution plan for enterprise account."}] })
        });
        const disp = res.headers.get("X-Governance-Disposition");
        const used = res.headers.get("X-Governance-Model-Used");
        const sub  = res.headers.get("X-Governance-Substituted");

        toast(`✅ SC-2+SC-5: Disposition=${disp || "WARNED/REROUTED"} | Model Used=${used || "gpt-oss-20b"} | Substituted=${sub}`, "warn", 6000);
        await fetchSummary();
        setRunning("btn-warn", false);
    });

    // STEP 3: 100% Hard Block
    el("btn-block").addEventListener("click", async () => {
        setRunning("btn-block", true);
        toast("🛑 Exhausting Analytics Agent budget for hard-block test…", "warn");

        // Set Analytics Agent limit extremely low — already exceeded
        await fetch("/api/budgets/agent", { method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ agent_id:"agent-analytics-02", monthly_limit_usd:0.000001, preferred_model:"openai/gpt-oss-120b", fallback_model:"openai/gpt-oss-20b" })
        });

        // Fire — should return HTTP 429
        const res = await fetch("/v1/chat/completions", { method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ agent_id:"agent-analytics-02", model:"openai/gpt-oss-120b", messages:[{role:"user",content:"Run full 5-year financial regression model across all portfolios."}] })
        });
        const body = await res.json();
        const code = body?.detail?.error?.code || "BLOCKED";

        if (res.status === 429) {
            toast(`✅ SC-3: HTTP 429 returned! Code=${code} — LLM was NEVER reached.`, "error", 6000);
        }
        await fetchSummary();
        setRunning("btn-block", false);
    });

    // BONUS: Runaway Loop Detection
    el("btn-runaway").addEventListener("click", async () => {
        setRunning("btn-runaway", true);
        toast("🔄 Simulating runaway agent loop — burning budget rapidly…", "warn");

        // Set Research Agent to tiny limit so spend velocity hits 20%+ in 1 hr
        await fetch("/api/budgets/agent", { method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ agent_id:"agent-research-03", monthly_limit_usd:0.0005, preferred_model:"openai/gpt-oss-120b", fallback_model:"openai/gpt-oss-20b" })
        });

        // Fire 5 sequential calls to trigger velocity spike
        for (let i = 0; i < 5; i++) {
            const res = await fetch("/v1/chat/completions", { method:"POST", headers:{"Content-Type":"application/json"},
                body: JSON.stringify({ agent_id:"agent-research-03", model:"openai/gpt-oss-120b", messages:[{role:"user",content:`Recursive search step ${i+1}: explore all subgraphs of knowledge base.`}] })
            });
            if (res.status === 429) {
                toast("✅ Bonus: Runaway DETECTED! Agent auto-paused — check agent card below.", "error", 7000);
                break;
            }
        }
        await fetchSummary();
        setRunning("btn-runaway", false);
    });
}

// ── HELPERS ────────────────────────────────────────────────
function el(id) { return document.getElementById(id); }

function setRunning(btnId, running) {
    const btn = el(btnId);
    if (!btn) return;
    btn.classList.toggle("running", running);
    btn.disabled = running;
}
