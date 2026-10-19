import type { DoctorProviderInspection } from "./Doctor.js";
import { compareUnicodeCodePoints } from "../domain/unicodeCodePointOrder.js";

/** Preserve adapter-published settings independently of runtime readiness. */
export const providerRegistrationEnvironment = (
  inspections: readonly DoctorProviderInspection[],
): Readonly<Record<string, string>> =>
  Object.fromEntries(
    inspections
      .flatMap(({ registrationEnvironment }) =>
        Object.entries(registrationEnvironment),
      )
      .sort(([left], [right]) => compareUnicodeCodePoints(left, right)),
  );
