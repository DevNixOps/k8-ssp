from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SSP_")

    mock_mode: bool = True  # set SSP_MOCK_MODE=false to talk to a real cluster
    kubeconfig: str | None = None  # None -> in-cluster, then default kubeconfig
    jwt_secret: str = "change-me-in-production-use-32+-bytes"
    token_ttl_minutes: int = 60
    # "user:password:role[:ns1|ns2]" comma-separated. Roles: viewer, developer, admin
    users: str = "viewer:viewer:viewer,dev:dev:developer:dev|default,admin:admin:admin"
    max_log_tail: int = 5000
    max_streams_per_user: int = 3
    hidden_namespaces: str = "kube-system,kube-public,kube-node-lease"
    cors_origins: str = "http://localhost:5173"


settings = Settings()
