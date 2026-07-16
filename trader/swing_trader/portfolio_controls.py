"""Durable operator-owned portfolio allocation controls.

These settings are behavioral configuration, not secrets.  They live beside
the Finance ledger so Web, Desktop and the scheduled loop share one value and
updates survive container rebuilds.  The LLM may read them but cannot loosen
the hard risk caps or write them without the operator-facing API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import MetaData
from sqlmodel import Field as SqlField
from sqlmodel import Session, SQLModel, create_engine


CONTROL_METADATA = MetaData()


class PortfolioControls(BaseModel):
    """Five operator controls used by the deterministic risk gate."""

    model_config = ConfigDict(validate_assignment=True)

    invested_target_pct: float = Field(default=60.0, ge=0.0, le=95.0)
    invested_tolerance_pct: float = Field(default=5.0, ge=0.0, le=20.0)
    agent_budget_pct: float = Field(default=30.0, ge=0.0, le=95.0)
    agent_budget_tolerance_pct: float = Field(default=5.0, ge=0.0, le=20.0)
    max_position_pct: float = Field(default=12.0, gt=0.0, le=30.0)
    per_trade_risk_pct: float = Field(default=1.0, gt=0.0, le=1.6)
    max_new_positions_per_day: int = Field(default=3, ge=1, le=10)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _coherent(self) -> "PortfolioControls":
        # Avoid validate_assignment recursively re-entering this model validator.
        object.__setattr__(self, "base_currency", self.base_currency.upper())
        if self.invested_target_pct + self.invested_tolerance_pct > 95.0:
            raise ValueError("invested target plus tolerance must preserve at least 5% cash")
        if self.agent_budget_pct + self.agent_budget_tolerance_pct > (
            self.invested_target_pct + self.invested_tolerance_pct
        ):
            raise ValueError("agent budget must fit inside the total invested allocation")
        if self.max_position_pct > self.agent_budget_pct + self.agent_budget_tolerance_pct:
            raise ValueError("single-position cap cannot exceed the agent allocation ceiling")
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")
        return self

    @property
    def invested_ceiling_pct(self) -> float:
        return self.invested_target_pct + self.invested_tolerance_pct

    @property
    def agent_ceiling_pct(self) -> float:
        return self.agent_budget_pct + self.agent_budget_tolerance_pct

    @property
    def cash_reserve_floor_pct(self) -> float:
        return 100.0 - self.invested_ceiling_pct

    def public_dict(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "invested_ceiling_pct": self.invested_ceiling_pct,
            "agent_ceiling_pct": self.agent_ceiling_pct,
            "cash_reserve_floor_pct": self.cash_reserve_floor_pct,
        }


class _ControlRow(SQLModel, table=True):
    metadata = CONTROL_METADATA
    __tablename__ = "portfolio_controls"

    id: str = SqlField(default="default", primary_key=True)
    payload_json: str


class PortfolioControlStore:
    def __init__(self, url: str = "sqlite:///trader.db") -> None:
        import json

        self._json = json
        self._engine = create_engine(
            url,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        CONTROL_METADATA.create_all(self._engine)

    def get(self) -> PortfolioControls:
        with Session(self._engine) as session:
            row = session.get(_ControlRow, "default")
            if row is None:
                value = PortfolioControls()
                session.add(
                    _ControlRow(
                        id="default",
                        payload_json=self._json.dumps(value.model_dump(mode="json")),
                    )
                )
                session.commit()
                return value
            return PortfolioControls.model_validate(self._json.loads(row.payload_json))

    def update(self, value: PortfolioControls | dict[str, Any]) -> PortfolioControls:
        current = self.get()
        if isinstance(value, PortfolioControls):
            updated = value
        else:
            merged = current.model_dump()
            merged.update(value)
            merged["updated_at"] = datetime.now(timezone.utc)
            updated = PortfolioControls.model_validate(merged)
        with Session(self._engine) as session:
            row = session.get(_ControlRow, "default") or _ControlRow(
                id="default", payload_json="{}"
            )
            row.payload_json = self._json.dumps(updated.model_dump(mode="json"))
            session.add(row)
            session.commit()
        return updated
