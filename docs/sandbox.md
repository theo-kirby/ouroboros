# Codex's sandbox on Ubuntu

A Codex critic reads the repository inside Codex's read-only sandbox, which on Linux
is bubblewrap (`/usr/bin/bwrap`) with a private network namespace. Ubuntu 24.04 and
later set `kernel.apparmor_restrict_unprivileged_userns = 1`: an unconfined program
still gets a user namespace, but without the rights to bring up its loopback, so
every sandboxed command fails with

    bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

`ouroboros preflight` and `ouroboros run` check this with `codex sandbox -- true` (under the same profile the critic uses)
(no model call) and refuse a run whose Codex critic would grade blind.

## The fix

Give bwrap the same AppArmor allowance Ubuntu gives other sandboxing tools. As root:

```sh
cat > /etc/apparmor.d/bwrap <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/bwrap>
}
PROFILE
apparmor_parser -r /etc/apparmor.d/bwrap
```

Then `codex sandbox -- true` exits 0 and `ouroboros preflight` passes. This lets any
user's bwrap create user namespaces with capabilities inside them, which is what
bwrap is for; it does not change other programs.

The other way out is a critic on another harness (`roles.critic.harness: claude`),
whose read-only tools are an allow-list rather than a sandbox.
