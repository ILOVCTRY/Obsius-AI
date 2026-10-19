import * as t from "@babel/types";
import {
  resolveSemanticBindingState,
  type JavaScriptSemanticAnalysisState,
} from "./javascriptSemanticState.js";
import { semanticCallableResultExpressions } from "./javascriptSemanticCallableResults.js";
import {
  semanticStaticPropertyKey,
  unwrapJavaScriptExpression,
} from "./javascriptAstValues.js";
import {
  semanticPropertyPathKeyMatches,
  type JavaScriptSemanticPropertyPath as PropertyPath,
} from "./javascriptSemanticPropertyPaths.js";
import { compareUnicodeCodePoints } from "../unicodeCodePointOrder.js";

interface ReferencedValue {
  readonly node: t.Node;
  readonly path: PropertyPath;
}

export const semanticClassCandidates = (
  root: t.Node,
  state: JavaScriptSemanticAnalysisState,
): readonly (t.ClassDeclaration | t.ClassExpression)[] => {
  const pending: t.Node[] = [root];
  const seenNodes = new Set<t.Node>();
  const seenBindings = new Set<string>();
  const classes: (t.ClassDeclaration | t.ClassExpression)[] = [];
  for (let cursor = 0; cursor < pending.length; cursor++) {
    const node = pending[cursor];
    if (node === undefined) continue;
    const expression = unwrapJavaScriptExpression(node).node;
    if (seenNodes.has(expression)) continue;
    seenNodes.add(expression);
    if (t.isClass(expression)) {
      classes.push(expression);
    } else if (t.isIdentifier(expression)) {
      const binding = resolveSemanticBindingState(
        state,
        expression,
        expression.name,
      );
      if (binding === undefined || seenBindings.has(binding.bindingId))
        continue;
      seenBindings.add(binding.bindingId);
      for (const initializer of binding.initializers)
        pending.push(initializer.node);
    } else if (
      t.isConditionalExpression(expression) ||
      t.isLogicalExpression(expression)
    ) {
      pending.push(
        t.isConditionalExpression(expression)
          ? expression.consequent
          : expression.left,
        t.isConditionalExpression(expression)
          ? expression.alternate
          : expression.right,
      );
    } else if (t.isSequenceExpression(expression)) {
      const last = expression.expressions.at(-1);
      if (last !== undefined) pending.push(last);
    }
  }
  return classes;
};

const instanceFieldReferences = (
  root: t.ClassDeclaration | t.ClassExpression,
  propertyName: string | null,
  path: PropertyPath,
  state: JavaScriptSemanticAnalysisState,
): {
  readonly references: readonly ReferencedValue[];
  readonly definiteNames: ReadonlySet<string>;
} => {
  const references: ReferencedValue[] = [];
  const pending = [{ klass: root, excluded: new Set<string>() }];
  const selections = new Map<t.Node, ReadonlySet<string>>();
  let definiteNames: ReadonlySet<string> | undefined;
  for (let cursor = 0; cursor < pending.length; cursor++) {
    const current = pending[cursor];
    if (current === undefined) continue;
    const previous = selections.get(current.klass);
    // A shared base can be reached through different derived classes. Only
    // exclusions common to those paths can suppress a field in that base.
    const excluded =
      previous === undefined
        ? current.excluded
        : new Set([...previous].filter((name) => current.excluded.has(name)));
    if (previous !== undefined && previous.size === excluded.size) continue;
    selections.set(current.klass, excluded);
    const shadowed = new Set(excluded);
    for (const member of [...current.klass.body.body].reverse()) {
      if (!t.isClassProperty(member) || member.static) continue;
      const key = semanticStaticPropertyKey(member.key, member.computed);
      if (key !== null && shadowed.has(key)) continue;
      if (key !== null) shadowed.add(key);
      if (
        (propertyName === null || key === null || key === propertyName) &&
        member.value != null
      )
        references.push({ node: member.value, path });
    }
    const bases =
      current.klass.superClass == null
        ? []
        : semanticClassCandidates(current.klass.superClass, state);
    if (bases.length === 0)
      definiteNames =
        definiteNames === undefined
          ? shadowed
          : new Set([...definiteNames].filter((name) => shadowed.has(name)));
    for (const klass of bases) pending.push({ klass, excluded: shadowed });
  }
  return { references, definiteNames: definiteNames ?? new Set() };
};

