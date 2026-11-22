## Ticket

Closes #

## What

One paragraph. One idea per PR.

## Why

The spec criterion or user-visible behavior this answers.

## Check

- [ ] The failing run is recorded (red) and the passing run after (green).
- [ ] `uv run pytest` passes locally.
- [ ] The changed paths match the issue's `Touches:` list (or the PR carries
      `scope:expanded`).
- [ ] Docs are updated where the behavior changed.

## Review

Request a review when the change is risky or cross-cutting: the protocol,
persistence, auth, the world format, or a shared contract. Otherwise the
required `test` and `claim-guard` checks are the gate. A requested reviewer
checks the linked spec criterion and the red/green evidence, and may ask for a
mutation check; they do not fix the branch.
