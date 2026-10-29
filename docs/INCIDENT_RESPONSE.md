# Security incident response

Fill in the names, then keep this where more than one person can find it.

**Incident lead:** ______  **Backup:** ______  **Legal/privacy contact:** ______

## 1. Contain (first hour)
- Rotate whatever may be exposed: `SECRET_KEY` (this disconnects every linked YouTube/TikTok account and invalidates stored tokens), Anthropic, OpenAI, Stripe, Google, TikTok and SMTP keys, and `BACKUP_PASSPHRASE`.
- Log everyone out: delete rows from the `sessions` table, or restart after clearing it.
- If the server itself is compromised, take it offline and work from a fresh machine and a backup (`python -m app.backup restore`).
- Preserve evidence: copy logs and the database before changing anything.

## 2. Assess
- What data was reachable (emails, password hashes, project content, encrypted tokens), whose, and for how long?
- Passwords are scrypt-hashed and tokens are encrypted, but assume anything on a stolen disk can eventually be attacked.

## 3. Notify
- **EU/UK (GDPR):** tell the data protection authority within **72 hours** of becoming aware if people are at risk. Tell affected people without undue delay if the risk is high.
- **US states:** most have breach notification laws with their own deadlines (often 30–60 days). A lawyer should confirm which apply.
- Tell Stripe, Google and TikTok if their credentials or user tokens were involved.
- Say plainly what happened, what data, what you did, and what customers should do (change passwords, watch for phishing).

## 4. Recover and learn
- Fix the cause, restore service, force password resets if hashes may have leaked, and write a short post-mortem with dates, scope and changes made.
- Keep the record. Regulators ask for it.

## Reporting vulnerabilities
Researchers can reach us at the `SECURITY_EMAIL` address published in `/.well-known/security.txt`. Acknowledge within 3 business days and fix and credit promptly.