interface ClassPropertyLookup {
  readonly owner: t.ClassDeclaration | t.ClassExpression;
  readonly receiver?: t.ClassDeclaration | t.ClassExpression;
  readonly kind: "instance" | "static" | "prototype";
}

const classPropertyDescriptors = (
  klass: t.ClassDeclaration | t.ClassExpression,
  propertyName: string | null,
  kind: ClassPropertyLookup["kind"],
) => {
  const isStatic = kind === "static";
  const selectedGetters: t.ClassMethod[] = [];
  const fields: t.ClassProperty[] = [];
  const indexedMembers: {
    readonly member: t.ClassMethod | t.ClassProperty;
    readonly index: number;
    readonly key: string | null;
  }[] = [];
  klass.body.body.forEach((member, index) => {
    if (
      (t.isClassMethod(member) ||
        (kind === "static" && t.isClassProperty(member))) &&
      member.static === isStatic
    )
      indexedMembers.push({
        member,
        index,
        key: semanticStaticPropertyKey(member.key, member.computed),
      });
  });
  const matchingMembers =
    propertyName === null
      ? []
      : indexedMembers.filter(({ key }) => key === propertyName);
  const uncertainGetters = indexedMembers.filter(
    (entry): entry is typeof entry & { member: t.ClassMethod } => {
      const { member, key } = entry;
      return (
        t.isClassMethod(member) &&
        member.kind === "get" &&
        (propertyName === null || (member.computed && key === null))
      );
    },
  );
  const getters = matchingMembers.filter(
    (entry): entry is typeof entry & { member: t.ClassMethod } =>
      t.isClassMethod(entry.member) && entry.member.kind === "get",
  );
  const getter = getters.at(-1);
  const getterIndex = getter?.index ?? -1;
  const laterDataMember = matchingMembers.some(
    ({ member, index }) =>
      t.isClassProperty(member) ||
      (t.isClassMethod(member) &&
        member.kind === "method" &&
        index > getterIndex),
  );
  const seenFields = new Set<string>();
  for (const { member, key } of [...indexedMembers].reverse()) {
    if (!t.isClassProperty(member)) continue;
    if (key !== null && seenFields.has(key)) continue;
    if (key !== null) seenFields.add(key);
    if (propertyName === null || key === null || key === propertyName)
      fields.push(member);
  }
  if (getters.length > 0 && !laterDataMember && getter !== undefined)
    selectedGetters.push(getter.member);
  for (const { member, index } of uncertainGetters)
    if (
      !matchingMembers.some(
        ({ member: matching, index: matchingIndex }) =>
          (t.isClassProperty(matching) ||
            (t.isClassMethod(matching) &&
              (matching.kind === "method" || matching.kind === "get"))) &&
          matchingIndex > index,
      )
    )
      selectedGetters.push(member);
  return {
    getters: selectedGetters,
    fields,
    inherits: matchingMembers.length === 0,
  };
};

export const semanticClassPropertyReferences = (
  lookup: ClassPropertyLookup,
  selection: PropertyPath,
  state: JavaScriptSemanticAnalysisState,
  activeGetters: ReadonlyMap<t.Node, number> = new Map(),
): readonly ReferencedValue[] => {
  const root = lookup.owner;
  const receiver = lookup.receiver ?? root;
  const [key, ...path] = selection;
  const propertyName =
    key == null || typeof key === "object" ? null : String(key);
  const pending: (t.ClassDeclaration | t.ClassExpression)[] = [root];
  const seen = new Set<t.Node>();
  const possibleResults: ReferencedValue[] = [];
  // Instance fields are own properties, including fields initialized by base
  // constructors. They take precedence over every prototype descriptor.
  let instanceFields: ReadonlySet<string> = new Set();
  if (lookup.kind === "instance") {
    const fields = instanceFieldReferences(root, propertyName, path, state);
    possibleResults.push(...fields.references);
    instanceFields = fields.definiteNames;
    if (propertyName !== null && instanceFields.has(propertyName))
      return possibleResults;
  }
  for (let cursor = 0; cursor < pending.length; cursor++) {
    const klass = pending[cursor];
    if (klass === undefined || seen.has(klass)) continue;
    seen.add(klass);
    const descriptors = classPropertyDescriptors(
      klass,
      propertyName,
      lookup.kind,
    );
    for (const field of descriptors.fields)
      if (field.value != null)
        possibleResults.push({ node: field.value, path });
    for (const getter of descriptors.getters) {
      const key = semanticStaticPropertyKey(getter.key, getter.computed);
      if (key !== null && instanceFields.has(key)) continue;
      possibleResults.push(
        ...getterResultReferences(
          { getter, owner: klass, receiver },
          path,
          state,
          activeGetters,
        ),
      );
    }
    // An exact own descriptor suppresses inherited lookup. Dynamic computed
    // descriptors may or may not match, so retain possible superclass results.
    if (
      descriptors.inherits &&
      klass.superClass !== null &&
      klass.superClass !== undefined
    )
      pending.push(...semanticClassCandidates(klass.superClass, state));
  }
  return possibleResults;
};

