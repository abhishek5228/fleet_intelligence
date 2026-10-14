"""Guards against the problems this refactor removed creeping back in."""
import os
import re

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BACKEND)


def _source_files():
    for root in (os.path.join(BACKEND, "fleet"), BACKEND):
        for name in sorted(os.listdir(root)):
            if name.endswith(".py") and name != "conftest.py":
                yield os.path.join(root, name)
    for sub in ("advanced",):
        d = os.path.join(BACKEND, sub)
        for name in sorted(os.listdir(d)):
            if name.endswith(".py") and not name.startswith("test_"):
                yield os.path.join(d, name)
    wh = os.path.join(BACKEND, "fleet", "webhook")
    for name in sorted(os.listdir(wh)):
        if name.endswith(".py"):
            yield os.path.join(wh, name)


def test_no_hardcoded_phone_numbers_or_secrets_in_source():
    phone = re.compile(r"(?<![\w.])\+?\d{10,15}(?![\w.])")
    secret = re.compile(r"""(api[_-]?key|token|secret|password)\s*=\s*["'][^"']{8,}["']""", re.I)
    offenders = []
    for path in _source_files():
        text = open(path, encoding="utf-8").read()
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if phone.search(line) or secret.search(line):
                offenders.append(f"{os.path.relpath(path, REPO)}:{line_no}: {line.strip()[:80]}")
    assert offenders == []


def test_main_is_a_thin_entry_point():
    lines = [ln for ln in open(os.path.join(BACKEND, "main.py")).read().splitlines() if ln.strip()]
    assert len(lines) < 15


def test_no_module_is_a_monolith():
    for path in _source_files():
        with open(path, encoding="utf-8") as fh:
            assert sum(1 for _ in fh) < 500, f"{os.path.relpath(path, REPO)} is too large - split it"


def test_every_env_var_the_app_reads_is_documented():
    config = open(os.path.join(BACKEND, "fleet", "config.py"), encoding="utf-8").read()
    names = set(re.findall(r'_(?:text|flag|number)\(env, "([A-Z_]+)"', config))
    names |= set(re.findall(r'_structured\(env, file_cfg, "([A-Z_]+)"', config))
    documented = open(os.path.join(BACKEND, ".env.example"), encoding="utf-8").read()
    assert names and [n for n in sorted(names) if n not in documented] == []


def test_webhook_is_not_authenticated_by_the_shared_secret_alone():
    """Manager-only buttons must be checked against the sender, not just the button id."""
    src = open(os.path.join(BACKEND, "fleet", "webhook", "buttons.py"), encoding="utf-8").read()
    assert src.count("is_manager") >= 3 and "owns_incident" in src
