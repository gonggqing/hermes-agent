import { useRef, useState } from "react";
import { Check, ListChecks, Pencil, X } from "lucide-react";
import { api } from "@/lib/api";
import type {
  FinanceActionOutcome,
  FinanceCandidate,
  FinanceCandidateEdits,
  FinancePendingCandidate,
} from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { FinanceTranslations } from "@/i18n/types";
import { useFinanceT } from "./i18n";
import { FINANCE_ACTOR, fmtMoney, fmtQty, fmtTs, sideTone } from "./format";

/** Editable candidate fields (Loop.md §5.6: edits limited to these). */
const EDIT_FIELDS = ["qty", "limit", "stop", "tp", "sl"] as const;
type EditField = (typeof EDIT_FIELDS)[number];
type EditDraft = Record<EditField, string>;

function draftFrom(c: FinanceCandidate): EditDraft {
  return {
    qty: String(c.qty),
    limit: c.limit === null ? "" : String(c.limit),
    stop: c.stop === null ? "" : String(c.stop),
    tp: c.tp === null ? "" : String(c.tp),
    sl: c.sl === null ? "" : String(c.sl),
  };
}

/** Parse the draft into an edits payload; returns a localized error on bad input. */
function parseDraft(
  draft: EditDraft,
  ft: FinanceTranslations,
): FinanceCandidateEdits | string {
  const edits: FinanceCandidateEdits = {};
  for (const field of EDIT_FIELDS) {
    const raw = draft[field].trim();
    if (raw === "") continue; // untouched/cleared optional price — omit
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) {
      return ft.queue.errPositive.replace("{field}", ft.queue.fields[field]);
    }
    edits[field] = n;
  }
  if (edits.qty === undefined) return ft.queue.errQtyRequired;
  return edits;
}

function PriceCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-text-tertiary">{label}</span>
      <span className="font-mondwest normal-case text-sm text-foreground">
        {value}
      </span>
    </div>
  );
}

