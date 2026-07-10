---
name: screenshot-product-qa
description: Verify a local web app or desktop UI with screenshots, visual inspection, redaction checks, responsive states, and product-quality notes. Use when validating design fidelity, interface polish, or public-safe screenshots.
metadata:
  short-description: Screenshot-based product and UI QA
---

# Screenshot Product QA

Use this skill when the user asks whether a product UI is ready, beautiful, accurate to a
reference, or safe to show publicly.

## QA loop

1. Open the actual running surface, not a static guess.
2. Capture desktop and narrow/mobile screenshots when applicable.
3. Inspect the image visually before saving or publishing it.
4. Reject or redact screenshots containing:
   - secrets, tokens, keys, cookies, `.env` values;
   - private customer data, private messages, channel IDs;
   - unreleased commercial internals that should not be public.
5. Check product basics:
   - first viewport says what the product does;
   - navigation is clear;
   - empty/error/loading states exist;
   - primary action is obvious;
   - typography fits containers;
   - no incoherent overlaps;
   - real workflow is available, not only a landing page.
6. Check design fidelity when references exist:
   - spacing, density, colors, hierarchy, radius, icons, sidebar, tables/cards, modal states;
   - compare screenshots honestly and list mismatches.
7. Run a smoke interaction:
   - create/read/update or run the core flow where safe;
   - do not publish/send external messages unless the user explicitly asked and HITL is present.

## Output style

Lead with the blocking issues first, then polish issues, then what already works. If you changed
the UI, include what was verified and what still needs a human eye.
