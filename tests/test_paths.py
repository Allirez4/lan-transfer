import os
from pathlib import Path

import pytest

from lan_transfer.storage import confined, publish, relative_path


def test_publication_never_replaces_existing(tmp_path):
    source, destination = tmp_path / 'source.part', tmp_path / 'existing'
    source.write_bytes(b'new')
    destination.write_bytes(b'keep')
    with pytest.raises(OSError):
        publish(source, destination)
    assert source.read_bytes() == b'new'
    assert destination.read_bytes() == b'keep'


def test_unicode_normalization_and_names():
    assert relative_path('پوشه/سلام.txt') == 'پوشه/سلام.txt'
    assert relative_path('cafe\u0301.txt') == 'café.txt'


def test_symlink_ancestor_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sub').mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating symlinks requires Windows Developer Mode or elevated privileges')
    with pytest.raises(ValueError, match='symlink'):
        confined(link / 'sub', 'file')
