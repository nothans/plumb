"""The portal needs no network at runtime: no page, stylesheet or script
loads anything from another host. (CI also runs the whole portal and both
checkers in a container with no network; see the offline job in ci.yml.)"""

import re

from .conftest import ROOT

EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//|@import\s+(?:url\()?["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//""",
                      re.I)


def test_no_template_or_asset_loads_from_another_host():
    found = []
    for path in list((ROOT / "app" / "templates").rglob("*.html")) + list((ROOT / "app" / "static").rglob("*.*")):
        if path.suffix not in (".html", ".css", ".js", ".svg"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in EXTERNAL.finditer(line):
                # Links people follow (a team's repo, the record's issuer) are not loads; tags that fetch are.
                tag = line[:m.start()].rsplit("<", 1)[-1].split()[0].lower() if "<" in line[:m.start()] else ""
                if tag in ("a", "form"):
                    continue
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert not found, found


def test_pages_render_with_no_external_resources(anon):
    for path in ("/", "/projects", "/events/evt_01", "/verify", "/api"):
        html = anon.get(path).text
        for m in re.finditer(r"<(script|link|img|iframe)[^>]*(?:src|href)=\"([^\"]+)\"", html):
            assert not re.match(r"(?:https?:)?//", m.group(2)), (path, m.group(0))
