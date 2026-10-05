"""Write orchestration (goal 4+).

Task writes go to the local task store (`app.tasks_store`, goal 17); the notes
writer appends to Google Docs. Routers stay thin and call into this service. See
`.claude/rules/writes.md` for the safety invariants.
"""