function CandidateCard({
  pending,
  busy,
  onAct,
  ft,
}: {
  pending: FinancePendingCandidate;
  busy: boolean;
  onAct: (
    pending: FinancePendingCandidate,
    action: "approve" | "reject" | "edit",
    edits?: FinanceCandidateEdits,
  ) => void;
  ft: FinanceTranslations;
}) {
  const c = pending.candidate;
  const windowOpen = pending.window_open;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<EditDraft>(() => draftFrom(c));
  const [draftError, setDraftError] = useState<string | null>(null);
  const disabled = busy || !windowOpen;
  const windowClosedHint = ft.queue.windowClosedHint;
  const disabledHint = !windowOpen ? windowClosedHint : undefined;

  const saveAndApprove = () => {
    const edits = parseDraft(draft, ft);
    if (typeof edits === "string") {
      setDraftError(edits);
      return;
    }
    setDraftError(null);
    onAct(pending, "edit", edits);
  };

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono-ui text-base font-semibold text-foreground">
            {c.symbol}
          </span>
          <Badge tone={sideTone(c.side)}>{c.side}</Badge>
          <Badge tone="outline">{c.order_type}</Badge>
          <Badge tone="secondary">{c.pool}</Badge>
          {!windowOpen && <Badge tone="warning">{ft.queue.windowClosed}</Badge>}
          <span className="ml-auto font-mondwest normal-case text-xs text-muted-foreground">
            {ft.queue.confidenceVersion
              .replace("{pct}", (c.confidence * 100).toFixed(0))
              .replace("{version}", String(pending.version))}
          </span>
        </div>

        {editing ? (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {EDIT_FIELDS.map((field) => (
              <label key={field} className="flex flex-col gap-1">
                <span className="text-xs text-text-tertiary">
                  {ft.queue.fields[field]}
                </span>
                <Input
                  type="number"
                  step="0.01"
                  min="0"
                  value={draft[field]}
                  onChange={(e) =>
                    setDraft((d) => ({ ...d, [field]: e.target.value }))
                  }
                />
              </label>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            <PriceCell label={ft.queue.fields.qty} value={fmtQty(c.qty)} />
            <PriceCell label={ft.queue.fields.limit} value={fmtMoney(c.limit)} />
            <PriceCell label={ft.queue.fields.stop} value={fmtMoney(c.stop)} />
            <PriceCell label={ft.queue.fields.tp} value={fmtMoney(c.tp)} />
            <PriceCell label={ft.queue.fields.sl} value={fmtMoney(c.sl)} />
          </div>
        )}
        {draftError && (
          <p className="text-xs text-destructive">{draftError}</p>
        )}

        <p className="font-mondwest normal-case text-sm text-muted-foreground">
          {c.rationale || ft.queue.noRationale}
        </p>
        {c.risk_note && (
          <p className="font-mondwest normal-case text-xs text-warning">
            {ft.queue.riskNote.replace("{note}", c.risk_note)}
          </p>
        )}
        <p className="font-mondwest normal-case text-xs text-text-tertiary">
          {ft.queue.metaLine
            .replace("{ref}", fmtMoney(c.ref_px))
            .replace("{valid}", fmtTs(c.valid_until))
            .replace("{proposed}", fmtTs(c.ts))}
        </p>

        <div className="flex flex-wrap items-center gap-2" title={disabledHint}>
          {editing ? (
            <>
              <Button
                type="button"
                size="sm"
                disabled={disabled}
                onClick={saveAndApprove}
                prefix={busy ? <Spinner /> : <Check />}
              >
                {ft.queue.saveApprove}
              </Button>
              <Button
                type="button"
                size="sm"
                outlined
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                  setDraft(draftFrom(c));
                  setDraftError(null);
                }}
                prefix={<X />}
              >
                {ft.queue.cancel}
              </Button>
            </>
          ) : (
            <>
              <Button
                type="button"
                size="sm"
                disabled={disabled}
                onClick={() => onAct(pending, "approve")}
                prefix={busy ? <Spinner /> : <Check />}
              >
                {ft.queue.approve}
              </Button>
              <Button
                type="button"
                size="sm"
                destructive
                outlined
                disabled={disabled}
                onClick={() => onAct(pending, "reject")}
                prefix={<X />}
              >
                {ft.queue.reject}
              </Button>
              <Button
                type="button"
                size="sm"
                ghost
                disabled={disabled}
                onClick={() => setEditing(true)}
                prefix={<Pencil />}
              >
                {ft.queue.edit}
              </Button>
            </>
          )}
          {!windowOpen && (
            <span className="font-mondwest normal-case text-xs text-muted-foreground">
              {windowClosedHint}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * The confirmation queue (Loop.md §5.6): pending candidates as cards with
 * Approve / Reject / Edit ("Save & approve") actions posted to the
 * server-authoritative ConfirmationService with the web surface header,
 * expected version, and a per-user-action idempotency key.
 */
export function ApprovalQueue({
  pending,
  onActed,
  showToast,
}: {
  pending: FinancePendingCandidate[];
  onActed: () => void;
  showToast: (message: string, type: "error" | "success") => void;
}) {
  const ft = useFinanceT();
  const [busyKey, setBusyKey] = useState<string | null>(null);
  // One idempotency key per user action (candidate+action), created on the
  // first click and reused on retry after a network failure; dropped as
  // soon as the service responds (any status) so a later distinct action
  // gets a fresh key.
  const keysRef = useRef<Map<string, string>>(new Map());

  const act = async (
    pc: FinancePendingCandidate,
    action: "approve" | "reject" | "edit",
    edits?: FinanceCandidateEdits,
  ) => {
    const c = pc.candidate;
    const actionKey = `${c.id}:${action}`;
    let idem = keysRef.current.get(actionKey);
    if (!idem) {
      idem = crypto.randomUUID();
      keysRef.current.set(actionKey, idem);
    }
    setBusyKey(actionKey);
    try {
      const outcome = await api.financeCandidateAction(c.id, {
        action,
        actor: FINANCE_ACTOR,
        idempotency_key: idem,
        expected_version: pc.version,
        ...(edits ? { edits } : {}),
      });
      keysRef.current.delete(actionKey);
      renderOutcome(c, action, outcome);
    } catch (err) {
      // Network/proxy failure — keep the key so a retry replays safely.
      showToast(
        ft.queue.outcome.requestFailed
          .replace("{symbol}", c.symbol)
          .replace("{error}", String(err)),
        "error",
      );
    } finally {
      setBusyKey(null);
    }
  };

  const renderOutcome = (
    c: FinanceCandidate,
    action: "approve" | "reject" | "edit",
    outcome: FinanceActionOutcome,
  ) => {
    const o = ft.queue.outcome;
    const verb =
      action === "approve"
        ? ft.queue.verbApproved
        : action === "reject"
          ? ft.queue.verbRejected
          : ft.queue.verbEdited;
    switch (outcome.code) {
      case "applied":
        showToast(
          o.applied.replace("{symbol}", c.symbol).replace("{verb}", verb),
          "success",
        );
        onActed();
        break;
      case "replayed":
        showToast(o.replayed.replace("{symbol}", c.symbol), "success");
        onActed();
        break;
      case "window_closed":
        showToast(
          o.windowClosed
            .replace("{symbol}", c.symbol)
            .replace("{hint}", ft.queue.windowClosedHint),
          "error",
        );
        onActed();
        break;
      case "version_conflict":
        showToast(o.versionConflict.replace("{symbol}", c.symbol), "error");
        onActed();
        break;
      case "terminal":
        showToast(
          o.terminal
            .replace("{symbol}", c.symbol)
            .replace("{message}", outcome.message || o.terminalState),
          "error",
        );
        onActed();
        break;
      case "unknown_candidate":
        showToast(o.unknownCandidate.replace("{symbol}", c.symbol), "error");
        onActed();
        break;
      case "invalid_edit":
      case "invalid_action":
        showToast(
          o.invalid
            .replace("{symbol}", c.symbol)
            .replace("{message}", outcome.message || o.invalidFallback),
          "error",
        );
        break;
      case "service_unavailable":
        showToast(
          o.serviceUnavailable.replace("{message}", outcome.message ?? ""),
          "error",
        );
        break;
      default:
        showToast(
          o.unexpected
            .replace("{symbol}", c.symbol)
            .replace(
              "{message}",
              outcome.message ||
                o.unexpectedFallback.replace(
                  "{status}",
                  String(outcome.status),
                ),
            ),
          "error",
        );
        break;
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <ListChecks className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.queue.approvalTitle}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {pending.length === 0 ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            {ft.queue.noPending}
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {pending.map((pc) => (
              <CandidateCard
                key={pc.candidate.id}
                pending={pc}
                busy={busyKey !== null && busyKey.startsWith(`${pc.candidate.id}:`)}
                onAct={(p, action, edits) => void act(p, action, edits)}
                ft={ft}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
