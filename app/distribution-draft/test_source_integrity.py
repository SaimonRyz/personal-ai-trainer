import hashlib
import json
from pathlib import Path

import preflight_release as preflight


def test_git_metadata_is_not_a_package_input():
    assert preflight._is_install_state(Path('.git/objects/example'))
    assert preflight._is_install_state(Path('.DS_Store'))
    assert not preflight._is_install_state(Path('app/src/trainer/config/settings.py'))


def test_manifest_checks_sources_without_external_digest(tmp_path, monkeypatch):
    (tmp_path / 'app').mkdir()
    source = tmp_path / 'README.md'
    source.write_text('alpha')
    (tmp_path / 'PACKAGE_MANIFEST.json').write_text(json.dumps({'schema_version': 1, 'files': {
        'README.md': {'size': 5, 'sha256': hashlib.sha256(b'alpha').hexdigest()}}}))
    monkeypatch.setattr(preflight, 'PACKAGE', tmp_path)
    monkeypatch.setattr(preflight, 'APP', tmp_path / 'app')
    assert preflight.package_integrity(None)['state'] == 'SELF_CONSISTENT_ONLY'
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git/config').write_text('local metadata')
    assert preflight.package_integrity(None)['state'] == 'SELF_CONSISTENT_ONLY'
    source.write_text('changed')
    assert preflight.package_integrity(None)['state'] == 'INVALID'
