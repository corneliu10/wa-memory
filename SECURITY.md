# Security

wa-memory keeps a copy of your WhatsApp messages, and the keys of a linked WhatsApp device, on your computer.
A bug can expose private conversations, so please report problems privately.

- **Report privately.** Do not open a public issue. Use GitHub's private vulnerability reporting for this repository.
- **In scope:** anything that lets another program, website or person read the memory or the session, write to
  WhatsApp, or run commands through message text or attachments (the MCP server treats message text as data,
  never as instructions).
- **Your data:** everything lives in `~/.wa-memory` (folder 0700, files 0600): `memory.db` (messages),
  `session.db` (the linked device's keys), `media/` and `logs/`. wa-memory sends nothing anywhere except to
  WhatsApp itself. Anyone with a copy of `session.db` can act as this linked device until you remove it on your
  phone (WhatsApp > Settings > Linked devices); computer backups contain it too.
