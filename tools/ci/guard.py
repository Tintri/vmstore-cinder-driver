#!/usr/bin/env python3
"""Public repo guard: block a change from publishing internal information.

This repository is public. The guard inspects only what a change introduces:
  (a) lines added by `git diff -U0 <merge-base>..<head>` (all files),
  (b) commit messages in `<merge-base>..<head>`,
  (c) the PR title and body (env PR_TITLE / PR_BODY, empty on push events),
and optionally runs a gitleaks secret scan over the same commit range.

Matched values are always masked in output (workflow logs are public).
A line containing `guard:allow` is exempt (genuine false positives only).
Standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ALLOW_MARK = "guard:allow"
# The guard's own pattern definitions must not flag themselves.
EXCLUDED_PATHS = {"tools/ci/guard.py"}
ZERO_SHA = "0" * 40

UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(UUID)

# 1. Internal IPv4 (10/8, 172.16/12). 192.168/16 and documentation ranges
#    (192.0.2.x, 198.51.100.x) are not internal lab space and never match.
IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)")

# 2. Lab host names and tenant URLs.
HOST_RES = [
    re.compile(r"(?<![\w-])openstack-(?:vm-)?\d+(?![\w])", re.I),
    re.compile(r"(?<![\w-])ttvm\d+(?!\w)", re.I),
    re.compile(r"(?<![\w-])taf[a-z]{2}\d+(?!\w)", re.I),
    re.compile(r"[\w.-]*\.staging-pcd\.platform9\.com", re.I),
    re.compile(r"[\w.-]+\.app\.(?:[\w-]+\.)*platform9\.com", re.I),
]

# 3. UUID assigned in lab env files (test/lab/**/*.env*).
ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=(.*)$")

# 4. Zendesk ticket numbers ("Zendesk 1234567", "ZD#1234567", ticket URLs).
ZENDESK_RES = [
    re.compile(r"\b(?:zendesk|zd)[\s#:_-]*(?:ticket|case|id|no\.?)?[\s#:_-]*\d{4,}", re.I),
    re.compile(r"zendesk\.com/\S*?tickets?/\d+", re.I),
]


def mask(value):
    value = value.strip()
    if len(value) <= 3:
        return "*" * len(value)
    return value[:3] + "*" * min(len(value) - 3, 12)


FAKE_IP_DIRS = ("test/unit/", "test/functional/")


def is_ip_internal(m, path):
    o = [int(g) for g in m.groups()]
    if any(x > 255 for x in o):
        return False
    # 10.0.0.x is the placeholder range unit tests use for fake hosts; labs use
    # 10.136.x.x. Exempt it only inside the unit/functional test trees.
    if o[:3] == [10, 0, 0] and path and path.startswith(FAKE_IP_DIRS):
        return False
    return o[0] == 10 or (o[0] == 172 and 16 <= o[1] <= 31)


def load_denylist(raw):
    names = [n.strip() for n in re.split(r"[\n,]", raw or "") if n.strip()]
    return [re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.I) for n in names]


def scan_text(text, path, denylist):
    """Yield (rule, matched_value) for one line of text."""
    if ALLOW_MARK in text:
        return
    for m in IPV4_RE.finditer(text):
        if is_ip_internal(m, path):
            yield "internal-ip", m.group(0)
    for rx in HOST_RES:
        for m in rx.finditer(text):
            yield "lab-host", m.group(0)
    if path and path.startswith("test/lab/") and ".env" in os.path.basename(path):
        for m in UUID_RE.finditer(text):
            yield "lab-uuid", m.group(0)
    for rx in ZENDESK_RES:
        for m in rx.finditer(text):
            yield "zendesk-ticket", m.group(0)
    for rx in denylist:
        for m in rx.finditer(text):
            yield "customer-name", m.group(0)


def git(*args):
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        check=True, capture_output=True, text=True, errors="replace",
    ).stdout


def resolves(ref):
    return subprocess.run(["git", "rev-parse", "--verify", "-q", ref + "^{commit}"],
                          capture_output=True).returncode == 0


def merge_base(base, head):
    if not base or set(base) == {"0"} or not resolves(base):
        for cand in ("origin/master", head + "~1"):
            if resolves(cand):
                print(f"notice: base '{base}' unusable, falling back to {cand}")
                base = cand
                break
    return git("merge-base", base, head).strip()


def added_lines(mb, head):
    """Yield (path, lineno, text) for every line added in mb..head."""
    out = git("diff", "-U0", "--no-color", "--no-renames", f"{mb}..{head}")
    path = None
    old_left = new_left = 0
    lineno = 0
    hunk = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for line in out.split("\n"):
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                new_left -= 1
                if path:
                    yield path, lineno, line[1:]
                lineno += 1
            elif line.startswith("-"):
                old_left -= 1
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git"):
            path = None
        elif line.startswith("+++ "):
            p = line[4:].rstrip("\t")
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = hunk.match(line)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                lineno = int(m.group(2))
                new_left = int(m.group(3)) if m.group(3) is not None else 1


def commits(mb, head):
    out = git("log", "--format=%h%x1f%B%x1e", f"{mb}..{head}")
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if "\x1f" in rec:
            sha, body = rec.split("\x1f", 1)
            yield sha.strip(), body


def esc(s):
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def run_gitleaks(image, mb, head, findings):
    """Scan mb..head with the gitleaks CLI in Docker. Returns False on tool error."""
    repo = os.getcwd()
    with tempfile.TemporaryDirectory() as out:
        os.chmod(out, 0o777)
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{repo}:/repo:ro", "-v", f"{out}:/out",
            # git refuses to read a repo owned by another uid without this
            "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=/repo",
            image, "git", "/repo", f"--log-opts={mb}..{head}",
            "--redact=100", "--no-banner", "--exit-code", "0",
            "--report-format", "json", "--report-path", "/out/r.json",
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            print("ERROR: gitleaks failed to run (exit %d)" % p.returncode)
            print(p.stderr[-1500:])
            return False
        try:
            with open(os.path.join(out, "r.json")) as fh:
                leaks = json.load(fh) or []
        except (OSError, ValueError):
            print("ERROR: gitleaks produced no report (is this a regular checkout?)")
            print(p.stderr[-1500:])
            return False
    for lk in leaks:
        f = lk.get("File", "")
        if f in EXCLUDED_PATHS:
            continue
        findings.append({
            "rule": "gitleaks:" + lk.get("RuleID", "?"), "kind": "file",
            "file": f, "line": lk.get("StartLine", 0),
            "value": "[redacted]", "where": "commit " + lk.get("Commit", "")[:8],
        })
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--gitleaks-image", help="run gitleaks from this Docker image too")
    a = ap.parse_args()

    mb = merge_base(a.base, a.head)
    print(f"guard: scanning {mb[:10]}..{a.head}")
    denylist = load_denylist(os.environ.get("GUARD_DENYLIST", ""))
    if not denylist:
        print("notice: GUARD_DENYLIST is empty (secret unavailable, e.g. fork PR); "
              "skipping the customer-name check")

    findings = []
    for path, lineno, text in added_lines(mb, a.head):
        if path in EXCLUDED_PATHS:
            continue
        for rule, val in scan_text(text, path, denylist):
            findings.append({"rule": rule, "kind": "file", "file": path,
                             "line": lineno, "value": mask(val), "where": ""})
    for sha, body in commits(mb, a.head):
        for i, text in enumerate(body.split("\n"), 1):
            for rule, val in scan_text(text, None, denylist):
                findings.append({"rule": rule, "kind": "text", "file": "",
                                 "line": i, "value": mask(val),
                                 "where": f"commit message {sha}"})
    for label, key in (("PR title", "PR_TITLE"), ("PR body", "PR_BODY")):
        for i, text in enumerate(os.environ.get(key, "").split("\n"), 1):
            for rule, val in scan_text(text, None, denylist):
                findings.append({"rule": rule, "kind": "text", "file": "",
                                 "line": i, "value": mask(val), "where": label})

    ok = True
    if a.gitleaks_image:
        ok = run_gitleaks(a.gitleaks_image, mb, a.head, findings)

    for f in findings:
        if f["kind"] == "file":
            print(f"::error file={esc(f['file'])},line={f['line']}::{esc(f['rule'])}: "
                  f"{esc(f['value'])} {esc(f['where'])}".rstrip())
        else:
            print(f"ERROR: {f['where']} line {f['line']}: {f['rule']}: {f['value']}")

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as fh:
            fh.write("### Public repo guard\n\n")
            if findings:
                fh.write("| Rule | Location | Value (masked) |\n|---|---|---|\n")
                for f in findings:
                    loc = f"{f['file']}:{f['line']}" if f["kind"] == "file" else \
                        f"{f['where']}, line {f['line']}"
                    fh.write(f"| {f['rule']} | `{loc}` | `{f['value']}` |\n")
                fh.write("\nFix the content (or amend the commit and force-push; "
                         "history must not keep it). Use `guard:allow` on a line "
                         "only for a genuine false positive.\n")
            else:
                fh.write("No findings.\n")

    if findings or not ok:
        print(f"guard: FAILED with {len(findings)} finding(s)")
        return 1
    print("guard: OK, no findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
