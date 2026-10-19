import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readdir,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

if (process.platform !== "win32")
  throw new Error("Native inspection ownership requires real Windows NTFS");
const root = resolve(process.argv[2] ?? ".");
const artifact = resolve(
  process.argv[3] ?? ".cache/windows-native-inspection-fixture",
);
const workspace = await mkdtemp(join(tmpdir(), "rea-inspection-conformance-"));
try {
  // Isolate the fixture from the packaged production artifact and loader cache.
  await mkdir(join(workspace, "dist", "windows"), { recursive: true });
  await mkdir(join(workspace, "native", "windows", "build"), {
    recursive: true,
  });
  await writeFile(join(workspace, "package.json"), '{"type":"module"}');
  await copyFile(
    join(root, "dist", "windows", "WindowsNativeLoader.js"),
    join(workspace, "dist", "windows", "WindowsNativeLoader.js"),
  );
  await copyFile(
    join(root, "dist", "generatedPackageMetadata.js"),
    join(workspace, "dist", "generatedPackageMetadata.js"),
  );
  for (const name of ["manifest.json", "rea-windows-x64.node"])
    await copyFile(
      join(artifact, name),
      join(workspace, "native", "windows", "build", name),
    );
  const require = createRequire(join(root, "package.json"));
  await mkdir(join(workspace, "node_modules"));
  await symlink(
    dirname(require.resolve("zod/package.json")),
    join(workspace, "node_modules", "zod"),
    "junction",
  );
  const loader = pathToFileURL(
    join(workspace, "dist", "windows", "WindowsNativeLoader.js"),
  ).href;
  const source = `import assert from 'node:assert/strict';
import {readdirSync,renameSync,statSync} from 'node:fs';
import {join} from 'node:path';
import {systemWindowsNativeAuthority} from ${JSON.stringify(loader)};
const temp=process.env.TEMP;
const first=systemWindowsNativeAuthority();
assert.equal(first.available,false);
assert.equal(first.retryable,true);
assert.match(first.reason,/Delete owned object by handle failed.*Win32 5/s);
const [name,...extra]=readdirSync(temp);assert.ok(name);assert.equal(extra.length,0);
const path=join(temp,name);const identity=statSync(path,{bigint:true});
assert.throws(()=>renameSync(path,path+'-replacement'));
assert.equal(statSync(path,{bigint:true}).ino,identity.ino);
if(process.argv[2]!=='teardown'){
 const second=systemWindowsNativeAuthority();assert.equal(second.available,true);
 assert.equal(readdirSync(temp).length,0);
 assert.equal(systemWindowsNativeAuthority(),second);
}
console.log(JSON.stringify({mode:process.argv[2],firstFailed:true,retainedOwner:true,inode:identity.ino.toString()}));
`;
  const fixture = join(workspace, "probe.mjs");
  await writeFile(fixture, source);
  for (const mode of ["retry", "teardown"]) {
    const temp = join(workspace, mode);
    await mkdir(temp);
    const observed = JSON.parse(
      execFileSync(process.execPath, [fixture, mode], {
        encoding: "utf8",
        timeout: 20_000,
        windowsHide: true,
        env: { ...process.env, TEMP: temp, TMP: temp },
      }),
    );
    assert.equal(observed.retainedOwner, true);
    assert.deepEqual(await readdir(temp), []);
  }
  process.stdout.write(
    `${JSON.stringify({ ok: true, controlledDispositionFailure: true, retainedOwner: true, retryRecovers: true, successfulAdmissionCached: true, environmentTeardownCleansProbe: true, issue1701HostCauseReproduced: false })}\n`,
  );
} finally {
  await rm(workspace, { recursive: true, force: true });
}
