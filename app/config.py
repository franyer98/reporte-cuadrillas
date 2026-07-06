from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "cambia-esto"
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    LLM_ENABLED: bool = True  # False = modo validación gratuito (sin corrección IA)
    TIMEZONE: str = "America/Bogota"
    REPORT_CUTOFF: str = "18:30"
    REPORT_GRACE_MINUTES: int = 30
    DATABASE_URL: str = ""
    FOTOS_DIR: str = "data/fotos"

    class Config:
        env_file = ".env"

    @property
    def db_url(self) -> str:
        return self.DATABASE_URL or "sqlite:///./reportes.db"


settings = Settings()
