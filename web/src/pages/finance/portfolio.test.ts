import { describe, expect, it } from "vitest";

import { financeEn } from "@/i18n/en";
import {
  EMPTY_TRADE_FORM,
  eventTypeLabel,
  fmtCashAmount,
  fmtCostBasis,
  parseDraftEdits,
  parseTradeForm,
  type SelectedInstrument,
} from "./portfolio";

const ft = financeEn;
const INSTRUMENT: SelectedInstrument = {
  symbol: "AAPL",
  market: "US",
  currency: "USD",
};

describe("parseTradeForm", () => {
  it("requires a selected instrument", () => {
    const res = parseTradeForm({ ...EMPTY_TRADE_FORM, qty: "10" }, null, ft);
    expect(res).toBe(ft.portfolio.record.errInstrument);
  });

  it("rejects a non-positive quantity", () => {
    const res = parseTradeForm(
      { ...EMPTY_TRADE_FORM, qty: "0" },
      INSTRUMENT,
      ft,
    );
    expect(res).toBe(ft.portfolio.record.errQty);
  });

  it("builds a payload with the instrument fields and a positive qty", () => {
    const res = parseTradeForm(
      { ...EMPTY_TRADE_FORM, eventType: "buy", qty: "10", price: "150.5" },
      INSTRUMENT,
      ft,
    );
    expect(res).toEqual({
      event_type: "buy",
      symbol: "AAPL",
      market: "US",
      currency: "USD",
      qty: 10,
      price: 150.5,
    });
  });

  it("omits a blank price (unknown cost) rather than coercing it to 0", () => {
    const res = parseTradeForm(
      { ...EMPTY_TRADE_FORM, qty: "5", price: "" },
      INSTRUMENT,
      ft,
    );
    expect(typeof res).not.toBe("string");
    if (typeof res !== "string") {
      expect("price" in res).toBe(false);
    }
  });

  it("rejects a negative commission", () => {
    const res = parseTradeForm(
      { ...EMPTY_TRADE_FORM, qty: "5", commission: "-1" },
      INSTRUMENT,
      ft,
    );
    expect(res).toBe(ft.portfolio.record.errCommission);
  });

  it("normalizes occurred_at to an ISO timestamp", () => {
    const res = parseTradeForm(
      { ...EMPTY_TRADE_FORM, qty: "5", occurredAt: "2026-01-02T09:30" },
      INSTRUMENT,
      ft,
    );
    if (typeof res === "string") throw new Error(res);
    expect(res.occurred_at).toBe(new Date("2026-01-02T09:30").toISOString());
  });
});

describe("parseDraftEdits", () => {
  it("returns an empty edits object when nothing is touched", () => {
    const res = parseDraftEdits(
      { qty: "", price: "", commission: "", note: "" },
      ft,
    );
    expect(res).toEqual({});
  });

  it("rejects an invalid price", () => {
    const res = parseDraftEdits(
      { qty: "", price: "-3", commission: "", note: "" },
      ft,
    );
    expect(res).toBe(ft.portfolio.draftsView.errPrice);
  });

  it("collects only the touched fields", () => {
    const res = parseDraftEdits(
      { qty: "12", price: "", commission: "0", note: "adjust" },
      ft,
    );
    expect(res).toEqual({ qty: 12, commission: 0, note: "adjust" });
  });
});

describe("cost-basis / cash formatting", () => {
  it("renders a localized unknown when the cost basis is not known", () => {
    expect(fmtCostBasis(0, false, ft)).toBe(ft.portfolio.holdings.unknownCost);
    expect(fmtCostBasis(null, true, ft)).toBe(ft.portfolio.holdings.unknownCost);
  });

  it("renders the number when the cost basis is known", () => {
    expect(fmtCostBasis(150.5, true, ft)).toBe("150.50");
  });

  it("renders a localized unknown for an unknown cash amount", () => {
    expect(fmtCashAmount(null, false, ft)).toBe(
      ft.portfolio.holdings.unknownCash,
    );
  });
});

describe("eventTypeLabel", () => {
  it("localizes a known event type", () => {
    expect(eventTypeLabel("buy", ft)).toBe(ft.portfolio.eventTypes.buy);
  });

  it("falls back to the raw enum value for an unknown event type", () => {
    expect(eventTypeLabel("some_new_type", ft)).toBe("some_new_type");
  });
});
