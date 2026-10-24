---
name: wordpress-publisher
description: Create, publish, stage, update, and verify original art-directed custom HTML experiences on John Rector's WordPress site directly from Codex. Use whenever the user says write a blog, write an article, publish it, send this to my blog, upload these articles, make a WordPress draft, create a page or post, check what was published, connect WordPress, or otherwise wants public writing created for or moved into johnrector.me.
---

# Cloud execution adapter

This is John's exact personal WordPress Publisher standard, running without a Mac.
Use wordpress_context for the existing connected WordPress.com account, and the
wordpress_* functions for the credential-free, site-locked publisher adapter.
Do not invoke macOS Keychain or the Mac stdio server. All design, HTML, excerpt,
featured-media, metadata, exact-file dry-run, extraction and visual gates below
remain mandatory. No generic Markdown or reduced design standard.

Workflow: inspect recent public designs with wordpress_context; research the
correct subject using public professional sources. Do not use private accounts
as research material. Create a fresh art direction and custom HTML. Use actual
category/tag IDs from context; choose existing terms. Upload a relevant real
illustration with wordpress_media when needed; retain its actual URL/dimensions.

Run wordpress_prepare on the exact HTML. Then wordpress_snapshot(phase=preflight)
uploads an exact source/assets bundle into this isolated hosted container. Use
the provided render_article.py to render it with sandboxed Chromium, networking
blocked and UID/GID normalized. Cite the three viewport PNGs, heading-sheet PNG
and DOM audit JSON as actual file outputs; wordpress_artifacts imports the real /mnt/data files from this job’s isolated container and returns their IDs, even before file citations are emitted. Save preflight outputs in /mnt/data/wordpress-preflight and live outputs in /mnt/data/wordpress-live.
wordpress_publish requires these actual proofs and an independent visual review.
It stages and checks exact HTML, then publishes only on John's explicit request.
Next wordpress_snapshot(phase=live) captures the actual public page and public
assets; render that bundle with the same helper and call wordpress_verify.
Only after both extraction and live visual checks pass may you report completed.
If a check fails, repair the same article. Never invent screenshot evidence.

This container has only the publisher skill and public article context, not the
owner's private skill bundle, email, calendar, account credentials or contact
history. The application owns account transport and authorization.

---

# WordPress Publisher

Use the WordPress Publisher MCP tools for WordPress operations. Do not send the user to Terminal and do not ask them to create environment variables.

## Non-negotiable publication standard

Anything created, staged, published, or updated for John Rector's blogsite must be an original, art-directed custom HTML experience. Plain Markdown and ordinary WordPress article formatting are not acceptable public deliverables.

- Markdown may be used privately for notes or drafting, but the exact file sent to WordPress must end in `.html` or `.htm`.
- Never downgrade a designed article to Markdown. If custom HTML cannot be preserved, publish nothing and report the blocker.
- Treat every publication as a demonstration of AI's creative range. "Fancy" is not one house template.
- Derive the visual concept from the specific argument. Deliberately vary palette, typography, hero structure, pacing, section geometry, diagrams, and interaction model.
- Before designing, inspect recent custom HTML sources or recent live posts. Choose a materially different system from the most recent pieces. Do not reuse a successful layout with different words.
- Use meaningful, accessible, script-free interaction when it improves the idea: CSS radio sliders, scroll-snap sequences, accordions, checkbox reveals, timelines, comparisons, or another purpose-built mechanism. Do not repeat the same interaction mechanically.
- Use responsive semantic HTML, strongly scoped CSS, visible focus states, reduced-motion handling where relevant, and graceful behavior on mobile.
- Executable scripts are forbidden. The sole permitted script element is valid `application/ld+json` data, which the publisher places outside the declarative shadow root. The design remains isolated from theme styles.
- The publisher automatically activates single-post canvas mode: it removes the WordPress site header, header media, duplicate outer title/date, and their top spacing. The article's own designed title remains. Do not recreate generic WordPress chrome inside the experience.

## Routing

