"""Inline <script> blocks in the SuperAdmin templates must be valid JS.

A literal newline once got baked into a JS string literal here
(`job.log.join("<newline>")`), which is a syntax error: the whole script
failed to parse, the click handler was never attached, and the Build EXE
button silently did nothing. Nothing else in the suite caught it, because
the page still rendered and the endpoint still worked.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "superadmin_panel" / "templates"
SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


def _inline_scripts():
    for path in sorted(TEMPLATE_DIR.glob("*.html")):
        for index, body in enumerate(SCRIPT_RE.findall(path.read_text(encoding="utf-8"))):
            yield path.name, index, body


def _strip_jinja(body: str) -> str:
    body = re.sub(r"\{%.*?%\}", "", body, flags=re.S)
    return re.sub(r"\{\{.*?\}\}", "PLACEHOLDER", body, flags=re.S)


@pytest.mark.parametrize("name,index,body", list(_inline_scripts()))
def test_no_unterminated_string_literals(name, index, body):
    """Catches the exact failure above without needing a JS engine: a string
    literal opened on a line and never closed on that line."""
    for lineno, line in enumerate(_strip_jinja(body).splitlines(), 1):
        code = re.sub(r"\.", "", line)          # drop escapes
        code = re.sub(r"//.*$", "", code)         # drop line comments
        assert code.count('"') % 2 == 0, f"{name} script {index} line {lineno}: unterminated \" -> {line.strip()!r}"
        assert code.count("'") % 2 == 0, f"{name} script {index} line {lineno}: unterminated ' -> {line.strip()!r}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name,index,body", list(_inline_scripts()))
def test_scripts_parse_with_node(tmp_path, name, index, body):
    js = tmp_path / f"{name}.{index}.js"
    js.write_text(_strip_jinja(body), encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(js)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"{name} script {index} failed to parse:\n{result.stderr}"
