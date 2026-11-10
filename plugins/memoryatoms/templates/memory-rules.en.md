## Atom memory: how to write cards
- Write only through the atom_write MCP tool (it validates, dedups, indexes); never edit a card .md by hand. Cite recorded facts; do not record temporary attempts or unconfirmed guesses.
- Every new card starts at [臨]; it is promoted by use or append, not by you.
- create needs a `domain` from `memory/_meta/taxonomy.json`; unsure where it lands → `dry_run` first.
- Scope — follows the project or the person? Project rules／decisions → `shared`; only about this person → `personal`; this person across projects → `personal, cross_project=true`; every project in the organization → `org`.
- Realm: reusable in ≥2 projects, a system rule, valid for months → core; one app／tool／environment, valid for weeks → `realm=local`. Unsure → core.
- Secrets (passwords, tokens, keys, credentials, personal data, internal addresses) → `secret=true`; write where the value lives, never the value. General technical knowledge → `not_sensitive=<reason>`; unsure → treat as sensitive. Partly private → split into two cards.
- Lists (todos, backlog, audit results, lookup tables) do not go into cards: keep them in `memory/_staging/<topic>.md`; the card holds only a pointer. Cards are injected whole and long lists get cut.
- An injected card clearly helped or misled you → `atom_feedback(atom_name, verdict=helpful|misleading)`.
