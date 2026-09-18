# 20 - Diseno de interfaz (UI/UX)

> **Fuente de verdad visual:** `docs/design/design.md` (tema "Eucalipto y Ocre", tokens en el
> frontmatter YAML). Este documento lo traduce a especificaciones para la app Flutter y el panel
> web. Decisiones: **el YAML manda**, **solo modo claro**, iconografia **Material Symbols Outlined**.

## 1. Como especificar el diseno

| Forma | Flutter | Panel web | Nota |
|---|---|---|---|
| Link de Figma | Si (referencia) | Si | `docs/design/figma-*.txt`. |
| Imagenes/PNG (mockups) | Si | Si | Van en `docs/design/` y se registran en `docs/design/mockups.md`. |
| **Tokens** (colores/tipografia) | **Obligatorio** | Obligatorio | Seccion 3 de este documento. |
| CSS/Tailwind | **No** | Si | Solo panel web. |

Regla: para Flutter se pasan **tokens + mockups**, nunca CSS.

### 1.1 Convencion de mockups

- Nombre por pantalla: `seccion-pantalla.png` (ej. `cuentas-dashboard.png`).
- Cada imagen nueva se registra con una fila en `docs/design/mockups.md` (pantalla + HU + brief).
- El brief de cada pantalla incluye un campo `Mockup:` con la ruta de su imagen.
- Al construir una pantalla, el agente lee: este documento (tokens) + su brief + el mockup.

## 2. Conflictos del design system y resolucion

El YAML y la prosa de `design.md` difieren en algunos valores. **Se adopta el YAML.**

| Elemento | Prosa (se descarta) | YAML (canonico) |
|---|---|---|
| Primario | Eucalipto #2B4034 | `primary #152A1F` / `primary-container #2B4034` |
| Fondo base | Stone #F5F3EE | `surface/background #FBF9F4` (`surface-container-low #F5F3EE`) |
| Ochre | #D98C2B | `secondary-container #FDAA47` / `accent-text #9B5F12` |
| Error | Carmine #B31D3F | `error #BA1A1A` (`error-carmine #B31D3F` queda como token extra) |
| Borde de input | #D1CDC2 | `outline-variant #C2C8C2` |
| Divisor | #EBE8E0 | se define `divider = #E4E2DD` (de `surface-container-highest`) |
| Radio de tarjeta | 14px | `rounded.lg = 16px` |
| Radio boton/input | 10-12px | `rounded.md = 12px` |

## 3. Tokens de color

### 3.1 Base y superficies

| Token | Hex | Uso |
|---|---|---|
| `surface` | `#FBF9F4` | Fondo de pantalla. |
| `surface-dim` | `#DCDAD5` | Fondo atenuado. |
| `surface-bright` | `#FBF9F4` | Fondo claro. |
| `surface-container-lowest` | `#FFFFFF` | Tarjetas (Layer 1). |
| `surface-container-low` | `#F5F3EE` | Secciones. |
| `surface-container` | `#F0EEE9` | Contenedores. |
| `surface-container-high` | `#EAE8E3` | Contenedores elevados. |
| `surface-container-highest` | `#E4E2DD` | Maxima elevacion / divisores. |
| `on-surface` | `#1B1C19` | Texto principal. |
| `on-surface-variant` | `#424844` | Texto secundario. |
| `outline` | `#737873` | Bordes fuertes. |
| `outline-variant` | `#C2C8C2` | Bordes suaves / inputs. |
| `surface-tint` | `#4D6356` | Tinte. |
| `inverse-surface` | `#30312E` | Superficie inversa. |
| `inverse-on-surface` | `#F2F1EC` | Texto sobre inversa. |

### 3.2 Primario, secundario y terciario

| Token | Hex |
|---|---|
| `primary` | `#152A1F` |
| `on-primary` | `#FFFFFF` |
| `primary-container` | `#2B4034` |
| `on-primary-container` | `#94AB9C` |
| `inverse-primary` | `#B4CCBC` |
| `secondary` | `#895200` |
| `on-secondary` | `#FFFFFF` |
| `secondary-container` | `#FDAA47` |
| `on-secondary-container` | `#6E4100` |
| `tertiary` | `#381F20` |
| `on-tertiary` | `#FFFFFF` |
| `tertiary-container` | `#503435` |
| `on-tertiary-container` | `#C39C9D` |

