---
name: fastapi-security
description: Extend the Game Updater administration panel and device API securely.
---
Keep administrator cookies and device bearer authorization separate. Enforce CSRF, transactional
single-use enrollment and per-launcher lease ownership. Use AES-GCM and Argon2id. Test concurrent
registration and reservation, expiration, revocation and secret redaction.
References: https://fastapi.tiangolo.com/ and https://cryptography.io/
