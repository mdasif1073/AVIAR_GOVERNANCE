from typing import Dict, Tuple

# Pricing in USD per 1 Million Tokens
# Verified against this Groq account's /v1/models endpoint
MODEL_PRICING: Dict[str, Dict] = {
    "openai/gpt-oss-120b": {
        "input_per_million":  0.15,
        "output_per_million": 0.60,
        "tier": "heavyweight",
        "provider": "groq",
        "display": "GPT OSS 120B (via Groq)",
    },
    "openai/gpt-oss-20b": {
        "input_per_million":  0.075,
        "output_per_million": 0.30,
        "tier": "lightweight",
        "provider": "groq",
        "display": "GPT OSS 20B (via Groq)",
    },
    "openai/gpt-oss-safeguard-20b": {
        "input_per_million":  0.075,
        "output_per_million": 0.30,
        "tier": "lightweight",
        "provider": "groq",
        "display": "Safety GPT OSS 20B (via Groq)",
    },
    "qwen/qwen3.6-27b": {
        "input_per_million":  0.60,
        "output_per_million": 3.00,
        "tier": "midweight",
        "provider": "groq",
        "display": "Qwen 3.6 27B (via Groq)",
    },
    "groq/compound": {
        "input_per_million":  0.20,
        "output_per_million": 0.60,
        "tier": "heavyweight",
        "provider": "groq",
        "display": "Groq Compound",
    },
    "groq/compound-mini": {
        "input_per_million":  0.05,
        "output_per_million": 0.20,
        "tier": "lightweight",
        "provider": "groq",
        "display": "Groq Compound Mini",
    },
    "default": {
        "input_per_million":  0.15,
        "output_per_million": 0.60,
        "tier": "standard",
        "provider": "groq",
        "display": "Custom Model",
    },
}

# Substitution map: expensive model -> cheaper fallback under budget pressure
# 120B -> 20B: ~50% cost reduction
# compound -> compound-mini: ~75% cost reduction
MODEL_SUBSTITUTION_MAP: Dict[str, str] = {
    "openai/gpt-oss-120b":     "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b":        "openai/gpt-oss-20b",
    "groq/compound":           "groq/compound-mini",
    # Legacy names kept for compatibility
    "llama-3.3-70b-versatile": "openai/gpt-oss-20b",
    "llama-3.1-70b-versatile": "openai/gpt-oss-20b",
    "gpt-4o":                  "openai/gpt-oss-20b",
    "gemini-1.5-pro":          "openai/gpt-oss-20b",
}


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Tuple[float, float, float]:
    """
    Calculates the exact cost for a request based on prompt and completion tokens.
    Returns: (input_cost_usd, output_cost_usd, total_cost_usd)
    """
    pricing = MODEL_PRICING.get(model.lower(), MODEL_PRICING["default"])
    input_cost  = (prompt_tokens  / 1_000_000.0) * pricing["input_per_million"]
    output_cost = (completion_tokens / 1_000_000.0) * pricing["output_per_million"]
    total_cost  = input_cost + output_cost
    return round(input_cost, 8), round(output_cost, 8), round(total_cost, 8)


def get_cheaper_model(current_model: str) -> str:
    """Returns the cost-optimized fallback model for a given heavy model."""
    return MODEL_SUBSTITUTION_MAP.get(current_model.lower(), "openai/gpt-oss-20b")
