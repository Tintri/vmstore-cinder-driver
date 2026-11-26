#!/usr/bin/env python3
"""Fail on flake8 findings that sit on lines a change adds or modifies.

The driver files carry some old flake8 findings, so a plain flake8 run would
fail every PR. This script keeps only the findings on changed lines.

The harness image runs flake8 inside a Cinder checkout (so import-order checks
match), where files live under container paths. Two modes cover that:

  --list-container-paths   print, one per line, the container path of every
                           changed file that the harness image contains
  (default)                read flake8 output (stdin or --flake8-output),
                           map container paths back to repo paths, keep the
                           findings on changed lines, print GitHub
                           annotations, write $GITHUB_STEP_SUMMARY, exit 1
                           if any finding remains

Both modes need --base <ref>; changed lines come from
`git diff -U0 <base>...HEAD`, which diffs against the merge-base.
Standard library only.
"""
import argparse
import os
import re
import subprocess
import sys

DRIVER = '/cinder/cinder/volume/drivers/vmstore/'
UNIT = '/cinder/cinder/tests/unit/volume/drivers/vmstore/'
FUNCTIONAL = '/cinder/cinder/tests/functional/test_vmstore_driver.py'
DRIVER_FILES = ('__init__.py', 'api.py', 'nfs.py', 'options.py', 'utils.py')

HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
FINDING = re.compile(r'^(?P<path>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+): '
                     r'(?P<code>[A-Z]+\d+) (?P<msg>.*)$')


def repo_to_container(path):
    """Return the harness-image path of a repo file, or None."""
    if path in DRIVER_FILES:
        return DRIVER + path
    if path.startswith('test/unit/') and path.count('/') == 2:
        return UNIT + path[len('test/unit/'):]
    if path == 'test/functional/test_vmstore_driver.py':
        return FUNCTIONAL
    return None


def container_to_repo(path):
    """Inverse of repo_to_container; unknown paths come back unchanged."""
    if path.startswith(DRIVER):
        return path[len(DRIVER):]
    if path.startswith(UNIT):
        return 'test/unit/' + path[len(UNIT):]
    if path == FUNCTIONAL:
        return 'test/functional/test_vmstore_driver.py'
    return path


def changed_lines(base):
    """Map repo path -> set of added or changed line numbers."""
    out = subprocess.run(
        ['git', 'diff', '-U0', '--no-color', '--no-ext-diff',
         '--diff-filter=ACMR', '--src-prefix=a/', '--dst-prefix=b/',
         '%s...HEAD' % base, '--', '*.py'],
        check=True, capture_output=True, text=True).stdout
    result = {}
    current = None
    for line in out.splitlines():
        if line.startswith('+++ '):
            name = line[4:]
            current = name[2:] if name.startswith('b/') else None
            if current is not None:
                result.setdefault(current, set())
            continue
        match = HUNK.match(line)
        if match and current is not None:
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            result[current].update(range(start, start + count))
    return result


def write_summary(text):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(text)


def annotation_escape(text):
    return (text.replace('%', '%25').replace('\r', '%0D')
            .replace('\n', '%0A'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--base', required=True,
                        help='git ref to diff against (merge-base is used)')
    parser.add_argument('--list-container-paths', action='store_true',
                        help='print changed files as harness-image paths')
    parser.add_argument('--flake8-output', default='-',
                        help='flake8 output file (default: stdin)')
    args = parser.parse_args()

    changed = changed_lines(args.base)

    if args.list_container_paths:
        for path in sorted(changed):
            container = repo_to_container(path)
            if container:
                print(container)
        return 0

    if args.flake8_output == '-':
        text = sys.stdin.read()
    else:
        with open(args.flake8_output, encoding='utf-8') as handle:
            text = handle.read()

    total = 0
    kept = []
    for raw in text.splitlines():
        match = FINDING.match(raw)
        if not match:
            continue
        total += 1
        path = container_to_repo(match.group('path'))
        line = int(match.group('line'))
        # E902 means flake8 could not read a file: a tool error, never filtered.
        if line in changed.get(path, ()) or match.group('code') == 'E902':
            kept.append((path, line, int(match.group('col')),
                         match.group('code'), match.group('msg')))

    for path, line, col, code, msg in kept:
        print('::error file=%s,line=%d,col=%d,title=flake8 %s::%s' % (
            path, line, col, code, annotation_escape(msg)))

    lines = ['## flake8 on changed lines', '',
             'Base: `%s`. Files changed: %d. Findings in all linted files: '
             '%d. On changed lines: %d.' % (
                 args.base, len(changed), total, len(kept)), '']
    if kept:
        lines += ['| File | Line | Code | Message |', '|---|---|---|---|']
        lines += ['| %s | %d | %s | %s |' % (p, ln, c, m.replace('|', '\\|'))
                  for p, ln, _, c, m in kept]
    else:
        lines.append('No findings on changed lines.')
    write_summary('\n'.join(lines) + '\n')

    print('flake8: %d finding(s) on changed lines (%d total, older ones '
          'ignored)' % (len(kept), total))
    return 1 if kept else 0


if __name__ == '__main__':
    sys.exit(main())
