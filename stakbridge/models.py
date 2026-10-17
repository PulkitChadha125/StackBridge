"""Request bodies accepted by the API. JSON uses camelCase, Python uses snake_case."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel

NAME_PATTERN = r"^[A-Za-z0-9_\-]{1,40}$"


class ApiModel(BaseModel):
    # extra="forbid" turns a typo such as "symbol_suffix" into a 400 instead of a silently ignored field
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class AccountIn(ApiModel):
    id: str | None = Field(None, pattern=NAME_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    login: int = Field(gt=0)
    server: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)
    terminal_path: str = Field("", max_length=400)
    symbol_suffix: str = Field("", max_length=16)
    enabled: bool = True


class AccountPatch(ApiModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    login: int | None = Field(None, gt=0)
    server: str | None = Field(None, min_length=1, max_length=120)
    password: str | None = Field(None, min_length=1, max_length=200)
    terminal_path: str | None = Field(None, max_length=400)
    symbol_suffix: str | None = Field(None, max_length=16)
    enabled: bool | None = None
    trading_on: bool | None = None


Symbol = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class CopierClient(ApiModel):
    account_id: str = Field(min_length=1, max_length=40)
    multiplier: float = Field(gt=0, le=100)
    # Master symbol -> this client's symbol, for brokers that name a symbol differently (e.g. GOLD -> XAUUSD.m).
    symbols: dict[Symbol, Symbol] = Field(default_factory=dict, max_length=500)


class CopierIn(ApiModel):
    master_id: str | None = Field(None, min_length=1, max_length=40)
    # Lot multiplier and symbol names per client. A client not listed here copies at x1.
    clients: list[CopierClient] = []


class LoginIn(ApiModel):
    # The username is only used with a licence (the licence server's account); otherwise the page password is enough.
    username: str | None = Field(None, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeIn(ApiModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class ForgotIn(ApiModel):
    email: str = Field(min_length=3, max_length=200)
    code: str | None = Field(None, max_length=20)
    reset_token: str | None = Field(None, max_length=400)
    new_password: str | None = Field(None, max_length=200)
    confirm_password: str | None = Field(None, max_length=200)


class PasswordSetupIn(ApiModel):
    password: str = Field(min_length=8, max_length=200)


class PreferencesIn(ApiModel):
    """How the page looks on this PC. Kept by StakBridge because the EXE window forgets browser storage."""
    zoom: float | None = Field(None, ge=0.5, le=2.0)
    theme: Literal["system", "light", "dark"] | None = None
