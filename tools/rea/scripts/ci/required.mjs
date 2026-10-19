const jobs = JSON.parse(process.env.CI_JOBS);
const outputs = jobs.changes.outputs;
const required = ["changes", "static", "test"];
const lanes = {
  code: ["build", "test-shard"],
  runtime: ["windows-native-build", "package-e2e"],
  package: ["windows-curated"],
  apple: ["apple-artifacts"],
  fixtures: ["conformance-fixtures"],
  readiness: ["reconstruction-readiness"],
  docs: ["docs"],
};
for (const [scope, value] of Object.entries(outputs)) {
  if (value === "true") required.push(...(lanes[scope] ?? [scope]));
}
const failed = [...new Set(required)].filter(
  (name) => jobs[name]?.result !== "success",
);
for (const [name, job] of Object.entries(jobs)) {
  if (["failure", "cancelled"].includes(job.result) && !failed.includes(name))
    failed.push(name);
}
if (failed.length)
  throw new Error(`CI lanes did not pass: ${failed.join(", ")}`);
console.log("Every selected CI lane passed");
