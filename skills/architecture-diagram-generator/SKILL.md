---
name: architecture-diagram-generator
description: Create polished dark-themed architecture diagrams as self-contained HTML+SVG files. Use when the user asks for system, infrastructure, cloud, security, or network topology diagrams.
version: 1.1.0
author: Cocoon AI
category: diagram
---

# Architecture Diagram Generator

Create professional technical architecture diagrams as self-contained HTML files with inline SVG graphics and CSS styling.

## Usage

When the user asks for an architecture, system, infrastructure, network, security, or topology diagram:

1. Read this full skill definition
2. Use the provided resources as guidance
3. Generate a self-contained HTML file with inline SVG
4. Output only the final HTML artifact

## Required behavior

- Use a dark technical theme
- Use inline SVG, not external images
- Make the file self-contained
- Include a compact summary structure in the HTML
- Prefer architecture-style diagrams unless the user explicitly asks for another type

## Inputs

The primary input is the user's natural language system description.

If needed, infer:
- components
- boundaries
- data flow
- security zones
- external dependencies

## Resource usage

Check the `resources/template.html` file in this skill package for the base structure and visual pattern.

## Output

Return only a complete HTML document inside a fenced `html` code block.
