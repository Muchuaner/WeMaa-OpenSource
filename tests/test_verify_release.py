import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('verify_release', Path(__file__).parents[1] / 'scripts/verify_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize('actual', [
    {'files': 159, 'sha256': 'a' * 64},
    {'files': 265, 'sha256': 'b' * 64},
])
def test_partial_or_changed_image_is_rejected(tmp_path, monkeypatch, actual):
    manifest = tmp_path / 'ui.json'
    manifest.write_text(json.dumps({'files': 265, 'sha256': 'a' * 64}))
    monkeypatch.setattr(release.subprocess, 'check_output', lambda *args, **kwargs: json.dumps(actual).encode())
    with pytest.raises(RuntimeError, match='runtime update refused'):
        release.verify_image('wemaa:release', manifest)


def test_empty_manifest_never_runs_an_image(tmp_path, monkeypatch):
    manifest = tmp_path / 'ui.json'
    manifest.write_text(json.dumps({'files': 0, 'sha256': 'a' * 64}))
    def unexpected(*args, **kwargs):
        pytest.fail('An invalid manifest must fail before any Docker command')
    monkeypatch.setattr(release.subprocess, 'check_output', unexpected)
    with pytest.raises(ValueError):
        release.verify_image('wemaa:release', manifest)


def test_complete_matching_bundle_passes(tmp_path, monkeypatch):
    expected = {'files': 265, 'sha256': 'a' * 64}
    manifest = tmp_path / 'ui.json'
    manifest.write_text(json.dumps(expected))
    monkeypatch.setattr(release.subprocess, 'check_output', lambda *args, **kwargs: json.dumps(expected).encode())
    assert release.verify_image('wemaa:release', manifest) == expected
