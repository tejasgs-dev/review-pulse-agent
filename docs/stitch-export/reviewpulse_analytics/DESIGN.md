---
name: ReviewPulse Analytics
colors:
  surface: '#f9f9ff'
  surface-dim: '#d3daef'
  surface-bright: '#f9f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f1f3ff'
  surface-container: '#e9edff'
  surface-container-high: '#e1e8fd'
  surface-container-highest: '#dce2f7'
  on-surface: '#141b2b'
  on-surface-variant: '#464555'
  inverse-surface: '#293040'
  inverse-on-surface: '#edf0ff'
  outline: '#777587'
  outline-variant: '#c7c4d8'
  surface-tint: '#4d44e3'
  primary: '#3525cd'
  on-primary: '#ffffff'
  primary-container: '#4f46e5'
  on-primary-container: '#dad7ff'
  inverse-primary: '#c3c0ff'
  secondary: '#5a5e69'
  on-secondary: '#ffffff'
  secondary-container: '#dee2ef'
  on-secondary-container: '#60646f'
  tertiary: '#005522'
  on-tertiary: '#ffffff'
  tertiary-container: '#00702f'
  on-tertiary-container: '#78f591'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e2dfff'
  primary-fixed-dim: '#c3c0ff'
  on-primary-fixed: '#0f0069'
  on-primary-fixed-variant: '#3323cc'
  secondary-fixed: '#dee2ef'
  secondary-fixed-dim: '#c2c6d3'
  on-secondary-fixed: '#171c25'
  on-secondary-fixed-variant: '#424751'
  tertiary-fixed: '#7ffc97'
  tertiary-fixed-dim: '#62df7d'
  on-tertiary-fixed: '#002109'
  on-tertiary-fixed-variant: '#005320'
  background: '#f9f9ff'
  on-background: '#141b2b'
  surface-variant: '#dce2f7'
typography:
  display-stat:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-page:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '600'
    lineHeight: 36px
    letterSpacing: -0.02em
  headline-page-mobile:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.01em
  headline-section:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: -0.01em
  headline-card:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '600'
    lineHeight: 22px
    letterSpacing: -0.005em
  body-default:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 22px
  body-medium:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '500'
    lineHeight: 22px
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  meta-label:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.05em
  caption:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '500'
    lineHeight: 14px
    letterSpacing: 0.02em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1.5rem
  gutter-mobile: 1rem
  margin: 2rem
  margin-mobile: 1rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2rem
---

## Brand & Style

The design system embodies a calm, precise, and highly analytical atmosphere tailored for financial analysts, operations leaders, and risk officers. It prioritizes clarity over visual noise, delivering an interface that feels dependable, composed, and effortless to parse over extended monitoring sessions.

The aesthetic leans on **Modern Corporate Minimalism with Precision Metrics**:
- **Canvas Atmosphere:** Airy and grounded, utilizing cool, neutral grays with crisp pure-white surfaces.
- **Tone:** Methodical, transparent, and authoritative. It removes decorative clutter to let high-density quantitative signals and performance metrics lead.
- **Visual Distinction:** Subtle 1px structural framing, hyper-soft ambient depth, and intentional indigo accents that drive focus directly to key metrics, delta changes, and anomalies without inducing cognitive fatigue.

## Colors

The palette relies on a disciplined, high-readability structure calibrated strictly for a light environment.

### Core Palette
- **Canvas Base:** `#F7F8FA` — Provides a restful, low-glare backdrop that separates seamlessly from pure white containers.
- **Card / Surface:** `#FFFFFF` — Anchors data cards, tables, and modal elements.
- **Surface Outline / Divider:** `#E5E7EB` — 1px boundary lines ensuring clean visual segregation.
- **Primary Accent:** `#4F46E5` (Indigo) — Primary actions, active navigation states, trendlines, and primary chart series.
- **Subtle Tint Accent:** `#EEF2FF` — Pill/badge fills, hover states, filter backgrounds, and secondary bar charts.

