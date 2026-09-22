"""Errors whose message is safe, and useful, to hand back to the model.

A tool failure has two very different flavours and they must not be conflated:

  ACTIONABLE   the caller asked for something that cannot be answered as stated,
               and the message says what to do instead. "Filtering by tier
               requires tier_as_of..." is the most valuable sentence in this
               system: it is how a model discovers the distinction that a fifth
               of the headline number depends on. Swallowing it would hide the
               lesson the whole dataset exists to teach.

  INTERNAL     something broke. A database error quotes the failing statement,
               so the model must never see it -- it would learn the schema from
               the error and start reasoning about joins it may not write.

Raising ActionableError is therefore a deliberate statement: this text is written
for the caller, and contains no SQL, no schema detail and no internal identifiers.
"""

from __future__ import annotations


class ActionableError(Exception):
    """Safe to return to the model verbatim."""
