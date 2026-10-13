/// Concepto de negocio de un movimiento (E2-T35, HU05/HU06/HU08).
///
/// Presentacion pura (cliente delgado, `docs/19` §4): el backend decide que
/// pata existe, su signo y su monto (`accounts.movements_view` con
/// `entry_type`); aqui solo se traduce `entry_type` a etiqueta en español y
/// se marca lo interno (retenciones `2000->2100`, neto 0). El signo
/// (`+`/`-`) sale de `direction` en cada pantalla; aqui no se calcula nada.
///
/// Regla "nunca UUID": si la descripcion contiene un UUID o empieza por un
/// prefijo tecnico del motor (`hold`, `settle`, `own settle`, `interbank`,
/// ...), se descarta y se usa la etiqueta por `entry_type` (o el generico
/// `Movimiento` en filas legacy sin `entry_type`). Jamas se renderiza texto
/// crudo del ledger.
library;

/// Etiqueta en español + marca de visibilidad de un movimiento.
class MovementConcept {
  const MovementConcept({required this.label, required this.internal});

  /// Texto a mostrar (nunca un UUID ni mecanica del ledger).
  final String label;

  /// `true` = pata interna: se oculta en el preview del home y en el
  /// historial de cuenta (no es dinero del usuario, es retencion).
  final bool internal;
}

/// Tabla `entry_type -> (etiqueta, interno)` (E2-T35, valores que emite el
/// motor; claves en mayusculas).
const Map<String, MovementConcept> movementConcepts = {
  // Internos: retenciones `2000->2100` (neto 0), se ocultan.
  'TRANSFER_HOLD': MovementConcept(
    label: 'Retención por transferencia',
    internal: true,
  ),
  'INTERBANK_HOLD': MovementConcept(
    label: 'Retención por transferencia',
    internal: true,
  ),
  // Visibles: concepto de negocio + signo de `direction`.
  'TRANSFER_SETTLE': MovementConcept(label: 'Transferencia', internal: false),
  'OWN_TRANSFER': MovementConcept(
    label: 'Transferencia entre mis cuentas',
    internal: false,
  ),
  'INTERBANK_SEND': MovementConcept(
    label: 'Transferencia interbancaria',
    internal: false,
  ),
  'HOLD_RELEASE': MovementConcept(
    label: 'Devolución por operación no completada',
    internal: false,
  ),
  'INTERBANK_RETURN': MovementConcept(
    label: 'Devolución de transferencia interbancaria',
    internal: false,
  ),
  'INTERBANK_SETTLE': MovementConcept(label: 'Comisión', internal: false),
  'REVERSAL': MovementConcept(label: 'Reverso', internal: false),
};

/// UUID en texto (`settle <uuid>`, `hold <uuid>`, ...): nunca se muestra.
final RegExp _uuidPattern = RegExp(
  r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
  caseSensitive: false,
);

/// Prefijos tecnicos del motor que nunca son concepto de usuario.
bool _isTechnicalDescription(String description) {
  final t = description.trim().toLowerCase();
  return t.startsWith('hold ') ||
      t.startsWith('hold release') ||
      t.startsWith('settle ') ||
      t.startsWith('own settle') ||
      t.startsWith('interbank ');
}

/// Resuelve el concepto a mostrar para un movimiento.
///
/// - `entry_type` conocido -> etiqueta de la tabla (manda sobre la
///   descripcion, que es tecnica y compartida por asiento).
/// - Desconocido/`NULL` (filas legacy): la descripcion solo se usa si es
///   texto de usuario; si esta vacia, trae UUID o prefijo tecnico, se usa
///   el generico `Movimiento`.
MovementConcept conceptForMovement({String? entryType, String? description}) {
  final key = (entryType ?? '').trim().toUpperCase();
  final known = movementConcepts[key];
  if (known != null) return known;
  final raw = (description ?? '').trim();
  if (raw.isEmpty || _uuidPattern.hasMatch(raw) || _isTechnicalDescription(raw)) {
    return const MovementConcept(label: 'Movimiento', internal: false);
  }
  return MovementConcept(label: raw, internal: false);
}

/// Atajo de visibilidad para filtros de presentacion.
bool isInternalMovement({String? entryType, String? description}) =>
    conceptForMovement(entryType: entryType, description: description).internal;
