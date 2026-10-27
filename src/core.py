"""Post-install setup engine for SigmaOS-setup / SigmaOS-mac-run
(inspired by omamac and omarchy-mac).

Principles: everything is a declarative manifest (steps, packages, dotfiles,
checks) parsed and validated fail-fast before any plan is produced, plan and
execute are separated (only argv plans are built -- self_test never installs
anything), verification takes an injected root so the checker itself is pure and
deterministic.
"""
import argparse, json, os, re, shlex, sys

ENGINE = {
    "name": "sigma-setup",
    "summary": "Post-install setup: steps, packages, dotfiles, verify",
    "language": "python",
    "verbs": ["steps-plan", "packages-plan", "dotfiles-plan", "verify"],
    "principles": ["Separation of Concerns", "Declarative Manifests",
                   "Defensive Programming", "Fail-Fast", "KISS",
                   "Deterministic Testing"],
}

cli_doc = """\
$ sigma-setup steps-plan steps.json
1. [ ] locale     Set locale (UTC + UTF-8)
2. [ ] dots       Link dotfiles (optional)
$ sigma-setup packages-plan packages.json --backend pacman
$ sigma-setup dotfiles-plan dotfiles.json --root "$HOME"
$ sigma-setup verify checks.json --root "$HOME"
"""

_STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
_BACKENDS = ("pacman", "apt", "brew")
_MODES = ("link", "copy")
_CHECK_KINDS = ("file-exists", "dir-exists", "file-contains")
_CHECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class SetupError(ValueError):
    pass


def _load_json(text, what):
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise SetupError(f"{what} manifest is not valid JSON: {e}") from e
    return data


def _single_line(value, what):
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"empty {what}")
    if "\n" in value or "\x00" in value:
        raise SetupError(f"{what} must be a single line")
    return value


# --------------------------------------------------------------------------- #
#  Ordered setup steps
# --------------------------------------------------------------------------- #
def parse_steps(text):
    data = _load_json(text, "steps")
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise SetupError('steps manifest needs a "steps" list')
    out, seen = [], set()
    for i, row in enumerate(data["steps"]):
        if not isinstance(row, dict):
            raise SetupError(f"step #{i} is not an object")
        sid = row.get("id")
        if not isinstance(sid, str) or not _STEP_ID_RE.match(sid):
            raise SetupError(f"step #{i}: bad id {sid!r}")
        if sid in seen:
            raise SetupError(f"duplicate step id: {sid!r}")
        seen.add(sid)
        title = _single_line(row.get("title"), f"step {sid} title")
        run = row.get("run")
        if not isinstance(run, list) or not run:
            raise SetupError(f"step {sid}: run must be a non-empty argv list")
        for item in run:
            if not isinstance(item, str) or not item or "\n" in item:
                raise SetupError(f"step {sid}: bad argv item {item!r}")
        optional = row.get("optional", False)
        if not isinstance(optional, bool):
            raise SetupError(f"step {sid}: optional must be a boolean")
        out.append({"id": sid, "title": title, "run": list(run),
                    "optional": optional})
    if not out:
        raise SetupError("steps manifest is empty")
    return out


