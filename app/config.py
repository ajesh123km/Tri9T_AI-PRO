import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./tri9t.db")
    generated_store_path: str = os.getenv("GENERATED_STORE_PATH", "./data/generated.json")
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")
    llm_api_key: str | None = os.getenv("LLM_API_KEY") or None
    llm_model: str | None = os.getenv("LLM_MODEL") or None
    ocr_enabled: bool = os.getenv("OCR_ENABLED", "false").lower() == "true"


settings = Settings()

