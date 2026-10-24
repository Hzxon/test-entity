"""Runtime modules that run outside the testgate process.

runner._runtime_dirs() copies the ``_testgate_*.py`` files here into runner-owned temp dirs,
where a test process imports them by their bare names. They run under the project's
interpreter (ctx.python), so they use the standard library and what that interpreter already
needs to run the tests (pytest).

Import rule, enforced by tests/test_layering.py: no module under testgate/ outside this
package imports _testgate_wire, _testgate_oracle or _testgate_worker, and only runner imports
_testgate_channel.
"""
