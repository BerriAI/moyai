# Full Linux computers on Modal

Set `MOYAI_MODAL_RUNTIME=vm` to run Moyai's Modal computers with a full Linux
kernel. Leave it at `gvisor` for the existing default.

```dotenv
SANDBOX_PROVIDER=modal
MOYAI_MODAL_RUNTIME=vm
```

For source development, add this to `.env` and restart `uv run --frozen moyai dev`.
For Docker installations, set it in the installation's `.env` and recreate the
application services. The configuration command also accepts an exported
`MOYAI_MODAL_RUNTIME` when preparing a fresh installation.

Use the same setting in every API, worker, broker, coordinator, and image-build
process. This is a deployment setting, not a per-conversation switch. Drain active
work before changing it and restarting the deployment. Existing live computers
remain attached by ID; they do not change kernel. New computers, product
environment preparation, temporary recovery computers, and filesystem restores
use the selected deployment runtime. A restored computer uses the current
deployment setting, so changing it can change the kernel of recovered sessions.
Start a fresh conversation to test a changed setting.

Moyai validates the value before initializing its runtime and exports it to the
pinned Modal SDK's `MODAL_FUNCTION_RUNTIME` setting. The SDK then applies it to
every allocation, including the Harness's lazy startup and image builds.
`MODAL_FUNCTION_RUNTIME` is also accepted directly; if both variables are set,
they must agree. Conflicting runtime configurations in one process fail instead
of silently changing other computers' allocation policy.

The application retains Modal 1.5.5 and Harness 0.55. Modal 1.6 adds a per-create
`runtime` argument, but removes an exception class that this Harness release
still references. The SDK's existing configuration path avoids changing those
error semantics or replacing its cancellation and creation-recovery behavior.
Do not remove the SDK pin as part of enabling VMs.

## Why use a full kernel?

The investigated gVisor environment rejects Chromium's Netlink multicast
subscription with `EOPNOTSUPP`. Chromium aborts interface discovery, and WebRTC
ICE gathering can remain stuck with no host candidates. A full Linux kernel
supports that subscription. This option changes the execution environment;
it does not inject a Chromium shim or disable network-change notifications.

A VM improves compatibility with software that requires Linux kernel features.
It is not a guarantee that every benchmark will run. Resource requirements,
available devices, CPU architecture, nested virtualization, outbound networking,
and provider restrictions still need validation for a particular workload.
Modal currently documents GPUs as requiring the gVisor runtime.

## Verification

Through the actual Moyai application, ask the agent to run the same headed
Chromium diagnostic in a new conversation under each setting:

1. Record `uname -r` and `chromium --version`.
2. Bind an `AF_NETLINK`/`NETLINK_ROUTE` socket with multicast group mask `275`.
3. Create a browser `RTCPeerConnection({iceServers: []})`, add a data channel,
   set a local offer, and wait for ICE gathering to complete.
4. Record the candidate count and gathering state without adding browser
   networking flags or a Netlink shim.
5. Write a marker in `/workspace`, allow Moyai's existing answer checkpoint to
   finish, terminate only that test computer, and submit a follow-up requesting
   the marker. Confirm that the restored computer has the selected kernel.

Keep transport errors and unsupported configurations visible. Do not
automatically retry a failed VM allocation using gVisor: that would conceal the
compatibility requirement.

## Persistence is separate

This setting does not extend sandbox lifetimes or fix saving after human
computer control. Filesystem snapshots do not preserve live processes or RAM.
Independent computer persistence requires its own lifecycle, dirty-state
tracking, and a fenced checkpoint protocol.

Provider documentation: [Modal Sandboxes](https://modal.com/docs/guide/sandboxes).