### Typography & Content
- **Headings & Key Metrics:** `#111827` (Near-black) — Maximum clarity and authoritative hierarchy.
- **Body & Secondary Copy:** `#4B5563` (Muted Slate) — Comfortable, scannable reading contrast for descriptions and table cell data.
- **Meta, Labels & Captions:** `#9CA3AF` (Muted Neutral) — De-emphasized guidance labels, timestamps, and column headers.

### Semantic Status Colors
- **Positive / Stable:** `#16A34A` — Positive deltas, successful transaction volumes, compliant states.
- **Warning / Attention:** `#D97706` — Latency spikes, review bottlenecks, threshold alerts.
- **Critical / Negative:** `#DC2626` — Anomalies, failed checks, elevated risk indicators.

## Typography

The typography scale relies exclusively on `Inter` to deliver systematic clarity and rhythm across dashboards.

### Numerical Fidelity
All financial values, data tables, counters, and statistics must use tabular lining figures (`font-variant-numeric: tabular-nums` or `tnum`) to ensure aligned vertical scanning down columns and comparison cards.

### Hierarchy Guidelines
- **Page Titles (`headline-page`):** Set at 28px/600 in `#111827`. Scales down to 22px on mobile viewpoints.
- **Section Headers (`headline-section`):** 18px/600, demarcating card groups or data views.
- **Stat Values (`display-stat`):** 32px/600 in `#111827` using tabular figures with tight tracking (`-0.02em`) for maximum visual weight.
- **Body Copy (`body-default`):** 15px/400 in `#4B5563`, offering balanced proportion between small desktop UI elements and readability.
- **Meta Labels (`meta-label`):** 12px/600, rendered uppercase with `letterSpacing: 0.05em` in `#9CA3AF`. Applied to card titles, column labels, badge headers, and metric identifiers.

## Layout & Spacing

The layout is built around a structured, fluid grid system capped by a maximum container width to maintain legibility across ultra-wide monitors.

### Layout Architecture
- **Max Width:** The primary layout caps strictly at `1200px` centered horizontally.
- **Grid Configuration:** 
  - **Desktop (1024px+):** 12-column grid, `gutter` of 24px (`1.5rem`), outer canvas padding `margin` of 32px (`2rem`). Stat metrics commonly inhabit 3-column (4 cards across) or 4-column (3 cards across) spans.
  - **Tablet (768px – 1023px):** 6-column grid, `gutter` of 24px (`1.5rem`), outer padding of 24px (`1.5rem`). Stat cards reflow to 2 across (span 3).
  - **Mobile (< 768px):** 4-column grid or single-column stacked hierarchy, `gutter-mobile` of 16px (`1rem`), outer canvas padding `margin-mobile` of 16px (`1rem`).

### Component Spacing Rhythm
- **Card-to-Card Grid Gap:** Fixed at 24px (`1.5rem` / `space-lg`).
- **Internal Card Padding:** Uniform 24px (`1.5rem` / `space-lg`) on all sides.
- **Micro Spacing:** 4px (`space-xs`) for status dot spacing; 8px (`space-sm`) for badge padding and label-to-value gaps; 16px (`space-md`) between sub-sections within cards.

## Elevation & Depth

Visual depth is deliberately restrained to foster a quiet, distraction-free environment. Rather than dramatic drop shadows or stacked lighting layers, the system relies on crisp hairline boundaries reinforced by a micro-ambient shadow.

### Layer Hierarchy
1. **Canvas Layer (`#F7F8FA`):** The structural floor of the interface. Completely flat, no elevation.
2. **Card & Panel Layer (`#FFFFFF`):**
   - Border: Solid 1px `#E5E7EB`.
   - Shadow: `0 1px 3px rgba(0, 0, 0, 0.05)`.
   - The shadow provides subtle separation from the `#F7F8FA` background without lifting the card perceptually into the foreground.
