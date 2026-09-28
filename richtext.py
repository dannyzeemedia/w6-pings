"""Rich-text emails: clean up what the editor produces, and move between HTML and plain text.

The editor (bold, lists, links, colour...) gives us HTML. We only ever send a small, Gmail-safe set of tags, and we
keep a plain-text copy alongside it, which is what the learner compares (so formatting never looks like an edit)."""
import html as _html
import re
from html.parser import HTMLParser

ALLOWED = {"b", "strong", "i", "em", "u", "s", "strike", "del", "ul", "ol", "li", "a", "br", "div", "p", "blockquote",
           "span", "font", "h1", "h2", "h3"}
VOID = {"br"}
BLOCK = {"div", "p", "li", "ul", "ol", "blockquote", "h1", "h2", "h3"}
_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|[a-zA-Z]+)$")
_SIZES = {"1", "2", "3", "4", "5", "6", "7"}
_URL = re.compile(r"(https?://[^\s<>()]+)")


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        a = dict(attrs)
        keep = ""
        if tag == "a":
            href = (a.get("href") or "").strip()
            if not re.match(r"^(https?:|mailto:)", href, re.I):
                return
            keep = f' href="{_html.escape(href, quote=True)}"'
        elif tag == "font":
            if a.get("color") and _COLOR.match(a["color"].strip()):
                keep += f' color="{_html.escape(a["color"].strip(), quote=True)}"'
            if a.get("size") in _SIZES:
                keep += f' size="{a["size"]}"'
        elif tag == "span":
            m = re.search(r"color:\s*([^;]+)", a.get("style") or "", re.I)
            if m and _COLOR.match(m.group(1).strip()):
                keep = f' style="color:{_html.escape(m.group(1).strip(), quote=True)}"'
        elif tag == "blockquote":
            keep = ' style="margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex"'
        self.out.append(f"<{tag}{keep}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(_html.escape(data, quote=False))

    def result(self):
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.stack))


def sanitize(h):
    """Only the formatting Gmail shows; no scripts, styles, classes or tracking."""
    p = _Clean()
    p.feed(h or "")
    p.close()
    return p.result().strip()


def text_to_html(text):
    """Plain text (what the brain writes) as editor/Gmail HTML: one <div> per line, links clickable."""
    out = []
    for line in (text or "").split("\n"):
        esc = _html.escape(line, quote=False)
        esc = _URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', esc)
        out.append(f"<div>{esc}</div>" if line.strip() else "<div><br></div>")
    return "".join(out)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.lists, self.href, self.skip = [], [], None, 0

    def _nl(self):
        if self.out and not "".join(self.out[-2:]).endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            self._nl()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._nl()
            if self.lists:
                self.lists[-1][1] += 1
                kind, n = self.lists[-1]
                self.out.append("  " * (len(self.lists) - 1) + ("- " if kind == "ul" else f"{n}. "))
        elif tag in BLOCK:
            self._nl()
        elif tag == "a":
            self.href = dict(attrs).get("href")
            self.link_start = len(self.out)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._nl()
        elif tag in BLOCK:
            self._nl()
        elif tag == "a" and self.href:
            text = "".join(self.out[self.link_start:]).strip()
            if text and self.href.rstrip("/") not in (text.rstrip("/"), "mailto:" + text):
                self.out.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data.replace("\xa0", " "))


def html_to_text(h):
    """The words of an email, with lists as "- " / "1. " lines. What the learner compares."""
    p = _Text()
    p.feed(h or "")
    p.close()
    t = "".join(p.out)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def email_html(h):
    """Wrap cleaned editor HTML the way Gmail does."""
    return f'<div dir="ltr">{sanitize(h)}</div>'
