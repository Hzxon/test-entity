"""Runtime modules that run OUTSIDE the testgate process.

runner._runtime_dirs() copies the `_testgate_*.py` files here into two runner-owned temp
dirs (oracle dir: channel, wire, oracle; package dir: channel, wire, worker). They run under
the project's interpreter (ctx.python), so they are stdlib-only, plus pytest and hypothesis in
the oracle and coverage in the worker, which the sandbox interpreter already needs. They must
support the oldest Python testgate supports for sandboxes.

Trust map:
    _testgate_wire     both sides. Framing, the closed value codec, caps, every shared list.
    _testgate_oracle   oracle process (trusted). pytest plugin, import finder, proxies.
    _testgate_worker   worker process (untrusted). Serves the package. Nothing in it is secret,
                       and nothing it sends is believed beyond "this is a value / a handle".
    _testgate_channel  any pytest process, and the harness (fold). Owns the record format.

Import rule, enforced by a unit test that fails the build: no module under testgate/ outside
this package imports _testgate_wire, _testgate_oracle or _testgate_worker; runner imports only
_testgate_channel. The wire protocol cannot leak into the harness.
"""
