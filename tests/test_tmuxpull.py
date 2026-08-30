"""Basic tests for tmuxpull functionality."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tmuxpull import Repo, find_repos


def test_find_repos_empty():
    """Test find_repos with no git repos."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repos = find_repos([tmpdir], max_depth=2)
        assert repos == []


def test_find_repos_single():
    """Test find_repos with a single git repo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        
        repos = find_repos([tmpdir], max_depth=2)
        assert len(repos) == 1
        assert repos[0].path == repo_dir
        assert repos[0].name == "test-repo"


def test_find_repos_nested():
    """Test find_repos with nested structure and max_depth."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create repos at different depths
        shallow_repo = root / "shallow"
        shallow_repo.mkdir()
        (shallow_repo / ".git").mkdir()
        
        deep_repo = root / "deep" / "nested" / "repo"
        deep_repo.mkdir(parents=True)
        (deep_repo / ".git").mkdir()
        
        # With max_depth=1, should only find shallow
        repos = find_repos([tmpdir], max_depth=1)
        assert len(repos) == 1
        assert repos[0].name == "shallow"
        
        # With max_depth=3, should find both
        repos = find_repos([tmpdir], max_depth=3)
        assert len(repos) == 2
        names = {r.name for r in repos}
        assert names == {"shallow", "deep/nested/repo"}


def test_find_repos_skips_noise_dirs():
    """Test that find_repos skips noise directories like node_modules."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create a git repo inside node_modules (should be skipped)
        noise_repo = root / "project" / "node_modules" / "some-package"
        noise_repo.mkdir(parents=True)
        (noise_repo / ".git").mkdir()
        
        # Create a normal repo
        normal_repo = root / "project"
        (normal_repo / ".git").mkdir()
        
        repos = find_repos([tmpdir], max_depth=3)
        assert len(repos) == 1
        assert repos[0].name == "project"


def test_repo_dataclass():
    """Test Repo dataclass basic functionality."""
    repo = Repo(path=Path("/test/path"), name="test-name")
    assert repo.path == Path("/test/path")
    assert repo.name == "test-name"