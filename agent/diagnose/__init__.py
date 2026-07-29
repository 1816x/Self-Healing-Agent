"""Incident diagnosis agent.

Picks up an incident the Go monitor detected, runs a Claude
function-calling loop over read-only investigation tools, and writes a
root-cause diagnosis back to the shared SQLite store.
"""
