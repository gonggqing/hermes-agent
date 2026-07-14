import { describe, expect, it } from "vitest";

import { financeEn } from "@/i18n/en";
import { fmtPnlPct } from "./format";
import {
  EMPTY_TRADE_FORM,
  eventTypeLabel,
  fmtCashAmount,
  fmtCostBasis,
  parseDraftEdits,
  parseMarkPrice,
  parseTradeForm,
  priceSourceLabel,
  priceSourceTone,
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
      { qty: "12", price: "", commission: "0", note: "adjust", occurredAt: "" },
      ft,
    );
    expect(res).toEqual({ qty: 12, commission: 0, note: "adjust" });
  });

  it("sets occurred_at from an edited trade date (unblocks a draft missing time)", () => {
    const res = parseDraftEdits(
      { qty: "", price: "", commission: "", note: "", occurredAt: "2026-07-14" },
      ft,
    );
    expect(res).toHaveProperty("occurred_at");
    expect((res as { occurred_at: string }).occurred_at).toContain("2026-07-14");
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

describe("parseMarkPrice", () => {
  it("accepts a positive price", () => {
    expect(parseMarkPrice("4.744", ft)).toBe(4.744);
  });

  it("rejects a blank price", () => {
    expect(parseMarkPrice("", ft)).toBe(ft.portfolio.valuation.markErrPrice);
    expect(parseMarkPrice("   ", ft)).toBe(ft.portfolio.valuation.markErrPrice);
  });

  it("rejects a non-positive or non-numeric price", () => {
    expect(parseMarkPrice("0", ft)).toBe(ft.portfolio.valuation.markErrPrice);
    expect(parseMarkPrice("-3", ft)).toBe(ft.portfolio.valuation.markErrPrice);
    expect(parseMarkPrice("abc", ft)).toBe(ft.portfolio.valuation.markErrPrice);
  });
});

describe("priceSourceLabel", () => {
  it("localizes each known source", () => {
    expect(priceSourceLabel("live", ft)).toBe(ft.portfolio.valuation.sources.live);
    expect(priceSourceLabel("csv", ft)).toBe(ft.portfolio.valuation.sources.csv);
    expect(priceSourceLabel("manual", ft)).toBe(
      ft.portfolio.valuation.sources.manual,
    );
    expect(priceSourceLabel("none", ft)).toBe(ft.portfolio.valuation.sources.none);
  });

  it("gives each source a badge tone", () => {
    expect(priceSourceTone("live")).toBe("success");
    expect(priceSourceTone("none")).toBe("secondary");
  });
});

describe("fmtPnlPct", () => {
  it("renders a fraction as a signed percentage", () => {
    expect(fmtPnlPct(0.2)).toBe("+20.0%");
    expect(fmtPnlPct(-0.011)).toBe("-1.1%");
  });

  it("renders a dash for an unknown P&L%", () => {
    expect(fmtPnlPct(null)).toBe("—");
  });
});
