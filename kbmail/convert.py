"""Document -> Markdown conversion (markitdown) plus image extraction from Office files.

Native crashes. markitdown's converters run C/C++ code (onnxruntime, pdfium, lxml, ...). A crash there,
such as an access violation (0xC0000005), ends the Python process at once, so no exception handler
runs. Two common Windows causes:
- security software (McAfee, ...) loads its own, older copy of the Visual C++ runtime (msvcp140.dll)
  into every process. Code built with a newer compiler then crashes in it.
- a damaged or unusual attachment.
Hence:
- markitdown's content sniffing (Google magika, which runs onnxruntime) is off by default
  (`convert.sniff_content`). kbmail always knows the file extension, and markitdown picks its converter
  from that.
- `session()` turns on faulthandler, so a crash prints the Python stack, and records the file being
  converted in state/convert_crash.json. When a run dies, the next run reports that file and skips it.
"""

from __future__ import annotations

import faulthandler
import html as _html
import json
import re
import sys
import types
import zipfile
from contextlib import contextmanager
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
CRASH_FILE = "state/convert_crash.json"

_md = None
_opts: dict = {"sniff": False}
_guard: dict = {"marker": None, "skip": set()}


class _NoSniff:
    """Stands in for magika.Magika. Its result is never "ok", so markitdown uses the extension."""

    def __init__(self, *args, **kwargs):
        pass

    def identify_stream(self, *args, **kwargs):
        return types.SimpleNamespace(status="kbmail: content sniffing is off (convert.sniff_content)")

    identify_bytes = identify_path = identify_stream


def _markitdown():
    global _md
    if _md is None:
        _md = _new_markitdown(_opts["sniff"])
    return _md


def _new_markitdown(sniff: bool):
    if sniff:
        try:
            from markitdown import MarkItDown
        except ImportError:  # pragma: no cover
            return None
        return MarkItDown(enable_plugins=False)
    # markitdown imports magika at module level, and magika imports onnxruntime (native C++), so put a
    # stand-in module in place while markitdown is imported and constructed.
    stub = types.ModuleType("magika")
    stub.Magika = _NoSniff
    injected = "magika" not in sys.modules
    if injected:
        sys.modules["magika"] = stub
    try:
        import markitdown._markitdown as mm
        from markitdown import MarkItDown
    except ImportError:  # pragma: no cover
        return None
    finally:
        if injected and sys.modules.get("magika") is stub:
            del sys.modules["magika"]
    real, mm.magika = mm.magika, stub
    try:
        return MarkItDown(enable_plugins=False)
    finally:
        mm.magika = real


@contextmanager
def session(ws, log=print):
    """Wrap a conversion run: settings, crash report (faulthandler) and the crash marker (see above)."""
    global _md
    sniff = bool(ws.cfg("convert.sniff_content", False))
    if sniff != _opts["sniff"]:
        _opts["sniff"], _md = sniff, None
    marker = ws.path(CRASH_FILE)
    state = _load_marker(marker)
    if state.get("current"):
        log(
            f"warning: the previous conversion run crashed while converting {state['current']}. "
            f"It is skipped from now on (listed in {CRASH_FILE}; remove it there to try again)."
        )
        state["skip"] = sorted({*state.get("skip", []), state["current"]})
        state["current"] = None
        _write_marker(marker, state)
    was_enabled = faulthandler.is_enabled()
    if not was_enabled:
        try:
            faulthandler.enable()
        except (AttributeError, ValueError, OSError):  # stderr without a file descriptor
            pass
    _guard["marker"], _guard["skip"] = marker, set(state.get("skip", []))
    try:
        yield
    finally:
        _guard["marker"], _guard["skip"] = None, set()
        if not was_enabled:
            faulthandler.disable()


def _load_marker(marker: Path) -> dict:
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_marker(marker: Path, state: dict) -> None:
    if not state.get("current") and not state.get("skip"):
        marker.unlink(missing_ok=True)
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _set_current(path: str | None) -> None:
    if _guard["marker"] is not None:
        _write_marker(_guard["marker"], {"current": path, "skip": sorted(_guard["skip"])})


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
    key = str(path.resolve())
    if key in _guard["skip"]:
        return "", f"skipped: it crashed an earlier run (see {CRASH_FILE})"
    _set_current(key)
    try:
        res = md.convert(str(path))
        text = getattr(res, "markdown", None) or getattr(res, "text_content", "") or ""
        return text, None
    except Exception as e:  # unsupported format, encrypted file, ...
        return "", f"{type(e).__name__}: {e}"[:300]
    finally:
        _set_current(None)


def url_to_markdown(url: str) -> tuple[str, str | None]:
    md = _markitdown()
    if md is None:
        return "", "markitdown is not installed"
    if url in _guard["skip"]:
        return "", f"skipped: it crashed an earlier run (see {CRASH_FILE})"
    _set_current(url)
    try:
        res = md.convert(url)
        return (getattr(res, "markdown", None) or res.text_content or ""), None
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"[:300]
    finally:
        _set_current(None)


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
