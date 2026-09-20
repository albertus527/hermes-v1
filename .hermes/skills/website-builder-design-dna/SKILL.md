---
name: website-builder-design-dna
description: Website Builder R1 minimal Design DNA contract.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [website-builder, design-dna]
    category: website-builder
---

# Minimal Design DNA Contract

## Purpose

Design DNA is the design source of truth for a Website Builder R1 project.
It captures the concrete design decisions made by FRONTEND.

## Ownership

- **FRONTEND owns Design DNA.** FAST must not own it.
- Revisions update Design DNA where relevant rather than accumulating
  disconnected prompts.

## Minimal Fields

```yaml
design_dna:
  version: 1
  brand_personality: '' # e.g., "premium, modern, approachable"
  palette:
    primary: '' # e.g., "#1a1a2e"
    secondary: ''
    accent: ''
    background: ''
    text: ''
  typography:
    heading_font: ''
    body_font: ''
    scale: '' # e.g., "1.25"
  spacing:
    density: '' # e.g., "comfortable", "compact"
    unit: '' # e.g., "8px"
  page_inventory: [] # e.g., ["home", "services", "contact"]
  layout:
    navigation: '' # e.g., "top-bar", "sidebar"
    max_width: '' # e.g., "1280px"
  motion:
    enabled: true
    duration: '' # e.g., "300ms"
    easing: '' # e.g., "ease-out"
  primary_cta:
    label: ''
    destination: '' # e.g., "https://wa.me/..."
    style: '' # e.g., "primary-button"
  assets: [] # list of asset references
  verified_content: {} # facts verified by user
  unresolved_facts: [] # facts still needed
```

## Rules

1. Design DNA is persisted in project state.
2. `design_dna_version` increments on each update.
3. Never invent business facts in Design DNA.
4. Clearly label placeholders in preview; never silently in production.
5. Design DNA is a DECLARATIVE description of design intent, never an
   acceptance-test contract. Do NOT write implementation-level acceptance
   criteria into Design DNA: no DOM attributes, no CSS class names, no file
   paths, no event handlers, no component counts, no exclusivity rules
   (e.g. "exactly one CTA"), and no per-element QA or acceptance contracts.
   Design DNA records intent (palette, typography, personality, layout
   character); it is not a test specification and is never an authoritative
   source of blocking QA requirements.
