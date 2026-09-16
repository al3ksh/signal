---
name: SIGNAL
description: A private, themeable infrastructure instrument.
colors:
  bg: "#0d0e10"
  surface: "#17181a"
  text: "#f0f1f3"
  muted: "#92949a"
  line: "#323438"
  accent: "#d4d7de"
  red: "#f5a495"
  glacier: "#83ceff"
  ember: "#ffba83"
  iris: "#c0afff"
typography:
  headline: { fontFamily: "Space Grotesk Variable", fontSize: "clamp(30px, 3vw, 43px)", fontWeight: 500 }
  body: { fontFamily: "Space Grotesk Variable", fontSize: "13px" }
  label: { fontFamily: "JetBrains Mono Variable", fontSize: "9px" }
rounded: { tag: "3px", control: "4px", nav: "5px", panel: "7px", themeDialog: "10px" }
---

# SIGNAL design system

## User commitments

Keep the dense, experimental technical instrument layout. The user explicitly rejected the olive/green palette and requested neutral gray-black as the default, custom themes, more motion and interactions, and an English interface. All displayed measurements remain real. Health failures retain text labels and an independent attention color.

## Theme system

`src/theme.tsx` owns runtime semantic colors. `src/tokens.css` is the Carbon startup fallback. Every surface, selection, chart, border, input and overlay consumes tokens; no olive colors remain. `--lime` is a compatibility alias for the active accent, not a green brand commitment.

Presets: Carbon (#0d0e10 / #d4d7de), Glacier (#0c1117 / #83ceff), Ember (#15100d / #ffba83), Iris (#111019 / #c0afff). Custom themes accept canvas and accent hex colors. Derived text is checked against both canvas and surface (target 7:1 main, minimum 4.5:1; 4.5:1 muted). The accent also adapts to selected-control backgrounds. Accent foreground chooses pure black or white. Midtone canvases adjust surface elevation to preserve text contrast. Light custom canvases are supported.

Theme changes preview immediately and persist under `signal.theme.v2`. The modal has native focus containment, Escape dismissal, labeled color/hex controls, preset selection semantics, and a reset to Carbon. Motion preference is included. Preferences belong to this browser, not the monitored host.

## Type and layout

Self-hosted Space Grotesk provides headings and application names; self-hosted JetBrains Mono provides measurements and instrumentation. Existing dense type steps remain intentional: metadata 6–9px, controls and rows 8–12px, body 13px, titles 14–27px, metric values 29–43px. The mobile scale preserves the user's borderline-readability brief.

The sidebar becomes an icon rail at 1250px and a top navigation bar at 640px. The metrics form four columns on wide screens and two below 1000px. The table and container map share the main working region, with processes and the observation journal below. The theme dialog fits the viewport and scrolls internally when needed.

## Motion and interaction

- Live numeric readings ease to new values over 480ms. Meter changes use scaleX transforms.
- Table sorting and pinning preserve row continuity with a 320ms FLIP transform. Pinning persists by container name under `signal.pinned`.
- Hover/focus links table rows and map nodes. Other nodes recede and the selected node exposes CPU/RAM.
- The map switches between an orbit and a matrix. Node positions transition over 550ms; a rotating sweep and a bounded set of packets visualize observation flow, not measured network packets.
- View transitions last 240–300ms. The inspector arrives from the right in 350ms; controls respond in 150–200ms.
- Log tools filter actual returned lines, control wrapping, and optionally follow refreshed snapshots every five seconds.
- Charts retain keyboard sample inspection. Navigation, presets, filters, range selectors and toggles expose selected state.

`prefers-reduced-motion` always wins over the full-motion setting. The quiet mode removes transitions and loops. The map stops when paused, outside the viewport or in a hidden document; other CSS loops pause when the document is hidden. Effects run client-side, with no change to the host collector interval.

## Boundaries

Do not restore green-tinted hardcoded backgrounds. Do not introduce Docker mutations, command execution, invented uptime or historical values. Preserve private authentication and actual container data. External alert delivery and application-level health probes remain separate capabilities.