def render_steps(steps):
    lines = []
    for i, s in enumerate(steps, 1):
        note = " (optional)" if s["optional"] else ""
        lines.append(f"{i}. [ ] {s['id']:<10} {s['title']}{note}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  Package groups -> per-backend install argv
# --------------------------------------------------------------------------- #
def parse_packages(text):
    data = _load_json(text, "packages")
    if not isinstance(data, dict) or not data:
        raise SetupError('packages manifest needs a non-empty JSON object')
    out = {}
    for group, pkgs in data.items():
        if not isinstance(group, str) or not _GROUP_RE.match(group):
            raise SetupError(f"bad group name: {group!r}")
        if not isinstance(pkgs, list) or not pkgs:
            raise SetupError(f"group {group}: needs a non-empty package list")
        clean = []
        for p in pkgs:
            if not isinstance(p, str) or not _PKG_RE.match(p):
                raise SetupError(f"group {group}: bad package name {p!r}")
            if p not in clean:
                clean.append(p)  # de-duplicate inside a group
        out[group] = clean
    return out


def packages_plan(groups, backend="pacman"):
    if backend not in _BACKENDS:
        raise SetupError(f"unknown backend {backend!r} "
                         f"(have: {', '.join(_BACKENDS)})")
    if backend == "pacman":
        build = lambda pkgs: ["sudo", "pacman", "-S", "--noconfirm", *pkgs]
    elif backend == "apt":
        build = lambda pkgs: ["sudo", "apt-get", "install", "-y", *pkgs]
    else:
        build = lambda pkgs: ["brew", "install", *pkgs]
    plan, seen = {}, set()
    for group, pkgs in groups.items():
        fresh = [p for p in pkgs if p not in seen]   # first group wins
        seen.update(fresh)
        if fresh:                                    # empty group -> skipped
            plan[group] = build(fresh)
    return plan


# --------------------------------------------------------------------------- #
#  Dotfiles: manifest -> actions (existence injected, hence pure)
# --------------------------------------------------------------------------- #
def parse_dotfiles(text):
    data = _load_json(text, "dotfiles")
    if not isinstance(data, list) or not data:
        raise SetupError("dotfiles manifest needs a non-empty JSON list")
    out, seen = [], set()
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise SetupError(f"entry #{i} is not an object")
        src = _single_line(row.get("src"), f"entry #{i} src")
        dest = _single_line(row.get("dest"), f"entry #{i} dest")
        mode = row.get("mode", "link")
        if mode not in _MODES:
            raise SetupError(f"{src}: mode must be one of {_MODES}, "
                             f"got {mode!r}")
        if dest in seen:
            raise SetupError(f"duplicate dest: {dest!r}")
        seen.add(dest)
        out.append({"src": src, "dest": dest, "mode": mode})
    return out


def plan_dotfiles(pairs, exists=lambda p: False):
    """Annotate manifest entries with actions using an injected `exists`."""
    plan = []
    for pair in pairs:
        present = bool(exists(pair["dest"]))
        plan.append({**pair,
                     "action": "replace" if present else "create",
                     "backup": present})
    return plan


# --------------------------------------------------------------------------- #
#  Verification (pure over an injected root)
# --------------------------------------------------------------------------- #
def parse_checks(text):
    data = _load_json(text, "checks")
    if not isinstance(data, list) or not data:
        raise SetupError("checks manifest needs a non-empty JSON list")
    out, seen = [], set()
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise SetupError(f"check #{i} is not an object")
        cid = row.get("id")
        if not isinstance(cid, str) or not _CHECK_ID_RE.match(cid):
            raise SetupError(f"check #{i}: bad id {cid!r}")
        if cid in seen:
            raise SetupError(f"duplicate check id: {cid!r}")
        seen.add(cid)
        kind = row.get("kind")
        if kind not in _CHECK_KINDS:
            raise SetupError(f"{cid}: kind must be one of {_CHECK_KINDS}, "
                             f"got {kind!r}")
        path = row.get("path")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise SetupError(f"{cid}: bad path {path!r}")
        if path.startswith("/") or ".." in path.split("/"):
            raise SetupError(f"{cid}: path must be relative without '..'")
        entry = {"id": cid, "kind": kind, "path": path}
        if kind == "file-contains":
            entry["text"] = _single_line(row.get("text"), f"check {cid} text")
        out.append(entry)
    return out


def verify(checks, root):
    """Run checks against `root`. Pure with respect to planning (read-only)."""
    if not isinstance(root, str) or not root.strip():
        raise SetupError("empty root")
    results = []
    for c in checks:
        full = os.path.join(root, c["path"])
        if c["kind"] == "file-exists":
            ok = os.path.isfile(full)
            detail = f"{c['path']} {'found' if ok else 'MISSING'}"
        elif c["kind"] == "dir-exists":
            ok = os.path.isdir(full)
            detail = f"{c['path']} {'found' if ok else 'MISSING'}"
        else:  # file-contains
            if not os.path.isfile(full):
                ok, detail = False, f"{c['path']} MISSING"
            else:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                ok = c["text"] in body
                detail = (f"{c['path']} contains {c['text']!r}"
                          if ok else f"{c['path']} lacks {c['text']!r}")
        results.append({"id": c["id"], "ok": bool(ok), "detail": detail})
    return results


def all_ok(results):
    return all(r["ok"] for r in results)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=ENGINE["name"], description=ENGINE["summary"])
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("steps-plan", help="print the numbered step plan")
    sp.add_argument("file")
    pp = sub.add_parser("packages-plan", help="print per-group install argv")
    pp.add_argument("file")
    pp.add_argument("--backend", default="pacman", choices=_BACKENDS)
    dp = sub.add_parser("dotfiles-plan", help="print link/copy actions")
    dp.add_argument("file")
    dp.add_argument("--root", default=".")
    vf = sub.add_parser("verify", help="run manifest checks against a root")
    vf.add_argument("file")
    vf.add_argument("--root", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "steps-plan":
            print(render_steps(parse_steps(_read(args.file))), end="")
        elif args.cmd == "packages-plan":
            plan = packages_plan(parse_packages(_read(args.file)), args.backend)
            for group in plan:
                print(f"# {group}")
                print(shlex.join(plan[group]))
        elif args.cmd == "dotfiles-plan":
            pairs = parse_dotfiles(_read(args.file))
            root = os.path.expanduser(args.root)
            plan = plan_dotfiles(
                pairs, exists=lambda p: os.path.exists(os.path.join(root, p)))
            for row in plan:
                note = " (backup first)" if row["backup"] else ""
                print(f"{row['action']:<8} {row['mode']:<5} "
                      f"{row['src']} -> {row['dest']}{note}")
        elif args.cmd == "verify":
            results = verify(parse_checks(_read(args.file)), args.root)
            for r in results:
                print(f"[{'ok  ' if r['ok'] else 'FAIL'}] {r['id']}: {r['detail']}")
            if not all_ok(results):
                print(f"{sum(1 for r in results if not r['ok'])} check(s) failed",
                      file=sys.stderr)
                return 1
        return 0
    except (SetupError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def self_test() -> None:
    import tempfile

    # --- steps ---------------------------------------------------------------
    STEPS = """{"steps": [
      {"id": "locale", "title": "Set locale to UTC",
       "run": ["sigma-locale", "set", "en_US.UTF-8"]},
      {"id": "dots", "title": "Link dotfiles",
       "run": ["sigma-dots", "link"], "optional": true},
      {"id": "services", "title": "Enable services",
       "run": ["sigma-svc", "enable", "--all"]}
    ]}"""
    steps = parse_steps(STEPS)
    assert [s["id"] for s in steps] == ["locale", "dots", "services"]
    assert steps[1]["optional"] is True and steps[0]["optional"] is False
    assert steps[0]["run"] == ["sigma-locale", "set", "en_US.UTF-8"]
    plan_text = render_steps(steps)
    assert plan_text.splitlines()[0] == "1. [ ] locale     Set locale to UTC"
    assert "(optional)" in plan_text.splitlines()[1]
    assert plan_text.splitlines()[2].startswith("3. [ ] services")
    assert plan_text == render_steps(steps)          # deterministic
    for bad in ('{"steps": []}',
                '{"steps": ["nope"]}',
                # duplicate id
                '{"steps": [{"id": "a", "title": "A", "run": ["x"]},'
                ' {"id": "a", "title": "B", "run": ["y"]}]}',
                # bad id
                '{"steps": [{"id": "Bad ID", "title": "A", "run": ["x"]}]}',
                # missing title
                '{"steps": [{"id": "a", "run": ["x"]}]}',
                # run not a list
                '{"steps": [{"id": "a", "title": "A", "run": "x"}]}',
                # empty run
                '{"steps": [{"id": "a", "title": "A", "run": []}]}',
                # bad argv item
                '{"steps": [{"id": "a", "title": "A", "run": ["ok", 7]}]}',
                # bad optional
                '{"steps": [{"id": "a", "title": "A", "run": ["x"],'
                ' "optional": "yes"}]}',
                "not json"):
        try:
            parse_steps(bad)
        except SetupError:
            pass
        else:
            raise AssertionError(f"steps should have been rejected: {bad}")

    # --- packages ------------------------------------------------------------
    PKGS = """{"base": ["base-devel", "git", "git"],
               "apps": ["firefox", "gimp"],
               "dev": ["nodejs", "python"]}"""
    groups = parse_packages(PKGS)
    assert groups["base"] == ["base-devel", "git"]    # in-group dedupe
    pacman = packages_plan(groups, "pacman")
    assert pacman["base"] == ["sudo", "pacman", "-S", "--noconfirm",
                              "base-devel", "git"]
    assert pacman["apps"][0:3] == ["sudo", "pacman", "-S"]
    apt = packages_plan(groups, "apt")
    assert apt["base"] == ["sudo", "apt-get", "install", "-y",
                           "base-devel", "git"]
    brew = packages_plan(groups, "brew")
    assert brew["base"] == ["brew", "install", "base-devel", "git"]
    assert "sudo" not in brew["base"]                 # brew has no sudo
    # cross-group dedupe: first group wins
    dup = parse_packages('{"a": ["git", "vim"], "b": ["vim", "node"]}')
    plan = packages_plan(dup, "pacman")
    assert plan["a"] == ["sudo", "pacman", "-S", "--noconfirm", "git", "vim"]
    assert plan["b"] == ["sudo", "pacman", "-S", "--noconfirm", "node"]
    # empty groups are skipped
    empty = {"x": ["only"]}
    assert packages_plan({"x": []}, "pacman") == {}
    for bad_call in ((packages_plan, (groups, "portage")),
                     (parse_packages, ("not json",)),
                     (parse_packages, ("[]",)),
                     (parse_packages, ('{"Base Apps": ["git"]}',)),  # bad group
                     (parse_packages, ('{"g": []}',)),                # empty
                     (parse_packages, ('{"g": ["rm -rf /"]}',)),      # bad pkg
                     (parse_packages, ('{"g": ["git", 7]}',))):
        try:
            bad_call[0](*bad_call[1])
        except SetupError:
            pass
        else:
            raise AssertionError(f"should have rejected: {bad_call}")
    assert packages_plan(groups, "pacman") == pacman   # deterministic

    # --- dotfiles ------------------------------------------------------------
    DOTS = """[
      {"src": "zsh/zshrc", "dest": "~/.zshrc", "mode": "link"},
      {"src": "config/kitty", "dest": "~/.config/kitty", "mode": "copy"},
      {"src": "config/nvim", "dest": "~/.config/nvim"}
    ]"""
    pairs = parse_dotfiles(DOTS)
    assert [p["dest"] for p in pairs] == ["~/.zshrc", "~/.config/kitty",
                                          "~/.config/nvim"]
    assert pairs[2]["mode"] == "link"                  # default mode
    # injected existence predicate keeps the planner pure
    plan = plan_dotfiles(pairs, exists=lambda p: p.endswith("kitty"))
    assert [(p["action"], p["backup"]) for p in plan] == [
        ("create", False), ("replace", True), ("create", False)]
    assert plan_dotfiles(pairs) == plan_dotfiles(pairs)   # deterministic
    for bad in ("not json", "[]",
                '[{"src": "a", "dest": "b", "mode": "hard"}]',
                '[{"src": "a", "dest": "b"}, {"src": "c", "dest": "b"}]',  # dup dest
                '[{"dest": "b"}]',                    # missing src
                '[{"src": "a", "dest": ""}]'):
        try:
            parse_dotfiles(bad)
        except SetupError:
            pass
        else:
            raise AssertionError(f"dotfiles should have been rejected: {bad}")

    # --- verify (tempfile only, injected root) -------------------------------
    CHECKS = """[
      {"id": "zshrc", "kind": "file-exists", "path": ".zshrc"},
      {"id": "nvim", "kind": "dir-exists", "path": ".config/nvim"},
      {"id": "marker", "kind": "file-contains", "path": ".zshrc",
       "text": "sigma-shell"},
      {"id": "missing-marker", "kind": "file-contains", "path": ".zshrc",
       "text": "NOT-IN-FILE"}
    ]"""
    checks = parse_checks(CHECKS)
    assert [c["id"] for c in checks] == ["zshrc", "nvim", "marker",
                                         "missing-marker"]
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, ".zshrc"), "w", encoding="utf-8") as fh:
            fh.write("# generated by sigma-shell\nexport EDITOR=nvim\n")
        os.mkdir(os.path.join(root, ".config"))
        os.mkdir(os.path.join(root, ".config", "nvim"))
        results = verify(checks, root)
        by_id = {r["id"]: r for r in results}
        assert by_id["zshrc"]["ok"] is True
        assert by_id["nvim"]["ok"] is True
        assert by_id["marker"]["ok"] is True
        assert by_id["missing-marker"]["ok"] is False
        assert "lacks" in by_id["missing-marker"]["detail"]
        assert all_ok(results) is False
        assert all_ok(results[:3]) is True
        # a missing root/dir reports failures, never raises
        gone = verify(parse_checks('[{"id": "c", "kind": "dir-exists",'
                                   ' "path": "nope"}]'), os.path.join(root, "x"))
        assert gone[0]["ok"] is False
    for bad in ("not json", "[]",
                '[{"id": "c", "kind": "exec", "path": "p"}]',   # unknown kind
                '[{"id": "BAD", "kind": "file-exists", "path": "p"}]',
                '[{"id": "a", "kind": "file-exists", "path": "/etc/passwd"}]',
                '[{"id": "a", "kind": "file-exists", "path": "../x"}]',
                '[{"id": "a", "kind": "file-exists", "path": "p"},'
                ' {"id": "a", "kind": "dir-exists", "path": "q"}]',   # dup id
                '[{"id": "a", "kind": "file-contains", "path": "p"}]',  # no text
                '[{"id": "a", "kind": "file-exists"}]'):
        try:
            parse_checks(bad)
        except SetupError:
            pass
        else:
            raise AssertionError(f"checks should have been rejected: {bad}")
    try:
        verify(checks, "  ")
    except SetupError:
        pass
    else:
        raise AssertionError("empty root not rejected")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
