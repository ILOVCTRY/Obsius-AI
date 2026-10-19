import * as t from "@babel/types";

import {
  unwrapJavaScriptExpression,
  semanticStaticPropertyKey,
} from "./javascriptAstValues.js";
import { calleeName } from "./javascriptStaticAnalysisHelpers.js";

/** One RPC operation or transport URL recovered from client-side syntax. */
export interface JavaScriptRpcEndpoint {
  readonly value: string;
  readonly mechanism: string;
}

/** Per-source RPC recognition state; feed it every AST node in pre-order. */
export interface JavaScriptRpcScan {
  inspect(node: t.Node): JavaScriptRpcEndpoint | undefined;
}

// tRPC proxies preserve router and procedure names as property names, so they
// survive identifier minification. Method names follow @trpc/react-query and
// @trpc/tanstack-react-query 11.19 documentation; cache-key helpers such as
// queryKey are excluded because they never issue a request.
const TRPC_HOOK_METHODS: ReadonlySet<string> = new Set([
  "useQuery",
  "useSuspenseQuery",
  "useInfiniteQuery",
  "useSuspenseInfiniteQuery",
  "usePrefetchQuery",
  "usePrefetchInfiniteQuery",
  "useMutation",
  "useSubscription",
  "queryOptions",
  "infiniteQueryOptions",
  "mutationOptions",
  "subscriptionOptions",
]);

// @trpc/client 11.19 clientCallTypeMap methods; common outside tRPC (Apollo,
// database pools, stores), so they need corroborating evidence.
const TRPC_REQUEST_METHODS: ReadonlySet<string> = new Set(["query", "mutate"]);
// @trpc/client 11.19 TRPCSubscriptionObserver callbacks; RxJS observers use
// next/error/complete instead.
const TRPC_SUBSCRIPTION_CALLBACKS: ReadonlySet<string> = new Set([
  "onStarted",
  "onData",
  "onError",
  "onStopped",
  "onComplete",
  "onConnectionStateChange",
]);
// @trpc/client 11.19 client factories (createTRPCProxyClient is the
// deprecated alias of createTRPCClient).
const TRPC_CLIENT_FACTORIES: ReadonlySet<string> = new Set([
  "createTRPCClient",
  "createTRPCProxyClient",
]);
// Built-in HTTP links documented by tRPC 11.19 that carry a `url` option.
const TRPC_URL_LINKS: ReadonlySet<string> = new Set([
  "httpLink",
  "httpBatchLink",
  "httpBatchStreamLink",
  "httpSubscriptionLink",
]);
// Literals retained by @trpc/client 11.19 bundles (`this.name`, the default
// accept header key) or by the conventional transport path.
const TRPC_SOURCE_MARKERS = ["TRPCClientError", "trpc-accept", "/trpc"];

// Browser-owned roots whose methods share tRPC names, such as
// navigator.permissions.query.
const BROWSER_GLOBAL_NAMES: ReadonlySet<string> = new Set([
  "window",
  "self",
  "globalThis",
  "document",
  "navigator",
  "location",
  "history",
  "console",
  "performance",
  "screen",
  "top",
  "parent",
  "frames",
]);

const PROCEDURE_SEGMENT = /^[A-Za-z_$][\w$]*$/u;

interface MemberPath {
  readonly root: t.Node;
  /** Non-computed property names from the root outward. */
  readonly properties: readonly string[];
}

/** Read a static member path and the expression it starts from. */
const memberPath = (callee: t.Node): MemberPath | undefined => {
  const reversed: string[] = [];
  let current: t.Node = callee;
  while (
    t.isMemberExpression(current) ||
    t.isOptionalMemberExpression(current)
  ) {
    const property = semanticStaticPropertyKey(
      current.property,
      current.computed,
    );
    if (property === null || !PROCEDURE_SEGMENT.test(property))
      return undefined;
    reversed.push(property);
    current = current.object;
  }
  return { root: current, properties: reversed.reverse() };
};

