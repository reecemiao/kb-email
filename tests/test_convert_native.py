"""Conversion stays out of onnxruntime, and a native crash during conversion is reported and skipped next run."""

import subprocess
import sys
import textwrap

from kbmail import convert
from kbmail.convert import CRASH_FILE, file_to_markdown, session

from .conftest import docx_bytes

quiet = lambda *a, **k: None


def _run(code: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=cwd, capture_output=True, text=True)


def test_default_conversion_never_loads_onnxruntime(tmp_path):
    (tmp_path / "spec.docx").write_bytes(docx_bytes("Retry budget is three attempts."))
    (tmp_path / "page.html").write_text("<table><tr><td>alpha</td><td>beta</td></tr></table>", encoding="utf-8")
    res = _run(
        """
        import sys
        from pathlib import Path
        from kbmail.convert import file_to_markdown
        for name in ("spec.docx", "page.html"):
            text, err = file_to_markdown(Path(name).resolve())
            assert err is None, err
            print(text)
        print("LOADED:", sorted(m for m in sys.modules if m.split(".")[0] in ("onnxruntime", "magika")))
        """,
        tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert "Retry budget is three attempts." in res.stdout
    assert "alpha" in res.stdout and "beta" in res.stdout
    assert "LOADED: []" in res.stdout


def test_crash_is_reported_and_the_file_skipped_next_run(ws, tmp_path):
    bad = tmp_path / "bad.docx"
    good = tmp_path / "good.docx"
    bad.write_bytes(docx_bytes("never converted"))
    good.write_bytes(docx_bytes("Fine document."))
    # A native crash ends the process without any Python cleanup; os._exit behaves the same way.
    res = _run(
        f"""
        import os
        from pathlib import Path
        from kbmail import convert
        from kbmail.workspace import Workspace
        ws = Workspace.find({str(ws.root)!r})
        with convert.session(ws, log=print):
            md = convert._markitdown()
            md.convert = lambda *a, **k: os._exit(0xC5)
            convert.file_to_markdown(Path({str(bad)!r}))
        """,
        tmp_path,
    )
    assert res.returncode == 0xC5, res.stderr
    assert ws.path(CRASH_FILE).exists()

    msgs = []
    with session(ws, log=msgs.append):
        text, err = file_to_markdown(bad)
        assert text == "" and "crashed an earlier run" in err
        text, err = file_to_markdown(good)
        assert err is None and "Fine document." in text
    assert any(str(bad.resolve()) in m and "crashed" in m for m in msgs)

    # Reported once; later runs skip it silently (the per-file error says why).
    msgs.clear()
    with session(ws, log=msgs.append):
        assert "crashed an earlier run" in file_to_markdown(bad)[1]
    assert msgs == []

    # Outside a session (tests, library use) nothing is skipped or written.
    assert convert._guard["marker"] is None
    text, err = file_to_markdown(bad)
    assert err is None and "never converted" in text


def test_sniff_content_setting_switches_converter(ws, monkeypatch):
    seen = []
    monkeypatch.setattr(convert, "_new_markitdown", lambda sniff: seen.append(sniff) or object())
    monkeypatch.setattr(convert, "_md", None)
    monkeypatch.setitem(convert._opts, "sniff", False)
    with session(ws, log=quiet):
        convert._markitdown()
    ws.config.setdefault("convert", {})["sniff_content"] = True
    with session(ws, log=quiet):
        convert._markitdown()
    assert seen == [False, True]
