"""Pure HTML/metadata engine from John's installed WordPress Publisher.
No Keychain, application passwords, UI, or credential migration.
Source version: 0.1.0+codex.20260719071027. Pure functions retained verbatim.
"""
from __future__ import annotations
import fnmatch,html,json,re,unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

CUSTOM_HTML_HOST_CLASS = "codex-html-experience"

CUSTOM_HTML_ROOT_CLASS = "codex-html-shadow-root"

CUSTOM_HTML_CANVAS = """<style data-codex-theme-canvas>
  /* The post itself is the canvas. Remove duplicate WordPress chrome on single-post views. */
  body.single-post #masthead,
  body.single-post .site-header,
  body.single-post .header-media,
  body.single-post #header-featured-image,
  body.single-post article.post > .entry-header {
    display: none !important;
  }

  body.single-post #page,
  body.single-post #content,
  body.single-post .site-content,
  body.single-post .site-content > .wrapper,
  body.single-post #primary,
  body.single-post .content-area,
  body.single-post #main,
  body.single-post .site-main,
  body.single-post .singular-content-wrap,
  body.single-post article.post,
  body.single-post article.post > .entry-content {
    margin-top: 0 !important;
    padding-top: 0 !important;
  }

  body.single-post .site-content > .wrapper,
  body.single-post #primary,
  body.single-post .content-area,
  body.single-post #main,
  body.single-post .site-main,
  body.single-post .singular-content-wrap,
  body.single-post article.post,
  body.single-post article.post > .entry-content {
    width: 100% !important;
    max-width: none !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
  }

  body.single-post .site-content > .wrapper,
  body.single-post #primary,
  body.single-post article.post,
  body.single-post article.post > .entry-content {
    padding-left: 0 !important;
    padding-right: 0 !important;
  }
</style>"""

CUSTOM_HTML_BASELINE = f"""<style data-codex-theme-firewall>
  :host {{
    display: block;
  }}

  .{CUSTOM_HTML_ROOT_CLASS} {{
    all: initial;
    display: block;
    width: 100%;
    min-width: 0;
    color: #111111;
    background: transparent;
    font: normal 16px/1.5 Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    text-align: left;
  }}

  .{CUSTOM_HTML_ROOT_CLASS},
  .{CUSTOM_HTML_ROOT_CLASS} *,
  .{CUSTOM_HTML_ROOT_CLASS} *::before,
  .{CUSTOM_HTML_ROOT_CLASS} *::after {{
    box-sizing: border-box;
  }}
</style>"""

def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")

def source_files(paths: list[str], excludes: list[str]) -> list[Path]:
    supported_suffixes = {".html", ".htm"}
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.is_file():
            if path.suffix.lower() not in supported_suffixes:
                raise RuntimeError(
                    f"Not a custom HTML file: {path}. "
                    "WordPress publication requires an original .html or .htm experience; plain Markdown is disabled."
                )
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(
                item for item in path.iterdir() if item.is_file() and item.suffix.lower() in supported_suffixes
            )
        else:
            raise RuntimeError(f"Path does not exist: {path}")
        files.extend(item for item in candidates if not any(fnmatch.fnmatch(item.name, pattern) for pattern in excludes))
    unique = list(dict.fromkeys(files))
    if not unique:
        raise RuntimeError("No custom .html or .htm files matched the requested paths.")
    return unique

def extract_html_title(source: str, fallback: str, keep_title: bool) -> tuple[str, str]:
    comment = re.search(r"<!--\s*(?:wp-)?title\s*:\s*(.*?)\s*-->", source, flags=re.IGNORECASE | re.DOTALL)
    if comment:
        title = html.unescape(re.sub(r"<[^>]+>", "", comment.group(1))).strip()
        body = source if keep_title else source[: comment.start()] + source[comment.end() :]
        return title, body.strip()

    title_tag = re.search(r"<title\b[^>]*>(.*?)</title\s*>", source, flags=re.IGNORECASE | re.DOTALL)
    if title_tag:
        title = html.unescape(re.sub(r"<[^>]+>", "", title_tag.group(1))).strip()
        body = source if keep_title else source[: title_tag.start()] + source[title_tag.end() :]
        return title, body.strip()

    heading = re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", source, flags=re.IGNORECASE | re.DOTALL)
    if heading:
        title = html.unescape(re.sub(r"<[^>]+>", "", heading.group(1))).strip()
        body = source if keep_title else source[: heading.start()] + source[heading.end() :]
        return title, body.strip()

    return fallback, source.strip()

