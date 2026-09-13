# Website Builder R1 Skills

Explicit read-only Hermes skills for the Website Builder runtime.

These skills are **read-only at runtime**. They describe conventions, rules, and
contracts. They do NOT implement execution systems.

## Skill Index

| Skill         | Directory                        | Purpose                                  |
| ------------- | -------------------------------- | ---------------------------------------- |
| Environment   | `website-builder-environment/`   | Workspace conventions and boundaries     |
| Product Scope | `website-builder-product-scope/` | R1 scope and no-business-invention rules |
| Design DNA    | `website-builder-design-dna/`    | Minimal Design DNA contract              |

Each skill is a standard Hermes skill directory containing a `SKILL.md` with
frontmatter (`name`, `description`, `version`, `metadata.hermes.*`).

## Rules

1. These skills are **Website Builder specific**. Do not copy unrelated skills.
2. Do not install Omarchy. Omarchy is not an R1 dependency.
3. Do not mutate Trading Hermes configuration (`~/.hermes`).
4. Shared skills are read-only. Generated projects must not modify them.
5. UI UX Pro Max is **not** vendored here. FRONTEND references the real
   `ui-ux-pro-max` Hermes skill already installed in the Website Hermes profile
   when it needs design guidance.