- If the user says `write a blog`, `write an article`, or equivalent without specifying visibility, create the finished custom HTML experience locally. Publish only when publication is explicit or an established scheduled workflow already authorizes it.
- If the user explicitly says `publish`, `publish it`, `make it live`, or equivalent, call `wordpress_publish_markdown` with `status: publish`. Their explicit request is the publishing authorization; do not ask for a second confirmation.
- If the user says `draft`, `stage`, or `upload for review`, use `status: draft`.
- When the user refers to `it`, `these`, `the articles`, or `the book`, identify the finished custom HTML file or files from the active workspace and pass their absolute paths. Prefer the narrowest exact set; do not publish planning notes, README files, Markdown files, or manuscript source.
- For the Absorption series, retain category `Absorption` plus tags `absorption`, `automation`, `attention`, and `AI`, while still giving every article its own creative direction.

## Creative process

1. Identify the article's central conceptual motion: contrast, accumulation, threshold, transformation, recursion, navigation, or another idea-specific structure.
2. Inspect recent custom HTML publications and note what must not be repeated: palette, type pairing, hero treatment, section rhythm, primary diagram, and interaction.
3. Choose a new visual metaphor and interaction grammar that make the argument easier to feel or understand.
4. Build one self-contained HTML fragment with a title comment or `<title>` element, a unique wrapper class, scoped CSS, semantic sections, and no global selectors or executable scripts, plus the required JSON-LD data.
5. Ensure the prose remains the cornerstone. Interactions and visuals must reveal the argument, not decorate it.
6. Validate the exact file before publishing.

## Display typography gate

Keep oversized editorial lettering when it serves the art direction or the user requests it. Correct the layout and wrapping instead of defaulting to smaller type.

- Headings must wrap at word boundaries. Do not use `overflow-wrap: anywhere` or `word-break: break-all` to make titles, section headings, or display labels fit. Avoid `ch` width limits narrower than their longest word; `ch` measures the zero glyph, not an average capital letter.
- Give display type enough room through column proportions, padding, heading width, intentional whole-word lines, and line height. When responsive fitting is needed, relate it to the actual text container and preserve the large visual scale. Never hide broken typography with clipping or overflow suppression.
- Before publication, inspect the title, every section heading, and display labels at 320px, true 390px, desktop width, and immediately around layout breakpoints. Wait for fonts to load and account for fallback fonts.
- Audit individual words using DOM text ranges: a word spanning multiple line rectangles reveals an arbitrary split even when the element and page have no horizontal overflow. Check descendant `scrollWidth/clientWidth`, painted text bounds against their containers, and adjacent line/element collisions.
- After publication, repeat the heading audit inside `.codex-html-experience.shadowRoot` at desktop and true-mobile widths. View the affected headings at readable screenshot scale; a full-page thumbnail and zero document overflow do not establish that typography is correct.
- Treat split words, clipped glyphs, overlapping lines, or unreadable display labels as failed visual verification. Repair the same post and repeat the affected checks before reporting completion. This gate covers the entire authored experience, not just its H1.

## Connection

Call `wordpress_connection_status` before the first live operation in a task.

If WordPress is not connected, call `wordpress_connect`. This opens one normal macOS password dialog and saves the application password in macOS Keychain. Never request the password in chat and never place it in a command, prompt, file, log, or tool argument.

## Publishing and verification

Use `wordpress_publish_markdown` for custom HTML sources only. The legacy tool name is retained for compatibility, but the server rejects Markdown. The tool is duplicate-safe: it matches by WordPress slug and updates an existing post instead of creating a second copy.

For every live publication:

1. Run a dry run against the one exact HTML file or exact intended batch.
2. Publish that same path with the authorized status, categories, and tags.
3. Read the returned results. Do not claim success unless `succeeded` equals the requested count, `failed` is zero, and the returned status is correct.
4. Open a cache-busted public URL in the signed-in browser and verify the rendered shadow-DOM experience at desktop and mobile widths.
5. Confirm the current design markers are present, the intended interaction works, typography is readable, theme styles do not leak in, the site header and duplicate outer title/date are absent, the artwork begins at the top, and there is no horizontal overflow.
6. If the API succeeds but the live page does not show the new experience, the job is not complete. Diagnose and correct it.

