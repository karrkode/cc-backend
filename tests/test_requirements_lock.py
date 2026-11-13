"""CC-194: the dependency locks and the pinned base image stay consistent. Reads repo files only, no network."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pins(text):
    """name -> version for every `name==version` line, ignoring comments and extras."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;\\#]+)", line)
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def _lock_blocks(text):
    """Each locked requirement with its continuation lines, so hashes can be checked per package."""
    blocks, cur = [], None
    for line in text.splitlines():
        if re.match(r"^[A-Za-z0-9]", line):
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.startswith(" "):
            cur.append(line)
    return ["\n".join(b) for b in blocks]


def test_every_direct_pin_is_in_the_lock():
    direct = _pins((ROOT / "requirements.in").read_text())
    locked = _pins((ROOT / "requirements.txt").read_text())
    assert direct, "requirements.in has no pins"
    for name, version in direct.items():
        assert locked.get(name) == version, f"{name}=={version} is not in requirements.txt"


def test_locks_have_no_index_or_link_options():
    for fname in ("requirements.txt", "requirements-dev.txt", "requirements.in", "requirements-dev.in"):
        for line in (ROOT / fname).read_text().splitlines():
            bad = re.match(r"^(--(extra-)?index-url|--find-links|--trusted-host|-e\s|-i\s|-f\s)", line)
            assert not bad, f"{fname}: {line}"


def test_every_direct_requirement_is_pinned():
    for fname in ("requirements.in", "requirements-dev.in"):
        for line in (ROOT / fname).read_text().splitlines():
            if line and line[0].isalpha():
                assert "==" in line.split("#")[0], f"{fname}: not pinned with ==: {line}"


def test_every_locked_requirement_has_a_hash():
    for fname in ("requirements.txt", "requirements-dev.txt"):
        blocks = _lock_blocks((ROOT / fname).read_text())
        assert blocks, f"{fname} has no requirements"
        for b in blocks:
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", b), f"no hash in {fname}: {b.splitlines()[0]}"


def test_dev_lock_agrees_with_the_main_lock():
    main = _pins((ROOT / "requirements.txt").read_text())
    dev = _pins((ROOT / "requirements-dev.txt").read_text())
    for name, version in main.items():
        assert dev.get(name) == version, f"{name} is {version} in requirements.txt but {dev.get(name)} in dev"


def test_base_image_is_pinned_by_digest_and_matches_ci():
    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", (ROOT / "Dockerfile").read_text(), re.M | re.I)
    stages = {s for _, s in froms if s}
    external = [img for img, _ in froms if img not in stages]
    assert len(external) == 1, f"expected one external base image, got {external}"
    assert re.fullmatch(r"python:3\.\d+-slim@sha256:[0-9a-f]{64}", external[0]), external[0]
    minor = re.match(r"python:(3\.\d+)-slim", external[0]).group(1)
    ci = (ROOT / ".github/workflows/ci-cd.yml").read_text()
    assert f'python-version: "{minor}"' in ci, f"ci-cd.yml python-version differs from the image's {minor}"
