# Email deliverability

Confirmation and password-reset emails go straight to spam unless the sending domain proves it is allowed to send them.

Use any SMTP provider (Resend, Postmark, Amazon SES, Mailgun). In its dashboard, add and verify your sending domain (for example `mail.yourdomain.com`), then publish the DNS records it shows you:

| Record | What it does | Typical value |
|---|---|---|
| **SPF** (TXT on the sending domain) | Lists the servers allowed to send for you | `v=spf1 include:<provider's include> ~all` |
| **DKIM** (TXT or CNAME, provider gives the name and value) | Signs every message so it can't be forged | copy from the provider |
| **DMARC** (TXT on `_dmarc.yourdomain.com`) | Tells receivers what to do when SPF or DKIM fail | start with `v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.com`, move to `p=quarantine` after a few weeks of clean reports |

Then set `MAIL_FROM` to an address on that domain, for example `Verticlip <no-reply@yourdomain.com>`, and send real test messages to Gmail and Outlook inboxes. <https://www.mail-tester.com> scores a message and lists what is missing.

Keep these emails transactional only. Marketing email needs separate consent and an unsubscribe link, and a separate sending domain so it can't hurt delivery of the important ones.