Report the title and real live outcome concisely. Do not include wp-admin links unless the user asks for them.

## Excerpt and machine-readable publication gate

Every article must carry both an explicit WordPress excerpt and full-text JSON-LD. These requirements supersede any older blanket ban on script tags: executable JavaScript remains forbidden; `<script type="application/ld+json">` is the sole permitted script element and contains data only. No new WordPress plugins or third-party services. Do not state a price in any post.

1. Put `<!-- Excerpt: ... -->` immediately below the title comment in the exact HTML source. Write a standalone one- or two-sentence claim from the piece, at least 45 characters, without hashtags, trailing ellipses, or “in this post.” The publisher now sets the WordPress `excerpt` field from this comment; the comment alone is not proof of saved metadata.
2. Upload/select a real featured image using the existing WordPress workflow before publication. Include `<!-- Featured-Media: ID -->` with its actual attachment ID. Verify its source URL and dimensions; never invent an image or hard-code dimensions without checking.
3. Include exactly one `<script type="application/ld+json">` containing a schema.org `BlogPosting` in the source. The publisher extracts this block before wrapping the design and puts it in the light DOM after the template, immediately before `<!-- /wp:html -->`. Do not manually wrap the source in a template. Required fields: `@context: https://schema.org`, `@type: BlogPosting`, `@id: CANONICAL_URL#jr-article`, `mainEntityOfPage: {"@type":"WebPage","@id":CANONICAL_URL}`, `url`, exact `headline` (under 110 characters), `description` equal to the excerpt verbatim, factual `datePublished` and `dateModified`, `inLanguage: en-US`, `isAccessibleForFree: true`, `author: {"@type":"Person","name":"John Rector","url":"https://johnrector.me/author/jsrector/","jobTitle":"Entrepreneur, author, and AI strategist"}`, `publisher: {"@type":"Person","name":"John Rector","url":"https://johnrector.me/"}`, `image` as an ImageObject with the actual featured-media `source_url` and verified dimensions, `articleSection` as the primary category name, `keywords` matching actual post tags, `wordCount`, complete `articleBody`, and `isPartOf: {"@type":"Blog","name":"John Rector","url":"https://johnrector.me/blog/"}`. Use an actual dated permalink; the saved WordPress link is authoritative, and updates retain the original publication date.
4. `articleBody` must contain the full plain text of the authored experience in reading order, including headings and captions, excluding CSS, scripts, HTML comments, and theme chrome. Decode HTML entities and collapse whitespace. Never shorten it to a summary or just meet the 60-word minimum. `wordCount` counts that text. Keep one article H1 and hide the duplicate theme title. Use `keep_title_in_body: true` and prefer a title comment over a visible `<title>` element.
5. Add `citation` only for actual external sources linked in the article, with verified URLs and titles. When a title is unverified, omit `name`; when no external sources exist, omit `citation` entirely. Do not invent values.
6. Dry-run the exact file, then publish the same path. The publisher rejects missing excerpts, incomplete or inconsistent JSON-LD, executable scripts, and mismatched tags/categories. If a running MCP process still reports the old blanket script rejection, invoke this installed plugin's updated `mcp/server.py` through its JSON-RPC stdio interface using Python and the same tool arguments; this uses the existing secure Keychain connection. Do not strip metadata to bypass validation.
7. After publication, require a plain HTTP fetch of the actual public URL, without browser rendering, and parse complete JSON-LD blocks while excluding template descendants. Require exactly one `#jr-article` BlogPosting in the light DOM, full `articleBody` matching the authored text (at least 60 words), nonempty headline/url/datePublished, and description equal to the saved excerpt and at least 45 characters. The updated publisher performs this extraction check and reports its result; separately verify the saved excerpt, featured media, actual dates, category, and tags against the post metadata. JSON-LD makes text available to consumers that parse it; it does not guarantee that every RSS reader or scraper displays it.
8. Retain all desktop and true-mobile visual checks. API success or visual success alone is insufficient. If a metadata or extraction check fails, fix the same post/slug and rerun verification before claiming completion. A failed check may follow a successful write; inspect the existing post before retrying. Report success only after both extraction and visual gates pass.
