// Matriz de roles y menú (F-T12 corte 1; base del corte 2).

import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import { MENU, roleLabel, visibleMenu } from "./roles";

afterEach(cleanup);

describe("matriz de roles", () => {
  it("cada rol canónico ve su entrada", () => {
    expect(visibleMenu(["operations_analyst"]).map((e) => e.key)).toEqual(["operations"]);
    expect(visibleMenu(["fraud_analyst"]).map((e) => e.key)).toEqual(["fraud"]);
    expect(visibleMenu(["compliance_officer"]).map((e) => e.key)).toEqual(["compliance"]);
    expect(visibleMenu(["credit_analyst"]).map((e) => e.key)).toEqual(["credits"]);
    expect(visibleMenu(["security_admin"]).map((e) => e.key)).toEqual(["admin"]);
    expect(visibleMenu(["auditor"]).map((e) => e.key)).toEqual(["audit"]);
  });

  it("alias histórico analyst ve créditos; multi-rol ve varias", () => {
    expect(visibleMenu(["analyst"]).map((e) => e.key)).toEqual(["credits"]);
    expect(visibleMenu(["credit_analyst", "auditor"]).map((e) => e.key)).toEqual([
      "credits",
      "audit",
    ]);
  });

  it("sin roles no hay menú; el menú completo trae 6 rutas", () => {
    expect(visibleMenu([])).toEqual([]);
    expect(MENU.map((e) => e.path)).toEqual([
      "/operations",
      "/fraud",
      "/compliance",
      "/credits",
      "/admin",
      "/audit",
    ]);
  });

  it("etiquetas sin PII", () => {
    expect(roleLabel("credit_analyst")).toBe("Créditos");
    expect(roleLabel("ANALYST")).toBe("Créditos");
    expect(roleLabel("auditor")).toBe("Auditoría");
  });
});
