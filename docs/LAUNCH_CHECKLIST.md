# Launch checklist

The code covers the engineering side. These items need you, an account, or a professional. Tick them off before taking real customers.

## Legal
- [ ] Have a lawyer review `web/terms.html`, `web/privacy.html`, `web/copyright.html` and `web/refunds.html`. They are good-faith drafts, not legal advice.
- [ ] Set `LEGAL_NAME`, `LEGAL_ADDRESS`, `CONTACT_EMAIL` (and `PRIVACY_EMAIL`, `ABUSE_EMAIL`, `SECURITY_EMAIL` if different). The app refuses to start on https without them. Use role addresses at your own domain, not a personal inbox.
- [ ] **US DMCA safe harbor:** register a designated agent at <https://www.copyright.gov/dmca-directory/> (small fee, renew every 3 years). Use the same name and address as in `web/copyright.html`.
- [ ] Form a business entity if you haven't, and put it in `LEGAL_NAME`. Payments, tax and liability all hang off it.
- [ ] If you serve EU or UK customers at scale, consider an EU/UK representative and a Data Processing Agreement template for business customers.
- [ ] Sign the data processing terms offered by OpenAI, Anthropic, Stripe and your email provider (usually a click-through in their dashboards).

## Payments and tax
- [ ] Create the Stripe products, set `STRIPE_PRICE_PRO`, `STRIPE_PRICE_STUDIO`, `STRIPE_WEBHOOK_SECRET`.
- [ ] Turn on **Stripe Tax** in the Stripe dashboard (add your registrations and origin address), then set `STRIPE_TAX=true` so checkout adds VAT or sales tax and collects tax IDs.
- [ ] Turn on the Customer Portal so customers can cancel and download invoices.

## Platforms
- [ ] **Google:** submit the OAuth consent screen for verification for the `youtube.upload` scope. Until then only listed test users can connect. Your privacy policy must include the Google API Services "Limited Use" wording if you keep the YouTube section.
- [ ] **TikTok:** pass the Content Posting API audit for direct posting. Until then uploads go to the creator's inbox.

## Email
- [ ] Set the `SMTP_*` variables with any provider, then add DNS records: see `docs/EMAIL_DELIVERABILITY.md`.
- [ ] Send yourself a sign-up confirmation and a password reset from the live domain and check they land in the inbox.

## Operations
- [ ] Set `BACKUP_PASSPHRASE`, store a copy outside the server, copy `./backups` off the machine (rclone or rsync to S3, R2 or another host), and **restore one backup to prove it works** (`python -m app.backup verify <file>`).
- [ ] Set spend limits on your Anthropic and OpenAI accounts.
- [ ] Create a Cloudflare Turnstile widget and set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET`.
- [ ] Create a Sentry project (optional) and set `SENTRY_DSN`. Add an uptime monitor (UptimeRobot, Better Stack) on `/healthz`.
- [ ] Write down who handles a breach: `docs/INCIDENT_RESPONSE.md`.
- [ ] Run an accessibility pass with a real screen reader (VoiceOver or NVDA). The automated axe checks pass, but they find only part of the problems.

## Before each release
- [ ] `pytest` passes, the legal pages' dates are current, and any new data you collect appears in the Privacy Policy.
