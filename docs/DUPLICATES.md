# Duplicate detection

## How a draft is scored

1. Candidates: a full-text search over the knowledge base, any-word match, top 40.
2. Score each candidate 0-1: `0.65 x` weighted overlap of the summaries `+ 0.35 x` weighted overlap of
   the summary and first 600 characters of the description. Identical summaries score 1.0.
3. `>= dedupe_threshold` (default 0.60) is a likely duplicate; `>= 0.6 x threshold` is shown as related.

Word weights come from the project itself (`term_weights.py`): inverse document frequency, so a word
in nearly every ticket ("Overview", "API") counts little and a rare one counts a lot. A title that
appears three or more times in the project is treated as a template: it is never an automatic 1.0 and
its score is halved, because the same title on different parents is usually not a duplicate. The
weights are cached in the index and rebuilt when the ticket count drifts by more than 2%.

## Measuring it

```
jira-agent eval candidates            # most similar unlabelled pairs, to label by hand
jira-agent eval dedupe                # precision / recall by threshold, retrieval recall@k
jira-agent eval dedupe --scorer plain # word overlap only; idf | template | both for the parts
```

Labels live outside the repo, in `<kb_path>/eval/labels.json`:
`{"pairs": [{"a": "PROJ-1", "b": "PROJ-2", "label": "duplicate|related|different", "note": ""}]}`.
Pairs also come from Jira `duplicates` links (clone links count as related), reworded copies of real
tickets (three levels of rewording), identical-title tickets under different parents, and random
pairs. The report warns until there are 30 real duplicate pairs; below that, read thresholds as
indicative. Please label candidate lists for your own project and share the numbers, not the tickets.

## Result on a real 600-ticket project (3 real duplicates, 180 reworded, ~470 different)

| scorer | precision at 0.60 | recall at 0.60 | best F1 |
| --- | --- | --- | --- |
| plain | 0.46 | 0.77 | 0.66 |
| idf only | 0.53 | 0.76 | 0.67 |
| template rule only | 0.59 | 0.77 | 0.70 |
| both (default) | 0.70 | 0.75 | 0.73 (0.60-0.65) |

The default threshold stays 0.60: F1 is level between 0.60 and 0.65, and there are too few real
duplicates to justify moving it. Embeddings stay undecided until a project has 30+ labelled pairs.
