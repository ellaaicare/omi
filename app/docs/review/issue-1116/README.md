# Issue 1116 review evidence

Captured on 2026-07-26 from the `prod` Flutter flavor on:

- iPhone 17 Pro simulator
- iOS 26.5
- Bundle ID `com.ellaaicare.ella`
- Default-off fixtures enabled with `ELLA_ENTITLEMENT_GATE=true`,
  `ELLA_ENTITLEMENT_STUBS=true`, and `ELLA_ACCESS_DEMO_GALLERY=true`

The gallery is offline and permission-free. It covers every invite, entitlement,
provisioning, quota, session-limit, and technical-failure state added in issue
1116.

| Screenshot | Demo state |
| --- | --- |
| `00-demo-gallery.jpg` | Complete state gallery |
| `01-waitlist.jpg` | Waitlist |
| `02-invite-entry.jpg` | Invite entry |
| `03-invite-link-prefill.jpg` | Invite link / QR prefill |
| `04-invite-invalid.jpg` | Invalid invite |
| `05-invite-expired.jpg` | Expired invite |
| `06-invite-capacity.jpg` | Capacity reached |
| `07-invite-rate-limit.jpg` | Invite rate limit |
| `08-provisioning-timeout.jpg` | Provisioning timeout |
| `09-quota-soft-warning.jpg` | Quota soft warning |
| `10-quota-daily-stop.jpg` | Daily quota stop |
| `11-quota-monthly-stop.jpg` | Monthly quota stop |
| `12-quota-concurrent.jpg` | Concurrent-session stop |
| `13-quota-suspended.jpg` | Quota suspension |
| `14-session-max.jpg` | Maximum session length |
| `15-technical-voice-failure.jpg` | Technical voice failure |
| `16-entitled-active.jpg` | Active entitlement |
| `17-entitlement-suspended.jpg` | Suspended entitlement |
| `18-entitlement-revoked.jpg` | Revoked entitlement |
| `19-entitlement-expired.jpg` | Expired entitlement |
