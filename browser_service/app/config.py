from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    MAX_CONCURRENT_CONTEXTS: int = 5
    DEFAULT_TIMEOUT_MS: int = 30000
    PORT: int = 8000