3. **Floating Overlays & Popovers (Dropdowns, Datepickers, Tooltips):**
   - Background: Pure White (`#FFFFFF`).
   - Border: 1px `#E5E7EB`.
   - Shadow: `0 4px 12px rgba(0, 0, 0, 0.06), 0 1px 2px rgba(0, 0, 0, 0.04)`.
   - Provides clear contextual elevation during active interaction.

Hover effects on analytical surfaces do not elevate or shift vertically; they shift subtly via outline or background tone (e.g., border color shifting to `#D1D5DB` or table rows highlighting to `#F9FAFB`).

## Shapes

The design uses a clean geometry grounded in an intentional 16px corner radius for major containers, counterbalanced by smaller, compact radii for actionable elements.

### Curvature Tokens
- **Container Surfaces & Cards:** Explicitly set to `16px` (`rounded-xl` in this level-2 configuration) to create soft, modern card silhouettes that counter the density of numerical data.
- **Controls & Form Elements (Inputs, Buttons):** `8px` (`0.5rem`) for compact efficiency and crisp touch targets.
- **Chips, Badges & Status Indicators:** `9999px` (Pill shape) to contrast against rectangular card structures.
- **Status Dots:** 8px × 8px perfect circular elements (`rounded-full`).

## Components

### 1. Stat Cards
- **Container:** `#FFFFFF`, 16px border-radius, 1px `#E5E7EB` border, `0 1px 3px rgba(0,0,0,0.05)` shadow, 24px inner padding.
- **Header:** Meta label (`12px`, bold, uppercase, `#9CA3AF`, tracking `0.05em`) paired optionally with a subtle status badge or icon.
- **Metric:** Large display number (`32px`, semibold, `#111827`, tabular numerals).
- **Sub-Metric / Delta:** 13px regular text with a trailing status badge indicating period comparison (+4.2% in `#16A34A` or -1.8% in `#DC2626`).

### 2. Buttons
- **Primary:** Solid `#4F46E5` background, `#FFFFFF` text (`14px` medium), 8px border-radius, `8px 16px` padding. Hover: `#4338CA`.
- **Secondary:** `#FFFFFF` background, 1px `#E5E7EB` border, `#111827` text. Hover: `#F9FAFB`.
- **Tertiary / Ghost:** Transparent background, `#4B5563` text. Hover: `#F3F4F6`, `#111827` text.

### 3. Chips & Badges
- **Informational / Accent Badge:** `#EEF2FF` fill with `#4F46E5` text, pill radius, padding `4px 10px`, `12px` font weight 500.
- **Positive Badge:** `#DCFCE7` fill with `#16A34A` text.
- **Warning Badge:** `#FEF3C7` fill with `#D97706` text.
- **Critical Badge:** `#FEE2E2` fill with `#DC2626` text.

### 4. Status Dots
- Pure 8px × 8px solid circles positioned inline with label text with an 8px horizontal gap.
- **Green:** `#16A34A`
- **Amber:** `#D97706`
- **Red:** `#DC2626`
- Used to signify continuous health checks, real-time sync indicators, or transaction categorization.

### 5. Input Fields & Selects
- **Base:** `#FFFFFF` fill, 1px `#E5E7EB` border, 8px border-radius, `10px 14px` padding.
- **Text:** 14px `#111827`, placeholder text in `#9CA3AF`.
- **Focus:** 1px `#4F46E5` outline with an additional subtle 3px box-shadow ring in `rgba(79, 70, 229, 0.12)`.

### 6. Data Tables & Lists
- **Header Row:** `#F9FAFB` background, bottom border 1px `#E5E7EB`. Headers use `meta-label` styling (`12px`, uppercase, `#9CA3AF`).
- **Data Rows:** `#FFFFFF` background, 48px row height, horizontal border bottom 1px `#F3F4F6`. Cell typography is 14px `#4B5563` (with identifiers in `#111827` medium). Hover state: `#F9FAFB`.

### 7. Form Controls (Checkboxes & Radios)
- **Border:** 1px `#D1D5DB`, 4px radius (checkbox) or circular (radio), `#FFFFFF` base.
- **Selected State:** `#4F46E5` fill with white checkmark/dot.