### 3.3 Fijos (fixed)

| Token | Hex |
|---|---|
| `primary-fixed` | `#D0E8D7` |
| `primary-fixed-dim` | `#B4CCBC` |
| `on-primary-fixed` | `#0B1F15` |
| `on-primary-fixed-variant` | `#364B3F` |
| `secondary-fixed` | `#FFDCBC` |
| `secondary-fixed-dim` | `#FFB86A` |
| `on-secondary-fixed` | `#2C1700` |
| `on-secondary-fixed-variant` | `#683D00` |
| `tertiary-fixed` | `#FFDADA` |
| `tertiary-fixed-dim` | `#E6BDBD` |
| `on-tertiary-fixed` | `#2C1516` |
| `on-tertiary-fixed-variant` | `#5D3F40` |

### 3.4 Estados y extras

| Token | Hex | Uso |
|---|---|---|
| `error` | `#BA1A1A` | Errores. |
| `on-error` | `#FFFFFF` | Texto sobre error. |
| `error-container` | `#FFDAD6` | Fondo de error. |
| `on-error-container` | `#93000A` | Texto sobre error-container. |
| `error-carmine` | `#B31D3F` | Error critico (token extra). |
| `background` | `#FBF9F4` | Igual a `surface`. |
| `on-background` | `#1B1C19` | Igual a `on-surface`. |
| `surface-variant` | `#E4E2DD` | Variante. |
| `accent-text` | `#9B5F12` | Texto de acento (ochre oscuro). |
| `secondary-text` | `#6B7268` | Metadatos / texto atenuado. |
| `divider` | `#E4E2DD` | Divisores (definido). |
| **`success`** | `#2F6B4A` | Confirmaciones / aprobado (definido). |
| `on-success` | `#FFFFFF` | Texto sobre success. |
| `success-container` | `#D0E8D7` | Fondo de badges de exito. |
| `on-success-container` | `#0B1F15` | Texto sobre exito. |
| **`warning`** | `#B26A00` | Advertencias / en revision (definido). |
| `on-warning` | `#FFFFFF` | Texto sobre warning. |
| `warning-container` | `#FFDCBC` | Fondo de badges de advertencia. |
| `on-warning-container` | `#683D00` | Texto sobre advertencia. |

> No hay modo oscuro en el MVP (solo claro). Los `inverse-*` existen por completitud.

## 4. Tipografia (fuente Inter)

| Token | Tamano | Peso | Line height | Letter spacing |
|---|---|---|---|---|
| `display-lg` | 36 | 700 | 44 | -0.02em |
| `headline-lg-mobile` | 30 | 700 | 38 | - |
| `headline-md` | 28 | 700 | 36 | -0.01em |
| `headline-sm` | 24 | 600 | 32 | - |
| `title-md` | 20 | 600 | 28 | - |
| `body-lg` | 16 | 400 | 24 | - |
| `body-md` | 14 | 400 | 20 | - |
| `label-md` | 14 | 600 | 20 | 0.01em |
| `label-sm` | 12 | 500 | 16 | - |

Reglas: titulos y navegacion en `primary`; descripciones en `secondary-text`. Para montos grandes
usar `display-lg`; en movil cambiar a `headline-lg-mobile`.

## 5. Espaciado, radios y elevacion

| Token | Valor |
|---|---|
| `spacing.unit` | 4px |
| `spacing.margin-mobile` | 16px |
| `spacing.margin-desktop` | 24px |
| `spacing.gutter` | 16px |
| `spacing.container-padding` | 20px |
| `stack.xs / sm / md / lg / xl` | 4 / 8 / 16 / 24 / 48 px |
| `rounded.sm` | 4px |
| `rounded.DEFAULT` | 8px |
| `rounded.md` | 12px |
| `rounded.lg` | 16px |
| `rounded.xl` | 24px |
| `rounded.full` | 9999px (pills) |
| `shadow.card` | `0 2px 12px rgba(43,64,52,0.05)` (tinte de `primary-container`) |

## 6. Componentes