const objectKeys = (node: t.Node | undefined): readonly string[] =>
  t.isObjectExpression(node)
    ? node.properties.flatMap((property) => {
        if (!t.isObjectProperty(property) && !t.isObjectMethod(property))
          return [];
        const key = semanticStaticPropertyKey(property.key, property.computed);
        return key === null ? [] : [key];
      })
    : [];

const objectStringProperty = (
  node: t.ObjectExpression,
  key: string,
): string | undefined => {
  for (const property of node.properties)
    if (
      t.isObjectProperty(property) &&
      semanticStaticPropertyKey(property.key, property.computed) === key &&
      t.isStringLiteral(property.value)
    )
      return property.value.value;
  return undefined;
};

const objectProperty = (
  node: t.ObjectExpression,
  key: string,
): t.Node | undefined => {
  for (const property of node.properties)
    if (
      t.isObjectProperty(property) &&
      semanticStaticPropertyKey(property.key, property.computed) === key
    )
      return property.value;
  return undefined;
};

const GRAPHQL_OPERATION_TYPES: ReadonlySet<string> = new Set([
  "query",
  "mutation",
  "subscription",
]);
// A named GraphQL operation header (GraphQL October 2021, OperationDefinition):
// type, name, then variable definitions starting with `$`, a directive, or a
// selection set starting with a field name.
const GRAPHQL_OPERATION_HEADER =
  /^(query|mutation|subscription)[\s,]+([_A-Za-z][_0-9A-Za-z]*)[\s,]*(?:\([\s,]*\$|@[_A-Za-z]|\{[\s,]*[_A-Za-z])/u;

const isGraphqlIgnoredCharacter = (character: string): boolean =>
  character === "," || character === "﻿" || /\s/u.test(character);

/** Recover a named operation from GraphQL document text. */
export const graphqlOperationFromText = (
  text: string,
): JavaScriptRpcEndpoint | undefined => {
  // Skip GraphQL ignored tokens: whitespace, commas, BOM and # comments.
  const limit = text.length;
  let index = 0;
  while (index < limit) {
    const character = text.charAt(index);
    if (isGraphqlIgnoredCharacter(character)) index += 1;
    else if (character === "#") {
      while (index < limit && !/[\n\r]/u.test(text.charAt(index))) index += 1;
    } else break;
  }
  if (index >= limit) return undefined;
  const header = GRAPHQL_OPERATION_HEADER.exec(text.slice(index));
  const type = header?.[1];
  const name = header?.[2];
  return type === undefined || name === undefined
    ? undefined
    : { value: name, mechanism: `graphql:${type}` };
};

/** Recover a named operation from a precompiled GraphQL document object. */
export const graphqlOperationFromDocumentNode = (
  node: t.ObjectExpression,
): JavaScriptRpcEndpoint | undefined => {
  if (objectStringProperty(node, "kind") !== "OperationDefinition")
    return undefined;
  const type = objectStringProperty(node, "operation");
  if (type === undefined || !GRAPHQL_OPERATION_TYPES.has(type))
    return undefined;
  const name = objectProperty(node, "name");
  if (
    !t.isObjectExpression(name) ||
    objectStringProperty(name, "kind") !== "Name"
  )
    return undefined;
  const value = objectStringProperty(name, "value");
  return value === undefined
    ? undefined
    : { value, mechanism: `graphql:${type}` };
};

/** Bundlers use `(0, imported.factory)` to call an import without a receiver. */
const rpcFactoryName = (callee: t.Node): string => {
  let current = unwrapJavaScriptExpression(callee).node;
  while (t.isSequenceExpression(current)) {
    const last = current.expressions.at(-1);
    if (last === undefined) return "";
    current = unwrapJavaScriptExpression(last).node;
  }
  return calleeName(current);
};

/** Whether an initializer has the syntax of a documented tRPC client factory. */
export const isJavaScriptRpcClientFactory = (node: t.Node): boolean => {
  const call = unwrapJavaScriptExpression(node).node;
  return (
    (t.isCallExpression(call) || t.isOptionalCallExpression(call)) &&
    TRPC_CLIENT_FACTORIES.has(
      rpcFactoryName(call.callee).split(".").pop() ?? "",
    )
  );
};

/** Recover the literal `url` option passed to a documented tRPC HTTP link. */
const trpcLinkUrl = (
  node: t.CallExpression | t.OptionalCallExpression,
): JavaScriptRpcEndpoint | undefined => {
  const name = rpcFactoryName(node.callee);
  const factory = name.slice(name.lastIndexOf(".") + 1);
  if (!TRPC_URL_LINKS.has(factory)) return undefined;
  const options = node.arguments[0];
  if (!t.isObjectExpression(options)) return undefined;
  const url = objectStringProperty(options, "url");
  return url === undefined
    ? undefined
    : { value: url, mechanism: `trpc:link:${factory}` };
};

/**
 * Create the per-source scan. Hook and option-factory methods are distinctive.
 * `query`/`mutate` need a root bound to a tRPC client factory, or a tRPC source
 * marker with a router path of at least two segments; `subscribe` needs the
 * marker and a tRPC observer argument. `this` roots, browser globals and RTK
 * Query `endpoints` chains are never procedures.
 */
export const createJavaScriptRpcScan = (
  source: string,
  clientRoots: ReadonlySet<t.Node>,
): JavaScriptRpcScan => {
  let marker: boolean | undefined;
  const hasMarker = (): boolean =>
    (marker ??= TRPC_SOURCE_MARKERS.some((value) => source.includes(value)));
  // graphql-tag documents retain their source text in loc.source.body; that
  // text repeats the OperationDefinition already recovered from the object.
  const documentSourceBodies = new WeakSet<t.Node>();

  const procedure = (
    node: t.CallExpression | t.OptionalCallExpression,
  ): JavaScriptRpcEndpoint | undefined => {
    const path = memberPath(node.callee);
    if (path === undefined || t.isThisExpression(path.root)) return undefined;
    if (t.isIdentifier(path.root) && BROWSER_GLOBAL_NAMES.has(path.root.name))
      return undefined;
    const method = path.properties[path.properties.length - 1];
    const segments = path.properties.slice(0, -1);
    const first = segments[0];
    if (method === undefined || first === undefined) return undefined;
    if (BROWSER_GLOBAL_NAMES.has(first) || segments.includes("endpoints"))
      return undefined;
    const bound = clientRoots.has(path.root);
    const recognized = TRPC_HOOK_METHODS.has(method)
      ? true
      : TRPC_REQUEST_METHODS.has(method)
        ? bound || (segments.length >= 2 && hasMarker())
        : method === "subscribe" &&
          (bound || hasMarker()) &&
          objectKeys(node.arguments[1]).some((key) =>
            TRPC_SUBSCRIPTION_CALLBACKS.has(key),
          );
    return recognized
      ? { value: segments.join("."), mechanism: `trpc:procedure:${method}` }
      : undefined;
  };

  return {
    inspect: (node) => {
      if (t.isCallExpression(node) || t.isOptionalCallExpression(node))
        return trpcLinkUrl(node) ?? procedure(node);
      if (t.isObjectExpression(node)) {
        const body = objectProperty(node, "body");
        if (
          t.isStringLiteral(body) &&
          objectProperty(node, "locationOffset") !== undefined
        ) {
          documentSourceBodies.add(body);
          return undefined;
        }
        return graphqlOperationFromDocumentNode(node);
      }
      if (t.isStringLiteral(node))
        return documentSourceBodies.has(node)
          ? undefined
          : graphqlOperationFromText(node.value);
      if (t.isTemplateLiteral(node)) {
        // A template recovered without a cooked value stays dynamic.
        const cooked = node.quasis[0]?.value.cooked;
        return cooked === undefined || cooked === null
          ? undefined
          : graphqlOperationFromText(cooked);
      }
      return undefined;
    },
  };
};
