# Backend replica routing

Backend routing is an optional, experimental connection-placement layer. It is
disabled unless the server configuration includes `replication`. It does not yet
provide a complete active/active deployment: cross-replica discovery events,
scheduled-task coordination, and graceful inference-stream draining are still
required before enabling multiple production backends.

All replicas must use the same PostgreSQL metadata database, account and
authentication configuration, and shared artifact storage. Each replica needs
its own directly reachable IP endpoint and the same peer authentication secret.

```yaml
replication:
  endpoint_env: OMNIGENT_REPLICA_URL
  peer_key_env: OMNIGENT_REPLICA_KEY
  allowed_networks:
    - 10.42.0.0/16
  lease_seconds: 90
```

`OMNIGENT_REPLICA_URL` identifies this process, for example
`http://10.42.0.12:8000`. Endpoints must contain a literal IP in an allowed
network. Use the smallest relevant network ranges; unrestricted ranges and DNS
hostnames are rejected. Limit peer access with network policies and use HTTPS
where the network is not trusted. `OMNIGENT_REPLICA_KEY` must contain at least 32
bytes of secret material. Provision it through the deployment's secret system.

Requests may arrive at any replica. Connection-dependent requests are forwarded
to the backend holding the relevant host or runner lease. A forwarded request
retains its original credentials and origin and passes through ordinary route
authorization again. Peer signatures authenticate routing context; they do not
grant access to sessions. A request can make at most one peer hop.

Leases expire unless renewed. Generation tokens fence delayed disconnects and
heartbeats after reconnection. Runner connections attached to a host remain on
that host's backend. Managed launch tasks retain placement after their HTTP
request returns, including newly created sessions that have no host binding yet.
Runner listings include authenticated peer inventories, and host listings include
capabilities reported by the current lease owner.

A stale route or unavailable owner returns HTTP 503 with `Retry-After: 1`.
Mutating requests are not automatically replayed after a peer connection failure.
Backend-independent inference requests can be served by any replica, but an
established upstream HTTP stream still belongs to its serving process and must
finish before that process exits.