- **Boton primario**: fondo `primary`, texto `on-primary`, altura minima 52px, radio 12px.
- **Boton secundario**: borde 1.5px `primary`, texto `primary`, fondo transparente.
- **Boton ghost**: texto `primary`, sin fondo.
- **Input**: fondo `surface-container-lowest`, borde 1px `outline-variant`; foco 2px `primary`;
  label persistente arriba en `label-md`/`primary`.
- **Card**: fondo `surface-container-lowest`, radio 16px, `shadow.card`.
- **Card de acento**: fondo `surface-container-low`, borde izquierdo 2px `secondary-container`.
- **Chips/badges de estado**: fondo = contenedor del estado (10-100% opacidad), texto del estado
  (`success-container`/`warning-container`/`error-container`).
- **Indicador de saldo**: fondo `secondary-container`, texto `on-secondary-container`.
- **List items**: alto minimo 64px, divisores `divider`, icono en circulo con 5% de `primary`.
- **Iconos**: Material Symbols Outlined, 24px, trazo 1.5px; color `primary` o `secondary-text`.

## 7. Estados obligatorios por pantalla

Cargando, vacio, error y contenido. Toda pantalla debe verse bien en los cuatro.

## 8. Reglas de banca (no negociables)

- Datos sensibles enmascarados (cuenta, documento).
- Objetivos tactiles >= 44px (movil); contraste accesible.
- Nunca mostrar datos de otro cliente.
- Mensajes de error claros y accionables.
- El diseno **no** introduce logica: solo presenta lo que responde el backend
  (`docs/19-ejecucion-dos-campos.md`).

## 9. Donde vive en el codigo y mapeo a Flutter

| Campo | Ubicacion | Contenido |
|---|---|---|
| Flutter | `frontend/lib/theme/app_colors.dart` | Tokens de color. |
| Flutter | `frontend/lib/theme/app_typography.dart` | Escala `TextTheme` (Inter). |
| Flutter | `frontend/lib/theme/app_spacing.dart` | Espaciado y radios. |
| Flutter | `frontend/lib/theme/app_theme.dart` | `ThemeData` (Material 3, `ColorScheme`). |
| Flutter | `frontend/lib/components/` | Componentes reutilizables. |
| Panel web | `admin-web/` | Mismo token set (CSS variables / Tailwind). |

### 9.1 Mapeo token -> `ColorScheme` (Flutter)

Los tokens con nombre Material 3 van directo al `ColorScheme`. Los que no existen como campo se
exponen por un `ThemeExtension`.

- Directos: `primary`, `onPrimary`, `primaryContainer`, `onPrimaryContainer`, `secondary`,
  `onSecondary`, `secondaryContainer`, `onSecondaryContainer`, `tertiary`, `onTertiary`,
  `tertiaryContainer`, `onTertiaryContainer`, `error`, `onError`, `errorContainer`,
  `onErrorContainer`, `surface`, `onSurface`, `surfaceDim`, `surfaceBright`,
  `surfaceContainerLowest`, `surfaceContainerLow`, `surfaceContainer`,
  `surfaceContainerHigh`, `surfaceContainerHighest`, `onSurfaceVariant`, `outline`,
  `outlineVariant`, `inverseSurface`, `onInverseSurface`, `inversePrimary`, `surfaceTint`.
- Por `ThemeExtension` (no estan en `ColorScheme`): `accentText`, `secondaryText`, `errorCarmine`,
  `divider`, `success`, `onSuccess`, `successContainer`, `onSuccessContainer`, `warning`,
  `onWarning`, `warningContainer`, `onWarningContainer`.
- `background`/`onBackground` se tratan como alias de `surface`/`onSurface`.

## 10. Mockups

Los mockups y su registro viven en `docs/design/`:
- Imagenes: `docs/design/`.
- Registro: `docs/design/mockups.md` (archivo -> pantalla -> HU -> brief -> estado).
- Link de Figma del Sprint 1 (creacion de cuenta): `docs/design/figma-sprint1-creacionCuenta.txt`.

## 11. Como aplicarlo a una tarea

1. El brief de UI incluye este documento en su "Context pack" y su `Mockup:`.
2. El agente implementa primero el **tema** (tokens) y luego la pantalla.
3. Usa solo los componentes de la seccion 6 y cubre los 4 estados (seccion 7).
4. Se revisa contra el mockup y el checklist de la seccion 8.
