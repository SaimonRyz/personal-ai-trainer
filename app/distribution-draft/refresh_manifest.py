"""Developer-only manifest refresh; never repair an untrusted download this way."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from preflight_release import PACKAGE, _is_install_state


def main() -> None:
    files = {}
    for folder, directories, names in os.walk(PACKAGE, followlinks=False):
        base = Path(folder)
        for name in list(directories):
            entry = base / name
            relative = entry.relative_to(PACKAGE)
            if _is_install_state(relative) or name in {'.venv', 'wheelhouse', 'tmp'}:
                directories.remove(name)
            elif entry.is_symlink():
                raise ValueError('Linked directory is not a source input')
        for name in names:
            entry = base / name
            relative = entry.relative_to(PACKAGE)
            if str(relative) == 'PACKAGE_MANIFEST.json' or _is_install_state(relative):
                continue
            if entry.is_symlink() or name.endswith(('.pyc', '.log', '.dump', '.db')):
                raise ValueError('Unexpected local artifact: ' + str(relative))
            content = entry.read_bytes()
            files[relative.as_posix()] = {
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
            }
    result = {
        'schema_version': 1, 'status': 'experimental_alpha',
        'files': dict(sorted(files.items())),
    }
    (PACKAGE / 'PACKAGE_MANIFEST.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )
    print('MANIFEST_UPDATED files=' + str(len(files)))


if __name__ == '__main__':
    main()
