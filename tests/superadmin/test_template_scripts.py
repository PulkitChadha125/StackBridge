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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_handlers_on_the_admin_page_parse(tmp_path, client, sa_app):
    """onsubmit="..." handlers with the Admin's name in them. A name in double quotes once ended the attribute
    early: the confirm() never ran and Delete Admin deleted without asking."""
    import html
    from datetime import date, timedelta

    from superadmin_panel import db, repositories as repo, security

    from .test_superadmin_web import _login

    with sa_app.app_context():
        admin = repo.create_admin(db.get_db(), name="Ann \"Quick\" O'Brien & Co", username="ann_admin",
                                  password_hash=security.hash_password("Temp-Pass-1"), allowed_installations=1,
                                  expiry_date=(date.today() + timedelta(days=30)).isoformat())
    _login(client)
    page = client.get(f"/admins/{admin['id']}").get_data(as_text=True)
    handlers = re.findall(r'\son\w+="([^"]*)"', page)
    assert any("Permanently delete" in h for h in handlers), "the delete confirm handler was cut short"
    for index, handler in enumerate(handlers):
        js = tmp_path / f"handler{index}.js"
        js.write_text(f"function handler() {{ {html.unescape(handler)} }}", encoding="utf-8")
        result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        assert result.returncode == 0, f"handler does not parse: {handler!r}\n{result.stderr}"
