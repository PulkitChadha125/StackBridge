"""Request bodies accepted by the API. JSON uses camelCase, Python uses snake_case."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

NAME_PATTERN = r"^[A-Za-z0-9_\-]{1,40}$"


class ApiModel(BaseModel):
    # extra="forbid" turns a typo such as "strategy_tag" into a 400 instead of a silently ignored field
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class TradeIn(ApiModel):
    request_id: str = Field(min_length=1, max_length=64)
    strategy: str = Field(min_length=1, max_length=40)
    action: Literal["open", "close"]
    symbol: str | None = Field(None, min_length=1, max_length=32)
    side: Literal["buy", "sell"] | None = None
    type: Literal["market", "limit", "stop"] | None = None
    volume: float | None = Field(None, gt=0)
    price: float | None = Field(None, gt=0)
    sl: float | None = Field(None, ge=0)
    tp: float | None = Field(None, ge=0)
    comment: str | None = Field(None, max_length=31)

    @model_validator(mode="after")
    def _check_action_fields(self):
        if self.action == "open":
            missing = [name for name in ("symbol", "side", "type", "volume") if getattr(self, name) is None]
            if missing:
                raise ValueError(f"open needs: {', '.join(missing)}")
            if self.type in ("limit", "stop") and self.price is None:
                raise ValueError("price is required for limit and stop orders")
        return self

    def payload(self) -> dict:
        """Canonical form, used to tell a true resend from a reused requestId."""
        return self.model_dump(by_alias=True, exclude_none=True)


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


class StrategyIn(ApiModel):
    tag: str = Field(pattern=NAME_PATTERN)
    max_volume: float = Field(gt=0)
    active: bool = True


class StrategyPatch(ApiModel):
    active: bool | None = None
    max_volume: float | None = Field(None, gt=0)


class StrategyAccounts(ApiModel):
    account_ids: list[str]


class LoginIn(ApiModel):
    password: str = Field(min_length=1, max_length=200)


class PasswordSetupIn(ApiModel):
    password: str = Field(min_length=8, max_length=200)
