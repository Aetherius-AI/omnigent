# File diff computation benchmark

From the repository root, after installing frontend dependencies:

```sh
node web/scripts/benchmark-diff.mjs > /tmp/diff-current.jsonl
node web/scripts/benchmark-diff.mjs --budget-ms 5000 > /tmp/diff-default.jsonl
node web/scripts/benchmark-diff.mjs --algorithm legacy > /tmp/diff-legacy.jsonl
```

The default is the viewer's 1,000ms computation budget, the advanced algorithm,
and three samples per fixture. `--samples 1` runs a quicker comparison. Run
comparisons sequentially on the same machine without other CPU-heavy work.

Fixtures cover dense edits at several sizes, sparse edits, indentation, moved
blocks, repeated lines, a long single line, and new/deleted files. Each JSONL
record reports computation time, timeout status, line hunks, and character-level
mappings. Every sample verifies that applying the line hunks reconstructs the
modified file. That check catches omitted changes; it does not establish that
the alignment is the most readable possible diff.

The viewer retains Monaco's advanced algorithm because legacy skips character
refinement for changed blocks of 20 or more lines. Advanced returns coarser
hunks when its budget expires. The shorter budget can therefore sacrifice
character-level detail in cases that would finish within Monaco's default
five seconds. Sparse edits typically finish well below either budget, including
in large files. The budget is cooperative, not a hard wall-clock deadline.

This benchmark imports the installed Monaco implementation directly. It measures
computation in Node, excluding API requests, worker queueing, editor loading,
syntax highlighting, and rendering. It is not a managed-production latency
measurement. Revisit the comparison when upgrading Monaco.

For the real browser worker and rendered split/unified/mobile views:

```sh
corepack pnpm --dir web run build
.venv/bin/python -m pytest tests/browser_ui/files/test_file_line_navigation.py \
  -k large_diff --browser-ui-skip-build -s
```

The browser test checks the worker's budget, complete changed-line coverage, and
retained character mappings for sparse edits, and prints the observed worker
round-trip time. Timing is diagnostic rather than a machine-dependent CI limit.