const objectReceiverReference = (
  object: t.ObjectExpression,
  path: PropertyPath,
  state: JavaScriptSemanticAnalysisState,
): readonly ReferencedValue[] => {
  // A literal's enclosing binding owns its slots. Returning the literal
  // itself would bypass invalidation of that binding's observed properties.
  let current: t.Node = object;
  const projection: (string | number | null)[] = [];
  while (true) {
    const parent = state.parentsByNode.get(current);
    if (
      t.isVariableDeclarator(parent) &&
      parent.init === current &&
      t.isIdentifier(parent.id)
    )
      return [{ node: parent.id, path: [...projection, ...path] }];
    if (t.isObjectProperty(parent) && parent.value === current) {
      projection.unshift(
        semanticStaticPropertyKey(parent.key, parent.computed),
      );
      const container = state.parentsByNode.get(parent);
      if (t.isObjectExpression(container)) {
        current = container;
        continue;
      }
    }
    break;
  }
  return [{ node: object, path }];
};

// Getter return expressions use the receiver of the property read. Keep that
// identity when translating `this`, private fields, and superclass reads into
// the same reference paths used by ordinary member mutations.
interface GetterInvocation {
  readonly getter: t.ObjectMethod | t.ClassMethod;
  readonly owner: t.ObjectExpression | t.ClassDeclaration | t.ClassExpression;
  readonly receiver?:
    | t.ObjectExpression
    | t.ClassDeclaration
    | t.ClassExpression;
}

const getterResultReferences = (
  invocation: GetterInvocation,
  path: PropertyPath,
  state: JavaScriptSemanticAnalysisState,
  activeGetters: ReadonlyMap<t.Node, number> = new Map(),
): readonly ReferencedValue[] => {
  const { getter, owner, receiver = owner } = invocation;
  const previousLength = activeGetters.get(getter);
  if (previousLength !== undefined && previousLength <= path.length) return [];
  // Repeated receiver reads may revisit a getter while consuming a finite
  // path; recursive getters that preserve or grow that path cannot progress.
  const active = new Map(activeGetters).set(getter, path.length);
  const isStatic = t.isClassMethod(getter) && getter.static;
  const resolve = (
    node: t.Node,
    remaining: PropertyPath,
  ): readonly ReferencedValue[] => {
    const expression = unwrapJavaScriptExpression(node).node;
    if (t.isThisExpression(expression)) {
      if (t.isClass(receiver)) {
        return semanticClassPropertyReferences(
          { owner: receiver, kind: isStatic ? "static" : "instance" },
          remaining,
          state,
          active,
        );
      }
      return objectReceiverReference(receiver, remaining, state);
    }
    if (
      t.isMemberExpression(expression) ||
      t.isOptionalMemberExpression(expression)
    ) {
      if (
        t.isPrivateName(expression.property) &&
        t.isThisExpression(expression.object) &&
        t.isClass(owner)
      ) {
        const privateName = expression.property.id.name;
        return owner.body.body.flatMap((member) =>
          t.isClassPrivateProperty(member) &&
          member.static === isStatic &&
          member.key.id.name === privateName &&
          member.value != null
            ? [{ node: member.value, path: remaining }]
            : [],
        );
      }
      const key = semanticStaticPropertyKey(
        expression.property,
        expression.computed,
      );
      if (
        t.isSuper(expression.object) &&
        t.isClass(owner) &&
        owner.superClass != null
      ) {
        return semanticClassCandidates(owner.superClass, state).flatMap(
          (base) =>
            semanticClassPropertyReferences(
              {
                owner: base,
                receiver: t.isClass(receiver) ? receiver : owner,
                kind: isStatic ? "static" : "prototype",
              },
              [key, ...remaining],
              state,
              active,
            ),
        );
      }
      return resolve(expression.object, [key, ...remaining]);
    }
    if (t.isConditionalExpression(expression))
      return [
        ...resolve(expression.consequent, remaining),
        ...resolve(expression.alternate, remaining),
      ];
    if (t.isLogicalExpression(expression))
      return [
        ...resolve(expression.left, remaining),
        ...resolve(expression.right, remaining),
      ];
    if (t.isSequenceExpression(expression)) {
      const last = expression.expressions.at(-1);
      return last === undefined ? [] : resolve(last, remaining);
    }
    return [{ node: expression, path: remaining }];
  };
  return semanticCallableResultExpressions(getter).flatMap(({ node }) =>
    node === null ? [] : resolve(node, path),
  );
};

