import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    # App Config
    APP_NAME: str = "Aivar Agent Budget Controller"
    APP_ENV: str = "production"
    DEBUG: bool = False
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # LLM Provider Configuration
    GROQ_API_KEY: Optional[str] = os.getenv("GROQ_API_KEY", "")
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY", "")
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY", "")

    # Default Models
    DEFAULT_PRIMARY_MODEL: str = "llama-3.3-70b-versatile"
    DEFAULT_FALLBACK_MODEL: str = "llama-3.1-8b-instant"

    # AWS / DynamoDB Configuration
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    AWS_ACCESS_KEY_ID: Optional[str] = os.getenv("AWS_ACCESS_KEY_ID", "mock_key")
    AWS_SECRET_ACCESS_KEY: Optional[str] = os.getenv("AWS_SECRET_ACCESS_KEY", "mock_secret")
    DYNAMODB_ENDPOINT_URL: Optional[str] = os.getenv("DYNAMODB_ENDPOINT_URL", None) # e.g., http://localhost:8000 for DynamoDB Local
    USE_IN_MEMORY_DYNAMO_FALLBACK: bool = True # Automatically activates if AWS credentials/endpoint unavailable

    # Governance Default Thresholds
    WARN_THRESHOLD_PERCENT: float = 80.0
    HARD_BLOCK_THRESHOLD_PERCENT: float = 100.0
    RUNAWAY_VELOCITY_PERCENT: float = 20.0  # >20% consumed in 1 hour indicates runaway loop
    RUNAWAY_WINDOW_SECONDS: int = 3600

settings = Settings()
