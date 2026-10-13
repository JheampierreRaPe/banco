import { describe, expect, it } from "vitest";
import {
  formatDtiPercent,
  formatPEN,
  leaksRaw,
  maskRef,
  shortRef,
  timeAgo,
} from "./masking";

describe("masking (sin PII en claro)", () => {
  it("maskRef oculta UUIDs y conserva solo la cola", () => {
    const raw = "11111111-1111-4111-8111-111111111111";
    const masked = maskRef(raw);
    expect(masked).toBe("••••1111");
    expect(leaksRaw(masked, raw)).toBe(false);
  });

  it("shortRef no expone el id completo", () => {
    const raw = "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee";
    const short = shortRef(raw);
    expect(leaksRaw(short, raw)).toBe(false);
    expect(short.startsWith("CR-")).toBe(true);
  });

  it("formatPEN presenta S/ 9,999.99 sin tocar el valor", () => {
    expect(formatPEN(1_000_000)).toBe("S/ 10,000.00");
    expect(formatPEN(92750)).toBe("S/ 927.50");
    expect(formatPEN(null)).toBe("S/ —");
  });

  it("formatDtiPercent convierte el string del servidor a %", () => {
    expect(formatDtiPercent("0.38")).toBe("DTI 38%");
    expect(formatDtiPercent(null)).toBe("DTI —");
    expect(formatDtiPercent("no-numerico")).toBe("DTI —");
  });

  it("timeAgo degrada fechas inválidas sin romper", () => {
    expect(timeAgo(null)).toBe("fecha —");
    expect(timeAgo("no-fecha")).toBe("fecha —");
  });
});
