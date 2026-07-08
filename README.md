# Clinical Co-Pilot — OpenEMR AI Agent (AgentForge, Gauntlet AI Week 1)

A trust-first AI agent for OpenEMR that gives a physician fast, sourced patient context in the 90 seconds between rooms.

## Documents
- USERS.md — target user and use cases
- AUDIT.md — full codebase audit
- ARCHITECTURE.md — agent plan

## Architecture overview
One agent + typed read-only tools. Verification before display (every claim -> source ID). Access enforced at the data layer — the LLM is never the security boundary. Missing data surfaced, never filled.

## Deployment status
OpenEMR 8.2.0-dev fork configured on Railway (MySQL + OpenEMR services, environment wired, public domain generated). At submission, troubleshooting a container-install issue: DB connection succeeds (table creation begins) but OpenEMR auto-install does not complete cleanly on Railway single-container model. Live URL: https://openemr-production-10ed.up.railway.app — resolving post-submission.

## Data & compliance
Demo data only, no real PHI. Per project rules, assume a signed BAA with all LLM providers and no training use.
