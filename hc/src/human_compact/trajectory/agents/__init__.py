"""Engelbart's agents: how a goal page turn is routed and carried out.

The page keeps four surfaces -- Plan, Bart, Live preview, Terminal -- and
behind them six roles: an Overseer that routes, a Chat agent that answers,
a Brainstorm agent that explores choices with the reader, a Path agent
that plans, a Build agent that changes the project, and a Verifier that
checks the Build's claim. None of them is a process; each is a module here
with one entry point, and ``orchestrator.Orchestrator`` is what calls them.

The rule that holds it together: every interaction is written to a local
event log (``events``), and the Overseer is asked only on the events the
trigger policy (``policy``) names as meaningful transitions. A draft
saved, a row's text edited, a tab opened: recorded, never routed. A
message to Bart, a build requested, a build finishing, a verdict from the
Verifier: routed, once.

Everything that touches the project -- files, commands, the preview, the
build -- goes through a ``runtime.Runtime``. ``LocalRuntime`` is the one
there is; a sandboxed one slots in beside it without a change above.
"""
