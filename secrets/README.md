# Runtime secrets

Create these files locally before starting the production Compose profile:

- `floorplan_api_keys.txt`: one or more comma/newline-separated random API keys, each at least 32 characters.
- `compliance_api_key.txt`: one random internal service key, at least 32 characters.

Generate examples with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The actual `.txt` files are ignored by Git and must never be committed or added
to release archives. Docker mounts them read-only under `/run/secrets`.