class PublishingHTMLParser(HTMLParser):
    """Collect complete JSON-LD blocks outside templates and visible prose."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.capture = False
        self.buffer = []
        self.blocks = []
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "template":
            self.depth += 1
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "script" and self.depth == 0 and attrs.get("type", "").lower() == "application/ld+json":
            self.capture = True
            self.buffer = []
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            self.blocks.append(json.loads("".join(self.buffer)))
            self.capture = False
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag == "template":
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)
        if not self.skip and not self.depth:
            self.text.append(data)

def article_metadata(content):
    parser = PublishingHTMLParser()
    parser.feed(content)
    nodes = []
    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            nodes.append(value)
            collect(value.get("@graph", []))
    for block in parser.blocks:
        collect(block)
    matches = [node for node in nodes if str(node.get("@id", "")).endswith("#jr-article")]
    if len(matches) != 1:
        raise RuntimeError("Require exactly one #jr-article JSON-LD block outside templates.")
    article = matches[0]
    for field in ("headline", "description", "url", "datePublished", "dateModified", "articleSection"):
        if not isinstance(article.get(field), str) or not article[field].strip():
            raise RuntimeError(f"Missing article metadata: {field}")
    if article.get("@type") != "BlogPosting" or article.get("@context") != "https://schema.org":
        raise RuntimeError("Article metadata must be a schema.org BlogPosting.")
    if len(article["description"]) < 45:
        raise RuntimeError("Article description must be at least 45 characters.")
    body = article.get("articleBody", "")
    if not isinstance(body, str) or len(body.split()) < 60:
        raise RuntimeError("Full articleBody is required (at least 60 words).")
    if article.get("wordCount") != len(body.split()):
        raise RuntimeError("wordCount must count the complete articleBody prose.")
    if article["@id"] != article["url"] + "#jr-article":
        raise RuntimeError("Article @id must match its canonical URL plus #jr-article.")
    if article.get("mainEntityOfPage", {}).get("@id") != article["url"]:
        raise RuntimeError("mainEntityOfPage must match the canonical URL.")
    if not isinstance(article.get("keywords"), list):
        raise RuntimeError("keywords must mirror the post tags.")
    if not isinstance(article.get("image"), dict) or not article["image"].get("url"):
        raise RuntimeError("Featured image source URL is required.")
    return article

def separate_json_ld(body):
    blocks = []
    def extract(match):
        parser = HTMLParser()
        attrs = []
        parser.handle_starttag = lambda tag, values: attrs.extend(values)
        parser.feed(match.group(1) + "</script>")
        attributes = dict(attrs)
        if set(attributes) != {"type"} or attributes.get("type", "").lower() != "application/ld+json":
            raise RuntimeError("Custom HTML experiences cannot contain scripts except application/ld+json data.")
        value = json.loads(match.group(2))
        # Re-serialize data so it cannot terminate the script element.
        encoded = json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
        blocks.append('<script type="application/ld+json">' + encoded + '</script>')
        return ""
    body = re.sub(r"(<script\b[^>]*>)(.*?)</script\s*>", extract, body, flags=re.I | re.S)
    if re.search(r"<script\b", body, flags=re.I):
        raise RuntimeError("Custom HTML experiences cannot contain scripts or malformed script tags.")
    return body, "\n".join(blocks)

def protect_custom_html(source: str) -> str:
    """Publish a custom HTML experience inside a theme-isolated shadow root."""
    body = source.strip()
    if not body:
        raise RuntimeError("Custom HTML source is empty.")
    body, metadata = separate_json_ld(body)
    if not re.search(r"<style\b", body, flags=re.IGNORECASE):
        raise RuntimeError("Custom HTML experiences must include purpose-built scoped CSS in a <style> element.")
    if not re.search(r"<(?:article|main|section)\b", body, flags=re.IGNORECASE):
        raise RuntimeError("Custom HTML experiences must include semantic <article>, <main>, or <section> structure.")

    isolated = (
        f"{CUSTOM_HTML_CANVAS}\n"
        f'<div class="{CUSTOM_HTML_HOST_CLASS}" data-codex-html-experience '
        'style="display:block;width:100%;max-width:none;margin:0;padding:0;">\n'
        '<template shadowrootmode="open">\n'
        f"{CUSTOM_HTML_BASELINE}\n"
        f'<div class="{CUSTOM_HTML_ROOT_CLASS}">\n'
        f"{body}\n"
        "</div>\n"
        "</template>\n"
        "</div>"
    )
    return f"<!-- wp:html -->\n{isolated}\n{metadata}\n<!-- /wp:html -->"

def build_posts(paths: list[str], excludes: list[str], keep_title: bool) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for path in source_files(paths, excludes):
        raw = path.read_text(encoding="utf-8")
        fallback = path.stem.replace("-", " ").replace("_", " ").strip().title()
        title, body = extract_html_title(raw, fallback, keep_title)
        content = protect_custom_html(body)
        excerpt_match = re.search(r"<!--\s*Excerpt\s*:\s*(.*?)\s*-->", raw, re.I | re.S)
        if not excerpt_match or len(excerpt_match.group(1).strip()) < 45:
            raise RuntimeError("An explicit Excerpt comment of at least 45 characters is required.")
        excerpt = excerpt_match.group(1).strip()
        featured = re.search(r"<!--\s*Featured-Media\s*:\s*([1-9][0-9]*)\s*-->", raw, re.I)
        if not featured:
            raise RuntimeError("Featured-Media comment with an actual WordPress attachment ID is required.")
        metadata = article_metadata(content)
        if metadata["headline"] != title or metadata["description"] != excerpt:
            raise RuntimeError("JSON-LD headline/description must exactly match title/excerpt.")
        prose = PublishingHTMLParser()
        prose.feed(separate_json_ld(body)[0])
        if " ".join(prose.text).split() != metadata["articleBody"].split():
            raise RuntimeError("articleBody must contain the entire plain text of the authored article in order, excluding CSS and metadata.")
        for citation in metadata.get("citation", []):
            if citation.get("url") not in prose.links:
                raise RuntimeError("Every citation URL must be linked in the article.")
        source_format = "html"
        posts.append(
            {
                "file": str(path),
                "title": title,
                "slug": slugify(title),
                "content": content,
                "excerpt": excerpt,
                "featured_media": int(featured.group(1)),
                "metadata": metadata,
                "source_format": source_format,
            }
        )
    return posts
