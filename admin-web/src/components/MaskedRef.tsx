// Referencia enmascarada (F-T12 corte 1; docs/13#2.3, docs/16 regla 9).
// Muestra solo la cola (`••••XXXX`); JAMÁS renderiza el valor crudo
// (UUID/documento/cuenta) ni lo expone en atributos del DOM.

import { maskRef } from "../domain/masking";

export function MaskedRef({
  value,
  prefix,
}: {
  value: string | null | undefined;
  prefix?: string;
}) {
  return (
    <span className="masked-ref">
      {prefix ? `${prefix} ` : ""}
      {maskRef(value)}
    </span>
  );
}
