"""Trade lifecycle state machine and the trade supervisor.

Implemented: ``ports`` - the broker-neutral ExecutionPort and order lifecycle. Its only
implementation is the paper adapter; live execution does not exist.

Planned responsibilities:
  * explicit trade state machine with a closed transition set
  * tick-driven protection loop, free of any LLM
  * exit policy as pure, backtestable functions
"""
