---
name: pinout
description: Generate a single-component pinout diagram (SVG) from a YAML spec, and optionally a Fritzing part. Use when documenting what a module's pins do, what voltage they run at, or what a HAT/display steals from the Pi header — especially when the vendor datasheet is thin or missing.
---

# pinout

Renders one component per diagram from a YAML spec in `components/`.

    python3 generate.py components/hc-sr04.yaml -o out/
    python3 generate.py components/*.yaml -o out/ --fritzing

## Why this exists

The cheap-module datasheets omit the things that break a build. This schema makes
those fields mandatory, so the diagram cannot silently leave them out:

| field | the mistake it prevents |
|---|---|
| `voltage` on every signal pin | 5 V output into a 3.3 V GPIO |
| `danger: true` + `danger_note` | renders orange, boxed, and forced into "before you wire it" |
| `body_mm` | modules whose bodies collide even though their pins don't |
| `current_ma` | discovering the draw after the rail browns out |
| `bus` | I2C address clashes |
| `gotchas` | pull-ups, polarity, settling time, channel order |
| `source` | says "unverified" if you don't cite one |

## Adding a component

Copy an existing spec. Required: `name`, `body_mm`, `pins`. Every pin needs `n`,
`name` and `type` (`power` `gnd` `input` `output` `bidir` `i2c` `spi` `analog` `nc`).
Any pin that carries a level needs `voltage` — the generator refuses to render without it.

Set `danger: true` on any pin that will damage something if wired naively, and give a
`danger_note` saying what and why.

**Quote any gotcha containing a colon.** Unquoted, YAML turns it into a dict; the
validator catches this but the message is easier to understand if you just quote them.

## Conventions

- Pin spacing is enlarged for legibility. The body keeps its true aspect ratio and
  carries mm labels — the header line says so, so nobody scales off the picture.
- For a HAT or display, list **Pi header pin numbers** and treat the diagram as a map
  of what the board takes away from you. `lcd28-show-display.yaml` is the worked example.
- Cite `source`. "verified on the bench" is a legitimate source; silence is not.

## Fritzing export

`--fritzing` writes `out/fritzing/<name>.fzp` plus a breadboard SVG whose pin elements
carry the `connector<N>pin` ids the `.fzp` references. That is the real Fritzing custom
part structure, but **it has not been opened in Fritzing** — treat it as a starting
point and check the connectors land correctly before relying on it.

Tinkercad Circuits has no custom-part import, so it is out. EasyEDA is a schematic/PCB
tool rather than a breadboard tool; its part format is a poor fit for this job.
