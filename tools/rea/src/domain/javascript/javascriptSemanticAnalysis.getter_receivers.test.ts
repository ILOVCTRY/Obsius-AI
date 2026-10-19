import { describe, expect, it } from "vitest";
import { analyzeJavaScriptSemantics } from "./javascriptSemanticAnalysis.js";
import { onlyCallable } from "./javascriptSemanticAnalysis.fixture.js";

const resultValue = (body: string) =>
  onlyCallable(
    analyzeJavaScriptSemantics(`export function result() { ${body} }`),
    "result",
  ).returnSites[0]?.value;

const getterReceivers = [
  [
    "this",
    "const box = { x: 1, get self() { return this; } }; box.self.x = 2; return box.x;",
  ],
  [
    "this property",
    "const shared = { x: 1 }; const box = { s: shared, get v() { return this.s; } }; box.v.x = 2; return shared.x;",
  ],
  [
    "private field",
    "const shared = { x: 1 }; class B { #s = shared; get v() { return this.#s; } } new B().v.x = 2; return shared.x;",
  ],
  [
    "super getter",
    "const shared = { x: 1 }; class A { get v() { return shared; } } class B extends A { get v() { return super.v; } } new B().v.x = 2; return shared.x;",
  ],
  [
    "saved receiver alias",
    "const box = { x: 1, get self() { return this; } }; const alias = box.self; alias.x = 2; return box.x;",
  ],
  [
    "nested owner",
    "const outer = { box: { x: 1, get self() { return this; } } }; outer.box.self.x = 2; return outer.box.x;",
  ],
  [
    "inherited receiver field",
    "const shared = { x: 1 }; class A { get v() { return this.s; } } class B extends A { s = shared; } new B().v.x = 2; return shared.x;",
  ],
  [
    "super receiver field",
    "const shared = { x: 1 }; class A { get v() { return this.s; } } class B extends A { s = shared; get v() { return super.v; } } new B().v.x = 2; return shared.x;",
  ],
  [
    "static private field",
    "const shared = { x: 1 }; class B { static #s = shared; static get v() { return this.#s; } } B.v.x = 2; return shared.x;",
  ],
  [
    "inherited field",
    "const shared = { x: 1 }; class A { s = shared; } class B extends A { get v() { return this.s; } } new B().v.x = 2; return shared.x;",
  ],
  [
    "class getter chain",
    "const shared = { x: 1 }; class B { get s() { return shared; } get v() { return this.s; } } new B().v.x = 2; return shared.x;",
  ],
  [
    "inherited field shadows prototype getter",
    "const shared={x:1};class A{s=shared}class B extends A{get s(){return {x:1}} get v(){return this.s}}new B().v.x=2;return shared.x;",
  ],
  [
    "alternative inherited fields",
    "const shared={x:1};const Base=flag?class{s={x:1}}:class{s=shared};class B extends Base{get v(){return this.s}}new B().v.x=2;return shared.x;",
  ],
  [
    "alternative inherited getter",
    "const shared={x:1};const Base=flag?class{s={x:1}}:class{get s(){return shared}};class B extends Base{get v(){return this.s}}new B().v.x=2;return shared.x;",
  ],
  [
    "repeated class receiver",
    "const shared={x:1};class B{s=shared;get self(){return this}}new B().self.self.s.x=2;return shared.x;",
  ],
  [
    "getter chain",
    "const shared = { x: 1 }; const box = { s: shared, get self() { return this; }, get v() { return this.self.s; } }; box.v.x = 2; return shared.x;",
  ],
] as const;

describe("getter receiver mutation aliases (#1665)", () => {
  it.each(getterReceivers)(
    "does not report a stale literal through %s",
    (_shape, body) => {
      expect(resultValue(body)?.status).toBe("unknown");
    },
  );
  it.each([
    [
      "duplicate field",
      "const shared={x:1};class B{s=shared;s={x:1};get v(){return this.s}}new B().v.x=2;return shared.x;",
    ],
    [
      "super excludes instance fields",
      "const shared={x:1};class A{s=shared}class B extends A{get v(){return super.s ?? {x:1}}}new B().v.x=2;return shared.x;",
    ],
    [
      "fresh return",
      "const shared = { x: 1 }; const box = { get v() { return { x: 1 }; } }; box.v.x = 2; return shared.x;",
    ],
    [
      "read without write",
      "const box = { x: 1, get self() { return this; } }; const alias = box.self; return box.x;",
    ],
    [
      "unrelated sibling",
      "const box = { x: 1, y: 1, get self() { return this; } }; box.self.y = 2; return box.x;",
    ],
    [
      "replace getter",
      "const shared = { x: 1 }; const box = { s: shared, get v() { return this.s; } }; box.v = {}; return shared.x;",
    ],
    [
      "delete getter",
      "const shared = { x: 1 }; const box = { s: shared, get v() { return this.s; } }; delete box.v; return shared.x;",
    ],
    [
      "ordinary method read",
      "const box = { x: 1, self() { return this; } }; const method = box.self; return box.x;",
    ],
  ])("preserves a known literal for %s", (_shape, body) => {
    expect(resultValue(body)).toMatchObject({ status: "literal", value: 1 });
  });
  it("terminates recursive receiver getters", () => {
    expect(
      resultValue(
        "const box = { x: 1, get self() { return this.self; } }; box.self.x = 2; return box.x;",
      ),
    ).toMatchObject({ status: "literal", value: 1 });
  });
});
