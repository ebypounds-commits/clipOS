# Human Gates, Credentials & External Approvals

## Inputs Codex may eventually need from the owner
Do not block early milestones waiting for these. Build mock adapters/configuration first.

### Server / infrastructure
- Linux server SSH/admin access (only when deployment begins).
- production domain/subdomain and DNS control.
- optional Cloudflare credentials if automated DNS is desired.
- production SMTP/notification destination.
- external backup object-storage credentials.

### AI providers
At least one production model-provider credential; additional providers are optional. The system must run with mocks/local providers in CI.

### Social platforms
- YouTube/Google OAuth client and authorized channel/account; API audit/verification if required for intended public behavior.
- TikTok developer app, approved scopes, OAuth authorization, and app audit if required for public posting.
- Meta developer app/business/page/Instagram account and required permissions/app review for the implemented publishing path.

### Monetization
Affiliate-network credentials, sponsor records, analytics exports/APIs, payment provider only when corresponding milestone is enabled.

## Mandatory human gates
1. first production secrets import;
2. first connection of each real social account;
3. first public post on each platform account;
4. material monthly budget increase;
5. financial payout/bank destination changes;
6. contracts/sponsorship acceptance;
7. unresolved copyright/legal dispute;
8. destructive production-data operation above policy threshold;
9. privilege/OAuth-scope expansion that materially increases risk;
10. production enablement of a high-risk new agent/tool.

## Secret handling
- `.env.example` contains names only.
- production secrets enter via server secret manager/deployment environment, never chat logs or repository.
- database stores opaque secret references.
- redact tokens in logs/traces.
- rotation procedure exists for every connector.

## VPS-hosted build credentials
Hermes/Codex may need a GitHub build credential and local repository permissions, but these are not ClipOS production secrets. Use a dedicated least-privilege build identity/token or SSH deploy mechanism appropriate to the repository workflow. Keep production OAuth/API/provider credentials unavailable to the build agents until the corresponding milestone and human gate.

Running Hermes/Codex on the VPS does not authorize arbitrary root or production access. Privilege escalation remains an explicit infrastructure action.
