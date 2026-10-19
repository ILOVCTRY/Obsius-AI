import type {
  JavaScriptSemanticEvidenceContext,
  JavaScriptSemanticFingerprint,
  JavaScriptSemanticGraphNode,
  JavaScriptSemanticGraphRelation,
  JavaScriptSemanticGraphUnknown,
} from "./javascriptSemanticGraphSchemas.js";

/** File-local facts transported before binding them to the containing application graph. */
export interface JavaScriptSemanticFileProjection {
  readonly roots: readonly string[];
  readonly evidenceContexts: readonly JavaScriptSemanticEvidenceContext[];
  readonly nodes: readonly JavaScriptSemanticGraphNode[];
  readonly relations: readonly JavaScriptSemanticGraphRelation[];
  readonly unknowns: readonly JavaScriptSemanticGraphUnknown[];
  readonly fingerprints: readonly JavaScriptSemanticFingerprint[];
  readonly truncated: boolean;
}
