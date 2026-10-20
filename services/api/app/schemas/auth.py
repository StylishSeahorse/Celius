import uuid
from datetime import datetime

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


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class RegisterRequest(BaseModel):
    token: str = Field(min_length=20, max_length=100)
    # Lower-case letters, digits, dot, dash, underscore; 2-32 characters.
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
    password: str = Field(min_length=12, max_length=1024)
    display_name: str | None = Field(default=None, max_length=128)
    device_id: str | None = Field(default=None, max_length=128)


class InvitationCheck(BaseModel):
    expires_at: datetime
    note: str | None = None
