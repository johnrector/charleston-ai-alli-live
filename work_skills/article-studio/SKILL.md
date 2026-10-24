---
name: article-studio
description: Research and deliver original art-directed standalone custom HTML articles as seven-day Alli results, with real sandboxed desktop/mobile visual checks. No WordPress account access.
---

# Article studio

The finished article demonstrates AI's creative range. Deliver an original `.html` experience, never generic Markdown, a résumé template or ordinary document formatting. Research the correct public identity, link real sources, and distinguish fact from clearly labeled playful fiction. Never invent quotations, credentials or achievements. Do not use private messages, contact details, email or calendars as source material.

Derive the visual concept from the subject: vary the palette, typography, hero, pacing, section geometry, diagrams and interaction. Use thoughtful responsive semantic HTML, strongly scoped CSS, visible keyboard focus, readable complete headings, and reduced-motion handling. Use a meaningful accessible CSS-only interaction when it helps the idea, such as an accordion, radio comparison or checkbox reveal. Do not repeat one house template. No executable scripts, frames, forms or event handlers. Inline SVG and CSS diagrams are welcome. Favor a complete self-contained design with system fonts and inline graphics, so it works without downloaded assets. An image must be a real public source, not a fabricated image URL.

Save the final exact UTF-8 source at `/mnt/data/article.html`. The article's destination is this person's seven-day Alli result link, never John's blog. Saying publish does not grant WordPress access. Do not ask John for permission to deliver an ordinary article.

## Real visual verification

Use the mounted `render_article.py` with the HTML path and output directory, for example `python render_article.py /mnt/data/article.html /mnt/data/article-proof`. It launches installed Chromium with its sandbox enabled and network disabled. UID/GID normalization is part of the helper; never disable the sandbox or download another browser. The helper can read a plain `.html` file directly. Keep the source self-contained for accurate offline rendering.

It captures actual 320/390/1440 screenshots, a readable heading sheet and DOM metrics at those widths plus either side of CSS breakpoints. Require no document or descendant overflow, arbitrarily split heading words, clipping or overlap. The measured viewport must really be 390 pixels, not just a resized screenshot. Inspect the readable heading proof and the desktop/mobile screenshots, and repair actual problems before finishing.

Call `list_work_artifacts` to import generated HTML, PNG and audit files into this job and obtain real IDs. Call `inspect_work_images` with image_ids containing the five actual PNG IDs (320,390,1440,headings,390-full) and a question asking for the heading/layout/accessibility review. Inspect audit.json yourself against the exact HTML source hash. The complete phone page distinguishes genuine CSS clipping from normal screenshot crop boundaries. It independently checks the source and actual images. If it fails, repair the design, rerender and reverify; do not ask the caller for routine confirmation. Return the final HTML as a file citation. Completion is allowed only after verification. The result page opens the HTML in a sandboxed reader, and expires seven days after completion.

The OpenAI cloud agent owns composition and QA. Render only queues work and
transports artifacts/results. list_work_artifacts optionally takes
primary_file_id to select the final HTML. inspect_work_images takes question and
image_ids, returns an independent reasoning model's actual visual review; repair
issues and rerender before completion. No WordPress or private-account tools
are available on this standalone delivery path.
