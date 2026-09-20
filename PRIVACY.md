# Privacy

JobMailDesk is local-first.

- Email is opened through IMAP in read-only mode.
- The scanner uses `BODY.PEEK` and does not intentionally mark messages read.
- It has no send, reply, delete, move, or mailbox-settings feature.
- Passwords and authorization codes are stored only in the operating-system
  credential store.
- Full email bodies are parsed in memory and are not persisted.
- Markdown stores structured job facts and redacted evidence only.
- Optional local identity learning stores only corrected company/role tokens,
  keyed sender/template hashes, timestamps, and enabled/conflict state. It
  does not store message bodies, complete sender addresses, URLs, or
  credentials. Rebuilding the learning library reads a bounded recent IMAP
  window in read-only mode and learns only from messages already linked by a
  stable source hash to user-confirmed application records.
- Pending mail reviews persist only redacted structured fields and an opaque
  reference to any action link. Link values are encrypted locally with a key
  held by the operating-system credential store and are decrypted only for
  review or a confirmed task.
- Unread activity state contains only stable IDs, monotonic sequence numbers,
  event kinds, company scope, and destination tabs. It excludes message text,
  sender addresses, links, mailbox locators, and credentials. The macOS Dock
  receives only the aggregate unread count.
- Public research requests contain company, role, recruiting project, year,
  and stage. They exclude email addresses, phone numbers, passcodes, message
  identifiers, private links, and email body text.
- Obsidian export is optional. Sender and private-link export are disabled by
  default because a chosen vault may sync to a cloud provider.
- Scan progress stores only stage names, aggregate counts and the lookback window in memory. It does not include email subjects, bodies, addresses or credentials.
- The Windows updater reads public releases only from WizLiang/jobmaildesk on GitHub. Manual checks and downloads contact GitHub and its asset CDN without an account token, mailbox address, message data, or machine identifier. GitHub still receives ordinary network metadata such as the connection IP address. Daily checks are opt-in; downloading and restarting require user action. Legacy upstream update settings remain disabled.
