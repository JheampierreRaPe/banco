// Pruebas de los componentes compartidos (F-T12 corte 1).
// Cubre camino feliz y error de cada uno + guardia de PII en `MaskedRef`.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable } from "./DataTable";
import type { Column } from "./DataTable";
import { EmptyState, ErrorState } from "./EmptyState";
import { MaskedRef } from "./MaskedRef";
import { StatusChip } from "./StatusChip";

afterEach(cleanup);

describe("DataTable", () => {
  const columns: Column<{ ref: string; monto: string }>[] = [
    { key: "id", header: "Ref", render: (r: { ref: string }) => r.ref },
    { key: "monto", header: "Monto", render: (r: { monto: string }) => r.monto },
  ];

  it("camino feliz: caption, cabeceras y filas", () => {
    render(
      <DataTable
        caption="Bandeja"
        columns={columns}
        rows={[
          { ref: "CR-1111", monto: "S/ 10.00" },
          { ref: "CR-2222", monto: "S/ 20.00" },
        ]}
        rowKey={(r) => r.ref}
      />,
    );
    expect(screen.getByText("Bandeja")).toBeInTheDocument();
    expect(screen.getByText("CR-1111")).toBeInTheDocument();
    expect(screen.getByText("S/ 20.00")).toBeInTheDocument();
  });

  it("vacío: delega en EmptyState con acción", () => {
    render(
      <DataTable
        caption="Bandeja"
        columns={columns}
        rows={[]}
        rowKey={(_, i) => String(i)}
        emptyTitle="Sin expedientes"
        emptyAction={<button type="button">Reintentar</button>}
      />,
    );
    expect(screen.getByText("Sin expedientes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeInTheDocument();
  });
});

describe("StatusChip", () => {
  it("mapea tonos sin inventar estados", () => {
    const { rerender } = render(<StatusChip status="APPROVED" />);
    expect(screen.getByText("APPROVED").className).toContain("pill-ok");
    rerender(<StatusChip status="IN_REVIEW" />);
    expect(screen.getByText("IN_REVIEW").className).toContain("pill-warn");
    rerender(<StatusChip status="REJECTED" />);
    expect(screen.getByText("REJECTED").className).toContain("pill-bad");
    rerender(<StatusChip status="OTRO" label="Otro" />);
    const chip = screen.getByText("Otro");
    expect(chip.className).toContain("pill-info");
    expect(chip).toHaveAttribute("data-status", "OTRO");
  });
});

describe("EmptyState / ErrorState", () => {
  it("EmptyState muestra título, pista y acción", () => {
    render(<EmptyState title="Sin datos" hint="Nada por aquí" action={<span>acción</span>} />);
    expect(screen.getByText("Sin datos")).toBeInTheDocument();
    expect(screen.getByText("Nada por aquí")).toBeInTheDocument();
    expect(screen.getByText("acción")).toBeInTheDocument();
  });

  it("ErrorState: alerta con Reintentar que reintenta", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(<ErrorState message="Falló la carga." onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Falló la carga.");
    await user.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("ErrorState sin reintento no muestra botón", () => {
    render(<ErrorState message="Solo aviso." />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("ConfirmDialog", () => {
  it("cerrado: no renderiza nada", () => {
    render(
      <ConfirmDialog open={false} title="¿Seguro?" onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("abierto: confirma o cancela", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="¿Bloquear alerta?"
        body="Queda auditado."
        confirmLabel="Bloquear"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("alertdialog", { name: "¿Bloquear alerta?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Bloquear" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});

describe("MaskedRef", () => {
  const UUID = "11111111-1111-4111-8111-111111111111";

  it("enmascara sin filtrar el valor crudo", () => {
    const { container } = render(<MaskedRef value={UUID} prefix="CR" />);
    const text = container.textContent ?? "";
    expect(text).toContain("••••");
    expect(text).not.toContain(UUID);
    expect(container.innerHTML).not.toContain(UUID);
  });

  it("tolera nulos", () => {
    render(<MaskedRef value={null} />);
    expect(screen.getByText("••••----")).toBeInTheDocument();
  });
});
