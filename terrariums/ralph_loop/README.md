# Ralph Loop Terrarium

A Huntley-style [Ralph loop](https://ghuntley.com/loop/) rendered as a
KohakuTerrarium terrarium — two creatures joined by one broadcast
channel, no root agent. From proposal §4.2 (H3).

## Topology

```
user -> ralph_initializer              (first message → initializer direct inbox)
ralph_initializer  -> work-log         (INIT_DONE once, then exits)
work-log (broadcast)
  -> ralph_worker                      (every message wakes the worker)
ralph_worker  -> work-log              (per-turn status → re-triggers itself)
```

No root agent. `kt terrarium run` auto-mounts the first creature
(`ralph_initializer`) for your CLI input, so the first prompt you type
lands on the initializer, which is exactly where you want it.

## Files the loop produces

The initializer writes these at the workspace root, then commits them:

- `AGENTS.md` — static project shape: layout, tooling, conventions.
  The worker reads this every turn but never edits it.
- `progress.md` — a markdown checklist. Each `- [ ]` line is one unit
  of work. The worker flips exactly one checkbox per turn.
- `NOTES.md` — append-only running log. Each worker turn appends one
  entry with what was done and what was learned.

Every worker turn also produces one git commit. Git is the undo
button — if a turn produced garbage, `git revert` it.

## Running

```bash
kt terrarium run @kt-biome/terrariums/ralph_loop/
```

Then enter your goal as the first prompt. Example:
`Build a small CLI that counts lines of code by extension, with tests.`

The initializer will scaffold `AGENTS.md` / `progress.md` / `NOTES.md`,
commit them, post an `INIT_DONE` summary to `work-log`, and exit. That
post wakes the worker, which starts grinding through `progress.md`
one item per turn.

To watch the loop without interrupting:

```bash
kt terrarium observe @kt-biome/terrariums/ralph_loop/ --channel work-log
```

## Stopping

Three ways to stop the loop:

1. **Natural end.** When the worker finds `progress.md` fully
   checked, it posts `TERMINUS — all items complete` on `work-log`
   and exits. The `work-log` broadcast still wakes the worker one
   more time; on that wake the worker sees no unchecked items and
   posts `TERMINUS` again (idempotent) before exiting. Run
   `kt terrarium stop` when you see `TERMINUS`.
2. **Operator halt.** Write the literal word `STOP` anywhere inside
   `progress.md` or `NOTES.md`. The worker checks for it every turn
   and will emit `TERMINUS — STOP sentinel found` on the next wake.
3. **Hard stop.** `kt terrarium stop` from another shell, or Ctrl-C
   the `kt terrarium run` process.

## Sentinels

| String    | Where written          | Meaning                              |
|-----------|------------------------|--------------------------------------|
| `INIT_DONE`   | `work-log` (init once) | Scaffolding is in place; worker go.  |
| `INIT_SKIPPED`| `work-log` (init once) | Scaffolding already existed; no-op.  |
| `INIT_FAILED` | `work-log` (init once) | Init hit an unrecoverable error.     |
| `STOP`        | `progress.md` / `NOTES.md` (operator) | Ask worker to halt on next wake. |
| `TERMINUS`    | `work-log` (worker)    | Run is done; safe to `terrarium stop`. |

## Design notes

- Both creatures inherit `@kt-biome/creatures/general`. Role is
  prompt-shaped, not tool-stripped. The worker runs many iterations, so
  its `system.md` is intentionally lean and relies on `##info##` for
  tool detail.
- The `work-log` channel is `broadcast` rather than `queue` so a human
  observer can follow the loop without affecting delivery semantics.
- Termination is sentinel-based because kt terrariums do not yet have
  a per-creature "goal achieved" termination hook (see proposal §5.8 /
  H23). When that ships, this README becomes one sentinel shorter.

## Known gaps / TODOs

- The shipped `ralph_worker` creature sets `max_iterations: 300`, so
  the loop now has a hard budget instead of running forever. Lower or
  raise that cap in `creatures/ralph_worker/config.yaml` depending on
  how expensive you want the autonomous run to be.
- No pre-tool checkpointing. If a worker turn breaks the repo, git
  commits provide rollback, but intra-turn destructive commands are
  not stashed. Pair with the `checkpoint` plugin from kt-biome
  (proposal §4.3) for belt-and-braces safety.
- No automatic verifier. Each worker turn runs whatever check
  `AGENTS.md` tells it to, but there's no independent verifier creature
  (proposal §4.1, H1/H2). Compose with `pev_verifier` plugin when it
  ships, or add a third creature manually.
