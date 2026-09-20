import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { expect, it } from "vitest";

const controls = new Set(["DialogButton", "SmallButton", "ButtonItem", "DropdownItem", "Toggle", "ToggleField", "TextField", "ModalRoot", "ConfirmModal", "Focusable", "CheatRow", "button", "input", "select", "textarea", "a"]);
const events = new Set(["onClick", "onChange", "onCancel", "onOK", "onEscKeypress", "onActivate", "onActiveChange", "onMiddleButton", "closeModal"]);

it("keeps every owned control handler traced and listed in the action audit, including aliased Decky dialogs", () => {
  const files = readdirSync("src", { recursive: true }).map((file) => String(file).replaceAll("\\", "/")).filter((file) => file.endsWith(".tsx"));
  const ids: string[] = [];
  const missing: string[] = [];
  for (const file of files) {
    const ast = ts.createSourceFile(file, readFileSync(join("src", file), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const names = new Set(controls);
    for (const node of ast.statements) {
      if (!ts.isImportDeclaration(node) || node.moduleSpecifier.getText(ast) !== '"@decky/ui"') continue;
      const bindings = node.importClause?.namedBindings;
      if (bindings && ts.isNamedImports(bindings)) for (const item of bindings.elements) {
        names.add(item.name.text);
      }
    }
    const visit = (node: ts.Node) => {
      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName.getText(ast);
        if (names.has(tag)) for (const attribute of node.attributes.properties) {
          // A spread can carry a handler nobody traced, which is the whole
          // reason this refuses them. The one allowed here carries a single
          // named object whose only member is `placeholder`, spread because
          // Steam declares its own input's props as `HTMLAttributes` and so
          // does not list an attribute every input has.
          const forwardedAttribute = ts.isJsxSpreadAttribute(attribute)
            && file === "components/PanelDensity.tsx"
            && tag === "TextField"
            && attribute.expression.getText(ast) === "inInput";
          if (ts.isJsxSpreadAttribute(attribute) && tag !== "Focusable" && !forwardedAttribute) missing.push(`${file}: spread attributes on ${tag} can hide untraced handlers`);
          if (!ts.isJsxAttribute(attribute) || !events.has(attribute.name.getText(ast))) continue;
          const expression = attribute.initializer && ts.isJsxExpression(attribute.initializer) ? attribute.initializer.expression : undefined;
          // These two presentational forwarders carry their already traced
          // callback. Their call sites (SmallButton / CheatRow) are checked too.
          if (file === "components/PanelDensity.tsx" && tag === "DialogButton" && expression?.getText(ast) === "() => onClick()") continue;
          // `DestructiveAction` is the same forwarder for the one press in the
          // product that cannot be taken back: it marks the box behind Steam's
          // button and passes the caller's already traced callback through.
          if (file === "components/ModalActions.tsx" && tag === "DialogButton" && expression?.getText(ast) === "() => onClick()") continue;
          if (file === "components/CheatRow.tsx" && tag === "Toggle" && expression?.getText(ast) === "(checked) => onActiveChange?.(checked)") continue;
          // `FilterField` is the third of those forwarders: it labels a filter
          // inside its own box and passes the caller's already traced handler
          // straight through. Every call site is checked like any other.
          if (file === "components/PanelDensity.tsx" && tag === "TextField" && expression?.getText(ast) === "onChange") continue;
          // A handler that is there is traced. A dialog whose answers change
          // shape - the refusal dialog gains a third answer where a repair was
          // proven and drops the middle button where it was not - passes each
          // branch through the same rule, and a branch that is `undefined` is a
          // control that is not drawn rather than one nobody traced.
          const traced = (candidate: ts.Expression | undefined): boolean => {
            if (!candidate) return false;
            if (ts.isConditionalExpression(candidate)) return traced(candidate.whenTrue) && traced(candidate.whenFalse);
            if (ts.isIdentifier(candidate) && candidate.text === "undefined") return true;
            return ts.isCallExpression(candidate)
              && ["traceUiAction", "traceUiEdit"].includes(candidate.expression.getText(ast))
              && ts.isStringLiteral(candidate.arguments[0]);
          };
          if (!traced(expression)) {
            missing.push(`${file}:${ast.getLineAndCharacterOfPosition(attribute.pos).line + 1} ${tag}.${attribute.name.getText(ast)}`);
          } else if (tag === "TextField" && (!ts.isCallExpression(expression!) || expression!.expression.getText(ast) !== "traceUiEdit")) {
            missing.push(`${file}: TextField must coalesce typing`);
          }
        }
      }
      if (ts.isCallExpression(node) && ["traceUiAction", "traceUiEdit"].includes(node.expression.getText(ast)) && ts.isStringLiteral(node.arguments[0])) ids.push(node.arguments[0].text);
      ts.forEachChild(node, visit);
    };
    visit(ast);
  }
  expect(missing).toEqual([]);
  expect(new Set(ids).size).toBe(ids.length);
  const documented = [...readFileSync("docs/UI_ACTION_AUDIT.md", "utf8").matchAll(/^\| `([^`]+)` \|/gm)].map((match) => match[1]);
  expect(documented.sort()).toEqual(ids.sort());
});
