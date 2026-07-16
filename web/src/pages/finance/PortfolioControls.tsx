import { useEffect, useMemo, useState } from "react";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { api } from "@/lib/api";
import type {
  FinancePortfolioControls,
  FinancePortfolioControlsUpdate,
} from "@/lib/api";
import { useFinanceT } from "@/pages/finance/i18n";

type NumericKey = Exclude<
  keyof FinancePortfolioControlsUpdate,
  "base_currency"
>;

interface ControlField {
  key: NumericKey;
  label: string;
  max: number;
  min: number;
  step: number;
  suffix: string;
}

export function PortfolioControls() {
  const ft = useFinanceT();
  const copy = ft.portfolioControls;
  const [value, setValue] = useState<FinancePortfolioControls | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    void api.financePortfolioControls().then(
      (next) => {
        if (alive) setValue(next);
      },
      () => {
        if (alive) setError(copy.loadError);
      },
    );
    return () => {
      alive = false;
    };
  }, [copy.loadError]);

  const fields = useMemo<ControlField[]>(
    () => [
      {
        key: "invested_target_pct",
        label: copy.investedTarget,
        min: 0,
        max: 95,
        step: 1,
        suffix: "%",
      },
      {
        key: "invested_tolerance_pct",
        label: copy.investedTolerance,
        min: 0,
        max: 20,
        step: 1,
        suffix: "%",
      },
      {
        key: "agent_budget_pct",
        label: copy.agentBudget,
        min: 0,
        max: 95,
        step: 1,
        suffix: "%",
      },
      {
        key: "agent_budget_tolerance_pct",
        label: copy.agentTolerance,
        min: 0,
        max: 20,
        step: 1,
        suffix: "%",
      },
      {
        key: "max_position_pct",
        label: copy.maxPosition,
        min: 0.1,
        max: 30,
        step: 0.5,
        suffix: "%",
      },
      {
        key: "per_trade_risk_pct",
        label: copy.perTradeRisk,
        min: 0.1,
        max: 1.6,
        step: 0.1,
        suffix: "%",
      },
      {
        key: "max_new_positions_per_day",
        label: copy.maxNewPositions,
        min: 1,
        max: 10,
        step: 1,
        suffix: "",
      },
    ],
    [copy],
  );

  const change = (key: NumericKey, raw: string) => {
    const number = Number(raw);
    if (!Number.isFinite(number)) return;
    setValue((current) => (current ? { ...current, [key]: number } : current));
    setError(null);
    setMessage(null);
  };

  const save = async () => {
    if (!value) return;
    setSaving(true);
    setError(null);
    setMessage(null);
    const body: FinancePortfolioControlsUpdate = {
      invested_target_pct: value.invested_target_pct,
      invested_tolerance_pct: value.invested_tolerance_pct,
      agent_budget_pct: value.agent_budget_pct,
      agent_budget_tolerance_pct: value.agent_budget_tolerance_pct,
      max_position_pct: value.max_position_pct,
      per_trade_risk_pct: value.per_trade_risk_pct,
      max_new_positions_per_day: value.max_new_positions_per_day,
      base_currency: value.base_currency,
    };
    try {
      setValue(await api.financeUpdatePortfolioControls(body));
      setMessage(copy.saved);
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : String(reason);
      setError(copy.saveError.replace("{error}", detail));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{copy.title}</CardTitle>
        <p className="font-mondwest normal-case text-sm text-muted-foreground">
          {copy.description}
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {value ? (
          <>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {fields.map((field) => (
                <label className="space-y-1.5" key={field.key}>
                  <span className="block font-mondwest normal-case text-xs text-muted-foreground">
                    {field.label}
                  </span>
                  <div className="relative">
                    <Input
                      aria-label={field.label}
                      className={field.suffix ? "pr-8" : undefined}
                      max={field.max}
                      min={field.min}
                      onChange={(event) =>
                        change(field.key, event.target.value)
                      }
                      step={field.step}
                      type="number"
                      value={value[field.key]}
                    />
                    {field.suffix && (
                      <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
                        {field.suffix}
                      </span>
                    )}
                  </div>
                </label>
              ))}
              <label className="space-y-1.5">
                <span className="block font-mondwest normal-case text-xs text-muted-foreground">
                  {copy.baseCurrency}
                </span>
                <Input
                  aria-label={copy.baseCurrency}
                  disabled
                  value={value.base_currency}
                />
              </label>
            </div>

            <div className="grid gap-3 border-y border-border py-4 sm:grid-cols-2">
              <div>
                <p className="font-mondwest normal-case text-xs text-muted-foreground">
                  {copy.cashReserve}
                </p>
                <p className="font-mono-ui text-lg">
                  {Math.max(
                    0,
                    100 -
                      value.invested_target_pct -
                      value.invested_tolerance_pct,
                  ).toFixed(1)}
                  %
                </p>
              </div>
              <div>
                <p className="font-mondwest normal-case text-xs text-muted-foreground">
                  {copy.agentCeiling}
                </p>
                <p className="font-mono-ui text-lg">
                  {(
                    value.agent_budget_pct + value.agent_budget_tolerance_pct
                  ).toFixed(1)}
                  %
                </p>
              </div>
            </div>

            <div className="space-y-1 font-mondwest normal-case text-xs text-muted-foreground">
              <p>{copy.subsetNote}</p>
              <p>{copy.currencyNote}</p>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            {message && <p className="text-sm text-primary">{message}</p>}
            <Button disabled={saving} onClick={() => void save()}>
              {saving ? copy.saving : copy.save}
            </Button>
          </>
        ) : (
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {error ?? copy.loadError}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
