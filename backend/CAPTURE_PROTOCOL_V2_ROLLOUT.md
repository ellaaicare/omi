# Capture protocol v2 rollout gate

Protocol v2 is intentionally disabled unless every backend process has
`CAPTURE_PROTOCOL_V2_ROLLOUT_STATE=legacy_workers_drained`.

After the v2 epoch is enabled, `/v4/listen` accepts capture creation only when
the client explicitly sends `capture_protocol=2`. Missing/default protocol 0
and protocol 1 sockets close with WebSocket policy code 1008 and an update-app
message before a Firestore conversation or capture-authority document is
created. This is the intentional installed-client compatibility policy: never
silently upgrade a client that cannot drain and finalize the v2 tuple. Keep the
previous client/backend route available during staged rollout if those clients
must continue capturing; do not route them into the v2 worker pool.

This is a compatibility gate, not a mixed-version safety claim. The legacy
writer in the PR base performs unconditional Firestore segment, photo, source,
and timestamp updates. It never reads a generation field or the v2 Redis key,
so no new key or field can fence an already-paused legacy write.

Before setting the gate:

1. Stop routing new `/v4/listen` WebSockets to the legacy deployment.
2. Drain or forcibly close every legacy capture WebSocket and wait for every
   legacy worker process to exit. A rolling overlap is not permitted.
3. Verify there are no legacy workers or established legacy capture sockets.
4. Deploy the v2 code to every capture worker while the v2 gate remains unset.
5. Set `CAPTURE_PROTOCOL_V2_ROLLOUT_STATE=legacy_workers_drained` on every v2
   worker and restart them as one compatibility epoch.
6. Re-enable capture traffic. The first v2 access copies any untagged Redis
   compatibility values to the co-slotted `{capture:<uid>}` keys using only
   single-key legacy reads.
7. Verify protocol-0 and protocol-1 probes receive the update-app close and
   leave no in-progress conversation or active capture-authority document.

If any legacy worker is reintroduced, unset the gate and repeat the full drain.
Do not perform a rolling downgrade or claim old/new worker coexistence.
