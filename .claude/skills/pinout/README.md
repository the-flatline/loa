# pinout — component documentation for loa

Single-component pinout diagrams generated from YAML, because the vendor docs for these
modules are thin, inconsistent, or absent.

    python3 generate.py components/*.yaml -o out/

Seven components are specced, all verified during the loa build on 2026-09-07. The most
useful one is `lcd28-show-display.yaml`: a map of the Pi header pins the 2.8" touchscreen
consumes, which is not documented anywhere in one place, and which collides with the
PIR, the sonar TRIG and the ECHO divider tap.

See `SKILL.md` for the schema and conventions.
