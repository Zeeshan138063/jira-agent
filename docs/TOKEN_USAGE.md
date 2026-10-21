# Token usage

How much AI "token" capacity working with the Jira Agent uses, measured rather than guessed.
Tokens are the units the model reads and writes; more tokens mean more cost and slower answers.

## The short answer

A typical task costs **about 0.6k to 4k tokens of Jira information** handed to the model, on top of
a fixed baseline that comes with Claude itself. In a real headless Claude session, answering "does
this ticket already exist?" took **4 turns, about 16 seconds and about $0.16**, of which the
agent's own CLI output was **2.2k tokens (about 4% of the ~52k tokens processed)**. The rest is
Claude's own built-in instructions and tool definitions, re-read from cache on each turn.

## What having the plugin enabled costs

`claude plugin details jira-agent` projects **about 940 tokens always added to every session** (the seven
skill descriptions, which Claude keeps in view so it knows when to use them) and **1.0 to 2.2k tokens each
time a skill is actually used** (jira-api 1.1k, jira-groom 2.2k, jira-dedupe 1k, jira-auth 1.8k, jira-setup
1k, jira-knowledge 2.2k, jira-structure 1.7k). Those figures count differently from the character-based
estimates below, so expect a little difference.

## What the plugin controls

| Cost | Tokens | When |
|---|---|---|
| Agent instructions (`agents/jira-agent.md`) | ~1,500 | once per session |
| Skill instructions | jira-setup 650, jira-dedupe 810, jira-api 880, jira-auth 1,020, jira-structure 1,200, jira-knowledge 1,600, jira-groom 1,600 | each loaded only when used |
| What each CLI command returns | see below | each call |

Instruction sizes come from `jira-agent usage --static`.

## Per command (estimated, measured on a 607-ticket project)

| Command | Tokens returned |
|---|---|
| `session check` | ~620 |
| `kb context --format markdown` (default budget 2,500) | ~1,300 to 1,600 (the JSON form is larger, about 1,700 to 2,300) |
| `groom check` | ~460 |
| `groom questions` (one round) | ~530 to 630 |
| `jira get` (one ticket with comments) | ~580 |
| `jira meta` (create fields) | ~670 |
| `dedupe` | ~200 |
| `structure validate` (6 tickets) | ~720 |
| `structure patterns` | ~260 |
| `jira create` preview | ~100 |
| `kb show` (one full ticket) | 180 to 1,900 (average ~740) |

## Per workflow (real runs against the project, read-only)

| Workflow | CLI calls | Tokens returned |
|---|---|---|
| Start a session | 1 | 623 |
| "Does this ticket already exist?" | 1 | 1,423 |
| Groom an existing ticket | 3 | 2,623 |
| Create a new ticket (up to the preview) | 7 | 3,978 |
| Turn a requirement into an epic plan (validated, not created) | 3 | 2,524 |

**Why the context pack matters:** answering "does this exist?" the manual way (search, then read the
5 closest tickets in full) returned **4,628 tokens**. `kb context` returned **1,423**, about a third,
and also included the suggested epic, examples and conventions. It trims itself to a budget
(`--budget`, default 2,500) and reports what it dropped.

## A real session, measured by Claude itself

Headless Claude run with the plugin loaded, read-only, question "is there already a ticket about
<a topic that already has a ticket>?":

| Measure | Result |
|---|---|
| Turns / time / cost | 4 / ~16 s / $0.164 |
| Tokens processed | 8 fresh input, 17,079 written to cache, 34,590 read from cache, 1,025 output (55 thinking) |
| What the CLI handed over | 2,168 tokens in 2 calls (`session check` 623, `kb context` 1,545) |
| Permission denials | 0 |

A **write session** (the agent creating one approved ticket, headless): 7 turns, about 26 s of API
time, $0.175. The CLI handed the model 1,218 tokens in 4 calls (`session check` 623, `dedupe` 435,
create preview 133, confirmed create 27). Claude processed 14 fresh input, 15,387 cached-in, 88,847
cached-read and 1,681 output tokens. The agent followed the intended order: session check, duplicate
check, preview, then the confirmed create.

A control run in which no command was allowed to execute (so the agent could only read its
instructions and stop) cost $0.121 over 2 turns (13,422 cached-in, 9,543 cached-read, 598 output). That
is roughly the fixed price of starting a Claude session with this agent. The question itself added the
remaining ~$0.04. These are single runs, so treat them as ballparks: cost varies with the model,
caching and how the agent words its answer.

## See your own usage

```bash
uv run jira-agent usage                 # table by command: calls, total, average, 90th percentile, max
uv run jira-agent usage --days 7 --static --format json
uv run jira-agent usage --reset         # delete the log
```

Every CLI call is recorded in `~/.jira-agent/usage.jsonl` (private file): the **command name** (never its
arguments or output), an estimated token count, seconds and exit code. Set `JIRA_AGENT_NO_USAGE=1` to
turn it off.

## What the meter does and does not count

- It counts what the CLI **returned to the model**, estimated as characters divided by 4 (the same rule
  the context pack uses for its budget). Real model tokenisation differs a little.
- It does **not** count the model's own reasoning and replies, Claude's built-in instructions, or the
  agent and skill files. Use `--static` for the instruction sizes and Claude's own usage view for the
  rest.

## Keeping usage low

1. Start with `kb context` (one call) instead of searching and reading tickets one by one.
2. Prefer `--format markdown` and a smaller `--budget` for quick checks; raise it only when you need
   more detail.
3. Use `kb card` or `kb epic` before `kb show`; read a full ticket only when you must.
4. Keep the knowledge base current (`kb sync` is cheap) so fewer live lookups are needed.
