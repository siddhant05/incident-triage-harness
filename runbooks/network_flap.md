---
title: Transient network flap (likely false alarm)
team: network-ops
tags: [network, timeout, transient, false_alarm]
---

## Symptoms
- Short-burst connection resets across multiple services
- Resolves within 60s without action

## First steps
1. Confirm alert window < 60s
2. Check NOC dashboard for known maintenance
3. If matches pattern: low-confidence diagnosis, no page

## Escalation
- Page only if persists > 5 min
