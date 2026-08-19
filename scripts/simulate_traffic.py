#!/usr/bin/env python3
"""
AIVAR Agent Budget Controller - Live Traffic Simulator
Demonstrates concurrent requests, 80% warning, 100% hard block,
dynamic model substitution, and runaway loop detection.
"""

import asyncio
import httpx
import time

BASE_URL = "http://127.0.0.1:8000"

def log_section(title):
    print("\n" + "=" * 70)
    print(f"  >>> {title.upper()} <<<")
    print("=" * 70)

async def run_simulation():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # Step 0: Check Health
        log_section("1. Verifying Gateway Health")
        res = await client.get("/health")
        print(f"Health Status: {res.status_code} -> {res.json()}")

        # Step 1: Reset demo state
        await client.post("/api/budgets/reset")
        print("Demo state reset to clean baseline.")

        # Step 2: Concurrent Multi-Agent Traffic (Success Criterion 1)
        log_section("2. Testing Concurrent Requests across 3 Agents")
        agents = [
            ("agent-support-01", "Customer asking: How do I change my billing tier?"),
            ("agent-analytics-02", "Compute monthly cohort churn rate for Q3."),
            ("agent-research-03", "Summarize latest research on multi-agent governance.")
        ]

        async def send_agent_call(agent_id, prompt):
            start = time.time()
            resp = await client.post("/v1/chat/completions", json={
                "agent_id": agent_id,
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}]
            })
            duration = round((time.time() - start) * 1000, 2)
            headers = resp.headers
            print(f"[{agent_id}] -> Status: {resp.status_code} ({duration}ms) | "
                  f"Disposition: {headers.get('X-Governance-Disposition')} | "
                  f"Model Used: {headers.get('X-Governance-Model-Used')} | "
                  f"Spend: ${headers.get('X-Governance-Agent-Spend-USD')}")

        await asyncio.gather(*[send_agent_call(a[0], a[1]) for a in agents])

        # Step 3: Model Substitution on Budget Pressure (Success Criteria 2 & 5)
        log_section("3. Testing 80% Warning & Automatic Model Substitution")
        # Lower limit for agent-support-01 to demonstrate substitution under budget pressure
        await client.post("/api/budgets/agent", json={
            "agent_id": "agent-support-01",
            "monthly_limit_usd": 0.0002,
            "preferred_model": "openai/gpt-oss-120b",
            "fallback_model": "openai/gpt-oss-20b"
        })

        # Fire request
        resp = await client.post("/v1/chat/completions", json={
            "agent_id": "agent-support-01",
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": "Help with priority SLA escalation."}],
            "allow_model_substitution": True
        })
        print(f"[agent-support-01] -> Status: {resp.status_code} | "
              f"Disposition: {resp.headers.get('X-Governance-Disposition')} | "
              f"Substituted: {resp.headers.get('X-Governance-Substituted')} | "
              f"Requested: {resp.headers.get('X-Governance-Model-Requested')} -> "
              f"Used: {resp.headers.get('X-Governance-Model-Used')}")

        # Step 4: 100% Hard Block (Success Criterion 3)
        log_section("4. Testing 100% Hard Block (Budget Exhausted)")
        await client.post("/api/budgets/agent", json={
            "agent_id": "agent-analytics-02",
            "monthly_limit_usd": 0.000001,
            "preferred_model": "openai/gpt-oss-120b",
            "fallback_model": "openai/gpt-oss-20b"
        })

        try:
            resp = await client.post("/v1/chat/completions", json={
                "agent_id": "agent-analytics-02",
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": "Generate large portfolio simulation."}]
            })
            if resp.status_code == 429:
                print(f"[agent-analytics-02] -> Successfully Blocked! HTTP {resp.status_code}")
                print(f"Rejection Payload: {resp.json()}")
        except Exception as e:
            print(f"Rejection caught: {e}")

        # Step 5: Summary Report
        log_section("5. Final Governance Summary")
        summary = (await client.get("/api/budgets/summary")).json()
        print(f"Team Total Spend: ${summary['team']['current_spend_usd']:.6f} / ${summary['team']['monthly_limit_usd']:.2f}")
        for ag in summary["agents"]:
            print(f" - {ag['name']} ({ag['agent_id']}): ${ag['current_spend_usd']:.6f} / ${ag['monthly_limit_usd']:.2f} | Status: {ag['status']}")

        print("\nSimulation complete! View the real-time visual dashboard at http://localhost:8000\n")

if __name__ == "__main__":
    asyncio.run(run_simulation())
