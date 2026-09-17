---
name: website-cloner
description: Clone any website 1:1 by screenshot analysis and AI reconstruction.
version: "1.0"
author: Hermes / adapted from JCodesMore/ai-website-cloner-template (MIT)
license: MIT
metadata:
  hermes:
    tags: ["website", "clone", "frontend", "browser"]
    category: software-development
---

# Website Cloner Skill

Clone any website by inspecting it via browser tools and rebuilding it.

## When to Use
- User says "clone this website" or "copy this site"
- User provides a URL and wants a replica
- User wants to rebuild an existing site design

## How to Run

### Phase 1: Reconnaissance
1. `browser_navigate(url)` — load the target site
2. `browser_snapshot(full=true)` — capture full page structure
3. `browser_vision(annotate=true)` — screenshot with element labels
4. Scroll through all sections: `browser_scroll(direction="down")` + snapshot

### Phase 2: Extract Design Tokens
From the screenshot analysis, extract:
- Color palette (background, text, accents, borders)
- Typography (font families, sizes, weights)
- Spacing (padding, margins, gaps)
- Border radius, shadows, gradients

### Phase 3: Write Component Spec
Create `CLAUDE.md` or `SPEC.md` with:
- Page sections (header, hero, features, footer, etc.)
- Component hierarchy
- Responsive breakpoints
- Interactive elements catalogue

### Phase 4: Build
- Create HTML/CSS files replicating each section
- Use Tailwind CSS for rapid styling
- Test responsiveness at 3 breakpoints
- Verify against original screenshots

## Verification
- Side-by-side screenshot comparison
- All sections at correct viewport positions
- Colors match within 5% tolerance
- Responsive behavior preserved