export const semanticObjectPropertyReferences = (
  node: t.ObjectExpression,
  path: PropertyPath,
  state: JavaScriptSemanticAnalysisState,
): readonly ReferencedValue[] => {
  const [key, ...remaining] = path;
  const references: ReferencedValue[] = [];
  const overwritten = new Set<string>();
  const dataOverwritten = new Set<string>();
  const accessorNames = new Set<string>();
  const getterNames = new Set<string>();
  const setterNames = new Set<string>();
  for (const property of [...node.properties].reverse()) {
    if (t.isSpreadElement(property)) {
      const selected = key ?? null;
      if (typeof selected !== "object" && overwritten.has(String(selected)))
        continue;
      references.push({
        node: property.argument,
        path: [
          typeof selected === "object"
            ? {
                ...selected,
                excludedKeys: [
                  ...new Set([
                    ...(selected?.excludedKeys ?? []),
                    ...overwritten,
                  ]),
                ].sort(compareUnicodeCodePoints),
              }
            : selected,
          ...remaining,
        ],
      });
      if (
        selected !== null &&
        typeof selected !== "object" &&
        t.isObjectExpression(property.argument) &&
        objectExpressionDefinesKey(property.argument, String(selected))
      ) {
        overwritten.add(String(selected));
        dataOverwritten.add(String(selected));
      }
      continue;
    }
    const name = semanticStaticPropertyKey(property.key, property.computed);
    const isGetter = t.isObjectMethod(property) && property.kind === "get";
    const isSetter = t.isObjectMethod(property) && property.kind === "set";
    if (
      name !== null &&
      (isGetter || isSetter
        ? dataOverwritten.has(name) ||
          (isGetter ? getterNames.has(name) : setterNames.has(name))
        : dataOverwritten.has(name) || accessorNames.has(name))
    )
      continue;
    if (name === null || semanticPropertyPathKeyMatches(key ?? null, name)) {
      if (t.isObjectMethod(property) && property.kind === "get") {
        references.push(
          ...getterResultReferences(
            { getter: property, owner: node },
            remaining,
            state,
          ),
        );
      } else if (t.isObjectMethod(property) && property.kind === "set") {
        // A setter contributes no readable reference. A paired getter is
        // handled independently when it appears earlier in this object.
      } else {
        references.push({
          node: t.isObjectProperty(property) ? property.value : property,
          path: remaining,
        });
      }
    }
    if (name !== null) {
      if (isGetter) {
        getterNames.add(name);
        accessorNames.add(name);
        overwritten.add(name);
      } else if (isSetter) {
        setterNames.add(name);
        accessorNames.add(name);
        overwritten.add(name);
      } else if (
        !(
          t.isObjectProperty(property) &&
          !property.computed &&
          !property.shorthand &&
          name === "__proto__"
        )
      ) {
        overwritten.add(name);
        dataOverwritten.add(name);
      }
    }
  }
  return references;
};

const objectExpressionDefinesKey = (
  root: t.ObjectExpression,
  key: string,
): boolean => {
  const pending: t.ObjectExpression[] = [root];
  const seen = new Set<t.ObjectExpression>();
  for (let cursor = 0; cursor < pending.length; cursor++) {
    const object = pending[cursor];
    if (object === undefined || seen.has(object)) continue;
    seen.add(object);
    for (const property of object.properties) {
      if (t.isSpreadElement(property)) {
        if (t.isObjectExpression(property.argument))
          pending.push(property.argument);
        continue;
      }
      const name = semanticStaticPropertyKey(property.key, property.computed);
      if (
        name === key &&
        !(
          t.isObjectProperty(property) &&
          !property.computed &&
          !property.shorthand &&
          name === "__proto__"
        )
      )
        return true;
    }
  }
  return false;
};
