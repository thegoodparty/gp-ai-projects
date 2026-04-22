import re
from html import escape


_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
_BIDI_RE = re.compile(r"[\u202a-\u202e]")


def sanitize_html(raw_html: str) -> str:
    text = _SCRIPT_RE.sub("", raw_html)
    text = _STYLE_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _BIDI_RE.sub("", text)
    return text


def fence_content(content: str, source: str, fetched_at: str | None = None) -> str:
    safe_source = escape(source, quote=True)
    if fetched_at:
        safe_ts = escape(fetched_at, quote=True)
        return (
            f'<untrusted_web_content source="{safe_source}" fetched_at="{safe_ts}">'
            f"{content}"
            f"</untrusted_web_content>"
        )
    return (
        f'<untrusted_web_content source="{safe_source}">'
        f"{content}"
        f"</untrusted_web_content>"
    )
