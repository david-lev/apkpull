from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from apkfile._apk import ApkFile as RealApkFile

DATA_DIR = Path(__file__).parent / "data" / "apk"
POLITEDROID_PATH = DATA_DIR / "politedroid.apk"
TEST_DEBUG_PATH = DATA_DIR / "test-debug.apk"


@pytest.fixture
def politedroid_bytes() -> bytes:
    """A small real apk: package com.politedroid, version_code 4."""
    return POLITEDROID_PATH.read_bytes()


@pytest.fixture
def test_debug_bytes() -> bytes:
    """A minimal real apk: package org.t0t0.androguard.test, version_code 1."""
    return TEST_DEBUG_PATH.read_bytes()


@pytest.fixture
def force_splits():
    """Context manager factory patching ``apkfile._bundle.ApkFile`` so
    ``ApksFile.create()`` treats given real (non-split) fixture apks as
    splits -- neither politedroid.apk nor test-debug.apk carries an actual
    `android:split=` manifest attribute, so this is how tests get more than
    one of them into a bundle together without ``create()`` rejecting the
    extras as competing base apks.

    Pass explicit paths to force, or ``where=<predicate>`` to force every
    path matching it (e.g. every path not named ``base.apk``).
    """

    @contextmanager
    def _force(*paths: Path, where=None):
        forced = {Path(p) for p in paths}

        def _load(path):
            apk = RealApkFile(path)
            if Path(path) in forced or (where and where(Path(path))):
                apk.__dict__["is_split"] = True
                apk.__dict__["split_name"] = Path(path).stem
            return apk

        with patch("apkfile._bundle.ApkFile", side_effect=_load):
            yield

    return _force
