import { expect, it } from "vitest";

import {
  analyzeParsedJavaScriptSemantics,
  analyzeParsedJavaScriptSemanticsSteps,
  classifyParsedJavaScriptElectronBindings,
  classifyParsedJavaScriptElectronBindingsSteps,
  classifyParsedJavaScriptOpenReceivers,
  classifyParsedJavaScriptOpenReceiversSteps,
} from "./javascriptSemanticAnalysis.js";
import { parseJavaScriptSource } from "./javascriptSourceParser.js";
import {
  traverseJavaScriptAst,
  traverseJavaScriptAstSteps,
} from "./javascriptSemanticTraversal.js";

const source = `
import { readFile } from "node:fs";
const shared = { token: "TOKEN" };
export function load(path) {
  return new Promise((resolve) => setTimeout(() => resolve(readFile(path)), 1));
}
export const box = { get() { return shared; } };
box.get().value = process.env.MODE ?? "default";
`;

it("yields between semantic phases and returns the synchronous result (#1462)", () => {
  const parsed = parseJavaScriptSource(source, "main.js");
  if (parsed === null) throw new Error("Expected parsed source");
  const steps = analyzeParsedJavaScriptSemanticsSteps(parsed);
  let yields = 0;
  let step = steps.next();
  while (step.done !== true) {
    yields += 1;
    step = steps.next();
  }
  expect(yields).toBeGreaterThan(0);
  const reparsed = parseJavaScriptSource(source, "main.js");
  if (reparsed === null) throw new Error("Expected parsed source");
  expect(step.value).toEqual(analyzeParsedJavaScriptSemantics(reparsed));
});

it("traverses a large tree in steps with the synchronous visit order (#1462)", () => {
  const parsed = parseJavaScriptSource(
    Array.from(
      { length: 400 },
      (_, index) => `const v${index} = [${index}];`,
    ).join("\n"),
    "large.js",
  );
  if (parsed === null) throw new Error("Expected parsed source");
  const visits = (record: string[]) => ({
    enter: (node: { readonly type: string }) => record.push(`+${node.type}`),
    exit: (node: { readonly type: string }) => record.push(`-${node.type}`),
  });
  const synchronous: string[] = [];
  traverseJavaScriptAst(parsed.program, visits(synchronous));
  const stepped: string[] = [];
  const steps = traverseJavaScriptAstSteps(parsed.program, visits(stepped));
  let yields = 0;
  while (steps.next().done !== true) yields += 1;
  expect(yields).toBeGreaterThan(0);
  expect(stepped).toEqual(synchronous);
});

it("skips child subtrees without changing sibling ancestry or exit callbacks", () => {
  const parsed = parseJavaScriptSource("[1, [2], 3]", "selection.js");
  if (parsed === null) throw new Error("Expected parsed source");
  const entered: number[] = [];
  const exited: number[] = [];
  const parents: string[][] = [];
  const steps = traverseJavaScriptAstSteps(parsed.program, {
    skip: (node) => node.type === "ArrayExpression" && node.start === 4,
    enter: (node, parent, readAncestors) => {
      if (node.type !== "NumericLiteral") return;
      entered.push(node.value);
      expect(readAncestors().at(-1)).toBe(parent);
      parents.push(readAncestors().map(({ type }) => type));
    },
    exit: (node) => {
      if (node.type === "NumericLiteral") exited.push(node.value);
    },
  });
  while (steps.next().done !== true) {
    // Complete the same cooperative traversal used by fingerprint collection.
  }
  expect(entered).toEqual([1, 3]);
  expect(exited).toEqual([1, 3]);
  expect(parents).toEqual([
    ["Program", "ExpressionStatement", "ArrayExpression"],
    ["Program", "ExpressionStatement", "ArrayExpression"],
  ]);
});

