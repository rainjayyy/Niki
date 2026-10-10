# Security Policy

> **Critical**: Niki is a child-safety product (target users: ages 3-12).
> Security issues affecting child users are not protected and MUST be
> reported immediately through the channels below.

## Supported Versions

The latest commit on \`main\` is the supported version. Older commits
are not security-supported; please update before reporting.

## Reporting a Vulnerability

**Please DO NOT file a public GitHub issue** for security-sensitive
reports. Use one of these private channels instead:

1. **GitHub Security Advisories** (preferred): use the
   [private vulnerability reporting](https://github.com/rainjayyy/Niki/security/advisories/new)
   flow. GitHub keeps the report private until a fix is published.

2. **Email** (fallback): security@rainjayyy.com
   (replace with your actual security contact before publishing).

Include in the report:

- Reproduction steps (PoC preferred)
- Affected version / commit SHA
- Potential impact (especially: child-safety implications)
- Whether the report has been disclosed elsewhere

We will acknowledge within **48 hours** and aim for:

- **Critical** (child-data exposure, auth bypass, AES key disclosure): fix within 7 days
- **High** (data corruption, privilege escalation): fix within 30 days
- **Medium / Low**: next regular release

## Security Architecture (high level)

| Layer | Module | Notes |
|---|---|---|
| Input safety gateway | \`niki/safety/input_gateway.py\` | 4-tier (ALLOW / DOWNGRADE / ESCALATE / BLOCK) for the 5 sensitive categories (self-harm / violence / sexual / drugs / profanity). |
| Output safety gateway | \`niki/safety/output_gateway.py\` | Think-block stripping, age guardrail check, direct-answer detection, privacy anti-questioning. |
| Privacy classifier | \`niki/safety/privacy_tier.py\` | DAILY / MILD_NEGATIVE / HIGH_RISK / TREEHOLE_PRIVATE — drives alert + treehole routing. |
| Treehole encryption | \`niki/treehole/encryption.py\` | AES-256-GCM, 7-day purge, PII never in plaintext at rest. |
| Account auth | \`niki/auth/jwt_service.py\` + \`niki/auth/account.py\` | JWT 3-tier scope (partial / full / full_reauth), TOTP 2FA, lateral-auth (parent_id == path_parent_id), PII sha256 with deterministic salt. |
| Consent | \`niki/auth/consent.py\` | Dual-version (parent legal text + child illustrated version), timestamp + IP + device fingerprint. |
| Account lifecycle | \`niki/auth/lifecycle.py\` | 30-day deletion cooling, PDF deletion receipt. |

## Hardening Checklist (for production deployers)

- [ ] \`AES_ENCRYPTION_KEY\` set to a fresh 64-hex-char value (NOT the
      \`0000…0\` placeholder in .env.example).
- [ ] \`JWT_SECRET\` set to a high-entropy random value (NOT \`change-me-in-production\`).
- [ ] \`DEBUG=false\` in production (otherwise the startup guard in
      ISS-11 will refuse to boot if no real LLM key is configured).
- [ ] All LLM provider API keys are configured (\`MINIMAX_API_KEY\`,
      \`DEEPSEEK_API_KEY\`, \`DOUBAO_API_KEY\` as applicable).
- [ ] TLS terminated upstream (nginx / cloud LB).
- [ ] WAF or rate-limiting in front of \`/api/v1/auth/sms/send\` (slowapi
      default is 3/minute; check this matches your threat model).
- [ ] Postgres backups encrypted at rest + 30-day retention.
- [ ] VAPID keys for Web Push regenerated periodically (\`scripts/\`
      has a helper).
- [ ] \`niki/safety/privacy_tier.py\` confidence threshold reviewed for
      your locale.

## Known Limitations (by design)

- LLM judges (LLM-as-Judge) can produce false negatives on edge cases.
  The eval suite has 40 hard-asserted safety tests + 35 judge unit
  tests, but production deployments SHOULD run periodic human review.
- The mock/fallback TTS provider (\`FallbackTTSProvider\`) emits silent
  WAV. It MUST be replaced with a real neural TTS (\`EdgeTTSProvider\`)
  before any child-facing deployment.
- The mock OCR backend (\`FallbackOcrBackend\`) is hash-based. It MUST
  be replaced with \`PytesseractOcrBackend\` or \`HttpOcrBackend\` for
  any deployment that accepts user-uploaded images.

## Acknowledgements

Reported vulnerabilities (handled within SLA):

_(none yet — first release)_
