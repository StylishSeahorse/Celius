import uuid

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    device_id: str | None = Field(default=None, max_length=128)


class LoginResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    # Returned for non-browser clients (Tauri); browsers rely on the HttpOnly cookie.
    access_token: str
    expires_at: str


class MeResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    display_name: str | None
    is_admin: bool
    single_user_mode: bool
    capabilities: dict[str, bool]
