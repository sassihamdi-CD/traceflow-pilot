"""Central config. Correction 3: WORKSPACE_ID is a hardcoded env constant."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    workspace_id: str = "00000000-0000-0000-0000-000000000000"
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "traceflow-docs"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250929"
    frontend_url: str = "http://localhost:3001"  # comma-separated origins allowed
    intake_secret: str = ""  # shared secret for POST /api/intake/email (empty = 503)
    founder_key: str = ""  # TraceFlow founder key for /founder + /api/founder/*.
    worker_secret: str = ""  # shared secret for POST /api/worker/tick (empty = 401)
    # Server-only, never NEXT_PUBLIC_. Empty = founder area disabled.
    # Local-only demo mode: bypasses Supabase auth, R2, and Anthropic so the
    # whole flow can be exercised without any external credentials. NEVER
    # enable in production (it grants reviewer access to a fixed demo user).
    demo_mode: bool = False
    demo_storage_dir: str = "/srv/app/uploads"  # shared api/worker volume
    # Serverless: keep 2 per instance because Vercel runs many instances.
    # A single long-lived API host (Fly/Railway/VM) can raise this to 10-20.
    db_pool_max_size: int = 2
    port: int = 8000


settings = Settings()
WORKSPACE_ID = settings.workspace_id
