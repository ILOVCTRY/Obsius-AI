import type { EvidenceWriter } from "../investigation/InvestigationRecordPort.js";
import { createEvidence, type Evidence } from "../../domain/evidence.js";
import type { AnalysisError } from "../../domain/analysisErrorBase.js";
import type { JsonValue } from "../../domain/jsonValue.js";
import { ok, type Result } from "../../domain/result.js";

/**
 * Complete class-inventory pages dominate raw-call provenance for focused
 * searches: one traversal embeds every examined class name regardless of the
 * match count. The summary keeps the pagination audit inline (totals, page
 * offsets and sizes, elided item counts) and links the retained record that
 * still carries the complete inventory.
 */
const elideInventoryText = (text: string): string | undefined => {
  let page: JsonValue;
  try {
    page = JSON.parse(text) as JsonValue;
  } catch {
    return undefined;
  }
  if (
    typeof page !== "object" ||
    page === null ||
    Array.isArray(page) ||
    !Array.isArray(page.items)
  )
    return undefined;
  const { items, ...audited } = page;
  return JSON.stringify({ ...audited, items_omitted: items.length });
};

/** Replace inventoried page bodies with their pagination audit. */
const summarizeInventoryCall = (call: JsonValue): JsonValue => {
  if (
    typeof call !== "object" ||
    call === null ||
    Array.isArray(call) ||
    call.operation !== "list_classes"
  )
    return call;
  const response = call.response;
  if (
    typeof response !== "object" ||
    response === null ||
    Array.isArray(response) ||
    !Array.isArray(response.content)
  )
    return call;
  let elided = false;
  const content = response.content.map((entry: JsonValue) => {
    if (
      typeof entry !== "object" ||
      entry === null ||
      Array.isArray(entry) ||
      typeof entry.text !== "string"
    )
      return entry;
    const summarized = elideInventoryText(entry.text);
    if (summarized === undefined) return entry;
    elided = true;
    return { ...entry, text: summarized };
  });
  return elided
    ? { ...call, inventory_elided: true, response: { ...response, content } }
    : call;
};

/**
 * Retain the complete search Evidence — full raw provider calls included —
 * in the session record, then deliver a bounded summary Evidence inline.
 * Callers without a retaining session keep the complete inline delivery, so
 * complete CLI JSON output is unchanged. Coverage metadata stays truthful in
 * both copies: the examined inventory is never rewritten to matched-only.
 */
export const summarizeRetainedAndroidSearch = (
  evidence: Evidence,
  recordEvidence: EvidenceWriter["recordEvidence"] | undefined,
): Result<Evidence, AnalysisError> => {
  if (recordEvidence === undefined) return ok(evidence);
  const recorded = recordEvidence(evidence);
  if (!recorded.ok) return recorded;
  const subject = evidence.subject;
  const raw = evidence.raw_result;
  const summarized: JsonValue =
    raw === null ||
    typeof raw !== "object" ||
    Array.isArray(raw) ||
    !Array.isArray(raw.calls)
      ? raw
      : {
          ...raw,
          complete_evidence_id: evidence.evidence_id,
          calls: raw.calls.map(summarizeInventoryCall),
        };
  return ok(
    createEvidence(
      subject === null
        ? undefined
        : {
            path: subject.local_path,
            sha256: subject.digest.sha256,
            format: subject.format,
            ...(subject.architecture === null
              ? {}
              : { architecture: subject.architecture }),
          },
      evidence.provider,
      {
        operation: evidence.operation,
        parameters: evidence.parameters,
        result: evidence.normalized_result,
        rawResult: summarized,
        confidence: evidence.confidence,
        limitations: evidence.limitations,
        locations: evidence.locations,
        evidenceLinks: [evidence.evidence_id],
      },
    ),
  );
};
