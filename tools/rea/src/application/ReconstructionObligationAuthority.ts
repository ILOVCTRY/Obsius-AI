import type { Evidence } from "../domain/evidence.js";
import type { ReconstructionObligation } from "../domain/reconstructionObligationLedgerSchemas.js";

type ProofAuthority = ReconstructionObligation["required_fixture_authority"];
type OriginalAuthority =
  ReconstructionObligation["required_original_authority"];
interface ExpectedProof {
  readonly obligationId: string;
  readonly fixtureId?: string;
  readonly caseKind?: string;
  readonly verifierId?: string;
  readonly claimId?: string;
}

const proofEvidenceAuthorities: Readonly<
  Record<ProofAuthority, readonly Evidence["authority"][]>
> = {
  unit: ["shipped-artifact", "controlled-replay"],
  integration: ["controlled-replay"],
  protocol: ["controlled-replay", "external-service"],
  renderer: ["controlled-replay", "external-service"],
  "packaged-process": ["controlled-replay"],
  "native-abi": ["shipped-artifact", "controlled-replay"],
  "live-observation": ["controlled-replay", "external-service"],
  external: ["external-service"],
};

/** Return whether an actual reconstruction proof is comparable to a requirement. */
export const proofAuthoritySatisfies = (
  actual: ProofAuthority,
  required: ProofAuthority,
): boolean => {
  const accepted: Readonly<Record<ProofAuthority, readonly ProofAuthority[]>> =
    {
      unit: [
        "unit",
        "integration",
        "protocol",
        "renderer",
        "packaged-process",
        "native-abi",
        "live-observation",
        "external",
      ],
      integration: [
        "integration",
        "protocol",
        "renderer",
        "packaged-process",
        "native-abi",
        "live-observation",
      ],
      protocol: ["protocol", "packaged-process", "live-observation"],
      renderer: ["renderer", "packaged-process", "live-observation"],
      "packaged-process": ["packaged-process", "live-observation"],
      "native-abi": ["native-abi"],
      "live-observation": ["live-observation"],
      external: ["external"],
    };
  return accepted[required].includes(actual);
};

/** Check that Evidence can authenticate the declared reconstruction authority. */
export const evidenceAuthoritySupportsProof = (
  evidence: Evidence,
  authority: ProofAuthority,
  expected: ExpectedProof,
): boolean => {
  if (
    evidence.confidence !== "observed" ||
    !proofEvidenceAuthorities[authority].includes(evidence.authority) ||
    evidence.predicate_type !== "rea.reconstruction-proof" ||
    evidence.operation !== "verify_reconstruction_obligations"
  )
    return false;
  const result = evidence.normalized_result;
  if (typeof result !== "object" || result === null || Array.isArray(result))
    return false;
  const includes = (key: string, value: string | undefined): boolean =>
    value === undefined ||
    (Array.isArray(result[key]) && result[key].includes(value));
  return (
    result.passed === true &&
    includes("obligation_ids", expected.obligationId) &&
    includes("fixture_ids", expected.fixtureId) &&
    includes("case_kinds", expected.caseKind) &&
    includes("verifier_ids", expected.verifierId) &&
    includes("claim_ids", expected.claimId)
  );
};

/** Check that Evidence observes the original at the required boundary. */
export const evidenceSupportsOriginalAuthority = (
  evidence: Evidence,
  authority: OriginalAuthority,
): boolean => {
  if (evidence.confidence !== "observed") return false;
  if (authority === "static") return evidence.authority === "shipped-artifact";
  if (authority === "runtime")
    return ["controlled-replay", "external-service"].includes(
      evidence.authority,
    );
  if (authority === "process")
    return (
      evidence.operation === "capture_process_scenario" &&
      evidence.predicate_type === "rea.process-capture"
    );
  return evidence.authority === "external-service";
};
