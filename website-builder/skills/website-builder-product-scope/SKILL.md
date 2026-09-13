---
name: website-builder-product-scope
description: Website Builder R1 product scope and no-business-invention rules.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [website-builder, scope]
    category: website-builder
---

# R1 Product Scope and No-Business-Invention Rules

## Supported

- landing pages, company profiles, portfolios
- restaurant/cafe, barbershop/service-business, event/wedding, personal-brand sites
- product/SaaS marketing sites
- basic multi-page informational sites
- galleries
- outbound WhatsApp/social/contact links
- one supported contact-form integration with a verified destination
- responsive design, basic animation, SEO, accessibility, performance
- reference-driven redesign

## Not Supported

- native mobile/desktop apps
- authentication/user accounts
- custom CMS/admin panels
- shopping carts/payment processing
- custom booking engines
- complex databases/application dashboards
- ERP/full CRM/trading bots/games
- unrelated coding/debugging

## Core Rule

> Infer implementation details. Never invent business intent.

The system may infer:

- responsive behavior
- sensible typography
- accessibility basics
- navigation conventions
- performance practices

The system must NOT invent:

- services
- pricing
- addresses
- contact details
- testimonials
- claims
- conversion goals

## Scope Gate

Interpret as: WEBSITE, WEBSITE_RELATED, MIXED, OUT_OF_SCOPE, or UNCLEAR.

AI interprets; application code enforces authorization and capability boundaries.
