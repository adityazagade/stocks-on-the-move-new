# ADR-032: An mprocs cockpit over the console and the command

- **Status**: Accepted
- **Date**: 2026-09-14
- **Last Updated**: 2026-09-14
- **Author**: Aditya Zagade

## Context

Since ADR-031 the operator has two things to start: the console, a
long-lived server on the loopback interface, and the command itself, a
plan or a booking run, which the console starts as a child or the shell
starts by hand. Around them sit the smaller commands the docs name: the
settings check, the tests, the hooks. Each is one line in a terminal, and
on a Wednesday that is two or three terminals and a browser tab opened by
hand at `http://127.0.0.1:8766/`.

The owner asked to start everything in one go with mprocs, a terminal
multiplexer for named processes: one window, a list of processes on the
left, the output of the selected one on the right, `s` to start, `x` to
stop, `r` to restart, `q` to quit and stop them all. It reads
`mprocs.yaml` from the working directory, each process a `shell` or `cmd`
line with an optional `env` block and an `autostart` flag, so a process can
sit in the list and wait for a keystroke. It is a single binary from
Homebrew, Cargo or npm, installed on the owner's machine at 0.9.6, and no
part of the Python project.

Two facts shape the config. Under `uv run --env-file .env`, a variable
already in the environment wins over the file, so `PLAN_ONLY=1` set by
mprocs makes a plan whatever `.env` says (this is the recipe `ONBOARDING.md`
section 2 already uses). And a booking run from a keystroke would have no
confirmation: the console's Today page asks for the typed word `LIVE`
before a live run (ADR-031), and a row in a process list asks for nothing.

## Decision

**A committed `mprocs.yaml` at the repository root**, so that `mprocs` in
the checkout is the one command. It names `.env` and never reads it; it
holds no secret.

**The processes.**

- `console`, autostart: `uv run --env-file .env stocks-on-the-move-ui
  --open`. Stopped with `SIGINT`, which uvicorn takes as a graceful stop.
- `plan`, on demand: `uv run --env-file .env stocks-on-the-move` with
  `PLAN_ONLY: "1"` in its `env` block. A plan logs in, decides everything
  and sends nothing (ADR-022); `x` on it sends `SIGINT`, which the command
  already turns into `failed:KeyboardInterrupt` on the run's record.
- `check`, on demand: the settings check, `python -m
  stocks_on_the_move.settings --check` under `uv run --env-file .env`.
- `tests`, on demand: `uv run pytest -q`.
- `hooks`, on demand: `uv run pre-commit run --all-files`.
- Scrollback raised to 10,000 lines, since a plan over the NIFTY 500 logs
  more than the default 1,000.

**No booking run in the list.** A paper or live run starts from the
console's Today page, behind its token and the typed `LIVE`, or from the
shell by the command the README gives. mprocs never carries a process
whose start is a single keystroke and whose effect is an order.

**A `--open` flag on the console.** `stocks-on-the-move-ui --open` opens
the operator's browser at the console's URL once the server answers, from
a daemon thread that polls the port for up to thirty seconds and then
calls the standard library's `webbrowser.open`. Off by default; the
console's stdout says nothing new. `KITE_OPEN_BROWSER` stays what it is,
the child's own login tab (ADR-005).

**Docs.** `README.md` lists mprocs under Requirements as optional, with
`brew install mprocs`, and the console section gains the one line;
`ONBOARDING.md` section 2 names it beside the console command;
`CLAUDE.md` adds `mprocs` to the commands.

## Consequences

### Positive

- One command on a Wednesday: the console is up, the browser tab is open,
  a plan is one keystroke away and its output is on the same screen as
  the console's log.
- The dangerous start keeps its one gate. The console's `LIVE` field is
  the only way to a live run from a screen; mprocs adds no second way.
- Nothing new in the Python project: mprocs is a tool beside uv, and the
  processes it starts are the commands the docs already give. Ten lines
  of Python for `--open`.

### Negative

- Another tool to install, outside uv and outside Dependabot (ADR-013); it
  is optional, and every line in the config runs by hand without it.
- The config hardcodes `.env` and the default port, as the docs do. A
  different `--port` or env file means editing the line or a personal
  `~/.config/mprocs/mprocs.yaml`, which mprocs layers under the local one.
- `s` on the wrong row starts a plan. A plan is harmless and costs a
  login and a few minutes of Kite calls.

### Neutral

- mprocs runs `shell` lines through the platform shell; the config uses
  plain commands and the `env` block rather than shell syntax, so it is
  the same on Linux and Windows.
- The console's log tab and the rail already show a run started from
  mprocs, since both read the run directory (ADR-006): the `plan` process
  is the shell start the console was designed to sit beside.
- `--open` is a flag, not a setting, like `--port` (ADR-031).

## Alternatives Considered

### Option 1: Status quo, two terminals and a browser tab

**Pros:**

- Nothing to add.

**Cons:**

- The Wednesday routine begins with three windows opened by hand, and the
  owner asked for one.

### Option 2: A Makefile or justfile with targets

`make console`, `make plan`, `make tests`.

**Pros:**

- No new binary if make is present; one file of well-known shape.

**Cons:**

- Targets run one at a time in the foreground; nothing keeps the console
  up while a plan runs in the same window, and there is no view of both.

### Option 3: A Procfile runner (honcho, overmind, foreman)

**Pros:**

- honcho is Python and would come through `uv add --dev`.

**Cons:**

- A Procfile starts every process at once and stops them together; there
  is no row that waits for a keystroke, which is what the plan, the check
  and the tests need. A plan on every start would log in and hit Kite
  each time the console is opened.

### Option 4: A tmux script or tmuxinator layout

**Pros:**

- Panes, and tmux is common.

**Cons:**

- A script to maintain and a tmux keymap to learn; starting and stopping
  a process is a pane's shell, not a command. mprocs does the one thing
  needed with its own keys.

### Option 5: `--open` alone, no process manager

**Pros:**

- Zero tooling.

**Cons:**

- Halves the windows; does not answer the request.

## Implementation Plan

One commit naming ADR-032, the golden test untouched:

1. `mprocs.yaml` as decided, with a comment per process saying what it is
   and why booking runs are absent.
2. `--open` on `stocks-on-the-move-ui`: `parse_args` gains the flag; a
   small function polls the URL with an injected probe and calls an
   injected opener, tested with fakes for both the wait and the timeout;
   `main` starts it on a daemon thread before `uvicorn.run`.
3. The docs named above.
4. **Validation before Implemented.** `mprocs` in the checkout brings the
   console up and opens the tab; `s` on `plan` runs a plan whose directory
   appears on the console's Runs page and whose lines are in the mprocs
   pane; `x` on it leaves `failed:KeyboardInterrupt` in that run's record;
   `q` stops the console.

## References

- ADR-005 (the login tab), ADR-006 (the run record a stopped plan
  leaves), ADR-022 (plan mode), ADR-031 (the console, its `LIVE` gate and
  its `--port` flag)
- mprocs: https://github.com/pvolok/mprocs
- uv, `--env-file` precedence: a variable already in the environment is
  kept; checked on uv 0.12.9 while drafting.

## Notes

Open before Accepted:

- Whether `tests` and `hooks` belong in an operator's cockpit, or only
  `console`, `plan` and `check`. Drafted with all five; delete rows here
  before accepting and the config follows.
- Whether a `backtest` row is wanted. Left out because `run` needs a
  range and a label every time; mprocs can add an ad-hoc process with `a`
  when one is.
