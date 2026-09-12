# admin-web (panel interno)

Panel web para roles internos: operaciones, fraude, cumplimiento, credito, administracion de
seguridad y auditoria (solo lectura).

- Stack sugerido: Vite + React + TypeScript, TanStack Query, MUI o Ant Design, ECharts.
- Autenticacion y RBAC por rol (matriz de accesos).
- Datos personales y cuentas enmascarados por defecto; toda accion queda auditada.
- No edita asientos contables; los ajustes son asientos nuevos autorizados.

Se crea en la tarea `F-T12` (`docs/13-frontend-flutter-panel.md`).
