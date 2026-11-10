// Tabla genérica del panel (F-T12 corte 1). Solo presenta datos del
// servidor (cliente delgado); sin datos sensibles en claro (usar `MaskedRef`).

import type { ReactNode } from "react";
import { EmptyState } from "./EmptyState";

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => ReactNode;
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[];
  caption: string;
  emptyTitle?: string;
  emptyHint?: string;
  emptyAction?: ReactNode;
  rowKey: (row: T, index: number) => string;
}

export function DataTable<T>({
  columns,
  rows,
  caption,
  emptyTitle = "Sin datos",
  emptyHint,
  emptyAction,
  rowKey,
}: Props<T>) {
  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} hint={emptyHint} action={emptyAction} />;
  }
  return (
    <div className="table-wrap">
      <table className="data-table">
        <caption className="table-caption">{caption}</caption>
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key} scope="col">
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey(row, i)}>
              {columns.map((col) => (
                <td key={col.key}>{col.render ? col.render(row) : null}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
