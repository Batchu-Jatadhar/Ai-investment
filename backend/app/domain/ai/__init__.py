"""AI trade analyst. Advisory only, subtractive only.

Implemented: ``analyst`` - the verdict contract and its strict parser. No LLM client
is wired in; the only analyst is a local deterministic fake.

Planned responsibilities:
  * verdict is one of TAKE_TRADE / WAIT / REJECT
  * the output schema contains no price, quantity or order fields
  * no AI output may reach any execution code path
"""
