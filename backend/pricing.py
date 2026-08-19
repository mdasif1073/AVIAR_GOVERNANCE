from typing import Dict, Tuple

# Pricing in USD per 1 Million Tokens (Input / Output)
# Sources: Groq Cloud official pricing, OpenAI pricing, Google Gemini pricing
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Groq Models
    "llama-3.3-70b-versatile": {
        "input_per_million": 0.59,
        "output_per_million": 0.79,
        "tier": "heavyweight",
        "provider": "groq",
    },
    "llama-3.1-70b-versatile": {
        "input_per_million": 0.59,
        "output_per_million": 0.79,
        "tier": "heavyweight",
        "provider": "groq",
    },
    "llama-3.1-8b-instant": {
        "input_per_million": 0.05,
        "output_per_million": 0.08,
        "tier": "lightweight",
        "provider": "groq",
    },
    "llama3-8b-8192": {
        "input_per_million": 0.05,
        "output_per_million": 0.08,
        "tier": "lightweight",
        "provider": "groq",
    },
    "mixtral-8x7b-32768": {
        "input_per_million": 0.24,
        "output_per_million": 0.24,
        "tier": "midweight",
        "provider": "groq",
    },

    # OpenAI Models
    "gpt-4o": {
        "input_per_million": 2.50,
        "output_per_million": 10.00,
        "tier": "heavyweight",
        "provider": "openai",
    },
    "gpt-4o-mini": {
        "input_per_million": 0.15,
        "output_per_million": 0.60,
        "tier": "lightweight",
        "provider": "openai",
    },

    # Google Gemini Models
    "gemini-1.5-pro": {
        "input_per_million": 1.25,
        "output_per_million": 5.00,
        "tier": "heavyweight",
        "provider": "gemini",
    },
    "gemini-1.5-flash": {
        "input_per_million": 0.075,
        "output_per_million": 0.30,
        "tier": "lightweight",
        "provider": "gemini",
    },

    # Default fallback rate for unlisted models
    "default": {
        "input_per_million": 0.50,
        "output_per_million": 0.75,
        "tier": "standard",
        "provider": "custom",
    }
}

# Automatic model substitution mapping for cost-saving fallback under budget stress
MODEL_SUBSTITUTION_MAP: Dict[str, str] = {
    "llama-3.3-70b-versatile": "llama-3.1-8b-instant",
    "llama-3.1-70b-versatile": "llama-3.1-8b-instant",
    "gpt-4o": "gpt-4o-mini",
    "gemini-1.5-pro": "gemini-1.5-flash",
    "mixtral-8x7b-32768": "llama-3.1-8b-instant",
}

def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Tuple[float, float, float]:
    """
    Calculates the exact cost for a request based on prompt and completion tokens.
    Returns: (input_cost_usd, output_cost_usd, total_cost_usd)
    """
    pricing = MODEL_PRICING.get(model.lower(), MODEL_PRICING["default"])
    
    input_cost = (prompt_tokens / 1_000_000.0) * pricing["input_per_million"]
    output_cost = (completion_tokens / 1_000_000.0) * pricing["output_per_million"]
    total_cost = input_cost + output_cost
    
    return round(input_cost, 8), round(output_cost, 8), round(total_cost, 8)

def get_cheaper_model(current_model: str) -> str:
    """Returns the cost-optimized fallback model for a given heavy model."""
    return MODEL_SUBSTITUTION_MAP.get(current_model.lower(), "llama-3.1-8b-instant")
