---
title: Python NoneType / AttributeError triage
team: backend-platform
tags: [python, attribute_error, none_type, backend]
---

## Symptoms
- `AttributeError: 'NoneType' object has no attribute ...`
- Stack trace in service `*-api` or `*-worker`

## First steps
1. Check `git log -10 --name-only` on the offending file
2. Look for recent deploy in last 30 min that touched the file in the stack trace
3. If recent change matches → suggest rollback to oncall
4. If no recent change → check upstream dependency null returns

## Escalation
- P1+: page secondary on-call
- P2: comment in #oncall with hypothesis + suspect commit