it("preserves receiver and Electron alias scope across traversal pauses (#1462)", () => {
  const padding = Array.from(
    { length: 400 },
    (_, index) => `const v${index} = [${index}];`,
  ).join("\n");
  const text = `
import { BrowserWindow as NativeWindow } from "electron";
const ambient = window;
export function local(ambient, NativeWindow) {
  ${padding}
  ambient.open("/local");
  new NativeWindow();
}
ambient.open("/global");
new NativeWindow();
`;
  const parsed = parseJavaScriptSource(text, "receivers.js");
  if (parsed === null) throw new Error("Expected parsed source");
  const openSteps = classifyParsedJavaScriptOpenReceiversSteps(parsed);
  let openStep = openSteps.next();
  expect(openStep.done).toBe(false);
  while (openStep.done !== true) openStep = openSteps.next();
  expect([...openStep.value]).toEqual([
    [text.indexOf('ambient.open("/local")'), "local"],
    [text.indexOf('ambient.open("/global")'), "window"],
  ]);
  expect(openStep.value).toEqual(classifyParsedJavaScriptOpenReceivers(parsed));

  const electronSteps = classifyParsedJavaScriptElectronBindingsSteps(parsed);
  let electronStep = electronSteps.next();
  expect(electronStep.done).toBe(false);
  while (electronStep.done !== true) electronStep = electronSteps.next();
  expect([...electronStep.value]).toEqual([
    [text.lastIndexOf("NativeWindow()"), ["BrowserWindow"]],
  ]);
  expect(electronStep.value).toEqual(
    classifyParsedJavaScriptElectronBindings(parsed),
  );
});

it("resolves Electron APIs through proven immutable aliases", () => {
  const text = `
const {
  BrowserWindow: ImportedWindow,
  contextBridge: ImportedBridge,
  ipcMain: ImportedMain,
  ipcRenderer: ImportedRenderer,
} = require("electron");
const Win = ImportedWindow;
const bridge = ImportedBridge;
const ipc = ImportedMain;
const renderer = ImportedRenderer;
new Win({ webPreferences: { preload: "./preload.js" } });
bridge.exposeInMainWorld("api", {});
ipc.handle("main:ping", () => "pong");
renderer.invoke("main:ping");

const electron = require("electron");
const namespace = electron;
const directMember = electron.ipcMain;
directMember.handle("direct-namespace:ping", () => "pong");
const member = namespace.ipcMain;
member.handle("namespace:ping", () => "pong");
require("electron").ipcMain.handle("direct-require:ping", () => "pong");
require("electron").notAnApi.ipcMain.handle("nested-lookalike", () => "local");
require("electron")["ipcMain.handle"]("dotted-lookalike", () => "local");

function shadow(ipc) {
  ipc.handle("shadowed:ping", () => "local");
}
const fakeElectron = require("./fake-electron");
const fakeIpc = fakeElectron.ipcMain;
fakeIpc.handle("lookalike:ping", () => "local");
let reassigned = ImportedMain;
reassigned = fakeIpc;
reassigned.handle("reassigned:ping", () => "local");
let { ipcMain: reassignedImport } = require("electron");
reassignedImport = fakeIpc;
reassignedImport.handle("reassigned-import:ping", () => "local");
let transitive = ImportedMain;
transitive = fakeIpc;
const reassignedAlias = transitive;
reassignedAlias.handle("transitive-reassigned:ping", () => "local");
if (globalThis.register) {
  var conditional = require("electron").ipcMain;
}
const conditionalAlias = conditional;
conditionalAlias.handle("conditional-alias:ping", () => "local");
`;
  const parsed = parseJavaScriptSource(text, "aliases.js");
  if (parsed === null) throw new Error("Expected parsed source");

  expect([...classifyParsedJavaScriptElectronBindings(parsed)]).toEqual([
    [text.indexOf("Win({"), ["BrowserWindow"]],
    [text.indexOf("bridge.exposeInMainWorld"), ["contextBridge"]],
    [text.indexOf('ipc.handle("main:ping"'), ["ipcMain"]],
    [text.indexOf("renderer.invoke"), ["ipcRenderer"]],
    [text.indexOf("directMember.handle"), ["ipcMain"]],
    [text.indexOf("member.handle"), ["ipcMain"]],
    [text.indexOf('require("electron").ipcMain.handle'), []],
    [text.indexOf('require("electron").notAnApi.ipcMain.handle'), []],
    [text.indexOf('require("electron")["ipcMain.handle"]'), []],
  ]);
});
