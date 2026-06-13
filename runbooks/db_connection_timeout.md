---
title: Database connection timeout
team: infra-data
tags: [database, timeout, postgres, connection]
---

## Symptoms
- `OperationalError: timeout expired`
- `psycopg2.errors.ConnectionTimeout`

## First steps
1. Check RDS connection pool metrics
2. Check recent migrations or schema changes
3. Verify VPC peering / security group changes

## Escalation
- P0/P1: page infra-data on-call
- P2: post diagnosis in #incidents
