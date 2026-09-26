---
name: Eucalipto y Ocre
colors:
  surface: '#fbf9f4'
  surface-dim: '#dcdad5'
  surface-bright: '#fbf9f4'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f5f3ee'
  surface-container: '#f0eee9'
  surface-container-high: '#eae8e3'
  surface-container-highest: '#e4e2dd'
  on-surface: '#1b1c19'
  on-surface-variant: '#424844'
  inverse-surface: '#30312e'
  inverse-on-surface: '#f2f1ec'
  outline: '#737873'
  outline-variant: '#c2c8c2'
  surface-tint: '#4d6356'
  primary: '#152a1f'
  on-primary: '#ffffff'
  primary-container: '#2b4034'
  on-primary-container: '#94ab9c'
  inverse-primary: '#b4ccbc'
  secondary: '#895200'
  on-secondary: '#ffffff'
  secondary-container: '#fdaa47'
  on-secondary-container: '#6e4100'
  tertiary: '#381f20'
  on-tertiary: '#ffffff'
  tertiary-container: '#503435'
  on-tertiary-container: '#c39c9d'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d0e8d7'
  primary-fixed-dim: '#b4ccbc'
  on-primary-fixed: '#0b1f15'
  on-primary-fixed-variant: '#364b3f'
  secondary-fixed: '#ffdcbc'
  secondary-fixed-dim: '#ffb86a'
  on-secondary-fixed: '#2c1700'
  on-secondary-fixed-variant: '#683d00'
  tertiary-fixed: '#ffdada'
  tertiary-fixed-dim: '#e6bdbd'
  on-tertiary-fixed: '#2c1516'
  on-tertiary-fixed-variant: '#5d3f40'
  background: '#fbf9f4'
  on-background: '#1b1c19'
  surface-variant: '#e4e2dd'
  accent-text: '#9B5F12'
  secondary-text: '#6B7268'
  error-carmine: '#B31D3F'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 36px
    fontWeight: '700'
    lineHeight: 44px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '700'
    lineHeight: 36px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  title-md:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: 0.01em
  label-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
  headline-lg-mobile:
    fontFamily: Inter
    fontSize: 30px
    fontWeight: '700'
    lineHeight: 38px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  unit: 4px
  margin-mobile: 16px
  margin-desktop: 24px
  gutter: 16px
  container-padding: 20px
  stack-xs: 4px
  stack-sm: 8px
  stack-md: 16px
  stack-lg: 24px
  stack-xl: 48px
---

## Brand & Style

This design system shifts away from traditional financial tropes toward an editorial, organic-modernist aesthetic. It is defined by a sophisticated "Eucalyptus and Ochre" palette that evokes stability through earthy, natural tones rather than the synthetic blues of 90s banking. 

The personality is professional yet grounded, targeting an audience that values clarity and refined aesthetics. The design style is **Minimalism** with a focus on high-contrast legibility and structured whitespace. By using "Stone" as a base canvas instead of pure white, the UI feels more like a physical document or a high-end publication, fostering an emotional response of calm and trustworthiness.

## Colors

The color strategy relies on a "Nature-Industrial" contrast. The primary **Eucalyptus** (#2B4034) is used for all high-level navigation, headings, and primary actions to ensure a deep, authoritative presence that meets AAA accessibility standards against the stone background.

- **Surface Strategy:** The base layer is **Stone** (#F5F3EE). To create depth, secondary containers can use a slightly lighter tint or pure white to "lift" content.
- **The Ochre Duo:** **Ochre** (#D98C2B) is used strictly for fills, progress bars, and decorative accents. All interactive or informational text related to these accents must use **Darker Ochre** (#9B5F12) to maintain readability.
- **Functional States:** **Secondary Text** (#6B7268) provides a muted sage alternative for metadata, reducing visual noise. **Carmine Red** (#B31D3F) is reserved for critical errors, chosen for its sophisticated, deep hue that aligns with the organic palette.

## Typography

This design system uses **Inter** exclusively to maintain a systematic, clean, and utilitarian hierarchy. The "editorial" feel is achieved through tight tracking on large headings and generous line heights on body text.

- **Visual Weight:** Hierarchy is primarily established through the shift between Eucalyptus (Primary) for headings and Secondary Text for descriptions.
- **Large Scales:** For balance displays, use `display-lg`. On mobile devices, transition to `headline-lg-mobile` to prevent horizontal overflow of large currency values.
- **Clarity:** Labels use a slightly higher font weight (600) to ensure they stand out as structural guides even at smaller sizes.

## Layout & Spacing

The system follows a strict **4px incremental rhythm**. The layout philosophy is a **fixed grid** approach for content containers to maintain the editorial structure.

- **Mobile:** 4-column grid with 16px margins and 16px gutters.
- **Desktop:** 12-column grid with a max-width of 1200px, centered.
- **Padding:** Use `container-padding` (20px) inside cards to provide a luxurious sense of space around financial data.
- **Vertical Rhythm:** Use `stack-lg` (24px) to separate distinct sections and `stack-sm` (8px) for grouping related items like inputs and their labels.

## Elevation & Depth

Depth is achieved through **Tonal Layers** and extremely soft shadows to preserve the flat, modern aesthetic. 

- **Layer 0 (Base):** The Stone (#F5F3EE) background.
- **Layer 1 (Cards):** Pure White (#FFFFFF) surfaces. This creates a crisp contrast that helps information pop.
- **Shadows:** Avoid traditional heavy drop shadows. Use a subtle, tinted ambient shadow for Layer 1 surfaces: `0px 2px 12px rgba(43, 64, 52, 0.05)`. The green tint in the shadow helps it feel integrated with the Eucalyptus brand color.
- **Outlines:** Use low-contrast 1px outlines in `#D1CDC2` for input fields and non-elevated containers.

## Shapes

The shape language is "Structured-Soft." Following the 14px card requirement, the system uses a **Rounded** (Level 2) logic to create a consistent nested hierarchy.

- **Cards:** 14px (approx 0.875rem) - the primary container radius.
- **Interactive Elements:** Buttons and Input fields should use 10px to 12px to sit harmoniously within the 14px cards.
- **Small Elements:** Tooltips and badges use 6px to 8px.
- **Pill Factor:** Only use pill-shapes (full radius) for small status tags or search bars to distinguish them from standard action buttons.

## Components

### Buttons
- **Primary:** Background Eucalyptus (#2B4034), Text White (#FFFFFF). Minimum height 52px.
- **Secondary:** Border 1.5px Eucalyptus (#2B4034), Text Eucalyptus. 
- **Tertiary/Ghost:** Text Eucalyptus, no background. Used for less prominent actions.

### Input Fields
- **Default:** White background, 1px border in `#D1CDC2`. 
- **Active/Focus:** 2px border in Eucalyptus (#2B4034).
- **Labels:** Always persistent above the field using `label-md` in Eucalyptus.

### Cards
- **Standard Card:** White background, 14px radius, ambient shadow. 
- **Accent Card:** Stone background with a 2px left-border of Ochre (#D98C2B) for featured financial products.

### Chips & Badges
- **Status Badges:** Use a light 10% opacity background of the state color (e.g., Carmine for errors) with full opacity text for readability. 
- **Balance Indicators:** Use Ochre (#D98C2B) background with White text for high-importance value highlights.

### List Items
- Vertical height of 64px minimum. Use thin dividers in `#EBE8E0`. Leading icons should be placed in a subtle circular background using a 5% tint of Eucalyptus.

### Icons
- Use 24px outlined icons with a 1.5px stroke. Icons default to Eucalyptus, or Secondary Text (#6B7268) when used in a supportive, non-interactive role.