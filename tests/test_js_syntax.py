"""Every JavaScript file under ``web/js`` must parse (SPEC section 12: ``node --check``, skipped without ``node``).

ES modules are checked as ``.mjs`` copies (``node --check`` decides module-or-script from the extension, and the
originals are served as ``type="module"``).  The classic script ``js/unsupported.js`` (``<script nomodule>``) is parsed
as a script through ``vm.Script`` instead: a module is stricter and has other keywords, so a ``.mjs`` copy would prove
the wrong thing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from typing import List, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
NODE = shutil.which("node")
TIMEOUT_S = 60

_VM_SCRIPT = (
    "const fs = require('fs'); const vm = require('vm'); const file = process.argv[1];"
    " new vm.Script(fs.readFileSync(file, 'utf8'), { filename: file });"
)
_SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)


def javascript_files() -> List[str]:
    """Every ``.js`` file below ``web/js`` (sorted, absolute)."""
    found: List[str] = []
    for current, _dirs, names in os.walk(os.path.join(WEB, "js")):
        found += [os.path.join(current, name) for name in names if name.endswith(".js")]
    return sorted(found)


def classic_scripts() -> Set[str]:
    """Paths (relative to ``web/``, forward slashes) of the files ``index.html`` loads as classic scripts."""
    index = os.path.join(WEB, "index.html")
    if not os.path.isfile(index):
        return set()
    with open(index, "r", encoding="utf-8") as handle:
        html = handle.read()
    classic: Set[str] = set()
    for attributes in _SCRIPT_TAG.findall(html):
        src = re.search(r"""\bsrc\s*=\s*["']([^"']+)["']""", attributes)
        if src and not re.search(r"""\btype\s*=\s*["']module["']""", attributes):
            classic.add(src.group(1).lstrip("/"))
    return classic


def check(node: str, path: str, scratch: str, classic: bool) -> Optional[str]:
    """``None`` when ``path`` parses, else node's complaint (with the original file name instead of the temp copy)."""
    if classic:
        command = [node, "-e", _VM_SCRIPT, path]
        shown = path
    else:
        shown = os.path.join(scratch, "%d.mjs" % len(os.listdir(scratch)))
        shutil.copyfile(path, shown)
        command = [node, "--check", shown]
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_S,
        check=False,
    )
    if result.returncode == 0:
        return None
    message = result.stderr.decode("utf-8", "replace").strip()
    inside = os.path.normcase(path).startswith(os.path.normcase(ROOT))
    return message.replace(shown, os.path.relpath(path, ROOT) if inside else path)


@unittest.skipIf(NODE is None, "node is not installed: the JavaScript syntax check is skipped")
class JavaScriptSyntaxTests(unittest.TestCase):
    def test_every_script_parses(self) -> None:
        files = javascript_files()
        self.assertTrue(files, "no JavaScript files under web/js")
        classic = classic_scripts()
        assert NODE is not None
        failures: List[str] = []
        with tempfile.TemporaryDirectory(prefix="dtk-jscheck-") as scratch:
            for path in files:
                relative = os.path.relpath(path, WEB).replace(os.sep, "/")
                problem = check(NODE, path, scratch, relative in classic)
                if problem is not None:
                    failures.append("%s\n%s" % (relative, problem))
        self.assertEqual(failures, [], "JavaScript that does not parse:\n" + "\n\n".join(failures))

    def test_the_checker_rejects_broken_modules_and_scripts(self) -> None:
        assert NODE is not None
        with tempfile.TemporaryDirectory(prefix="dtk-jscheck-") as scratch:
            broken = os.path.join(scratch, "broken.js")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("export const a = ;\n")
            self.assertIsNotNone(check(NODE, broken, scratch, classic=False))
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("import x from './x.js';\n")
            self.assertIsNotNone(check(NODE, broken, scratch, classic=True), "import is not valid in a classic script")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("import x from './x.js';\nawait x;\n")
            self.assertIsNone(check(NODE, broken, scratch, classic=False), "a module may import and await at top level")

    def test_the_nomodule_script_is_classified_as_classic(self) -> None:
        self.assertIn("js/unsupported.js", classic_scripts())
        self.assertNotIn("js/main.js", classic_scripts())


if __name__ == "__main__":
    unittest.main()
