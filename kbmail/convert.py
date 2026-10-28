"""Document -> Markdown conversion (markitdown) plus image extraction from Office files."""

from __future__ import annotations

import html as _html
import re
import zipfile
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".xml"}
CONVERTIBLE_EXT = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".msg",
    ".eml",
    ".epub",
    ".ipynb",
    ".zip",
    ".rtf",
    ".doc",
    ".ppt",
}
OFFICE_MEDIA = {".docx": "word/media/", ".pptx": "ppt/media/", ".xlsx": "xl/media/"}

_md = None


def _markitdown():
    global _md
    if _md is None:
        try:
            from markitdown import MarkItDown
        except ImportError:  # pragma: no cover
            return None
        _md = MarkItDown(enable_plugins=False)
    return _md


def vtt_to_markdown(text: str) -> str:
    """WebVTT meeting transcript (Teams 'Download transcript') -> speaker-attributed Markdown."""
    out: list[str] = []
    last_speaker, start = None, ""
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        cue = next((ln for ln in lines if "-->" in ln), None)
        body = lines[lines.index(cue) + 1 :] if cue else lines
        if cue:
            start = cue.split("-->")[0].strip().split(".")[0]
        for ln in body:
            m = re.match(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", ln)
            speaker, said = (m.group(1).strip(), m.group(2)) if m else (None, ln)
            said = re.sub(r"<[^>]+>", "", said).strip()
            if not said:
                continue
            if speaker and speaker != last_speaker:
                out.append(f"\n**{speaker}** ({start}):")
                last_speaker = speaker
            out.append(said)
    return ("# Meeting transcript\n" + "\n".join(out)).strip() + "\n"


def file_to_markdown(path: Path) -> tuple[str, str | None]:
    """Return (markdown, error). Never raises."""
    ext = path.suffix.lower()
    if ext == ".vtt":
        return vtt_to_markdown(path.read_text(encoding="utf-8-sig", errors="replace")), None
    if ext in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace"), None
    md = _markitdown()
    if md is None:
        return "", "markitdown is not installed"
    try:
        res = md.convert(str(path))
        text = getattr(res, "markdown", None) or getattr(res, "text_content", "") or ""
        return text, None
    except Exception as e:  # unsupported format, encrypted file, ...
        return "", f"{type(e).__name__}: {e}"[:300]


def url_to_markdown(url: str) -> tuple[str, str | None]:
    md = _markitdown()
    if md is None:
        return "", "markitdown is not installed"
    try:
        res = md.convert(url)
        return (getattr(res, "markdown", None) or res.text_content or ""), None
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"[:300]


def html_to_markdown(path: Path) -> str:
    text, err = file_to_markdown(path)
    if err or not text.strip():
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
        raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
        text = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def office_media(path: Path) -> list[tuple[str, bytes]]:
    """Images embedded in docx/pptx/xlsx (they are zip files with a media folder)."""
    prefix = OFFICE_MEDIA.get(path.suffix.lower())
    if not prefix:
        return []
    try:
        with zipfile.ZipFile(path) as z:
            return [(n.split("/")[-1], z.read(n)) for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")]
    except (zipfile.BadZipFile, OSError):
        return []


CID_IMG_RE = re.compile(r"!\[[^\]]*\]\(cid:([^)\s]+)[^)]*\)")
DATA_IMG_RE = re.compile(r"!\[[^\]]*\]\(data:image/[^)]*\)")
ANY_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
