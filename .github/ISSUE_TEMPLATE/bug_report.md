---
name: Bug report
about: The action did something it shouldn't (silently passed a regression, crashed, posted a malformed comment, etc.)
title: "[bug] "
labels: bug
---

### What happened
<!-- The action did X; you expected Y. Include the bit of the PR-comment output that's wrong if possible. -->

### Minimal reproduction
<!-- A 5-10 line workflow snippet + a tiny EvalSuite that triggers the bug. The smaller, the faster we can ship a fix. -->

```yaml
# .github/workflows/eval.yml
on: [pull_request]
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: multivon-ai/eval-action@v1
        with:
          suite: evals/my_suite.py
```

### Environment
- Action version (the `@v1` you pinned):
- multivon-eval version (if known):
- Runner: ubuntu-latest / macos-latest / self-hosted?

### Logs / artifacts
<!-- Paste the relevant section of the Actions log. The PR comment itself, if posted, is the most useful single artifact. -->
